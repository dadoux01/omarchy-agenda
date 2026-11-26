"""A small HTTPS client with the guarantees the plugin needs.

urllib follows redirects on its own and carries the Authorization header to
whatever host the redirect names. That is the wrong default for a client
holding calendar passwords, so redirects are followed here by hand:

* only https:// is ever contacted;
* credentials are attached only to hosts inside the account's scope (the
  configured server's own domain, e.g. *.icloud.com for caldav.icloud.com);
* responses are capped in size and every request has a timeout.
"""

import http.client
import ipaddress
import socket
import ssl
import urllib.parse

from . import USER_AGENT

MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 5
REDIRECT_CODES = (301, 302, 303, 307, 308)

# Multi-host provider domains whose per-user regional endpoints live under
# subdomains (e.g. caldav.icloud.com -> p42-caldav.icloud.com).
MULTI_HOST_SCOPES = (
    "icloud.com",
    "googleapis.com",
    "googleusercontent.com",
    "yahoo.com",
    "fastmail.com",
    "zoho.com",
    "zoho.eu",
    "mailbox.org",
    "gmx.net",
    "gmx.com",
    "web.de",
)


class NetError(Exception):
    """A failure with a stable machine-readable code for the UI."""

    def __init__(self, code, detail="", status=0):
        super().__init__(code if not detail else code + ": " + detail)
        self.code = code
        self.detail = detail
        self.status = status


class Response:
    def __init__(self, status, headers, body, url):
        self.status = status
        self.headers = headers
        self.body = body
        self.url = url

    def header(self, name, default=""):
        return self.headers.get(name.lower(), default)

    def text(self):
        return self.body.decode("utf-8", errors="replace")


def auth_scope(url):
    """The domain credentials for `url` may be sent to.

    Known multi-host providers (e.g. caldav.icloud.com -> icloud.com) allow
    sibling endpoints under the provider domain; all other hosts stay exact
    so sibling tenants on shared hosting or public-suffix domains cannot steal
    credentials on redirect.
    """
    host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    for suffix in MULTI_HOST_SCOPES:
        if host == suffix or host.endswith("." + suffix):
            return suffix
    return host


def _host_detail(url):
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def _is_internal_ip(ip):
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def _is_internal_host(host, resolve=False):
    """True for a host on the local machine or network.

    With `resolve`, a name is looked up too, so a public name pointing at a
    private address counts. That is best effort: the lookup is not pinned
    for the connection that follows.
    """
    h = (host or "").strip("[]").lower().rstrip(".")
    if not h or h == "localhost" or h.endswith(".localhost") or h.endswith(".local"):
        return True
    try:
        return _is_internal_ip(ipaddress.ip_address(h))
    except ValueError:
        pass
    if not resolve:
        return False
    try:
        infos = socket.getaddrinfo(h, 443, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return False  # the connection will fail on its own
    for info in infos:
        try:
            if _is_internal_ip(ipaddress.ip_address(info[4][0].split("%", 1)[0])):
                return True
        except ValueError:
            continue
    return False


def _clean_headers(headers):
    out = {}
    for k, v in (headers or {}).items():
        ks = str(k).replace("\r", "").replace("\n", "").replace("\x00", "")
        vs = str(v).replace("\r", "").replace("\n", "").replace("\x00", "")
        if ks:
            out[ks] = vs
    return out


def host_in_scope(host, scope):
    host = (host or "").lower().rstrip(".")
    scope = (scope or "").lower().rstrip(".")
    if not host or not scope:
        return False
    return host == scope or host.endswith("." + scope)


def _ssl_context():
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def request(method, url, headers=None, body=None, auth=None, scope=None,
            timeout=12, max_bytes=MAX_RESPONSE_BYTES):
    """Send one request, following redirects under the rules above.

    `auth` is a full Authorization header value. `scope` defaults to the
    scope of the first URL.
    """
    if isinstance(body, str):
        body = body.encode("utf-8")
    if scope is None:
        scope = auth_scope(url)
    ctx = _ssl_context()

    initial_host = _host_detail(url)
    # A server on the local network may redirect within it; a public one may
    # not send the request (and its credentials) there.
    initial_internal = _is_internal_host(initial_host)
    for hop in range(MAX_REDIRECTS + 1):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise NetError("insecure_url", _host_detail(url))
        host = parts.hostname
        if (hop > 0 and not initial_internal and host.lower() != initial_host
                and _is_internal_host(host, resolve=True)):
            raise NetError("insecure_url", host)
        port = parts.port or 443
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query

        send_headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
        if headers:
            send_headers.update(_clean_headers(headers))
        if auth and host_in_scope(host, scope):
            send_headers["Authorization"] = _clean_headers({"Authorization": auth})["Authorization"]

        conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
        try:
            conn.request(method, path, body=body, headers=send_headers)
            resp = conn.getresponse()
            status = resp.status
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            if status in REDIRECT_CODES and resp_headers.get("location"):
                resp.read(64 * 1024)
                loc = resp_headers["location"].replace("\r", "").replace("\n", "").strip()
                url = urllib.parse.urljoin(url, loc)
                if status == 303:
                    method, body = "GET", None
                continue
            data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise NetError("response_too_large", host)
            return Response(status, resp_headers, data, url)
        except NetError:
            raise
        except ssl.SSLError as exc:
            raise NetError("tls_error", str(exc))
        except (socket.timeout, TimeoutError):
            raise NetError("timeout", host)
        except (OSError, http.client.HTTPException) as exc:
            raise NetError("network", str(exc))
        finally:
            conn.close()
    raise NetError("too_many_redirects", _host_detail(url))


def raise_for_status(resp, ok=(200, 201, 204, 207)):
    if resp.status in ok:
        return
    if resp.status == 401:
        raise NetError("auth_failed", status=401)
    if resp.status == 403:
        raise NetError("forbidden", status=403)
    if resp.status == 404:
        raise NetError("not_found", _host_detail(resp.url), status=404)
    if resp.status == 412:
        raise NetError("conflict", status=412)
    if resp.status == 429:
        raise NetError("rate_limited", status=429)
    if resp.status >= 500:
        raise NetError("server_error", str(resp.status), status=resp.status)
    raise NetError("http_error", str(resp.status), status=resp.status)
