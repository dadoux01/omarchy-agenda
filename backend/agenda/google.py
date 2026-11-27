"""Google Calendar through the user's own OAuth client.

The plugin ships no client id: each user creates a "Desktop app" OAuth
client in their own Google Cloud project (docs/google.md) and hands Agenda
the JSON file Google lets them download. Sign-in is the loopback flow for
installed apps (RFC 8252) with PKCE: a one-shot listener on 127.0.0.1 and a
random port, the browser for consent, and the code exchanged here.

Stored in the keyring: {"client_id", "client_secret", "refresh_token"}.
Access tokens live in memory only.
"""

import base64
import datetime as dt
import hashlib
import html
import http.server
import json
import os
import re
import secrets as _random
import shutil
import stat
import subprocess
import threading
import time
import urllib.parse

from . import ics, net
from .providers import Pending, ProviderError, clean

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"
SCOPE_EVENTS = "https://www.googleapis.com/auth/calendar.events"
SCOPE_LIST = "https://www.googleapis.com/auth/calendar.calendarlist.readonly"
SCOPES = SCOPE_EVENTS + " " + SCOPE_LIST
API_SCOPE = "googleapis.com"
LOGIN_TIMEOUT = 300
MAX_PAGES = 40
MAX_CLIENT_FILE = 64 * 1024

CLIENT_ID_RE = re.compile(r"^[0-9]{6,20}-[a-z0-9]{8,64}\.apps\.googleusercontent\.com$")

DONE_PAGE = """<!doctype html><meta charset="utf-8"><title>Agenda</title>
<style>body{font:16px system-ui,sans-serif;background:#111;color:#ddd;display:grid;place-items:center;height:90vh}</style>
<p>%s</p>"""


# --------------------------------------------------------------------------
# The client file


def read_client_file(path):
    """client_id and client_secret from the JSON Google offers for download."""
    path = os.path.expanduser(str(path or "").strip())
    if not path or not os.path.isabs(path):
        raise ProviderError("google_client_file")
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_CLIENT_FILE:
            raise ProviderError("google_client_file")
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            fd = None
            data = json.load(fh)
    except (OSError, ValueError):
        raise ProviderError("google_client_file")
    finally:
        if fd is not None:
            os.close(fd)
    return client_from_json(data)


def client_from_json(data):
    if not isinstance(data, dict):
        raise ProviderError("google_client_file")
    if "web" in data and "installed" not in data:
        raise ProviderError("google_client_web")
    inner = data.get("installed")
    if not isinstance(inner, dict):
        raise ProviderError("google_client_file")
    client_id = str(inner.get("client_id", "")).strip()
    client_secret = str(inner.get("client_secret", "")).strip()
    if not CLIENT_ID_RE.match(client_id) or not client_secret or len(client_secret) > 200:
        raise ProviderError("google_client_file")
    return client_id, client_secret


def find_client_files():
    """Candidate client files in ~/Downloads, newest first."""
    folder = os.path.join(os.path.expanduser("~"), "Downloads")
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for name in names:
        if not (name.startswith("client_secret_") and name.endswith(".json")):
            continue
        path = os.path.join(folder, name)
        try:
            client_id, _ = read_client_file(path)
            out.append({"path": path, "name": name, "clientId": client_id,
                        "mtime": int(os.path.getmtime(path))})
        except (ProviderError, OSError):
            continue
    out.sort(key=lambda f: -f["mtime"])
    return out[:10]


# --------------------------------------------------------------------------
# OAuth


def _pkce():
    verifier = base64.urlsafe_b64encode(os.urandom(48)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


class _Callback(http.server.BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.request.settimeout(5.0)

    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        state = (query.get("state") or [""])[0]
        if state != self.server.expected_state:
            # Favicon requests, stray tabs, anyone else: not our answer.
            self.send_response(404)
            self.end_headers()
            return
        self.server.result = {k: v[0] for k, v in query.items() if v}
        ok = "code" in self.server.result
        body = (DONE_PAGE % ("Agenda is connected. You can close this tab.<br>"
                             "Agenda est connecté. Tu peux fermer cet onglet." if ok else
                             "Sign-in was cancelled. / Connexion annulée.")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def open_browser(url):
    tool = shutil.which("xdg-open")
    if not tool:
        raise ProviderError("no_browser")
    subprocess.Popen([tool, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def authorize(client_id, client_secret, cancel=None, login_hint="", opener=open_browser,
              timeout=LOGIN_TIMEOUT):
    """Run the browser sign-in and return the token response."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _Callback)
    server.timeout = 0.5
    server.expected_state = _random.token_urlsafe(24)
    server.result = None
    redirect = "http://127.0.0.1:%d/" % server.server_address[1]
    verifier, challenge = _pkce()
    params = {
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": server.expected_state,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    if login_hint:
        params["login_hint"] = login_hint
    try:
        opener(AUTH_URL + "?" + urllib.parse.urlencode(params))
        deadline = time.time() + timeout
        while server.result is None:
            if cancel is not None and cancel.is_set():
                raise ProviderError("cancelled")
            if time.time() > deadline:
                raise ProviderError("login_timeout")
            server.handle_request()
    finally:
        server.server_close()
    result = server.result
    if "code" not in result:
        raise ProviderError("cancelled" if result.get("error") == "access_denied" else "google_denied",
                            result.get("error", ""))
    tokens = _token_request({
        "grant_type": "authorization_code",
        "code": result["code"],
        "code_verifier": verifier,
        "redirect_uri": redirect,
        "client_id": client_id,
        "client_secret": client_secret,
    })
    if not tokens.get("refresh_token"):
        raise ProviderError("google_no_refresh")
    granted = set(str(tokens.get("scope", "")).split())
    if SCOPE_EVENTS not in granted or SCOPE_LIST not in granted:
        # Google lets people untick scopes on the consent screen.
        raise ProviderError("google_scopes")
    return tokens


def _token_request(fields):
    body = urllib.parse.urlencode(fields)
    try:
        resp = net.request("POST", TOKEN_URL, body=body,
                           headers={"Content-Type": "application/x-www-form-urlencoded",
                                    "Accept": "application/json"}, timeout=10)
    except net.NetError as exc:
        raise ProviderError(exc.code, exc.detail)
    try:
        data = json.loads(resp.body or b"{}")
    except ValueError:
        data = {}
    if resp.status != 200:
        err = str(data.get("error", "")) if isinstance(data, dict) else ""
        if err == "invalid_grant":
            raise ProviderError("reauth_required")
        if err in ("invalid_client", "unauthorized_client"):
            raise ProviderError("google_client_invalid")
        raise ProviderError("google_token", err or str(resp.status))
    if not isinstance(data, dict) or not data.get("access_token"):
        raise ProviderError("google_token")
    return data


# --------------------------------------------------------------------------
# API session


class Session:
    def __init__(self, secret):
        try:
            data = json.loads(secret)
        except ValueError:
            raise ProviderError("missing_secret")
        self.client_id = data.get("client_id", "")
        self.client_secret = data.get("client_secret", "")
        self.refresh_token = data.get("refresh_token", "")
        if not (self.client_id and self.client_secret and self.refresh_token):
            raise ProviderError("missing_secret")
        self.access_token = ""
        self.expires = 0
        self.lock = threading.Lock()
        self.calendar_defaults = None

    def token(self):
        with self.lock:
            if self.access_token and time.time() < self.expires - 60:
                return self.access_token
            data = _token_request({
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            })
            self.access_token = data["access_token"]
            self.expires = time.time() + max(60, int(data.get("expires_in", 3600)))
            return self.access_token

    def call(self, method, path, query=None, body=None, headers=None, ok=(200, 204)):
        url = API + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        h = {"Accept": "application/json"}
        if body is not None:
            h["Content-Type"] = "application/json"
            body = json.dumps(body)
        if headers:
            h.update(headers)
        for attempt in range(2):
            resp = net.request(method, url, headers=h, body=body, auth="Bearer " + self.token(),
                               scope=API_SCOPE, timeout=15 if method != "GET" else 20)
            if resp.status == 401 and attempt == 0:
                with self.lock:
                    self.access_token = ""  # revoked or expired early; refresh once
                continue
            break
        if resp.status in ok or resp.status == 304:
            return resp
        if resp.status == 403:
            reason = ""
            try:
                errs = json.loads(resp.body).get("error", {}).get("errors", [])
                reason = errs[0].get("reason", "") if errs else ""
            except (ValueError, AttributeError):
                pass
            if reason in ("insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"):
                raise ProviderError("google_scopes")
            if reason in ("rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"):
                raise ProviderError("rate_limited")
            if reason == "accessNotConfigured":
                raise ProviderError("google_api_disabled")
            raise ProviderError("forbidden", reason)
        net.raise_for_status(resp, ok)
        return resp

    def json(self, *args, **kw):
        resp = self.call(*args, **kw)
        if resp.status == 304 or not resp.body:
            return None
        try:
            return json.loads(resp.body)
        except ValueError:
            raise ProviderError("bad_response")

    def calendars(self):
        items = []
        token = None
        for _ in range(10):
            q = {"minAccessRole": "reader", "maxResults": 250}
            if token:
                q["pageToken"] = token
            data = self.json("GET", "/users/me/calendarList", q) or {}
            items.extend(data.get("items", []))
            token = data.get("nextPageToken")
            if not token:
                break
        out = []
        for c in items:
            if not isinstance(c, dict) or not c.get("id") or c.get("deleted"):
                continue
            out.append({
                "id": str(c["id"])[:500],
                "name": clean(c.get("summaryOverride") or c.get("summary") or c["id"], 120),
                "color": _color(c.get("backgroundColor")),
                "readOnly": c.get("accessRole") not in ("owner", "writer"),
                "primary": bool(c.get("primary")),
                "hidden": bool(c.get("hidden")),
                "defaultAlarms": _popup_seconds(c.get("defaultReminders")),
            })
        out.sort(key=lambda c: (not c["primary"], c["hidden"], c["name"].lower()))
        return out


def _color(value):
    value = str(value or "")
    return value.lower() if re.match(r"^#[0-9A-Fa-f]{6}$", value) else ""


def _popup_seconds(reminders):
    out = set()
    for r in reminders or []:
        if isinstance(r, dict) and r.get("method", "popup") == "popup":
            try:
                minutes = int(r.get("minutes", -1))
            except (TypeError, ValueError):
                continue
            if 0 <= minutes <= 40320:
                out.add(minutes * 60)
    return sorted(out)


_TAG_BREAK = re.compile(r"<\s*(br|/p|/div|/li)\s*/?\s*>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]{0,2000}>")


def html_to_text(value, limit):
    """Google stores descriptions as HTML. The panel shows plain text, so
    tags are removed and entities decoded; nothing is ever rendered."""
    text = _TAG_BREAK.sub("\n", str(value or ""))
    text = _TAG.sub("", text)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return clean(text, limit)


def _when(value, zone):
    if not isinstance(value, dict):
        return None
    if value.get("dateTime"):
        try:
            t = dt.datetime.fromisoformat(str(value["dateTime"]).replace("Z", "+00:00"))
        except ValueError:
            return None
        return t if t.tzinfo else t.replace(tzinfo=zone)
    if value.get("date"):
        try:
            return dt.date.fromisoformat(str(value["date"]))
        except ValueError:
            return None
    return None


def event_from_api(item, default_alarms, zone):
    """One instance from events.list(singleEvents=true) as an occurrence."""
    if not isinstance(item, dict) or item.get("status") == "cancelled" or not item.get("id"):
        return None
    start = _when(item.get("start"), zone)
    end = _when(item.get("end"), zone)
    if start is None:
        return None
    if end is None or type(end) is not type(start) or end < start:
        end = start if isinstance(start, dt.datetime) else start + dt.timedelta(days=1)
    reminders = item.get("reminders") or {}
    alarms = default_alarms if reminders.get("useDefault", True) else _popup_seconds(reminders.get("overrides"))
    meeting = ""
    if item.get("hangoutLink"):
        meeting = ics.safe_meeting_url(str(item["hangoutLink"]))
    if not meeting:
        for ep in (item.get("conferenceData") or {}).get("entryPoints", []) or []:
            if isinstance(ep, dict) and ep.get("entryPointType") == "video":
                meeting = ics.safe_meeting_url(str(ep.get("uri", "")))
                if meeting:
                    break
    location = clean(item.get("location"), ics.MAX_TEXT["LOCATION"])
    description = html_to_text(item.get("description"), ics.MAX_TEXT["DESCRIPTION"])
    if not meeting:
        meeting = ics.safe_meeting_url(location + " " + description)
    recurring = bool(item.get("recurringEventId"))
    return {
        "uid": str(item.get("recurringEventId") or item["id"])[:500],
        "title": clean(item.get("summary"), ics.MAX_TEXT["SUMMARY"]),
        "location": location,
        "description": description,
        "start_ms": ics.to_epoch_ms(start, zone),
        "end_ms": ics.to_epoch_ms(end, zone),
        "all_day": not isinstance(start, dt.datetime),
        "recurring": recurring,
        "recurrence_id": str(item["id"])[:1000] if recurring else "",
        "alarms_before": list(alarms),
        "alarms_at": [],
        "meeting_url": meeting,
        "status": str(item.get("status", "")).upper(),
        "href": str(item["id"])[:1000],
        "etag": str(item.get("etag", ""))[:200],
    }


def _cal_path(calendar_id):
    return "/calendars/" + urllib.parse.quote(calendar_id, safe="")


# --------------------------------------------------------------------------
# Provider


class GoogleProvider:
    name = "google"

    def connect(self, params, cancel=None, opener=open_browser):
        if params.get("clientFile"):
            client_id, client_secret = read_client_file(params["clientFile"])
        else:
            client_id = str(params.get("clientId", "")).strip()
            client_secret = str(params.get("clientSecret", "")).strip()
            if not CLIENT_ID_RE.match(client_id) or not client_secret or len(client_secret) > 200:
                raise ProviderError("google_client_file")
        tokens = authorize(client_id, client_secret, cancel, opener=opener)
        secret = json.dumps({"client_id": client_id, "client_secret": client_secret,
                             "refresh_token": tokens["refresh_token"]})
        session = Session(secret)
        session.access_token = tokens["access_token"]
        try:
            expires_in = int(tokens.get("expires_in", 3600))
        except (TypeError, ValueError):
            expires_in = 3600
        session.expires = time.time() + max(60, expires_in)
        try:
            cals = session.calendars()
        except net.NetError as exc:
            raise ProviderError(exc.code, exc.detail)
        if not cals:
            raise ProviderError("no_calendars")
        primary = next((c for c in cals if c["primary"]), cals[0])
        email = primary["id"] if "@" in primary["id"] else ""
        for c in cals:
            # Calendars hidden in Google Calendar start hidden here too.
            c["enabled"] = not c.pop("hidden")
            c.pop("primary")
        return Pending("google", "google", "Google" + (" · " + email if email else ""),
                       "google", email, secret, cals)

    def reconnect(self, secret, cancel=None, opener=open_browser, login_hint=""):
        """A fresh refresh token for an account Google signed out."""
        try:
            old = json.loads(secret)
            client_id = old["client_id"]
            client_secret = old["client_secret"]
        except (ValueError, KeyError, TypeError):
            raise ProviderError("missing_secret")
        tokens = authorize(client_id, client_secret, cancel,
                           login_hint=login_hint, opener=opener)
        old["refresh_token"] = tokens["refresh_token"]
        return json.dumps(old)

    def session(self, acc, secret):
        return Session(secret)

    def fetch(self, session, acc, cal, start, end, state, force, zone):
        if session.calendar_defaults is None:
            # Default reminders can change in Google Calendar; read them once
            # per sync rather than trusting what was saved at sign-in.
            try:
                session.calendar_defaults = {c["id"]: c["defaultAlarms"] for c in session.calendars()}
            except (ProviderError, net.NetError):
                session.calendar_defaults = {}
        defaults = session.calendar_defaults.get(cal["id"], cal.get("defaultAlarms", []))
        query = {
            "singleEvents": "true",
            "orderBy": "startTime",
            "timeMin": start.astimezone(ics.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "timeMax": end.astimezone(ics.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "maxResults": 250,
        }
        headers = {}
        fingerprint = ",".join(str(s) for s in defaults)
        if state and not force:
            etag, _, old_fp = state.partition("\n")
            if etag and old_fp == fingerprint:
                headers["If-None-Match"] = etag
        out = []
        first_etag = ""
        token = None
        for page in range(MAX_PAGES):
            q = dict(query)
            if token:
                q["pageToken"] = token
            resp = session.call("GET", _cal_path(cal["id"]) + "/events", q,
                                headers=headers if page == 0 else None)
            if resp.status == 304:
                return None
            try:
                data = json.loads(resp.body)
            except ValueError:
                raise ProviderError("bad_response")
            if page == 0:
                first_etag = resp.header("etag") or str(data.get("etag", ""))
            for item in data.get("items", []):
                occ = event_from_api(item, defaults, zone)
                if occ:
                    out.append(occ)
            token = data.get("nextPageToken")
            if not token:
                break
        return first_etag + "\n" + fingerprint, out

    def create(self, session, acc, cal, p, zone):
        if p["all_day"]:
            start = {"date": p["start_date"].isoformat()}
            end = {"date": p["end_date"].isoformat()}
        else:
            start = {"dateTime": dt.datetime.fromtimestamp(p["start_ms"] / 1000, zone).isoformat()}
            end = {"dateTime": dt.datetime.fromtimestamp(p["end_ms"] / 1000, zone).isoformat()}
            # A recurring event needs a time zone to expand in; fall back to
            # UTC when the local zone has no IANA name.
            tzname = getattr(zone, "key", "") or ("UTC" if p.get("rrule") else "")
            if tzname:
                start["timeZone"] = end["timeZone"] = tzname
        body = {
            "summary": p["title"],
            "start": start,
            "end": end,
            "reminders": {"useDefault": False,
                          "overrides": [{"method": "popup", "minutes": m} for m in sorted(set(p["reminders"]))][:5]},
        }
        if p["location"]:
            body["location"] = p["location"]
        if p["description"]:
            body["description"] = p["description"]
        if p.get("rrule"):
            body["recurrence"] = ["RRULE:" + p["rrule"]]
        session.call("POST", _cal_path(cal["id"]) + "/events", body=body, ok=(200,))

    def delete(self, session, acc, cal, ev, scope):
        if scope == "series" and ev.get("recurring"):
            target, headers = ev["uid"], {}
        else:
            target = ev["href"]
            headers = {"If-Match": ev["etag"]} if ev.get("etag") else {}
        path = _cal_path(cal["id"]) + "/events/" + urllib.parse.quote(target, safe="")
        try:
            session.call("DELETE", path, headers=headers, ok=(200, 204, 410))
        except net.NetError as exc:
            if exc.code == "not_found":
                return  # already gone: what was asked is true
            raise
