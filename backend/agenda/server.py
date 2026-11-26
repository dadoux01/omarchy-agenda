"""Long-running helper spoken to by Service.qml over stdin/stdout.

Requests:  {"id": 7, "op": "sync", ...}            one JSON object per line
Replies:   {"id": 7, "ok": true, "result": {...}}   or "ok": false, "error"
Pushes:    {"event": "state", "state": {...}}       {"event": "open"}

Requests run one at a time on a worker thread, so writes never race each
other. Reminders and the daily recap run on a scheduler thread that only
reads the cache, so they keep working offline.
"""

import datetime as dt
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

from . import VERSION, engine as engine_mod, google, reminders, storage

FIRED_FILE = "fired.json"
TICK_SECONDS = 15
RESUME_GAP_SECONDS = 90
MAX_LIVE_NOTIFICATIONS = 12
MAX_LINE = 256 * 1024
# Omarchy draws a notification's mark from the "omarchy-glyph" hint, in the
# theme's colours, the way its own notifications look: in the toast and in
# notification history alike. Nerd Font md-calendar. Other notification
# daemons ignore the hint.
GLYPH = "\U000F00ED"


DEFAULT_STRINGS = {
    "app": "Agenda",
    "inMinutes": "In %1 min",
    "inHours": "In %1 h",
    "inHoursMinutes": "In %1 h %2 min",
    "inDays": "In %1 days",
    "startsNow": "Starting now",
    "late": "%1 (late)",
    "allDay": "All day",
    "privateTitle": "Upcoming event",
    "join": "Join",
    "snooze": "Snooze 5 min",
    "open": "Open",
    "recapTitle": "Today",
    "recapEmpty": "Nothing planned today",
    "recapMore": "+%1 more",
}


def fill(template, *args):
    out = template
    for i, a in enumerate(args, 1):
        out = out.replace("%" + str(i), str(a))
    return out


def plain_summary(text):
    """The summary is plain text by the notification spec (Omarchy shows it
    as PlainText): only control characters go."""
    return "".join(c for c in text if ord(c) >= 32 or c == "\n").replace("\n", " ")


def neutral_body(text):
    """The body is markup. Omarchy decodes entities and then lets <b>, <i>,
    <a href> and a few others through, so escaping alone would not stop an
    event title from becoming a link. Swap the characters markup is made of
    for look-alikes instead: what the server sent is shown, never obeyed."""
    out = text.replace("<", "\u2039").replace(">", "\u203a").replace("&", "\uff06")
    return "".join(c for c in out if ord(c) >= 32 or c == "\n")


class Server:
    def __init__(self, out=sys.stdout):
        self.out = out
        self.out_lock = threading.Lock()
        self.engine = engine_mod.Engine()
        self.requests = queue.Queue()
        self.strings = dict(DEFAULT_STRINGS)
        self.use24h = True
        self.configured = threading.Event()
        self.started_at = time.time()
        self.synced_once = False
        self.recap_pending = True
        self.last_sync = 0.0
        self.sync_requested = threading.Event()
        self.sync_queued = False
        self.cancel_login = threading.Event()
        self.login_lock = threading.Lock()
        self.live_notifications = 0
        self.live_lock = threading.Lock()
        self.closing = threading.Event()
        state = storage.read_json(storage.state_path(FIRED_FILE), {})
        state = state if isinstance(state, dict) else {}
        self.fired = state.get("fired", {}) if isinstance(state.get("fired"), dict) else {}
        self.snoozes = state.get("snoozes", []) if isinstance(state.get("snoozes"), list) else []
        self.last_recap_day = str(state.get("lastRecapDay", ""))
        self.state_lock = threading.Lock()

    # ------------------------------------------------------------ output

    def emit(self, obj):
        if self.closing.is_set():
            return
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        with self.out_lock:
            try:
                self.out.write(line + "\n")
                self.out.flush()
            except (BrokenPipeError, OSError, ValueError):
                self.closing.set()

    def snapshot(self):
        eng = self.engine
        with eng.lock:
            return {
                "version": VERSION,
                "accounts": eng.public_accounts(),
                "events": eng.events(),
                "settings": dict(eng.settings),
                "syncing": eng.syncing,
                "syncedAt": eng.cache.get("syncedAt", 0),
            }

    def push_state(self):
        self.emit({"event": "state", "state": self.snapshot()})

    # ------------------------------------------------------------ requests

    def handle(self, req):
        op = req.get("op")
        p = req.get("params") or {}
        eng = self.engine
        if op == "configure":
            strings = p.get("strings")
            if isinstance(strings, dict):
                for k, v in strings.items():
                    if k in DEFAULT_STRINGS and isinstance(v, str) and 0 < len(v) <= 200:
                        self.strings[k] = v
            self.use24h = bool(p.get("use24h", True))
            self.configured.set()
            return self.snapshot()
        if op == "state":
            return self.snapshot()
        if op == "sync":
            if p.get("background"):
                self.sync_queued = False
            self.do_sync(force=bool(p.get("force")))
            return {"syncedAt": eng.cache.get("syncedAt", 0)}
        if op in ("connect", "discover"):
            return eng.connect(p, self.cancel_login)
        if op == "reconnect":
            res = eng.reconnect(str(p.get("id", "")), self.cancel_login)
            self.requests.put({"op": "sync", "params": {"force": True}})
            return res
        if op == "cancelLogin":
            self.cancel_login.set()
            return {}
        if op == "googleClientFiles":
            return {"files": google.find_client_files()}
        if op == "saveAccount":
            res = eng.save_account(str(p.get("token", "")), str(p.get("label", "")),
                                   p.get("calendars"))
            self.do_sync(force=True)
            return res
        if op == "updateAccount":
            eng.update_account(str(p.get("id", "")), p.get("label"), p.get("calendars"))
            self.do_sync()
            return {}
        if op == "removeAccount":
            eng.remove_account(str(p.get("id", "")))
            self.push_state()
            return {}
        if op == "createEvent":
            res = eng.create_event(p)
            self.do_sync(only={res["calendarKey"]})
            return res
        if op == "deleteEvent":
            scope = "occurrence" if p.get("scope") == "occurrence" else "series"
            res = eng.delete_event(str(p.get("key", "")), scope)
            self.do_sync(only={res["calendarKey"]})
            return res
        if op == "setSettings":
            settings = eng.set_settings(p.get("settings") or {})
            self.push_state()
            return settings
        if op == "snooze":
            try:
                mins = int(p.get("minutes", 5) or 5)
            except (TypeError, ValueError):
                mins = 5
            self.snooze(str(p.get("key", "")), max(1, min(1440, mins)))
            return {}
        if op == "testNotification":
            self.notify(self.strings["app"], fill(self.strings["inMinutes"], 10), [], None)
            return {}
        raise engine_mod.EngineError("unknown_op")

    def do_sync(self, force=False, only=None):
        self.engine.syncing = True
        self.push_state()
        try:
            self.engine.sync(force=force, only=only)
        finally:
            self.last_sync = time.time()
            self.synced_once = True
            self.push_state()

    def run_request(self, req):
        rid = req.get("id")
        try:
            result = self.handle(req)
            if rid is not None:
                self.emit({"id": rid, "ok": True, "result": result})
        except engine_mod.EngineError as exc:
            if rid is not None:
                self.emit({"id": rid, "ok": False,
                           "error": {"code": exc.code, "detail": str(exc.detail)[:300]}})
        except storage.StorageError as exc:
            if rid is not None:
                code = "corrupt_secrets" if str(exc) == "corrupt_secrets" else "storage"
                self.emit({"id": rid, "ok": False,
                           "error": {"code": code, "detail": str(exc)[:300]}})
        except Exception as exc:  # never let one bad request kill the helper
            sys.stderr.write("agenda: %s failed: %s\n" % (req.get("op"), type(exc).__name__))
            if rid is not None:
                self.emit({"id": rid, "ok": False,
                           "error": {"code": "internal", "detail": type(exc).__name__}})

    def worker(self):
        while True:
            req = self.requests.get()
            if req is None:
                return
            self.run_request(req)

    def start_login(self, req):
        """A browser sign-in waits on the user for minutes: it gets its own
        thread so syncs and other requests keep flowing meanwhile. One at a
        time; a second one is refused rather than racing for the browser."""
        if not self.login_lock.acquire(blocking=False):
            self.emit({"id": req.get("id"), "ok": False, "error": {"code": "login_busy", "detail": ""}})
            return
        self.cancel_login.clear()

        def run():
            try:
                self.run_request(req)
            finally:
                self.login_lock.release()

        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------ scheduler

    def scheduler(self):
        last_tick = time.time()
        while not self.closing.is_set():
            self.sync_requested.wait(TICK_SECONDS)
            if self.closing.is_set():
                break
            forced = self.sync_requested.is_set()
            self.sync_requested.clear()
            now = time.time()
            resumed = now - last_tick > RESUME_GAP_SECONDS
            last_tick = now
            if resumed:
                self.recap_pending = True
            try:
                self.tick(now, resumed or forced)
            except Exception as exc:
                sys.stderr.write("agenda: scheduler: %s\n" % (type(exc).__name__,))

    def sync_interval(self, now_ms, events):
        minutes = int(self.engine.settings.get("syncMinutes", 10))
        soon = any(now_ms < e["start"] < now_ms + 30 * 60 * 1000 for e in events)
        if soon:
            minutes = min(minutes, 3)
        return minutes * 60

    def tick(self, now, want_sync):
        now_ms = int(now * 1000)
        events = self.engine.events()
        if self.engine.accounts and not self.sync_queued and (
                want_sync or not self.synced_once
                or now - self.last_sync >= self.sync_interval(now_ms, events)):
            # One background sync waiting at a time; a slow network must not
            # pile them up behind each other.
            self.sync_queued = True
            self.requests.put({"op": "sync", "params": {"background": True}})
        if not self.configured.is_set() and now - self.started_at < 20:
            return
        self.send_due(now_ms, events)
        self.maybe_recap(now, events)

    def send_due(self, now_ms, events):
        if self.engine.demo is not None:
            return  # demo events never notify
        settings = self.engine.settings
        with self.state_lock:
            due = reminders.due(events, now_ms, self.fired, settings, self.snoozes)
            if not due:
                return
            for r in due:
                self.fired[r["key"]] = now_ms
            self.fired = reminders.prune_fired(self.fired, now_ms)
            self.snoozes = [s for s in self.snoozes if s["key"] not in self.fired
                            and now_ms - s["at"] < reminders.FIRED_KEEP_MS]
            self.save_state()
        for r in due[:6]:
            self.notify_reminder(r, now_ms)

    def maybe_recap(self, now, events):
        if self.engine.demo is not None:
            return
        if not self.recap_pending:
            return
        if not self.synced_once and now - self.started_at < 60 and self.engine.accounts:
            return
        self.recap_pending = False
        today = dt.datetime.now(self.engine.zone).date()
        if not self.engine.settings.get("dailyRecap", True) or not self.engine.accounts:
            return
        with self.state_lock:
            if self.last_recap_day == today.isoformat():
                return
            self.last_recap_day = today.isoformat()
            self.save_state()
        self.send_recap(today, events)

    def save_state(self):
        storage.write_json(storage.state_path(FIRED_FILE), {
            "fired": self.fired, "snoozes": self.snoozes, "lastRecapDay": self.last_recap_day})

    def snooze(self, event_key, minutes):
        minutes = max(1, min(120, minutes))
        at = int(time.time() * 1000) + minutes * 60 * 1000
        with self.state_lock:
            self.snoozes.append({"key": "snooze|%s|%d" % (event_key, at), "eventKey": event_key, "at": at})
            if len(self.snoozes) > 100:
                self.snoozes = self.snoozes[-100:]
            self.save_state()

    # ------------------------------------------------------------ notifications

    def when_text(self, r):
        s = self.strings
        if r["kind"] == "start" or r["offset"] <= 30:
            text = s["startsNow"]
        else:
            minutes = int(round(r["offset"] / 60))
            if minutes < 60:
                text = fill(s["inMinutes"], minutes)
            elif minutes < 1440:
                h, m = divmod(minutes, 60)
                text = fill(s["inHours"], h) if m == 0 else fill(s["inHoursMinutes"], h, m)
            else:
                text = fill(s["inDays"], int(round(minutes / 1440)))
        if r["late"]:
            text = fill(s["late"], text)
        return text

    def notify_reminder(self, r, now_ms):
        ev = r["event"]
        s = self.strings
        zone = self.engine.zone
        private = self.engine.settings.get("privateNotifications", False)
        when = self.when_text(r)
        if ev.get("allDay"):
            time_text = s["allDay"]
        else:
            time_text = reminders.format_time(ev["start"], zone, self.use24h) + " – " + \
                reminders.format_time(ev["end"], zone, self.use24h)
        if private:
            title = s["privateTitle"]
            body = when
        else:
            title = ev.get("title") or s["privateTitle"]
            lines = [when + " · " + time_text]
            if ev.get("location"):
                lines.append(ev["location"])
            body = "\n".join(lines)
        actions = []
        if ev.get("meetingUrl") and not private:
            actions.append(("join", s["join"]))
        if r["kind"] != "start" and now_ms < ev["start"]:
            actions.append(("snooze", s["snooze"]))
        actions.append(("open", s["open"]))

        def on_action(name):
            if name == "join":
                open_url(ev.get("meetingUrl", ""))
            elif name == "snooze":
                self.snooze(ev["key"], 5)
            elif name in ("open", "default"):
                self.emit({"event": "open"})

        self.notify(title, body, actions, on_action,
                    urgency="critical" if r["kind"] == "start" and ev.get("meetingUrl") else "normal")

    def send_recap(self, day, events):
        s = self.strings
        todays = reminders.events_on(events, day, self.engine.zone)
        private = self.engine.settings.get("privateNotifications", False)
        if not todays:
            body = s["recapEmpty"]
        else:
            lines = []
            for ev in todays[:8]:
                when = s["allDay"] if ev.get("allDay") else \
                    reminders.format_time(ev["start"], self.engine.zone, self.use24h)
                title = s["privateTitle"] if private else (ev.get("title") or "…")
                lines.append(when + "  " + title)
            if len(todays) > 8:
                lines.append(fill(s["recapMore"], len(todays) - 8))
            body = "\n".join(lines)
        self.notify(s["recapTitle"], body, [("open", s["open"])],
                    lambda name: self.emit({"event": "open"}) if name in ("open", "default") else None)

    def notify(self, title, body, actions, on_action, urgency="normal"):
        tool = shutil.which("notify-send")
        if not tool:
            return
        args = [tool, "-a", self.strings["app"], "-u", urgency]
        args += ["-h", "string:omarchy-glyph:" + GLYPH]
        with self.live_lock:
            can_wait = bool(actions) and on_action is not None and \
                self.live_notifications < MAX_LIVE_NOTIFICATIONS
            if can_wait:
                self.live_notifications += 1
        if can_wait:
            for name, label in actions:
                args += ["-A", name + "=" + label]
        args += ["--", plain_summary(title)[:300], neutral_body(body)[:1500]]

        def run():
            try:
                res = subprocess.run(args, capture_output=True, text=True,
                                     timeout=6 * 3600 if can_wait else 15, check=False)
                choice = res.stdout.strip().splitlines()[-1] if res.stdout.strip() else ""
                if can_wait and choice and re.match(r"^[a-z]+$", choice):
                    on_action(choice)
            except (OSError, subprocess.SubprocessError):
                pass
            finally:
                if can_wait:
                    with self.live_lock:
                        self.live_notifications -= 1

        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------ main

    def serve(self, inp=sys.stdin):
        worker = threading.Thread(target=self.worker, daemon=True)
        worker.start()
        threading.Thread(target=self.scheduler, daemon=True).start()
        self.push_state()
        for line in inp:
            if len(line) > MAX_LINE:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except ValueError:
                continue
            if not isinstance(req, dict):
                continue
            op = req.get("op")
            params = req.get("params") if isinstance(req.get("params"), dict) else {}
            if op == "reconnect" or (op in ("connect", "discover") and params.get("kind") == "google"):
                self.start_login(req)
            elif op == "cancelLogin":
                self.cancel_login.set()
                self.emit({"id": req.get("id"), "ok": True, "result": {}})
            else:
                self.requests.put(req)
        # stdin closed: the shell went away. Finish what was asked, then exit.
        self.requests.put(None)
        worker.join(timeout=30)
        self.closing.set()
        self.sync_requested.set()


def open_url(url):
    if not re.match(r"^https://[A-Za-z0-9.-]+(:\d+)?(/[^\s\"'<>`]*)?$", url or "") or len(url) > 2000:
        return
    tool = shutil.which("xdg-open")
    if tool:
        subprocess.Popen([tool, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)


def main(argv):
    if len(argv) > 1 and argv[1] == "--version":
        print(VERSION)
        return 0
    try:
        storage.ensure_dir(storage.config_dir())
        storage.ensure_dir(storage.state_dir())
    except storage.StorageError as exc:
        sys.stderr.write("agenda: %s\n" % exc)
        return 1
    Server().serve()
    return 0
