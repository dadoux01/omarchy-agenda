import datetime as dt
import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from agenda import caldav, engine, ics, net, reminders, secrets, server, storage  # noqa: E402

PARIS = ZoneInfo("Europe/Paris")


class ScopeTests(unittest.TestCase):
    def test_scope(self):
        self.assertEqual(net.auth_scope("https://caldav.icloud.com/"), "icloud.com")
        self.assertEqual(net.auth_scope("https://example.com/dav"), "example.com")
        self.assertEqual(net.auth_scope("https://mycal.co.uk/dav"), "mycal.co.uk")
        self.assertEqual(net.auth_scope("https://tenant.nextcloud-hoster.org/"), "tenant.nextcloud-hoster.org")
        self.assertTrue(net.host_in_scope("p42-caldav.icloud.com", "icloud.com"))
        self.assertFalse(net.host_in_scope("icloud.com.evil.io", "icloud.com"))
        self.assertFalse(net.host_in_scope("evilicloud.com", "icloud.com"))

    def test_insecure_url_refused(self):
        with self.assertRaises(net.NetError) as ctx:
            net.request("GET", "http://example.com/")
        self.assertEqual(ctx.exception.code, "insecure_url")

    def test_redirect_to_loopback_refused(self):
        FakeConn.routes = {("GET", "caldav.icloud.com", "/"): (302, {"Location": "https://127.0.0.1/secret"}, "")}
        with mock.patch("http.client.HTTPSConnection", FakeConn):
            with self.assertRaises(net.NetError) as ctx:
                net.request("GET", "https://caldav.icloud.com/", auth="Basic secret")
        self.assertEqual(ctx.exception.code, "insecure_url")


class FakeConn:
    """Stands in for http.client.HTTPSConnection, scripted per host+path."""
    routes = {}
    seen = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host = host

    def request(self, method, path, body=None, headers=None):
        FakeConn.seen.append((method, self.host, path, dict(headers or {})))
        self.key = (method, self.host, path)

    def getresponse(self):
        status, headers, body = FakeConn.routes[self.key]
        return FakeResp(status, headers, body)

    def close(self):
        pass


class FakeResp:
    def __init__(self, status, headers, body):
        self.status = status
        self._headers = headers
        self._body = body if isinstance(body, bytes) else body.encode()

    def getheaders(self):
        return list(self._headers.items())

    def read(self, n=-1):
        return self._body if n < 0 else self._body[:n]


class RedirectTests(unittest.TestCase):
    # Names the tests resolve, so no test touches real DNS.
    ADDRESSES = {"intranet.example.com": "10.0.0.5"}

    def setUp(self):
        FakeConn.seen = []
        patcher = mock.patch("http.client.HTTPSConnection", FakeConn)
        patcher.start()
        self.addCleanup(patcher.stop)

        def lookup(host, port, *args, **kwargs):
            ip = self.ADDRESSES.get(host, "93.184.216.34")
            return [(2, 1, 6, "", (ip, port))]
        dns = mock.patch("socket.getaddrinfo", side_effect=lookup)
        dns.start()
        self.addCleanup(dns.stop)

    def test_redirect_to_name_on_private_address_refused(self):
        FakeConn.routes = {("GET", "a.example.com", "/"):
                           (302, {"Location": "https://intranet.example.com/"}, "")}
        with self.assertRaises(net.NetError) as ctx:
            net.request("GET", "https://a.example.com/", auth="Basic x")
        self.assertEqual(ctx.exception.code, "insecure_url")

    def test_lan_server_may_redirect_within_lan(self):
        FakeConn.routes = {
            ("GET", "nas.local", "/"): (302, {"Location": "https://192.168.1.20/dav"}, ""),
            ("GET", "192.168.1.20", "/dav"): (200, {}, "ok"),
        }
        resp = net.request("GET", "https://nas.local/")
        self.assertEqual(resp.status, 200)

    def test_credentials_not_sent_off_domain(self):
        FakeConn.routes = {
            ("GET", "caldav.icloud.com", "/"): (302, {"Location": "https://p1-caldav.icloud.com/x"}, ""),
            ("GET", "p1-caldav.icloud.com", "/x"): (302, {"Location": "https://evil.example/steal"}, ""),
            ("GET", "evil.example", "/steal"): (200, {}, "ok"),
        }
        net.request("GET", "https://caldav.icloud.com/", auth="Basic secret")
        auth = [(h, "Authorization" in hd) for _, h, _, hd in FakeConn.seen]
        self.assertEqual(auth, [("caldav.icloud.com", True), ("p1-caldav.icloud.com", True),
                                ("evil.example", False)])

    def test_redirect_to_http_refused(self):
        FakeConn.routes = {("GET", "a.example.com", "/"): (301, {"Location": "http://a.example.com/"}, "")}
        with self.assertRaises(net.NetError) as ctx:
            net.request("GET", "https://a.example.com/", auth="Basic x")
        self.assertEqual(ctx.exception.code, "insecure_url")

    def test_size_cap(self):
        FakeConn.routes = {("GET", "a.example.com", "/"): (200, {}, b"x" * 100)}
        with self.assertRaises(net.NetError) as ctx:
            net.request("GET", "https://a.example.com/", max_bytes=10)
        self.assertEqual(ctx.exception.code, "response_too_large")


PRINCIPAL = """<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response><d:href>/</d:href>
<d:propstat><d:prop><d:current-user-principal><d:href>/123/principal/</d:href></d:current-user-principal>
</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>"""

HOME = """<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<d:response><d:href>/123/principal/</d:href><d:propstat><d:prop><c:calendar-home-set>
<d:href>https://p9-caldav.icloud.com:443/123/calendars/</d:href></c:calendar-home-set></d:prop>
<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>"""

CALS = """<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"
 xmlns:cs="http://calendarserver.org/ns/" xmlns:a="http://apple.com/ns/ical/">
<d:response><d:href>/123/calendars/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype>
</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
<d:response><d:href>/123/calendars/home/</d:href><d:propstat><d:prop>
<d:resourcetype><d:collection/><c:calendar/></d:resourcetype><d:displayname>Maison</d:displayname>
<a:calendar-color>#FF2968FF</a:calendar-color><cs:getctag>c1</cs:getctag>
<c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set>
<d:current-user-privilege-set><d:privilege><d:read/></d:privilege><d:privilege><d:write/></d:privilege></d:current-user-privilege-set>
</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
<d:response><d:href>/123/calendars/tasks/</d:href><d:propstat><d:prop>
<d:resourcetype><d:collection/><c:calendar/></d:resourcetype><d:displayname>Rappels</d:displayname>
<c:supported-calendar-component-set><c:comp name="VTODO"/></c:supported-calendar-component-set>
</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
<d:response><d:href>/123/calendars/holidays/</d:href><d:propstat><d:prop>
<d:resourcetype><d:collection/><c:calendar/></d:resourcetype><d:displayname>Fériés</d:displayname>
<d:current-user-privilege-set><d:privilege><d:read/></d:privilege></d:current-user-privilege-set>
</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>"""

ICS_DATA = ("BEGIN:VCALENDAR&#13;\nBEGIN:VEVENT&#13;\nUID:e1&#13;\nDTSTART:20261125T130000Z&#13;\n"
            "DTEND:20261125T140000Z&#13;\nSUMMARY:Dentiste &amp; co&#13;\nBEGIN:VALARM&#13;\nACTION:DISPLAY&#13;\n"
            "TRIGGER:-PT15M&#13;\nEND:VALARM&#13;\nEND:VEVENT&#13;\nEND:VCALENDAR&#13;\n")

REPORT = """<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<d:response><d:href>/123/calendars/home/e1.ics</d:href><d:propstat><d:prop><d:getetag>"et1"</d:getetag>
<c:calendar-data>""" + ICS_DATA + """</c:calendar-data></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
</d:response></d:multistatus>"""

CTAG = """<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/">
<d:response><d:href>/123/calendars/home/</d:href><d:propstat><d:prop><cs:getctag>c1</cs:getctag></d:prop>
<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>"""


def icloud_routes():
    return {
        ("PROPFIND", "caldav.icloud.com", "/"): (207, {}, PRINCIPAL),
        ("PROPFIND", "caldav.icloud.com", "/123/principal/"): (207, {}, HOME),
        ("PROPFIND", "p9-caldav.icloud.com", "/123/calendars/"): (207, {}, CALS),
        ("PROPFIND", "p9-caldav.icloud.com", "/123/calendars/home/"): (207, {}, CTAG),
        ("REPORT", "p9-caldav.icloud.com", "/123/calendars/home/"): (207, {}, REPORT),
        ("PUT", "p9-caldav.icloud.com", "/123/calendars/home/x.ics"): (201, {"ETag": '"n"'}, ""),
        ("DELETE", "p9-caldav.icloud.com", "/123/calendars/home/e1.ics"): (204, {}, ""),
    }


class IsolatedDirs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name + "/c",
                                           "XDG_DATA_HOME": self.tmp.name + "/d",
                                           "XDG_STATE_HOME": self.tmp.name + "/s", "TZ": "Europe/Paris"})
        env.start()
        self.addCleanup(env.stop)
        # Never touch the real keyring from tests.
        nokey = mock.patch.object(secrets, "_secret_tool", lambda: None)
        nokey.start()
        self.addCleanup(nokey.stop)
        FakeConn.seen = []
        FakeConn.routes = icloud_routes()
        conn = mock.patch("http.client.HTTPSConnection", FakeConn)
        conn.start()
        self.addCleanup(conn.stop)


class CalDavTests(IsolatedDirs):
    def test_discover_icloud(self):
        base, cals = caldav.discover("https://caldav.icloud.com/", "me@icloud.com", "app-pass")
        self.assertEqual([c["name"] for c in cals], ["Maison", "Fériés"])
        self.assertEqual(cals[0]["color"], "#ff2968")
        self.assertFalse(cals[0]["readOnly"])
        self.assertTrue(cals[1]["readOnly"])
        self.assertEqual(cals[0]["id"], "https://p9-caldav.icloud.com:443/123/calendars/home/")

    def test_unsafe_xml_rejected(self):
        FakeConn.routes[("PROPFIND", "caldav.icloud.com", "/")] = (
            207, {}, '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x/>')
        FakeConn.routes[("PROPFIND", "caldav.icloud.com", "/.well-known/caldav")] = (404, {}, "")
        with self.assertRaises(net.NetError):
            caldav.discover("https://caldav.icloud.com/", "me@icloud.com", "p")

    def test_unsafe_xml_rejected_beyond_4kb(self):
        pad = "<!-- " + ("a" * 5000) + " -->"
        payload = b'<?xml version="1.0"?>' + pad.encode() + b'<!DOCTYPE foo><x/>'
        with self.assertRaises(net.NetError) as ctx:
            caldav.parse_xml(payload)
        self.assertEqual(ctx.exception.code, "unsafe_xml")

    def test_auth_failure(self):
        FakeConn.routes[("PROPFIND", "caldav.icloud.com", "/")] = (401, {}, "")
        with self.assertRaises(net.NetError) as ctx:
            caldav.discover("https://caldav.icloud.com/", "me@icloud.com", "bad")
        self.assertEqual(ctx.exception.code, "auth_failed")

    def test_icloud_forbidden_means_bad_credentials(self):
        FakeConn.routes[("PROPFIND", "caldav.icloud.com", "/")] = (403, {}, "")
        eng = engine.Engine()
        with self.assertRaises(engine.EngineError) as ctx:
            eng.discover("icloud", "", "me@icloud.com", "bad")
        self.assertEqual(ctx.exception.code, "auth_failed")

    def test_guess_server(self):
        self.assertEqual(caldav.guess_server("a@me.com"), "https://caldav.icloud.com/")
        self.assertEqual(caldav.guess_server("a@example.org"), "https://example.org/.well-known/caldav")
        self.assertEqual(caldav.guess_server("nope"), "")


class EngineTests(IsolatedDirs):
    def add_account(self, eng):
        found = eng.discover("icloud", "", "me@icloud.com", "app-pass")
        return eng.save_account(found["token"], "", [{"id": found["calendars"][1]["id"], "enabled": False}])

    def test_account_secret_kept_out_of_config(self):
        eng = engine.Engine()
        res = self.add_account(eng)
        self.assertEqual(res["secretStore"], "@file")
        with open(storage.config_path("accounts.json")) as fh:
            cfg = fh.read()
        self.assertNotIn("app-pass", cfg)
        mode = stat.S_IMODE(os.stat(storage.state_path("secrets.json")).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(storage.config_dir()).st_mode), 0o700)

    def test_sync_and_events(self):
        eng = engine.Engine()
        self.add_account(eng)
        with mock.patch.object(engine.Engine, "window", lambda self, now=None: (
                dt.datetime(2026, 11, 24, tzinfo=PARIS), dt.datetime(2027, 1, 25, tzinfo=PARIS))):
            errors = eng.sync()
        self.assertEqual(errors, {})
        evs = eng.events()
        self.assertEqual(len(evs), 1)
        ev = evs[0]
        self.assertEqual(ev["title"], "Dentiste & co")
        self.assertEqual(ev["alarms"], [900])
        self.assertEqual(ev["color"], "#ff2968")
        self.assertEqual(ev["etag"], '"et1"')
        # Second sync with the same ctag does not REPORT again.
        FakeConn.seen = []
        with mock.patch.object(engine.Engine, "window", lambda self, now=None: (
                dt.datetime(2026, 11, 24, tzinfo=PARIS), dt.datetime(2027, 1, 25, tzinfo=PARIS))):
            eng.sync()
        self.assertNotIn("REPORT", [m for m, *_ in FakeConn.seen])

    def test_sync_partial_auth_failure_preserves_remaining_calendars(self):
        eng = engine.Engine()
        found = eng.discover("icloud", "", "me@icloud.com", "app-pass")
        eng.save_account(found["token"], "", [{"id": c["id"], "enabled": True} for c in found["calendars"]])
        cal0 = found["calendars"][0]["id"]
        cal1 = found["calendars"][1]["id"]
        FakeConn.routes[("PROPFIND", "p9-caldav.icloud.com", "/123/calendars/holidays/")] = (207, {}, CTAG)
        FakeConn.routes[("REPORT", "p9-caldav.icloud.com", "/123/calendars/holidays/")] = (207, {}, REPORT)
        with mock.patch.object(engine.Engine, "window", lambda self, now=None: (
                dt.datetime(2026, 11, 24, tzinfo=PARIS), dt.datetime(2027, 1, 25, tzinfo=PARIS))):
            self.assertEqual(eng.sync(), {})
        # Fail cal0 with 401 on next sync; cal1 cached data must survive the early break.
        FakeConn.routes[("PROPFIND", "p9-caldav.icloud.com", "/123/calendars/home/")] = (401, {}, "")
        with mock.patch.object(engine.Engine, "window", lambda self, now=None: (
                dt.datetime(2026, 11, 24, tzinfo=PARIS), dt.datetime(2027, 1, 25, tzinfo=PARIS))):
            errors = eng.sync(force=True)
        self.assertEqual(list(errors.values()), ["auth_failed"])
        acc_id = eng.accounts[0]["id"]
        self.assertIn(acc_id + "|" + cal0, eng.cache["calendars"])
        self.assertIn(acc_id + "|" + cal1, eng.cache["calendars"])

    def test_delete_series_uses_if_match(self):
        eng = engine.Engine()
        self.add_account(eng)
        with mock.patch.object(engine.Engine, "window", lambda self, now=None: (
                dt.datetime(2026, 11, 24, tzinfo=PARIS), dt.datetime(2027, 1, 25, tzinfo=PARIS))):
            eng.sync()
        key = eng.events()[0]["key"]
        FakeConn.seen = []
        eng.delete_event(key, "series")
        method, host, path, headers = FakeConn.seen[-1]
        self.assertEqual((method, path, headers.get("If-Match")), ("DELETE", "/123/calendars/home/e1.ics", '"et1"'))

    def test_read_only_calendar_refuses_writes(self):
        eng = engine.Engine()
        self.add_account(eng)
        acc = eng.accounts[0]
        with self.assertRaises(engine.EngineError) as ctx:
            eng.create_event({"accountId": acc["id"], "calendarId": acc["calendars"][1]["id"],
                              "title": "x", "start": 0, "end": 3600000})
        self.assertEqual(ctx.exception.code, "read_only")

    def test_create_event_puts_ics(self):
        eng = engine.Engine()
        self.add_account(eng)
        acc = eng.accounts[0]
        with mock.patch.object(ics.uuid, "uuid4") as u:
            u.return_value.hex = "x"
            FakeConn.routes[("PUT", "p9-caldav.icloud.com",
                             "/123/calendars/home/x_dadoux01.agenda.ics")] = (201, {}, "")
            start = int(dt.datetime(2026, 11, 26, 14, tzinfo=PARIS).timestamp() * 1000)
            eng.create_event({"accountId": acc["id"], "calendarId": acc["calendars"][0]["id"],
                              "title": "Dentiste", "start": start, "end": start + 3600000,
                              "reminders": [10]})
        method, host, path, headers = FakeConn.seen[-1]
        self.assertEqual(method, "PUT")
        self.assertEqual(headers.get("If-None-Match"), "*")

    def test_create_event_validation(self):
        eng = engine.Engine()
        self.add_account(eng)
        acc = eng.accounts[0]
        base = {"accountId": acc["id"], "calendarId": acc["calendars"][0]["id"]}
        for bad, code in [({"title": "", "start": 0, "end": 1}, "missing_title"),
                          ({"title": "x", "start": 10, "end": 5}, "bad_time")]:
            with self.assertRaises(engine.EngineError) as ctx:
                eng.create_event(dict(base, **bad))
            self.assertEqual(ctx.exception.code, code)

    def test_settings_validated(self):
        eng = engine.Engine()
        s = eng.set_settings({"syncMinutes": 0, "reminders": "yes", "agendaDays": 9999, "bogus": 1})
        self.assertEqual(s["syncMinutes"], 2)
        self.assertTrue(s["reminders"])
        self.assertEqual(s["agendaDays"], 31)
        self.assertNotIn("bogus", s)

    def test_remove_account_clears_secret_and_cache(self):
        eng = engine.Engine()
        res = self.add_account(eng)
        eng.remove_account(res["id"])
        self.assertEqual(eng.accounts, [])
        self.assertIsNone(secrets.load(res["id"], "@file"))

    def test_corrupt_secrets_file_not_overwritten(self):
        storage.ensure_dir(storage.state_dir())
        with open(storage.state_path("secrets.json"), "w") as fh:
            fh.write("{corrupt")
        with self.assertRaises(storage.StorageError):
            secrets.store("acc1", "secret")
        with open(storage.state_path("secrets.json")) as fh:
            self.assertEqual(fh.read(), "{corrupt")


def ev(key="k", start=0, end=None, alarms=(), all_day=False, title="T"):
    return {"key": key, "start": start, "end": end if end is not None else start + 3600000,
            "alarms": list(alarms), "alarmsAt": [], "allDay": all_day, "title": title}


class ReminderTests(unittest.TestCase):
    S = {"reminders": True, "notifyAtStart": True, "extraReminderMinutes": 0, "lateGraceMinutes": 15}

    def test_due_window(self):
        e = ev(start=3600_000, alarms=[600])
        self.assertEqual(reminders.due([e], 3000_000 - 1, {}, self.S), [])
        due = reminders.due([e], 3000_000, {}, self.S)
        self.assertEqual([(r["offset"], r["late"]) for r in due], [(600, False)])
        due = reminders.due([e], 3000_000 + 10 * 60_000, {}, self.S)
        self.assertEqual([r["offset"] for r in due], [600, 0])
        self.assertTrue(due[0]["late"])
        self.assertEqual(reminders.due([e], 3000_000 + 16 * 60_000, {}, self.S)[0]["offset"], 0)

    def test_not_fired_twice(self):
        e = ev(start=3600_000, alarms=[600])
        due = reminders.due([e], 3000_000, {}, self.S)
        fired = {r["key"]: 1 for r in due}
        self.assertEqual(reminders.due([e], 3000_000 + 1000, fired, self.S), [])

    def test_moved_event_fires_again(self):
        fired = {reminders.fired_key(ev(start=3600_000), 600): 1}
        self.assertEqual(len(reminders.due([ev(start=4200_000, alarms=[600])], 3600_000, fired,
                                           dict(self.S, notifyAtStart=False))), 1)

    def test_settings(self):
        e = ev(start=3600_000, alarms=[600])
        s = dict(self.S, reminders=False, notifyAtStart=False, extraReminderMinutes=2)
        self.assertEqual([r["offset"] for r in reminders.due([e], 3480_000, {}, s)], [120])
        self.assertEqual(reminders.reminder_offsets(ev(all_day=True), self.S), [])

    def test_snooze(self):
        e = ev(key="a", start=3600_000)
        sn = [{"key": "snooze|a|100", "eventKey": "a", "at": 100}]
        due = reminders.due([e], 200, {}, self.S, sn)
        self.assertEqual([r["kind"] for r in due], ["snooze"])

    def test_events_on_day(self):
        day = dt.date(2026, 11, 25)
        s, e = reminders.day_bounds(day, PARIS)
        evs = [ev("b", s + 9 * 3600_000), ev("a", s, s + 86400_000, all_day=True),
               ev("x", e + 1), ev("y", s - 7200_000, s - 3600_000)]
        self.assertEqual([x["key"] for x in reminders.events_on(evs, day, PARIS)], ["a", "b"])

    def test_dst_day_bounds(self):
        s, e = reminders.day_bounds(dt.date(2026, 10, 25), PARIS)
        self.assertEqual(e - s, 25 * 3600_000)


class DemoTests(IsolatedDirs):
    def test_demo_replaces_accounts_and_never_writes(self):
        storage.write_json(storage.state_path("demo.json"), {
            "accounts": [{"label": "Work", "calendars": [{"name": "Team", "color": "#336699"}]}],
            "events": [{"title": "Standup", "day": 1, "time": "09:30", "minutes": 15, "alarms": [600]}]})
        eng = engine.Engine()
        self.add_real = False
        self.assertEqual([a["label"] for a in eng.public_accounts()], ["Work"])
        ev = eng.events()[0]
        self.assertEqual((ev["title"], ev["end"] - ev["start"], ev["color"]), ("Standup", 15 * 60000, "#336699"))
        self.assertEqual(eng.sync(), {})
        self.assertEqual(FakeConn.seen, [], "demo mode never touches the network")
        with self.assertRaises(engine.EngineError):
            eng.delete_event(ev["key"], "series")
        srv = server.Server(out=io.StringIO())
        sent = []
        srv.notify = lambda *a, **k: sent.append(a)
        srv.send_due(ev["start"] - 600000, srv.engine.events())
        srv.maybe_recap(time.time(), [])
        self.assertEqual(sent, [])


class ServerTests(IsolatedDirs):
    def make(self):
        out = io.StringIO()
        srv = server.Server(out=out)
        return srv, out

    def test_unknown_op_reports_error(self):
        srv, out = self.make()
        srv.requests.put({"id": 1, "op": "nope"})
        srv.requests.put(None)
        srv.worker()
        reply = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertEqual(reply, {"id": 1, "ok": False, "error": {"code": "unknown_op", "detail": ""}})

    def test_notification_args_are_escaped_and_separate(self):
        srv, _ = self.make()
        calls = []
        with mock.patch.object(server.shutil, "which", lambda name: "/usr/bin/" + name), \
                mock.patch.object(server.subprocess, "run",
                                  lambda args, **kw: calls.append(args) or mock.Mock(stdout="")), \
                mock.patch.object(server.threading, "Thread",
                                  lambda target, daemon: mock.Mock(start=target)):
            srv.notify("<b>$(rm -rf ~)</b>\x07", '<a href="https://evil.example">x</a> &lt;b&gt; & b', [], None)
        args = calls[0]
        self.assertEqual(args[-3], "--")
        self.assertEqual(args[-2], "<b>$(rm -rf ~)</b>", "summary is plain text: shown as typed")
        self.assertEqual(args[args.index("-h") + 1], "string:omarchy-glyph:" + server.GLYPH)
        self.assertNotIn("-i", args, "an image icon would take the glyph's place")
        self.assertFalse(any(ch in args[-1] for ch in "<>&"), "body must carry no markup characters")
        self.assertIn("evil.example", args[-1])
        self.assertNotIn("-A", args)

    def test_private_reminder_hides_title(self):
        srv, _ = self.make()
        srv.engine.settings["privateNotifications"] = True
        sent = []
        srv.notify = lambda title, body, actions, cb, urgency="normal": sent.append((title, body, actions))
        e = dict(ev(start=10_000_000, title="Secret meeting"), meetingUrl="https://meet.google.com/x",
                 location="HQ")
        srv.notify_reminder({"event": e, "offset": 600, "late": False, "kind": "before", "key": "z"}, 9_400_000)
        title, body, actions = sent[0]
        self.assertNotIn("Secret", title + body)
        self.assertNotIn("HQ", body)
        self.assertNotIn("join", [a for a, _ in actions])

    def test_recap_once_per_day(self):
        srv, _ = self.make()
        srv.engine.accounts = [{"id": "a"}]
        srv.synced_once = True
        sent = []
        srv.send_recap = lambda day, events: sent.append(day)
        srv.maybe_recap(time.time(), [])
        srv.recap_pending = True
        srv.maybe_recap(time.time(), [])
        self.assertEqual(len(sent), 1)
        srv2, _ = self.make()
        srv2.engine.accounts = [{"id": "a"}]
        srv2.synced_once = True
        srv2.send_recap = lambda day, events: sent.append(day)
        srv2.maybe_recap(time.time(), [])
        self.assertEqual(len(sent), 1, "a shell restart must not repeat today's recap")

    def test_when_text(self):
        srv, _ = self.make()
        self.assertEqual(srv.when_text({"kind": "before", "offset": 600, "late": False}), "In 10 min")
        self.assertEqual(srv.when_text({"kind": "before", "offset": 5400, "late": True}), "In 1 h 30 min (late)")
        self.assertEqual(srv.when_text({"kind": "start", "offset": 0, "late": False}), "Starting now")

    def test_open_url_validation(self):
        with mock.patch.object(server.subprocess, "Popen") as popen, \
                mock.patch.object(server.shutil, "which", lambda n: "/usr/bin/xdg-open"):
            server.open_url("file:///etc/passwd")
            server.open_url("https://x.example/a b")
            server.open_url("https://meet.google.com/abc")
        self.assertEqual(popen.call_count, 1)


if __name__ == "__main__":
    unittest.main()
