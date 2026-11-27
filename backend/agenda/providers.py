"""Providers behind one interface, so the engine never asks which is which.

    connect(params)                       -> Pending (calendars found, secret)
    session(account, secret)              -> whatever the provider reuses per sync
    fetch(session, account, calendar, start, end, state, force)
                                          -> None if unchanged, else (state, occurrences)
    create(session, account, calendar, event, zone)
    delete(session, account, calendar, event, scope)

Occurrences are the dicts ics.events_from_calendar() returns, plus `href`
and `etag` (what a later delete needs).
"""

import datetime as dt
import re
import urllib.parse

from . import caldav, ics, net

PALETTE = ["#7aa2f7", "#9ece6a", "#e0af68", "#f7768e", "#bb9af7", "#7dcfff",
           "#ff9e64", "#73daca", "#c0caf5", "#db4b4b"]


class ProviderError(Exception):
    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class Pending:
    """What a successful connect hands to save_account()."""

    def __init__(self, provider, kind, label, server, username, secret, calendars):
        self.provider = provider
        self.kind = kind
        self.label = label
        self.server = server
        self.username = username
        self.secret = secret
        self.calendars = calendars
        for i, cal in enumerate(self.calendars):
            if not cal.get("color"):
                cal["color"] = PALETTE[i % len(PALETTE)]


def clean(value, limit):
    value = "".join(c for c in str(value or "") if c == "\n" or ord(c) >= 32)
    return value.strip()[:limit]


# --------------------------------------------------------------------------
# CalDAV (iCloud and the rest)


class CalDavProvider:
    name = "caldav"

    def connect(self, params):
        kind = params.get("kind")
        username = clean(params.get("username"), 320)
        password = str(params.get("password") or "")
        if not username or not password or len(password) > 1024:
            raise ProviderError("missing_credentials")
        server = "https://caldav.icloud.com/" if kind == "icloud" else str(params.get("server") or "")
        try:
            base, cals = caldav.discover(server, username, password)
        except net.NetError as exc:
            # iCloud answers 403, not 401, to an Apple ID / app password it
            # does not accept, so the user gets the message that helps.
            code = "auth_failed" if exc.code == "forbidden" and kind == "icloud" else exc.code
            raise ProviderError(code, exc.detail)
        label = "iCloud" if kind == "icloud" else (urllib.parse.urlsplit(base).hostname or "CalDAV")
        calendars = [{k: c[k] for k in ("id", "name", "color", "readOnly")} for c in cals]
        return Pending("caldav", kind, label, base, username, password, calendars)

    def session(self, acc, secret):
        return caldav.Client(acc["server"], acc["username"], secret)

    def fetch(self, client, acc, cal, start, end, state, force, zone):
        ctag = ""
        try:
            ctag = client.ctag(cal["id"])
        except net.NetError as exc:
            if exc.code in ("auth_failed", "forbidden", "not_found"):
                raise
        if not force and ctag and ctag == state:
            return None
        items = client.fetch_range(cal["id"], start.astimezone(ics.UTC), end.astimezone(ics.UTC))
        out = []
        for item in items:
            try:
                occs = ics.events_from_calendar(item["data"], start, end, zone)
            except ics.IcsError:
                continue
            for o in occs:
                o["href"] = item["href"]
                o["etag"] = item["etag"]
                out.append(o)
        return ctag, out

    def create(self, client, acc, cal, p, zone):
        if p["all_day"]:
            start, end = p["start_date"], p["end_date"]
        else:
            start = dt.datetime.fromtimestamp(p["start_ms"] / 1000, ics.UTC)
            end = dt.datetime.fromtimestamp(p["end_ms"] / 1000, ics.UTC)
        uid, text = ics.build_event(p["title"], start, end, p["all_day"], p["location"],
                                    p["description"], p["reminders"],
                                    rrule=p.get("rrule", ""), zone=zone)
        client.put_new(cal["id"], uid, text)

    def delete(self, client, acc, cal, ev, scope):
        if scope == "occurrence" and ev.get("recurring") and ev.get("recurrenceId"):
            for _ in range(2):
                etag, text = client.get(ev["href"])
                try:
                    updated = ics.add_exdate(text, ev["uid"], ev["recurrenceId"])
                except ics.IcsError as exc:
                    raise ProviderError("bad_event", str(exc))
                try:
                    client.put_existing(ev["href"], updated, etag)
                    return
                except net.NetError as exc:
                    if exc.code != "conflict":
                        raise
            raise ProviderError("conflict")
        client.delete(ev["href"], ev.get("etag", ""))


# --------------------------------------------------------------------------
# Plain .ics / webcal links, read only


def normalize_feed_url(url):
    url = str(url or "").strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    elif url.lower().startswith("webcals://"):
        url = "https://" + url[len("webcals://"):]
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or len(url) > 4000:
        raise ProviderError("insecure_url")
    return url


def _feed_name(text, fallback):
    m = re.search(r"^X-WR-CALNAME[^:\r\n]*:(.*)$", text[:200000], re.MULTILINE)
    name = ics.unescape_text(m.group(1).strip(), 120) if m else ""
    return name or fallback


def _feed_color(text):
    m = re.search(r"^X-APPLE-CALENDAR-COLOR[^:\r\n]*:(#[0-9A-Fa-f]{6,8})", text[:200000], re.MULTILINE)
    return caldav.normalize_color(m.group(1)) if m else ""


class IcsProvider:
    """A calendar published as one .ics file. The link itself is the secret:
    Google's and Proton's private addresses grant read access to anyone who
    has them, so it is kept in the keyring, not in accounts.json."""

    name = "ics"

    def _get(self, url, headers=None):
        resp = net.request("GET", url, headers=dict({"Accept": "text/calendar, */*;q=0.5"}, **(headers or {})),
                           timeout=20)
        net.raise_for_status(resp, ok=(200, 304))
        return resp

    def connect(self, params):
        url = normalize_feed_url(params.get("url"))
        try:
            resp = self._get(url)
            text = resp.text()
            # Validate now so a wrong link fails here, not at every sync.
            ics.parse(text)
        except net.NetError as exc:
            raise ProviderError(exc.code, exc.detail)
        except ics.IcsError as exc:
            raise ProviderError("bad_feed", str(exc))
        if "BEGIN:VCALENDAR" not in text[:4096].upper():
            raise ProviderError("bad_feed")
        host = urllib.parse.urlsplit(url).hostname or "ics"
        name = _feed_name(text, host)
        cal = {"id": "feed", "name": name, "color": _feed_color(text), "readOnly": True}
        return Pending("ics", "ics", name, host, "", url, [cal])

    def session(self, acc, secret):
        return secret

    def fetch(self, url, acc, cal, start, end, state, force, zone):
        headers = {}
        if state and not force:
            etag, _, modified = state.partition("\n")
            if etag:
                headers["If-None-Match"] = etag
            if modified:
                headers["If-Modified-Since"] = modified
        resp = self._get(url, headers)
        if resp.status == 304:
            return None
        new_state = resp.header("etag") + "\n" + resp.header("last-modified")
        try:
            occs = ics.events_from_calendar(resp.text(), start, end, zone)
        except ics.IcsError as exc:
            raise ProviderError("bad_feed", str(exc))
        for o in occs:
            o["href"] = ""
            o["etag"] = ""
        return new_state, occs

    def create(self, *args):
        raise ProviderError("read_only")

    def delete(self, *args):
        raise ProviderError("read_only")
