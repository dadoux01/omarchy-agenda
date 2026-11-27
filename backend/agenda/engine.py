"""Accounts, sync, and writes, independent of how requests arrive and of
which provider serves an account (see providers.py and google.py)."""

import datetime as dt
import hashlib
import secrets as _random
import threading
import time

from . import caldav, google, ics, net, providers, secrets, storage

ACCOUNTS_FILE = "accounts.json"
SETTINGS_FILE = "settings.json"
CACHE_FILE = "cache.json"
CACHE_VERSION = 2

WINDOW_PAST_DAYS = 1
WINDOW_FUTURE_DAYS = 62
PENDING_TTL = 600
MAX_EVENTS_PER_CALENDAR = 20000

# The "repeat" choices of the new event form, as RRULE values. Weekly,
# monthly and yearly follow the first occurrence's weekday or date, like
# Google Calendar's own presets.
REPEAT_RULES = {
    "daily": "FREQ=DAILY",
    "weekdays": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
    "weekly": "FREQ=WEEKLY",
    "monthly": "FREQ=MONTHLY",
    "yearly": "FREQ=YEARLY",
}

DEFAULT_SETTINGS = {
    "syncMinutes": 10,
    "reminders": True,
    "extraReminderMinutes": 0,
    "notifyAtStart": True,
    "privateNotifications": False,
    "dailyRecap": True,
    "lateGraceMinutes": 15,
    "defaultReminderMinutes": 10,
    "defaultCalendar": "",
    "agendaDays": 7,
}

SETTING_LIMITS = {
    "syncMinutes": (2, 120),
    "extraReminderMinutes": (0, 1440),
    "lateGraceMinutes": (1, 120),
    "defaultReminderMinutes": (-1, 10080),
    "agendaDays": (1, 31),
}

PROVIDERS = {
    "caldav": providers.CalDavProvider(),
    "google": google.GoogleProvider(),
    "ics": providers.IcsProvider(),
}
KIND_PROVIDER = {"icloud": "caldav", "caldav": "caldav", "ics": "ics", "google": "google"}


class EngineError(Exception):
    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


_clean = providers.clean


def event_key(account_id, calendar_id, uid, rid):
    raw = "\x1f".join([account_id, calendar_id, uid, rid or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _code(exc):
    return getattr(exc, "code", None) or type(exc).__name__


DEMO_FILE = "demo.json"


def load_demo():
    """Screenshots and docs without anyone's real calendar.

    A demo.json in the state directory replaces every account with the fake
    ones it lists. Nothing is fetched, nothing is written to a server and no
    reminder fires. Format:
      {"accounts": [{"label", "calendars": [{"name", "color", "readOnly"}]}],
       "events": [{"title", "calendar": 0, "day": 0, "time": "09:30",
                   "minutes": 30, "allDay": false, "location", "meetingUrl",
                   "recurring": false, "alarms": [600]}]}
    day is relative to today; calendar indexes all the demo calendars.
    """
    data = storage.read_json(storage.state_path(DEMO_FILE), None)
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
        return None
    return data


class Engine:
    def __init__(self):
        self.lock = threading.RLock()
        self.zone = ics.local_zone()
        self.accounts = self._load_accounts()
        self.settings = self._load_settings()
        self.cache = storage.read_json(storage.state_path(CACHE_FILE), {})
        if not isinstance(self.cache, dict) or self.cache.get("version") != CACHE_VERSION:
            self.cache = {"version": CACHE_VERSION, "calendars": {}, "syncedAt": 0, "errors": {}}
        self.pending = {}
        self.syncing = False
        self.demo = load_demo()

    # ------------------------------------------------------------ files

    def _load_accounts(self):
        data = storage.read_json(storage.config_path(ACCOUNTS_FILE), {})
        accounts = data.get("accounts", []) if isinstance(data, dict) else []
        out = []
        for acc in accounts:
            if not isinstance(acc, dict) or acc.get("provider") not in PROVIDERS:
                continue
            if not isinstance(acc.get("id"), str) or not isinstance(acc.get("calendars"), list):
                continue
            out.append(acc)
        return out

    def _save_accounts(self):
        storage.write_json(storage.config_path(ACCOUNTS_FILE),
                           {"version": 1, "accounts": self.accounts})

    def _load_settings(self):
        data = storage.read_json(storage.config_path(SETTINGS_FILE), {})
        merged = dict(DEFAULT_SETTINGS)
        if isinstance(data, dict):
            merged.update(self._validate_settings(data))
        return merged

    def _validate_settings(self, values):
        out = {}
        for key, default in DEFAULT_SETTINGS.items():
            if key not in values:
                continue
            v = values[key]
            if isinstance(default, bool):
                if isinstance(v, bool):
                    out[key] = v
            elif isinstance(default, int):
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    lo, hi = SETTING_LIMITS.get(key, (0, 10 ** 6))
                    out[key] = int(min(hi, max(lo, v)))
            elif isinstance(default, str):
                if isinstance(v, str) and len(v) <= 2000:
                    out[key] = v
        return out

    def set_settings(self, values):
        with self.lock:
            self.settings.update(self._validate_settings(values or {}))
            storage.write_json(storage.config_path(SETTINGS_FILE), self.settings)
            return dict(self.settings)

    def _save_cache(self):
        storage.write_json(storage.state_path(CACHE_FILE), self.cache)

    # ------------------------------------------------------------ accounts

    def _demo_calendars(self):
        out = []
        for a, acc in enumerate(self.demo.get("accounts", [])):
            for c, cal in enumerate(acc.get("calendars", [])):
                out.append(("demo%d" % a, "cal%d" % c, acc, cal))
        return out

    def public_accounts(self):
        if self.demo is not None:
            accounts = {}
            for aid, cid, acc, cal in self._demo_calendars():
                entry = accounts.setdefault(aid, {
                    "id": aid, "provider": "demo", "kind": str(acc.get("kind", "caldav")),
                    "label": _clean(acc.get("label"), 80), "username": _clean(acc.get("username"), 120),
                    "server": "", "error": "", "calendars": []})
                entry["calendars"].append({"id": cid, "name": _clean(cal.get("name"), 120),
                                           "color": caldav.normalize_color(str(cal.get("color", ""))),
                                           "enabled": True, "readOnly": bool(cal.get("readOnly"))})
            return list(accounts.values())
        with self.lock:
            errors = self.cache.get("errors", {})
            out = []
            for acc in self.accounts:
                out.append({
                    "id": acc["id"],
                    "provider": acc["provider"],
                    "kind": acc.get("kind", acc["provider"]),
                    "label": acc.get("label", ""),
                    "username": acc.get("username", ""),
                    "server": acc.get("server", ""),
                    "error": errors.get(acc["id"], ""),
                    "calendars": [{
                        "id": c["id"], "name": c.get("name", ""), "color": c.get("color", ""),
                        "enabled": bool(c.get("enabled", True)), "readOnly": bool(c.get("readOnly")),
                    } for c in acc["calendars"]],
                })
            return out

    def _account(self, account_id):
        for acc in self.accounts:
            if acc["id"] == account_id:
                return acc
        raise EngineError("unknown_account")

    def _secret(self, acc):
        secret = secrets.load(acc["id"], acc.get("secret", ""))
        if not secret:
            raise EngineError("missing_secret")
        return secret

    def _provider(self, acc):
        return PROVIDERS[acc["provider"]]

    def connect(self, params, cancel=None):
        """Check credentials and list the calendars, without saving yet.
        Google opens the browser here and waits for the user (up to 5 min)."""
        kind = str(params.get("kind", ""))
        name = KIND_PROVIDER.get(kind)
        if not name:
            raise EngineError("unknown_provider")
        try:
            if name == "google":
                pending = PROVIDERS["google"].connect(params, cancel)
            else:
                pending = PROVIDERS[name].connect(params)
        except providers.ProviderError as exc:
            raise EngineError(exc.code, exc.detail)
        except net.NetError as exc:
            raise EngineError(exc.code, exc.detail)
        label = _clean(params.get("label"), 80) or pending.label
        token = _random.token_hex(16)
        now = time.time()
        with self.lock:
            self.pending = {k: v for k, v in self.pending.items() if now - v["at"] < PENDING_TTL}
            self.pending[token] = {"at": now, "pending": pending, "label": label}
        return {"token": token, "label": label,
                "calendars": [{"id": c["id"], "name": c["name"], "color": c["color"],
                               "readOnly": c["readOnly"], "enabled": c.get("enabled", True)}
                              for c in pending.calendars]}

    # Kept for callers of the first version.
    def discover(self, kind, server, username, password, label=""):
        return self.connect({"kind": kind, "server": server, "username": username,
                             "password": password, "label": label})

    def save_account(self, token, label, selection):
        with self.lock:
            entry = self.pending.pop(token, None)
        if not entry or time.time() - entry["at"] > PENDING_TTL:
            raise EngineError("pending_expired")
        pending = entry["pending"]
        chosen = {s.get("id"): s for s in (selection or []) if isinstance(s, dict)}
        account_id = _random.token_hex(6)
        cals = []
        for cal in pending.calendars:
            sel = chosen.get(cal["id"], {})
            color = caldav.normalize_color(str(sel.get("color", ""))) or cal["color"]
            entry_cal = {"id": cal["id"], "name": cal["name"], "color": color,
                         "enabled": bool(sel.get("enabled", cal.get("enabled", True))),
                         "readOnly": bool(cal["readOnly"])}
            if cal.get("defaultAlarms"):
                entry_cal["defaultAlarms"] = cal["defaultAlarms"]
            cals.append(entry_cal)
        marker = secrets.store(account_id, pending.secret)
        account = {
            "id": account_id, "provider": pending.provider, "kind": pending.kind,
            "label": _clean(label, 80) or entry["label"], "server": pending.server,
            "username": pending.username, "secret": marker, "calendars": cals,
        }
        with self.lock:
            self.accounts.append(account)
            self._save_accounts()
        return {"id": account_id, "secretStore": marker}

    def reconnect(self, account_id, cancel=None):
        """Sign a Google account in again after Google revoked its token
        (seven days in "Testing" mode, a password change, a revoke)."""
        with self.lock:
            acc = self._account(account_id)
            if acc["provider"] != "google":
                raise EngineError("unknown_provider")
            old_secret = self._secret(acc)
            login_hint = acc.get("username", "")
        try:
            secret = PROVIDERS["google"].reconnect(old_secret, cancel, login_hint=login_hint)
        except providers.ProviderError as exc:
            raise EngineError(exc.code, exc.detail)
        except net.NetError as exc:
            raise EngineError(exc.code, exc.detail)
        with self.lock:
            acc = self._account(account_id)
            marker = secrets.store(acc["id"], secret)
            acc["secret"] = marker
            self._save_accounts()
            self.cache.get("errors", {}).pop(account_id, None)
        return {"secretStore": marker}

    def update_account(self, account_id, label=None, calendars=None):
        with self.lock:
            acc = self._account(account_id)
            if label is not None:
                acc["label"] = _clean(label, 80) or acc.get("label", "")
            if isinstance(calendars, list):
                by_id = {c.get("id"): c for c in calendars if isinstance(c, dict)}
                for cal in acc["calendars"]:
                    upd = by_id.get(cal["id"])
                    if not upd:
                        continue
                    if "enabled" in upd:
                        cal["enabled"] = bool(upd["enabled"])
                    if "color" in upd:
                        cal["color"] = caldav.normalize_color(str(upd["color"])) or cal["color"]
            self._save_accounts()

    def remove_account(self, account_id):
        with self.lock:
            acc = self._account(account_id)
            self.accounts = [a for a in self.accounts if a["id"] != account_id]
            self._save_accounts()
            cals = self.cache.get("calendars", {})
            for key in [k for k in cals if k.startswith(account_id + "|")]:
                del cals[key]
            self.cache.get("errors", {}).pop(account_id, None)
            self._save_cache()
        secrets.delete(acc["id"])

    # ------------------------------------------------------------ sync

    def window(self, now=None):
        today = (now or dt.datetime.now(self.zone)).astimezone(self.zone).date()
        start_day = today - dt.timedelta(days=WINDOW_PAST_DAYS)
        end_day = today + dt.timedelta(days=WINDOW_FUTURE_DAYS)
        start = dt.datetime(start_day.year, start_day.month, start_day.day, tzinfo=self.zone)
        end = dt.datetime(end_day.year, end_day.month, end_day.day, tzinfo=self.zone)
        return start, end

    def sync(self, force=False, only=None):
        """Refresh every enabled calendar. `only` = set of cache keys to
        refetch even if the provider says nothing changed."""
        if self.demo is not None:
            self.cache["syncedAt"] = int(time.time() * 1000)
            return {}
        with self.lock:
            accounts = [dict(a, calendars=[dict(c) for c in a["calendars"]]) for a in self.accounts]
            old = dict(self.cache.get("calendars", {}))
            self.syncing = True
        start, end = self.window()
        window_ms = int(start.timestamp() * 1000)
        errors = {}
        fresh = {}
        try:
            for acc in accounts:
                provider = self._provider(acc)
                try:
                    session = provider.session(acc, self._secret(acc))
                except (EngineError, providers.ProviderError) as exc:
                    errors[acc["id"]] = _code(exc)
                    for cal in acc["calendars"]:
                        ckey = acc["id"] + "|" + cal["id"]
                        if ckey in old:
                            fresh[ckey] = old[ckey]
                    continue
                cals = acc["calendars"]
                for idx, cal in enumerate(cals):
                    if not cal.get("enabled", True):
                        continue
                    ckey = acc["id"] + "|" + cal["id"]
                    cached = old.get(ckey)
                    same_window = bool(cached) and cached.get("windowStart") == window_ms
                    refetch = force or not same_window or bool(only and ckey in only)
                    try:
                        result = provider.fetch(session, acc, cal, start, end,
                                                cached.get("state", "") if cached else "",
                                                refetch, self.zone)
                        if result is None and cached:
                            fresh[ckey] = cached
                            continue
                        if result is None:
                            result = provider.fetch(session, acc, cal, start, end, "", True, self.zone)
                        state, occs = result
                        fresh[ckey] = {"state": state or "", "windowStart": window_ms,
                                       "events": self._events_for(acc, cal, occs)}
                    except (net.NetError, ics.IcsError, providers.ProviderError) as exc:
                        errors[acc["id"]] = _code(exc)
                        if cached:
                            fresh[ckey] = cached
                        if _code(exc) in ("auth_failed", "reauth_required", "missing_secret",
                                          "google_client_invalid"):
                            for rest in cals[idx + 1:]:
                                rkey = acc["id"] + "|" + rest["id"]
                                if rkey in old:
                                    fresh[rkey] = old[rkey]
                            break  # every calendar of this account would fail the same way
        finally:
            with self.lock:
                self.syncing = False
        with self.lock:
            self.cache["calendars"] = fresh
            self.cache["errors"] = errors
            self.cache["syncedAt"] = int(time.time() * 1000)
            self._save_cache()
        return errors

    def _events_for(self, acc, cal, occs):
        out = []
        for o in occs:
            out.append({
                "key": event_key(acc["id"], cal["id"], o["uid"], o["recurrence_id"]),
                "accountId": acc["id"],
                "calendarId": cal["id"],
                "uid": o["uid"][:500],
                "href": o.get("href", ""),
                "etag": o.get("etag", ""),
                "title": o["title"],
                "location": o["location"],
                "description": o["description"],
                "start": o["start_ms"],
                "end": o["end_ms"],
                "allDay": o["all_day"],
                "recurring": o["recurring"],
                "recurrenceId": o["recurrence_id"],
                "meetingUrl": o["meeting_url"],
                "alarms": o["alarms_before"],
                "alarmsAt": o["alarms_at"],
            })
            if len(out) >= MAX_EVENTS_PER_CALENDAR:
                break
        return out

    def _demo_events(self):
        cals = self._demo_calendars()
        today = dt.datetime.now(self.zone).date()
        out = []
        for i, ev in enumerate(self.demo.get("events", [])):
            if not isinstance(ev, dict) or not cals:
                continue
            aid, cid, acc, cal = cals[int(ev.get("calendar", 0)) % len(cals)]
            day = today + dt.timedelta(days=int(ev.get("day", 0)))
            if ev.get("allDay"):
                start = dt.datetime(day.year, day.month, day.day, tzinfo=self.zone)
                end = start + dt.timedelta(days=max(1, int(ev.get("days", 1))))
            else:
                hh, mm = (int(x) for x in str(ev.get("time", "09:00")).split(":"))
                start = dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=self.zone)
                end = start + dt.timedelta(minutes=int(ev.get("minutes", 60)))
            out.append({
                "key": "demo%d" % i, "accountId": aid, "calendarId": cid, "uid": "demo%d" % i,
                "href": "", "etag": "", "title": _clean(ev.get("title"), 300),
                "location": _clean(ev.get("location"), 300), "description": _clean(ev.get("description"), 2000),
                "start": int(start.timestamp() * 1000), "end": int(end.timestamp() * 1000),
                "allDay": bool(ev.get("allDay")), "recurring": bool(ev.get("recurring")),
                "recurrenceId": "x" if ev.get("recurring") else "",
                "meetingUrl": ics.safe_meeting_url(str(ev.get("meetingUrl", ""))),
                "alarms": [int(x) for x in ev.get("alarms", []) if isinstance(x, int)], "alarmsAt": [],
                "calendarName": _clean(cal.get("name"), 120), "accountLabel": _clean(acc.get("label"), 80),
                "color": caldav.normalize_color(str(cal.get("color", ""))), "readOnly": bool(cal.get("readOnly")),
            })
        out.sort(key=lambda e: (e["start"], e["title"]))
        return out

    def events(self):
        """Every cached occurrence, decorated with its calendar's look."""
        if self.demo is not None:
            return self._demo_events()
        with self.lock:
            cal_info = {}
            for acc in self.accounts:
                for cal in acc["calendars"]:
                    cal_info[acc["id"] + "|" + cal["id"]] = (acc, cal)
            out = []
            for ckey, entry in self.cache.get("calendars", {}).items():
                info = cal_info.get(ckey)
                if not info or not info[1].get("enabled", True):
                    continue
                acc, cal = info
                for ev in entry.get("events", []):
                    e = dict(ev)
                    e["calendarName"] = cal.get("name", "")
                    e["accountLabel"] = acc.get("label", "")
                    e["color"] = cal.get("color", "")
                    e["readOnly"] = bool(cal.get("readOnly"))
                    out.append(e)
        out.sort(key=lambda e: (e["start"], e["title"]))
        return out

    def find_event(self, key):
        for ev in self.events():
            if ev["key"] == key:
                return ev
        raise EngineError("unknown_event")

    # ------------------------------------------------------------ writes

    def _writable(self, account_id, calendar_id):
        if self.demo is not None:
            raise EngineError("read_only")
        acc = self._account(account_id)
        for cal in acc["calendars"]:
            if cal["id"] == calendar_id:
                if cal.get("readOnly") or acc["provider"] == "ics":
                    raise EngineError("read_only")
                return acc, cal
        raise EngineError("unknown_calendar")

    def _rrule(self, p, start_ms, all_day):
        """The RRULE value (without "RRULE:") for the form's "repeat" choice,
        or "" for a one-off event. "until" is the last day the series may
        occur on, as local midnight in ms; absent means no end."""
        repeat = str(p.get("repeat") or "")
        if not repeat:
            return ""
        if repeat not in REPEAT_RULES:
            raise EngineError("bad_repeat")
        rule = REPEAT_RULES[repeat]
        until = p.get("until")
        if until is None or until == "":
            return rule
        try:
            last = dt.datetime.fromtimestamp(int(until) / 1000, self.zone).date()
            first = dt.datetime.fromtimestamp(start_ms / 1000, self.zone).date()
        except (TypeError, ValueError, OverflowError, OSError):
            raise EngineError("bad_repeat")
        if last < first:
            raise EngineError("bad_repeat")
        if all_day:
            return rule + ";UNTIL=" + last.strftime("%Y%m%d")
        # Every occurrence starting on or before the last day (local time)
        # is kept; for a timed start, UNTIL must be in UTC (RFC 5545).
        end_of_day = dt.datetime.combine(last, dt.time(23, 59, 59), self.zone)
        return rule + ";UNTIL=" + end_of_day.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def create_event(self, p):
        acc, cal = self._writable(str(p.get("accountId", "")), str(p.get("calendarId", "")))
        title = _clean(p.get("title"), 300)
        if not title:
            raise EngineError("missing_title")
        try:
            start_ms, end_ms = int(p["start"]), int(p["end"])
        except (KeyError, TypeError, ValueError):
            raise EngineError("bad_time")
        if end_ms <= start_ms or end_ms - start_ms > 366 * 86400 * 1000:
            raise EngineError("bad_time")
        reminders = []
        for m in (p.get("reminders", []) or [])[:10]:
            if isinstance(m, (int, float)) and not isinstance(m, bool) and 0 <= m <= 40320:
                reminders.append(int(m))
        all_day = bool(p.get("allDay"))
        try:
            start_date = dt.datetime.fromtimestamp(start_ms / 1000, self.zone).date()
            end_date = dt.datetime.fromtimestamp(end_ms / 1000, self.zone).date()
        except (OverflowError, OSError, ValueError):
            raise EngineError("bad_time")
        if end_date <= start_date:
            end_date = start_date + dt.timedelta(days=1)
        event = {
            "title": title, "all_day": all_day,
            "start_ms": start_ms, "end_ms": end_ms,
            "start_date": start_date, "end_date": end_date,
            "location": _clean(p.get("location"), 300),
            "description": _clean(p.get("description"), 2000),
            "reminders": reminders,
            "rrule": self._rrule(p, start_ms, all_day),
        }
        provider = self._provider(acc)
        try:
            provider.create(provider.session(acc, self._secret(acc)), acc, cal, event, self.zone)
        except (net.NetError, providers.ProviderError) as exc:
            raise EngineError(exc.code, exc.detail)
        return {"calendarKey": acc["id"] + "|" + cal["id"]}

    def delete_event(self, key, scope):
        ev = self.find_event(key)
        acc, cal = self._writable(ev["accountId"], ev["calendarId"])
        provider = self._provider(acc)
        try:
            provider.delete(provider.session(acc, self._secret(acc)), acc, cal, ev, scope)
        except (net.NetError, providers.ProviderError) as exc:
            raise EngineError(exc.code, exc.detail)
        return {"calendarKey": acc["id"] + "|" + cal["id"]}
