import datetime as dt
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agenda import engine, google, net, providers  # noqa: E402
from test_backend import FakeConn, IsolatedDirs  # noqa: E402

PARIS = ZoneInfo("Europe/Paris")
CLIENT_ID = "123456789012-abcdefghijklmnop.apps.googleusercontent.com"
CLIENT = {"installed": {"client_id": CLIENT_ID, "client_secret": "GOCSPX-secret",
                        "redirect_uris": ["http://localhost"]}}
TOKENS = {"access_token": "ya29.access", "expires_in": 3599, "refresh_token": "1//refresh",
          "scope": google.SCOPE_EVENTS + " " + google.SCOPE_LIST, "token_type": "Bearer"}


def browser(path_and_query_for):
    """A stand-in for the user's browser: follow the auth URL to the loopback
    redirect, the way Google would after consent."""
    def opener(url):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        redirect = q["redirect_uri"][0]

        def go():
            for extra in path_and_query_for(q):
                try:
                    with urllib.request.urlopen(redirect + "?" + urllib.parse.urlencode(extra), timeout=5) as r:
                        r.read()
                except urllib.error.HTTPError as err:
                    err.close()
                except Exception:
                    pass
        threading.Thread(target=go, daemon=True).start()
    return opener


class ClientFileTests(unittest.TestCase):
    def test_installed_client(self):
        self.assertEqual(google.client_from_json(CLIENT), (CLIENT_ID, "GOCSPX-secret"))

    def test_web_client_refused_with_its_own_message(self):
        with self.assertRaises(providers.ProviderError) as ctx:
            google.client_from_json({"web": CLIENT["installed"]})
        self.assertEqual(ctx.exception.code, "google_client_web")

    def test_bad_client_id(self):
        bad = {"installed": dict(CLIENT["installed"], client_id="evil.example.com")}
        with self.assertRaises(providers.ProviderError):
            google.client_from_json(bad)

    def test_find_client_files(self):
        import tempfile
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(home + "/Downloads")
            with open(home + "/Downloads/client_secret_x.apps.googleusercontent.com.json", "w") as fh:
                json.dump(CLIENT, fh)
            with open(home + "/Downloads/client_secret_broken.json", "w") as fh:
                fh.write("{nope")
            with open(home + "/Downloads/other.json", "w") as fh:
                json.dump(CLIENT, fh)
            with mock.patch.dict(os.environ, {"HOME": home}):
                found = google.find_client_files()
        self.assertEqual([f["clientId"] for f in found], [CLIENT_ID])


class AuthorizeTests(unittest.TestCase):
    def setUp(self):
        self.token_calls = []
        patcher = mock.patch.object(google, "_token_request",
                                    lambda fields: self.token_calls.append(fields) or dict(TOKENS))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_loopback_flow_with_pkce(self):
        seen = {}

        def answers(q):
            seen.update(q)
            return [{"state": "wrong", "code": "stolen"},            # ignored
                    {"state": q["state"][0], "code": "the-code"}]

        tokens = google.authorize(CLIENT_ID, "GOCSPX-secret", opener=browser(answers), timeout=10)
        self.assertEqual(tokens["refresh_token"], "1//refresh")
        self.assertEqual(seen["code_challenge_method"], ["S256"])
        self.assertTrue(seen["redirect_uri"][0].startswith("http://127.0.0.1:"))
        self.assertEqual(seen["access_type"], ["offline"])
        call = self.token_calls[0]
        self.assertEqual(call["code"], "the-code")
        import base64, hashlib
        challenge = base64.urlsafe_b64encode(hashlib.sha256(call["code_verifier"].encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(challenge, seen["code_challenge"][0])

    def test_denied(self):
        with self.assertRaises(providers.ProviderError) as ctx:
            google.authorize(CLIENT_ID, "s", opener=browser(lambda q: [{"state": q["state"][0], "error": "access_denied"}]),
                             timeout=10)
        self.assertEqual(ctx.exception.code, "cancelled")

    def test_cancel(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(providers.ProviderError) as ctx:
            google.authorize(CLIENT_ID, "s", cancel, opener=lambda url: None, timeout=10)
        self.assertEqual(ctx.exception.code, "cancelled")

    def test_unticked_scope(self):
        google._token_request = lambda fields: dict(TOKENS, scope=google.SCOPE_LIST)
        with self.assertRaises(providers.ProviderError) as ctx:
            google.authorize(CLIENT_ID, "s", opener=browser(lambda q: [{"state": q["state"][0], "code": "c"}]),
                             timeout=10)
        self.assertEqual(ctx.exception.code, "google_scopes")


class ApiMappingTests(unittest.TestCase):
    def test_timed_event_with_default_reminders_and_meet(self):
        occ = google.event_from_api({
            "id": "abc_20261126T130000Z", "recurringEventId": "abc", "etag": '"e1"',
            "summary": "Standup", "location": "Bureau",
            "description": "Agenda:<br>1. <b>Point</b> &amp; démo<script>x</script>",
            "start": {"dateTime": "2026-11-26T14:00:00+01:00"}, "end": {"dateTime": "2026-11-26T14:15:00+01:00"},
            "reminders": {"useDefault": True},
            "hangoutLink": "https://meet.google.com/abc-defg-hij",
        }, [600], PARIS)
        self.assertEqual(occ["alarms_before"], [600])
        self.assertEqual(occ["meeting_url"], "https://meet.google.com/abc-defg-hij")
        self.assertEqual(occ["description"], "Agenda:\n1. Point & démox")
        self.assertTrue(occ["recurring"])
        self.assertEqual((occ["uid"], occ["recurrence_id"], occ["href"]), ("abc", "abc_20261126T130000Z", "abc_20261126T130000Z"))

    def test_all_day_and_overrides(self):
        occ = google.event_from_api({
            "id": "x", "summary": "Off", "start": {"date": "2026-12-24"}, "end": {"date": "2026-12-26"},
            "reminders": {"useDefault": False, "overrides": [{"method": "email", "minutes": 60},
                                                             {"method": "popup", "minutes": 1440}]},
        }, [600], PARIS)
        self.assertTrue(occ["all_day"])
        self.assertEqual(occ["alarms_before"], [86400])
        self.assertEqual(occ["end_ms"] - occ["start_ms"], 2 * 86400000)

    def test_cancelled_and_garbage(self):
        self.assertIsNone(google.event_from_api({"id": "x", "status": "cancelled"}, [], PARIS))
        self.assertIsNone(google.event_from_api({"id": "x", "start": {"dateTime": "nope"}}, [], PARIS))
        self.assertIsNone(google.event_from_api("nope", [], PARIS))


def google_routes():
    cal_list = {"items": [
        {"id": "me@gmail.com", "summary": "me@gmail.com", "primary": True, "accessRole": "owner",
         "backgroundColor": "#9FE1E7", "defaultReminders": [{"method": "popup", "minutes": 10}]},
        {"id": "fr.french#holiday@group.v.calendar.google.com", "summary": "Jours fériés",
         "accessRole": "reader", "backgroundColor": "#16A765"},
        {"id": "hidden@group.calendar.google.com", "summary": "Old", "accessRole": "owner", "hidden": True},
    ]}
    page1 = {"etag": '"list1"', "nextPageToken": "p2", "items": [
        {"id": "e1", "etag": '"a"', "summary": "Dentiste", "start": {"dateTime": "2026-11-26T14:00:00+01:00"},
         "end": {"dateTime": "2026-11-26T15:00:00+01:00"}, "reminders": {"useDefault": True}}]}
    page2 = {"items": [
        {"id": "s1_20261127T080000Z", "recurringEventId": "s1", "etag": '"b"', "summary": "Standup",
         "start": {"dateTime": "2026-11-27T09:00:00+01:00"}, "end": {"dateTime": "2026-11-27T09:15:00+01:00"},
         "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 5}]}}]}
    return {
        ("POST", "oauth2.googleapis.com", "/token"): (200, {}, json.dumps({"access_token": "ya29.new", "expires_in": 3600})),
        ("GET", "www.googleapis.com", "/calendar/v3/users/me/calendarList?minAccessRole=reader&maxResults=250"):
            (200, {}, json.dumps(cal_list)),
        "events-me-1": (200, {"ETag": '"list1"'}, json.dumps(page1)),
        "events-me-2": (200, {}, json.dumps(page2)),
    }


class GoogleConn(FakeConn):
    """FakeConn that also matches event list URLs by their calendar and page."""

    def getresponse(self):
        method, host, path = self.key
        if "/events?" in path and method == "GET":
            cal = urllib.parse.unquote(path.split("/calendars/")[1].split("/events")[0])
            q = urllib.parse.parse_qs(path.split("?", 1)[1])
            headers = FakeConn.seen[-1][3]
            if headers.get("If-None-Match") == '"list1"':
                from test_backend import FakeResp
                return FakeResp(304, {}, b"")
            if cal == "me@gmail.com":
                key = "events-me-2" if q.get("pageToken") == ["p2"] else "events-me-1"
            else:
                from test_backend import FakeResp
                return FakeResp(200, {}, json.dumps({"items": []}).encode())
            status, h, body = FakeConn.routes[key]
            from test_backend import FakeResp
            return FakeResp(status, h, body)
        return super().getresponse()


class GoogleEngineTests(IsolatedDirs):
    def setUp(self):
        super().setUp()
        FakeConn.routes = google_routes()
        conn = mock.patch("http.client.HTTPSConnection", GoogleConn)
        conn.start()
        self.addCleanup(conn.stop)
        auth = mock.patch.object(google, "authorize", lambda *a, **kw: dict(TOKENS))
        auth.start()
        self.addCleanup(auth.stop)
        window = mock.patch.object(engine.Engine, "window", lambda self, now=None: (
            dt.datetime(2026, 11, 24, tzinfo=PARIS), dt.datetime(2027, 1, 25, tzinfo=PARIS)))
        window.start()
        self.addCleanup(window.stop)

    def connect(self, eng):
        found = eng.connect({"kind": "google", "clientId": CLIENT_ID, "clientSecret": "GOCSPX-secret"})
        return found, eng.save_account(found["token"], "", [])

    def test_connect_lists_calendars(self):
        eng = engine.Engine()
        found, saved = self.connect(eng)
        self.assertEqual(found["label"], "Google · me@gmail.com")
        cals = {c["name"]: c for c in found["calendars"]}
        self.assertFalse(cals["me@gmail.com"]["readOnly"])
        self.assertTrue(cals["Jours fériés"]["readOnly"])
        self.assertFalse(cals["Old"]["enabled"], "hidden in Google, hidden here")
        acc = eng.accounts[0]
        self.assertEqual(acc["calendars"][0]["defaultAlarms"], [600])
        with open(os.environ["XDG_CONFIG_HOME"] + "/dadoux01.agenda/accounts.json") as fh:
            self.assertNotIn("refresh", fh.read())

    def test_sync_pages_and_reminders_then_304(self):
        eng = engine.Engine()
        self.connect(eng)
        self.assertEqual(eng.sync(), {})
        evs = {e["title"]: e for e in eng.events()}
        self.assertEqual(evs["Dentiste"]["alarms"], [600])
        self.assertEqual(evs["Standup"]["alarms"], [300])
        self.assertTrue(evs["Standup"]["recurring"])
        auth_headers = [h.get("Authorization") for m, host, p, h in FakeConn.seen if host == "www.googleapis.com"]
        self.assertTrue(all(a == "Bearer ya29.access" or a == "Bearer ya29.new" for a in auth_headers))
        FakeConn.seen = []
        eng.sync()
        self.assertEqual(len(eng.events()), 2, "304 keeps the cached events")

    def test_token_never_sent_to_other_hosts(self):
        eng = engine.Engine()
        self.connect(eng)
        eng.sync()
        for m, host, p, h in FakeConn.seen:
            if "Authorization" in h:
                self.assertTrue(host.endswith("googleapis.com"), host)

    def test_delete_series_and_occurrence(self):
        eng = engine.Engine()
        self.connect(eng)
        eng.sync()
        standup = [e for e in eng.events() if e["title"] == "Standup"][0]
        FakeConn.routes[("DELETE", "www.googleapis.com", "/calendar/v3/calendars/me%40gmail.com/events/s1")] = (204, {}, "")
        FakeConn.routes[("DELETE", "www.googleapis.com",
                         "/calendar/v3/calendars/me%40gmail.com/events/s1_20261127T080000Z")] = (204, {}, "")
        eng.delete_event(standup["key"], "series")
        self.assertEqual(FakeConn.seen[-1][2], "/calendar/v3/calendars/me%40gmail.com/events/s1")
        eng.delete_event(standup["key"], "occurrence")
        self.assertEqual(FakeConn.seen[-1][2], "/calendar/v3/calendars/me%40gmail.com/events/s1_20261127T080000Z")
        self.assertEqual(FakeConn.seen[-1][3].get("If-Match"), '"b"')

    def test_create_body(self):
        eng = engine.Engine()
        self.connect(eng)
        acc = eng.accounts[0]
        bodies = []
        orig = FakeConn.request

        def capture(conn, method, path, body=None, headers=None):
            if method == "POST" and "/events" in path:
                bodies.append(json.loads(body))
            return orig(conn, method, path, body, headers)
        FakeConn.routes[("POST", "www.googleapis.com", "/calendar/v3/calendars/me%40gmail.com/events")] = (200, {}, "{}")
        with mock.patch.object(GoogleConn, "request", capture):
            start = int(dt.datetime(2026, 11, 26, 14, tzinfo=PARIS).timestamp() * 1000)
            eng.create_event({"accountId": acc["id"], "calendarId": "me@gmail.com", "title": "RDV",
                              "start": start, "end": start + 3600000, "reminders": [10, 60]})
            eng.create_event({"accountId": acc["id"], "calendarId": "me@gmail.com", "title": "Off",
                              "start": start, "end": start + 1, "allDay": True})
            eng.create_event({"accountId": acc["id"], "calendarId": "me@gmail.com", "title": "Sport",
                              "start": start, "end": start + 3600000, "repeat": "weekly"})
        self.assertEqual(bodies[0]["start"], {"dateTime": "2026-11-26T14:00:00+01:00", "timeZone": "Europe/Paris"})
        self.assertEqual(bodies[0]["reminders"]["overrides"],
                         [{"method": "popup", "minutes": 10}, {"method": "popup", "minutes": 60}])
        self.assertEqual((bodies[1]["start"], bodies[1]["end"]), ({"date": "2026-11-26"}, {"date": "2026-11-27"}))
        self.assertNotIn("recurrence", bodies[0])
        self.assertEqual(bodies[2]["recurrence"], ["RRULE:FREQ=WEEKLY"])

    def test_revoked_token_is_reported(self):
        eng = engine.Engine()
        self.connect(eng)
        FakeConn.routes[("POST", "oauth2.googleapis.com", "/token")] = (400, {}, json.dumps({"error": "invalid_grant"}))
        errors = eng.sync()
        self.assertEqual(list(errors.values()), ["reauth_required"])

    def test_read_only_calendar(self):
        eng = engine.Engine()
        self.connect(eng)
        acc = eng.accounts[0]
        with self.assertRaises(engine.EngineError) as ctx:
            eng.create_event({"accountId": acc["id"], "calendarId": "fr.french#holiday@group.v.calendar.google.com",
                              "title": "x", "start": 0, "end": 1000})
        self.assertEqual(ctx.exception.code, "read_only")


FEED = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nX-WR-CALNAME:Fériés\\, France\r\nBEGIN:VEVENT\r\nUID:h1\r\n"
        "DTSTART;VALUE=DATE:20261225\r\nSUMMARY:Noël\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")


class IcsTests(IsolatedDirs):
    def setUp(self):
        super().setUp()
        FakeConn.routes = {("GET", "calendar.example.org", "/private-abc/basic.ics"): (200, {"ETag": '"f1"'}, FEED)}
        window = mock.patch.object(engine.Engine, "window", lambda self, now=None: (
            dt.datetime(2026, 11, 24, tzinfo=PARIS), dt.datetime(2027, 1, 25, tzinfo=PARIS)))
        window.start()
        self.addCleanup(window.stop)

    def test_webcal_feed_read_only_and_link_kept_secret(self):
        eng = engine.Engine()
        found = eng.connect({"kind": "ics", "url": "webcal://calendar.example.org/private-abc/basic.ics"})
        self.assertEqual(found["calendars"][0]["name"], "Fériés, France")
        eng.save_account(found["token"], "", [])
        with open(os.environ["XDG_CONFIG_HOME"] + "/dadoux01.agenda/accounts.json") as fh:
            self.assertNotIn("private-abc", fh.read())
        eng.sync()
        self.assertEqual([e["title"] for e in eng.events()], ["Noël"])
        self.assertTrue(eng.events()[0]["readOnly"])
        with self.assertRaises(engine.EngineError):
            eng.delete_event(eng.events()[0]["key"], "series")
        FakeConn.routes[("GET", "calendar.example.org", "/private-abc/basic.ics")] = (304, {}, "")
        eng.sync()
        self.assertEqual(FakeConn.seen[-1][3].get("If-None-Match"), '"f1"')
        self.assertEqual(len(eng.events()), 1)

    def test_http_and_garbage_refused(self):
        eng = engine.Engine()
        with self.assertRaises(engine.EngineError) as ctx:
            eng.connect({"kind": "ics", "url": "http://calendar.example.org/x.ics"})
        self.assertEqual(ctx.exception.code, "insecure_url")
        FakeConn.routes[("GET", "calendar.example.org", "/page")] = (200, {}, "<html>hello</html>")
        with self.assertRaises(engine.EngineError) as ctx:
            eng.connect({"kind": "ics", "url": "https://calendar.example.org/page"})
        self.assertEqual(ctx.exception.code, "bad_feed")


if __name__ == "__main__":
    unittest.main()
