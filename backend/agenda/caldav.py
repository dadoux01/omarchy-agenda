"""CalDAV (RFC 4791): iCloud, Fastmail, Nextcloud, Zoho, Yahoo, Radicale...

Discovery follows RFC 6764 as far as the standard library allows (no DNS
SRV lookups): the configured URL, then /.well-known/caldav on it and on the
e-mail domain, then current-user-principal -> calendar-home-set -> the
calendars under it.
"""

import base64
import re
import urllib.parse
import xml.etree.ElementTree as ET
import xml.parsers.expat as expat

from . import net

NS = {
    "d": "DAV:",
    "c": "urn:ietf:params:xml:ns:caldav",
    "cs": "http://calendarserver.org/ns/",
    "a": "http://apple.com/ns/ical/",
}

KNOWN_SERVERS = {
    "icloud.com": "https://caldav.icloud.com/",
    "me.com": "https://caldav.icloud.com/",
    "mac.com": "https://caldav.icloud.com/",
    "fastmail.com": "https://caldav.fastmail.com/",
    "fastmail.fm": "https://caldav.fastmail.com/",
    "yahoo.com": "https://caldav.calendar.yahoo.com/",
    "zoho.com": "https://calendar.zoho.com/",
    "zohomail.eu": "https://calendar.zoho.eu/",
    "zoho.eu": "https://calendar.zoho.eu/",
    "gmx.net": "https://caldav.gmx.net/",
    "gmx.de": "https://caldav.gmx.net/",
    "gmx.com": "https://caldav.gmx.com/",
    "web.de": "https://caldav.web.de/",
    "mailbox.org": "https://dav.mailbox.org/",
    "posteo.de": "https://posteo.de:8443/",
}

PROPFIND_PRINCIPAL = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>"""

PROPFIND_HOME = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<d:prop><c:calendar-home-set/></d:prop></d:propfind>"""

PROPFIND_CALENDARS = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"
 xmlns:cs="http://calendarserver.org/ns/" xmlns:a="http://apple.com/ns/ical/">
<d:prop>
 <d:resourcetype/><d:displayname/><a:calendar-color/><cs:getctag/>
 <c:supported-calendar-component-set/><d:current-user-privilege-set/>
</d:prop></d:propfind>"""

PROPFIND_CTAG = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/">
<d:prop><cs:getctag/><d:sync-token/></d:prop></d:propfind>"""

REPORT_RANGE = """<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<d:prop><d:getetag/><c:calendar-data/></d:prop>
<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">
<c:time-range start="{start}" end="{end}"/>
</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>"""


def basic_auth(username, password):
    token = base64.b64encode((username + ":" + password).encode("utf-8")).decode("ascii")
    return "Basic " + token


def parse_xml(body):
    parser = expat.ParserCreate()

    def _forbid(*_args, **_kwargs):
        raise net.NetError("unsafe_xml")

    parser.StartDoctypeDeclHandler = _forbid
    parser.EntityDeclHandler = _forbid
    parser.UnparsedEntityDeclHandler = _forbid
    parser.ExternalEntityRefHandler = _forbid
    try:
        parser.Parse(body, True)
        return ET.fromstring(body)
    except net.NetError:
        raise
    except (expat.ExpatError, ET.ParseError):
        raise net.NetError("bad_xml")


def _tag(prefix, name):
    return "{" + NS[prefix] + "}" + name


def _responses(root):
    for resp in root.findall("d:response", NS):
        href = resp.findtext("d:href", default="", namespaces=NS).strip()
        props = {}
        for propstat in resp.findall("d:propstat", NS):
            status = propstat.findtext("d:status", default="", namespaces=NS)
            if " 200" not in status:
                continue
            prop = propstat.find("d:prop", NS)
            if prop is None:
                continue
            for child in prop:
                props[child.tag] = child
        yield href, props


class Client:
    def __init__(self, base_url, username, password):
        self.base_url = base_url
        self.auth = basic_auth(username, password)
        self.scope = net.auth_scope(base_url)

    def call(self, method, url, body=None, depth=None, headers=None, ok=(200, 201, 204, 207),
             timeout=12):
        h = {}
        if body is not None:
            h["Content-Type"] = "application/xml; charset=utf-8"
        if depth is not None:
            h["Depth"] = str(depth)
        if headers:
            h.update(headers)
        resp = net.request(method, url, headers=h, body=body, auth=self.auth,
                           scope=self.scope, timeout=timeout)
        net.raise_for_status(resp, ok)
        return resp

    def _scoped_url(self, base, href):
        target = urllib.parse.urljoin(base, href)
        parts = urllib.parse.urlsplit(target)
        if parts.scheme != "https" or not net.host_in_scope(parts.hostname or "", self.scope):
            return ""
        return target

    def _href_prop(self, url, body, prop_tag):
        resp = self.call("PROPFIND", url, body=body, depth=0)
        root = parse_xml(resp.body)
        for _, props in _responses(root):
            el = props.get(prop_tag)
            if el is not None:
                href = el.findtext("d:href", default="", namespaces=NS).strip()
                if href:
                    target = self._scoped_url(resp.url, href)
                    if target:
                        return target
        return ""

    def principal(self):
        candidates = [self.base_url]
        parts = urllib.parse.urlsplit(self.base_url)
        well_known = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/.well-known/caldav", "", ""))
        if well_known not in candidates:
            candidates.append(well_known)
        last_error = None
        for url in candidates:
            try:
                found = self._href_prop(url, PROPFIND_PRINCIPAL, _tag("d", "current-user-principal"))
                if found:
                    return found
            except net.NetError as exc:
                if exc.code in ("auth_failed", "forbidden"):
                    raise
                last_error = exc
        if last_error:
            raise last_error
        raise net.NetError("no_principal")

    def calendar_home(self, principal):
        home = self._href_prop(principal, PROPFIND_HOME, _tag("c", "calendar-home-set"))
        if not home:
            raise net.NetError("no_calendar_home")
        return home

    def calendars(self, home):
        resp = self.call("PROPFIND", home, body=PROPFIND_CALENDARS, depth=1)
        root = parse_xml(resp.body)
        out = []
        for href, props in _responses(root):
            rtype = props.get(_tag("d", "resourcetype"))
            if rtype is None or rtype.find("c:calendar", NS) is None:
                continue
            comps = props.get(_tag("c", "supported-calendar-component-set"))
            if comps is not None:
                names = {c.get("name", "").upper() for c in comps.findall("c:comp", NS)}
                if names and "VEVENT" not in names:
                    continue
            url = self._scoped_url(resp.url, href)
            if not url:
                continue
            name = (props.get(_tag("d", "displayname")).text or "").strip() \
                if props.get(_tag("d", "displayname")) is not None else ""
            color = ""
            c = props.get(_tag("a", "calendar-color"))
            if c is not None and c.text:
                color = normalize_color(c.text)
            ctag_el = props.get(_tag("cs", "getctag"))
            privs = props.get(_tag("d", "current-user-privilege-set"))
            read_only = False
            if privs is not None:
                granted = {p[0].tag for p in privs.findall("d:privilege", NS) if len(p)}
                writable = {_tag("d", "write"), _tag("d", "write-content"), _tag("d", "all"),
                            _tag("d", "bind")}
                read_only = not (granted & writable)
            out.append({
                "id": url,
                "name": (name or url.rstrip("/").rsplit("/", 1)[-1])[:120],
                "color": color,
                "readOnly": read_only,
                "ctag": (ctag_el.text or "").strip() if ctag_el is not None else "",
            })
        return out

    def ctag(self, calendar_url):
        resp = self.call("PROPFIND", calendar_url, body=PROPFIND_CTAG, depth=0)
        root = parse_xml(resp.body)
        for _, props in _responses(root):
            for tag in (_tag("cs", "getctag"), _tag("d", "sync-token")):
                el = props.get(tag)
                if el is not None and el.text:
                    return el.text.strip()
        return ""

    def fetch_range(self, calendar_url, start_utc, end_utc):
        body = REPORT_RANGE.format(start=start_utc.strftime("%Y%m%dT%H%M%SZ"),
                                   end=end_utc.strftime("%Y%m%dT%H%M%SZ"))
        resp = self.call("REPORT", calendar_url, body=body, depth=1, timeout=20)
        root = parse_xml(resp.body)
        items = []
        for href, props in _responses(root):
            data = props.get(_tag("c", "calendar-data"))
            if data is None or not data.text:
                continue
            etag_el = props.get(_tag("d", "getetag"))
            item_href = self._scoped_url(resp.url, href)
            if not item_href:
                continue
            items.append({
                "href": item_href,
                "etag": (etag_el.text or "").strip() if etag_el is not None else "",
                "data": data.text,
            })
        return items

    def put_new(self, calendar_url, uid, ics_text):
        name = re.sub(r"[^A-Za-z0-9._-]", "_", uid)[:200] + ".ics"
        url = calendar_url.rstrip("/") + "/" + urllib.parse.quote(name)
        resp = self.call("PUT", url, body=ics_text.encode("utf-8"),
                         headers={"Content-Type": "text/calendar; charset=utf-8",
                                  "If-None-Match": "*"},
                         ok=(200, 201, 204), timeout=15)
        return url, resp.header("etag")

    def get(self, href):
        resp = self.call("GET", href, ok=(200,), timeout=15)
        return resp.header("etag"), resp.text()

    def put_existing(self, href, ics_text, etag):
        h = {"Content-Type": "text/calendar; charset=utf-8"}
        if etag:
            h["If-Match"] = etag
        resp = self.call("PUT", href, body=ics_text.encode("utf-8"), headers=h,
                         ok=(200, 201, 204), timeout=15)
        return resp.header("etag")

    def delete(self, href, etag):
        h = {"If-Match": etag} if etag else {}
        self.call("DELETE", href, headers=h, ok=(200, 204, 404), timeout=15)


def normalize_color(value):
    value = value.strip()
    if re.match(r"^#[0-9A-Fa-f]{8}$", value):
        return value[:7].lower()
    if re.match(r"^#[0-9A-Fa-f]{6}$", value):
        return value.lower()
    return ""


def guess_server(username):
    """A starting URL from an e-mail address, or ""."""
    if "@" not in username:
        return ""
    domain = username.rsplit("@", 1)[1].strip().lower()
    if not re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", domain):
        return ""
    return KNOWN_SERVERS.get(domain, "https://" + domain + "/.well-known/caldav")


def normalize_server(server):
    server = (server or "").strip()
    if not server:
        return ""
    if "://" not in server:
        server = "https://" + server
    parts = urllib.parse.urlsplit(server)
    if parts.scheme != "https" or not parts.hostname:
        raise net.NetError("insecure_url", server)
    path = parts.path or "/"
    return urllib.parse.urlunsplit(("https", parts.netloc, path, "", ""))


def discover(server, username, password):
    """Find the calendars of an account. Returns (base_url, calendars)."""
    base = normalize_server(server) or guess_server(username)
    if not base:
        raise net.NetError("no_server")
    client = Client(base, username, password)
    principal = client.principal()
    home = client.calendar_home(principal)
    cals = client.calendars(home)
    if not cals:
        raise net.NetError("no_calendars")
    return base, cals
