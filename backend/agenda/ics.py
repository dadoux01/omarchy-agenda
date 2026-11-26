"""iCalendar (RFC 5545) reading, recurrence expansion, and writing.

Input comes from the network and is treated as hostile: sizes, line counts,
recurrence work and instance counts are all capped, and nothing in it is
ever evaluated.
"""

import calendar as _cal
import datetime as dt
import os
import re
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_ICS_BYTES = 10 * 1024 * 1024
MAX_LINES = 400_000
MAX_INSTANCES_PER_EVENT = 500
# RDATE and EXDATE values read per event, all their properties together.
MAX_DATE_VALUES_PER_EVENT = 5_000
MAX_PERIODS = 100_000
MAX_TEXT = {"SUMMARY": 300, "LOCATION": 300, "DESCRIPTION": 2000}

UTC = dt.timezone.utc
WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]

# Exchange and Outlook name zones the Windows way.
WINDOWS_ZONES = {
    "Romance Standard Time": "Europe/Paris",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw",
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "E. Europe Standard Time": "Europe/Chisinau",
    "FLE Standard Time": "Europe/Kiev",
    "GTB Standard Time": "Europe/Bucharest",
    "Russian Standard Time": "Europe/Moscow",
    "Turkey Standard Time": "Europe/Istanbul",
    "Arabian Standard Time": "Asia/Dubai",
    "India Standard Time": "Asia/Kolkata",
    "China Standard Time": "Asia/Shanghai",
    "Singapore Standard Time": "Asia/Singapore",
    "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "New Zealand Standard Time": "Pacific/Auckland",
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "Pacific Standard Time": "America/Los_Angeles",
    "Atlantic Standard Time": "America/Halifax",
    "SA Pacific Standard Time": "America/Bogota",
    "E. South America Standard Time": "America/Sao_Paulo",
    "Morocco Standard Time": "Africa/Casablanca",
    "South Africa Standard Time": "Africa/Johannesburg",
    "UTC": "UTC",
}


class IcsError(Exception):
    pass


# --------------------------------------------------------------------------
# Local zone


def local_zone():
    tz = os.environ.get("TZ", "").lstrip(":")
    if tz:
        try:
            return ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    try:
        target = os.path.realpath("/etc/localtime")
        marker = "/zoneinfo/"
        if marker in target:
            return ZoneInfo(target.split(marker, 1)[1])
    except (ZoneInfoNotFoundError, ValueError, OSError):
        pass
    return dt.datetime.now().astimezone().tzinfo


# --------------------------------------------------------------------------
# Parsing


class Prop:
    __slots__ = ("name", "params", "value", "raw")

    def __init__(self, name, params, value, raw):
        self.name = name
        self.params = params
        self.value = value
        self.raw = raw


class Component:
    def __init__(self, name):
        self.name = name
        self.props = []
        self.children = []

    def get(self, name):
        for p in self.props:
            if p.name == name:
                return p
        return None

    def get_all(self, name):
        return [p for p in self.props if p.name == name]

    def value(self, name, default=""):
        p = self.get(name)
        return p.value if p else default


def unfold(text):
    lines = []
    for line in re.split(r"\r\n|\n|\r", text):
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def parse_line(line):
    """Split `NAME;P=V;Q="a:b":value` without being fooled by quoted colons."""
    i, n = 0, len(line)
    in_quotes = False
    while i < n:
        ch = line[i]
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ":" and not in_quotes:
            break
        i += 1
    if i >= n:
        return None
    head, value = line[:i], line[i + 1:]
    parts = []
    buf, in_quotes = [], False
    for ch in head:
        if ch == '"':
            in_quotes = not in_quotes
            buf.append(ch)
        elif ch == ";" and not in_quotes:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    name = parts[0].strip().upper()
    if not name:
        return None
    params = {}
    for part in parts[1:]:
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
            v = v[1:-1]
        params[k.strip().upper()] = v
    return name, params, value


def parse(text):
    if isinstance(text, bytes):
        if len(text) > MAX_ICS_BYTES:
            raise IcsError("too_large")
        text = text.decode("utf-8", errors="replace")
    elif len(text) > MAX_ICS_BYTES:
        raise IcsError("too_large")
    lines = unfold(text)
    if len(lines) > MAX_LINES:
        raise IcsError("too_many_lines")
    root = Component("ROOT")
    stack = [root]
    for raw in lines:
        if not raw.strip():
            continue
        parsed = parse_line(raw)
        if not parsed:
            continue
        name, params, value = parsed
        if name == "BEGIN":
            comp = Component(value.strip().upper())
            stack[-1].children.append(comp)
            if len(stack) > 16:
                raise IcsError("too_deep")
            stack.append(comp)
        elif name == "END":
            if len(stack) > 1:
                stack.pop()
        else:
            stack[-1].props.append(Prop(name, params, value, raw))
    return root


def unescape_text(value, limit=None):
    out, i, n = [], 0, len(value)
    while i < n:
        ch = value[i]
        if ch == "\\" and i + 1 < n:
            nxt = value[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    text = "".join(out)
    text = "".join(c for c in text if c in "\n\t" or ord(c) >= 32)
    if limit and len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text.strip()


def escape_text(value):
    return (value.replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n"))


# --------------------------------------------------------------------------
# Dates


def resolve_zone(tzid, vtimezones=None):
    if not tzid:
        return None
    name = tzid.strip()
    candidates = [name]
    if name.startswith("/"):
        candidates.append("/".join(name.split("/")[-2:]))
    if name in WINDOWS_ZONES:
        candidates.insert(0, WINDOWS_ZONES[name])
    if vtimezones and name in vtimezones:
        candidates.append(vtimezones[name])
    for cand in candidates:
        try:
            return ZoneInfo(cand)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return local_zone()


_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_DATETIME_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)$")


def parse_date_value(value, params=None, vtimezones=None):
    """Return a date (all-day) or an aware datetime."""
    value = value.strip()
    params = params or {}
    m = _DATE_RE.match(value)
    if m and params.get("VALUE", "DATE").strip().upper() in ("DATE", ""):
        try:
            return dt.date(int(m[1]), int(m[2]), int(m[3]))
        except (ValueError, OverflowError):
            raise IcsError("bad_date")
    m = _DATETIME_RE.match(value)
    if not m:
        raise IcsError("bad_date")
    try:
        naive = dt.datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]),
                            min(int(m[6]), 59))
    except (ValueError, OverflowError):
        raise IcsError("bad_date")
    if m[7] == "Z":
        return naive.replace(tzinfo=UTC)
    zone = resolve_zone(params.get("TZID"), vtimezones) if params.get("TZID") else local_zone()
    return naive.replace(tzinfo=zone)


def iter_date_list(props, vtimezones=None, budget=None):
    """Dates of comma-separated RDATE / EXDATE properties, read lazily.

    `budget` is a one-item list holding how many values may still be read;
    it is spent in place, so several calls can share it. One huge list then
    costs bounded work and is never split into a list of its own.
    """
    if budget is None:
        budget = [MAX_DATE_VALUES_PER_EVENT]
    for prop in props:
        raw = prop.value
        pos = 0
        while budget[0] > 0:
            budget[0] -= 1
            comma = raw.find(",", pos)
            part = (raw[pos:] if comma == -1 else raw[pos:comma]).strip()
            if part and "/" not in part:  # PERIOD values are not supported
                try:
                    yield parse_date_value(part, prop.params, vtimezones)
                except IcsError:
                    pass
            if comma == -1:
                break
            pos = comma + 1


_DUR_RE = re.compile(
    r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def parse_duration(value):
    m = _DUR_RE.match(value.strip().upper())
    if not m or value.strip().upper() in ("P", "-P", "+P", "PT"):
        raise IcsError("bad_duration")
    sign = -1 if m[1] == "-" else 1
    weeks, days, hours, minutes, seconds = (int(x) if x else 0 for x in m.groups()[1:])
    try:
        return sign * dt.timedelta(weeks=weeks, days=days, hours=hours,
                                   minutes=minutes, seconds=seconds)
    except OverflowError:
        raise IcsError("bad_duration")


def occurrence_key(value):
    """Comparable identity for EXDATE / RECURRENCE-ID matching."""
    if isinstance(value, dt.datetime):
        return ("t", int(value.timestamp()))
    return ("d", value.isoformat())


def to_epoch_ms(value, zone=None):
    if isinstance(value, dt.datetime):
        return int(value.timestamp() * 1000)
    zone = zone or local_zone()
    return int(dt.datetime(value.year, value.month, value.day, tzinfo=zone).timestamp() * 1000)


# --------------------------------------------------------------------------
# Recurrence


def parse_rrule(value):
    rule = {}
    for part in value.split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        rule[k.strip().upper()] = v.strip().upper()
    return rule


def _byday(value):
    out = []
    for item in value.split(","):
        m = re.match(r"^([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)$", item.strip())
        if m:
            out.append((int(m[1]) if m[1] else 0, WEEKDAYS.index(m[2])))
    return out


def _ints(value, lo, hi):
    out = []
    for item in value.split(","):
        try:
            n = int(item)
        except ValueError:
            continue
        if lo <= abs(n) <= hi and n != 0:
            out.append(n)
    return out


def _add_months(year, month, delta):
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _weekdays_in_month(year, month, weekday):
    days = _cal.monthrange(year, month)[1]
    return [dt.date(year, month, d) for d in range(1, days + 1)
            if dt.date(year, month, d).weekday() == weekday]


def _month_candidates(year, month, rule, start_day):
    days_in_month = _cal.monthrange(year, month)[1]
    bymonthday = _ints(rule.get("BYMONTHDAY", ""), 1, 31)
    byday = _byday(rule.get("BYDAY", ""))
    by_md = set()
    for d in bymonthday:
        day = d if d > 0 else days_in_month + d + 1
        if 1 <= day <= days_in_month:
            by_md.add(dt.date(year, month, day))
    by_wd = set()
    for ordinal, weekday in byday:
        matches = _weekdays_in_month(year, month, weekday)
        if ordinal == 0:
            by_wd.update(matches)
        elif -len(matches) <= ordinal <= len(matches) and ordinal != 0:
            by_wd.add(matches[ordinal - 1] if ordinal > 0 else matches[ordinal])
    if bymonthday and byday:
        return sorted(by_md & by_wd)
    if bymonthday:
        return sorted(by_md)
    if byday:
        return sorted(by_wd)
    if start_day <= days_in_month:
        return [dt.date(year, month, start_day)]
    return []


def _year_weekday_candidates(year, byday):
    days = []
    d = dt.date(year, 1, 1)
    while d.year == year:
        days.append(d)
        d += dt.timedelta(days=1)
    out = set()
    for ordinal, weekday in byday:
        matches = [x for x in days if x.weekday() == weekday]
        if ordinal == 0:
            out.update(matches)
        elif -len(matches) <= ordinal <= len(matches):
            out.add(matches[ordinal - 1] if ordinal > 0 else matches[ordinal])
    return sorted(out)


def _setpos(cands, rule):
    positions = _ints(rule.get("BYSETPOS", ""), 1, 366)
    if not positions:
        return cands
    out = []
    for p in positions:
        if -len(cands) <= p <= len(cands):
            out.append(cands[p - 1] if p > 0 else cands[p])
    return sorted(set(out))


def expand_rrule(rule_value, dtstart, range_start, range_end,
                 max_instances=MAX_INSTANCES_PER_EVENT, zone=None):
    """Start values (dates or aware datetimes) of the occurrences of a rule
    that begin inside [range_start, range_end), dtstart included when it
    matches. Wall-clock time is kept across DST, as RFC 5545 requires.

    `range_start`/`range_end` are aware datetimes.
    """
    rule = parse_rrule(rule_value)
    freq = rule.get("FREQ")
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        return [dtstart] if _in_range(dtstart, range_start, range_end, zone) else []
    try:
        interval = min(10_000, max(1, int(rule.get("INTERVAL", "1"))))
    except ValueError:
        interval = 1
    try:
        count = int(rule["COUNT"]) if "COUNT" in rule else None
    except ValueError:
        count = None
    if count is not None and count <= 0:
        return []
    until = None
    if "UNTIL" in rule:
        try:
            until = parse_date_value(rule["UNTIL"], {} if len(rule["UNTIL"]) > 8 else {"VALUE": "DATE"})
        except IcsError:
            until = None

    all_day = not isinstance(dtstart, dt.datetime)
    dt_zone = None if all_day else dtstart.tzinfo
    cmp_zone = zone or dt_zone or range_start.tzinfo
    start_date = dtstart if all_day else dtstart.date()
    start_time = None if all_day else dtstart.timetz().replace(tzinfo=None)
    bymonth = set(_ints(rule.get("BYMONTH", ""), 1, 12))
    byday = _byday(rule.get("BYDAY", ""))
    wkst = WEEKDAYS.index(rule["WKST"]) if rule.get("WKST") in WEEKDAYS else 0

    def make(d):
        if all_day:
            return d
        return dt.datetime.combine(d, start_time).replace(tzinfo=dt_zone)

    def past_until(value):
        if until is None:
            return False
        if isinstance(until, dt.datetime):
            if isinstance(value, dt.datetime):
                return value > until
            return dt.datetime(value.year, value.month, value.day, tzinfo=dt_zone or UTC) > until
        v = value.date() if isinstance(value, dt.datetime) else value
        return v > until

    # Skip whole periods ahead when nothing has to be counted.
    skip = 0
    if count is None:
        target = range_start.astimezone(dt_zone).date() if dt_zone else range_start.date()
        target -= dt.timedelta(days=1)
        if target > start_date:
            if freq == "DAILY":
                skip = (target - start_date).days // interval
            elif freq == "WEEKLY":
                skip = (target - start_date).days // 7 // interval
            elif freq == "MONTHLY":
                months = (target.year - start_date.year) * 12 + target.month - start_date.month
                skip = max(0, months - 1) // interval
            elif freq == "YEARLY":
                skip = max(0, target.year - start_date.year - 1) // interval
            skip = max(0, skip - 1)

    out = []
    emitted = 0
    week_anchor = start_date - dt.timedelta(days=(start_date.weekday() - wkst) % 7)
    for period in range(skip, skip + MAX_PERIODS):
        step = period * interval
        try:
            if freq == "DAILY":
                d = start_date + dt.timedelta(days=step)
                cands = [d]
                if bymonth and d.month not in bymonth:
                    cands = []
                if byday and d.weekday() not in [w for _, w in byday]:
                    cands = []
                md = _ints(rule.get("BYMONTHDAY", ""), 1, 31)
                if md:
                    last = _cal.monthrange(d.year, d.month)[1]
                    if not any((m if m > 0 else last + m + 1) == d.day for m in md):
                        cands = []
                period_start = d
            elif freq == "WEEKLY":
                ws = week_anchor + dt.timedelta(days=7 * step)
                if byday:
                    offsets = sorted({(w - wkst) % 7 for _, w in byday})
                    cands = [ws + dt.timedelta(days=off) for off in offsets]
                else:
                    cands = [ws + dt.timedelta(days=(start_date.weekday() - wkst) % 7)]
                if bymonth:
                    cands = [c for c in cands if c.month in bymonth]
                cands = _setpos(cands, rule)
                period_start = ws
            elif freq == "MONTHLY":
                y, m = _add_months(start_date.year, start_date.month, step)
                cands = [] if (bymonth and m not in bymonth) else _month_candidates(y, m, rule, start_date.day)
                cands = _setpos(cands, rule)
                period_start = dt.date(y, m, 1)
            else:  # YEARLY
                y = start_date.year + step
                if y > 9000:
                    break
                months = sorted(bymonth) if bymonth else [start_date.month]
                if byday and not bymonth and "BYMONTHDAY" not in rule:
                    cands = _year_weekday_candidates(y, byday)
                else:
                    cands = []
                    for m in months:
                        if "BYMONTHDAY" in rule or byday:
                            cands.extend(_month_candidates(y, m, rule, start_date.day))
                        elif start_date.day <= _cal.monthrange(y, m)[1]:
                            cands.append(dt.date(y, m, start_date.day))
                cands = _setpos(sorted(cands), rule)
                period_start = dt.date(y, 1, 1)
        except (ValueError, OverflowError):
            break

        if period_start.year > 9000:
            break
        for c in cands:
            if c < start_date:
                continue
            try:
                value = make(c)
            except (ValueError, OverflowError):
                continue
            if past_until(value):
                return out
            emitted += 1
            if count is not None and emitted > count:
                return out
            if _starts_after(value, range_end, cmp_zone):
                return out
            if _in_range(value, range_start, range_end, cmp_zone):
                out.append(value)
                if len(out) >= max_instances:
                    return out
    return out


def _as_dt(value, zone=None):
    if isinstance(value, dt.datetime):
        return value
    return dt.datetime(value.year, value.month, value.day, tzinfo=zone or local_zone())


def _in_range(value, range_start, range_end, zone=None):
    v = _as_dt(value, zone)
    return range_start <= v < range_end


def _starts_after(value, range_end, zone=None):
    return _as_dt(value, zone) >= range_end


# --------------------------------------------------------------------------
# Events


def _vtimezone_aliases(root):
    out = {}
    for cal in root.children:
        for comp in cal.children:
            if comp.name == "VTIMEZONE":
                tzid = comp.value("TZID")
                loc = comp.value("X-LIC-LOCATION")
                if tzid and loc:
                    out[tzid] = loc
    return out


def _alarms(comp, vtz):
    """Reminders as seconds before the start, or absolute epoch ms."""
    before, absolute = [], []
    for alarm in comp.children:
        if alarm.name != "VALARM":
            continue
        action = alarm.value("ACTION", "DISPLAY").strip().upper()
        if action not in ("DISPLAY", "AUDIO"):
            continue
        trig = alarm.get("TRIGGER")
        if not trig:
            continue
        try:
            if trig.params.get("VALUE", "").upper() == "DATE-TIME":
                absolute.append(to_epoch_ms(parse_date_value(trig.value, {}, vtz)))
                continue
            offset = parse_duration(trig.value)
        except IcsError:
            continue
        if trig.params.get("RELATED", "START").upper() == "END":
            # Rare; treated as relative to the start, which is what every
            # client shows for the reminders people actually set.
            pass
        seconds = int(-offset.total_seconds())
        if -86400 * 7 <= seconds <= 86400 * 60:
            before.append(seconds)
    return sorted(set(before)), sorted(set(absolute))


def _end_for(comp, start, vtz):
    p = comp.get("DTEND")
    if p:
        try:
            end = parse_date_value(p.value, p.params, vtz)
            if type(end) is type(start) and end > start:
                return end
        except IcsError:
            pass
    p = comp.get("DURATION")
    if p:
        try:
            dur = parse_duration(p.value)
            if dur.total_seconds() > 0:
                return start + dur
        except (IcsError, OverflowError):
            pass
    if isinstance(start, dt.datetime):
        return start
    try:
        return start + dt.timedelta(days=1)
    except OverflowError:
        return start


MEETING_HOSTS = (
    "meet.google.com", "zoom.us", "zoomgov.com", "teams.microsoft.com",
    "teams.live.com", "webex.com", "meet.jit.si", "whereby.com",
    "chime.aws", "facetime.apple.com", "gotomeeting.com", "around.co",
)
_URL_RE = re.compile(r"https://[A-Za-z0-9.-]+(?::\d+)?(?:/[A-Za-z0-9._~%!$&'()*+,;=:@/?#-]*)?")


def safe_meeting_url(text):
    for url in _URL_RE.findall(text or ""):
        url = url.rstrip(").,;'\"")
        host = url[8:].split("/", 1)[0].split(":", 1)[0].lower()
        if any(host == h or host.endswith("." + h) for h in MEETING_HOSTS) and len(url) <= 2000:
            return url
    return ""


def _occurrence_end(occ_start, master_start, master_duration):
    if type(occ_start) is type(master_start):
        dur = master_duration
    elif not isinstance(occ_start, dt.datetime):
        dur = dt.timedelta(days=1)
    else:
        dur = dt.timedelta(hours=1)
    try:
        return occ_start + dur
    except OverflowError:
        return occ_start


def events_from_calendar(text, range_start, range_end, zone=None):
    """Flatten one iCalendar object into occurrence dicts inside the range.

    Each dict: uid, title, location, description, start, end (date or aware
    datetime), all_day, recurring, recurrence_id (raw value or ""),
    alarms_before (seconds), alarms_at (epoch ms), meeting_url, status.
    """
    zone = zone or local_zone()
    root = parse(text)
    vtz = _vtimezone_aliases(root)
    masters, overrides = {}, {}
    for cal in root.children:
        if cal.name != "VCALENDAR":
            continue
        for comp in cal.children:
            if comp.name != "VEVENT":
                continue
            uid = comp.value("UID").strip()[:500] or ("nouid-" + str(id(comp)))
            if comp.get("RECURRENCE-ID"):
                overrides.setdefault(uid, []).append(comp)
            else:
                masters[uid] = comp

    out = []
    for uid in set(masters) | set(overrides):
        master = masters.get(uid)
        uid_overrides = {}
        for ov in overrides.get(uid, []):
            rid = ov.get("RECURRENCE-ID")
            try:
                uid_overrides[occurrence_key(parse_date_value(rid.value, rid.params, vtz))] = ov
            except IcsError:
                continue

        emitted_for_uid = 0
        if master is not None:
            try:
                p = master.get("DTSTART")
                start = parse_date_value(p.value, p.params, vtz)
            except (IcsError, AttributeError):
                start = None
            if start is not None:
                end = _end_for(master, start, vtz)
                duration = end - start
                rrule = master.get("RRULE")
                recurring = rrule is not None or bool(master.get_all("RDATE"))
                if recurring:
                    # Look back by the event length so one already running counts.
                    lookback = duration if isinstance(duration, dt.timedelta) else dt.timedelta(0)
                    win_start = range_start - lookback
                    # One budget for the whole event: RDATE and EXDATE share the
                    # values read, DTSTART, RRULE and RDATE share the
                    # occurrences kept. Only exclusions near the window can
                    # match an occurrence.
                    values = [MAX_DATE_VALUES_PER_EVENT]
                    excluded = set()
                    margin = dt.timedelta(days=1)
                    for ex in iter_date_list(master.get_all("EXDATE"), vtz, values):
                        if _in_range(ex, win_start - margin, range_end + margin, zone):
                            excluded.add(occurrence_key(ex))
                    starts = {}

                    def keep(value):
                        key = occurrence_key(value)
                        if key not in excluded and key not in uid_overrides:
                            starts.setdefault(key, value)

                    if _in_range(start, win_start, range_end, zone):
                        keep(start)
                    if rrule:
                        r_limit = MAX_INSTANCES_PER_EVENT + len(excluded) + len(uid_overrides)
                        for rs in expand_rrule(rrule.value, start, win_start, range_end,
                                               max_instances=r_limit, zone=zone):
                            keep(rs)
                            if len(starts) >= MAX_INSTANCES_PER_EVENT:
                                break
                    if len(starts) < MAX_INSTANCES_PER_EVENT:
                        for rd in iter_date_list(master.get_all("RDATE"), vtz, values):
                            if _in_range(rd, win_start, range_end, zone):
                                keep(rd)
                                if len(starts) >= MAX_INSTANCES_PER_EVENT:
                                    break
                    for s in sorted(starts.values(), key=lambda v: _as_dt(v, zone)):
                        rid_raw = _format_like(master.get("DTSTART"), s)
                        occ = _occurrence(master, uid, s, _occurrence_end(s, start, duration),
                                          True, rid_raw, vtz, zone)
                        if occ and _overlaps(occ, range_start, range_end, zone):
                            out.append(occ)
                            emitted_for_uid += 1
                else:
                    occ = _occurrence(master, uid, start, end, False, "", vtz, zone)
                    if occ and _overlaps(occ, range_start, range_end, zone):
                        out.append(occ)
                        emitted_for_uid += 1

        for key, ov in uid_overrides.items():
            if emitted_for_uid >= MAX_INSTANCES_PER_EVENT:
                break
            if ov.value("STATUS").strip().upper() == "CANCELLED":
                continue
            try:
                p = ov.get("DTSTART")
                s = parse_date_value(p.value, p.params, vtz)
            except (IcsError, AttributeError):
                continue
            e = _end_for(ov, s, vtz)
            rid = ov.get("RECURRENCE-ID")
            occ = _occurrence(ov, uid, s, e, True, rid.value.strip() if rid else "", vtz, zone,
                              rid_params=rid.params if rid else None)
            if occ and _overlaps(occ, range_start, range_end, zone):
                out.append(occ)
                emitted_for_uid += 1
    out.sort(key=lambda o: (o["start_ms"], o["title"]))
    return out


def _format_like(dtstart_prop, value):
    """Render an occurrence start the way the series' DTSTART is written,
    so it can be used verbatim as a RECURRENCE-ID / EXDATE value."""
    if not isinstance(value, dt.datetime):
        return value.strftime("%Y%m%d")
    if dtstart_prop is not None and dtstart_prop.value.strip().endswith("Z"):
        return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return value.strftime("%Y%m%dT%H%M%S")


def _occurrence(comp, uid, start, end, recurring, rid_raw, vtz, zone, rid_params=None):
    status = comp.value("STATUS").strip().upper()
    if status == "CANCELLED":
        return None
    title = unescape_text(comp.value("SUMMARY"), MAX_TEXT["SUMMARY"])
    location = unescape_text(comp.value("LOCATION"), MAX_TEXT["LOCATION"])
    description = unescape_text(comp.value("DESCRIPTION"), MAX_TEXT["DESCRIPTION"])
    before, absolute = _alarms(comp, vtz)
    meeting = safe_meeting_url(" ".join([
        comp.value("URL"), comp.value("X-GOOGLE-CONFERENCE"), location, description]))
    all_day = not isinstance(start, dt.datetime)
    return {
        "uid": uid,
        "title": title,
        "location": location,
        "description": description,
        "start_ms": to_epoch_ms(start, zone),
        "end_ms": to_epoch_ms(end, zone),
        "all_day": all_day,
        "recurring": recurring,
        "recurrence_id": rid_raw,
        "alarms_before": before,
        "alarms_at": absolute,
        "meeting_url": meeting,
        "status": status,
    }


def _overlaps(occ, range_start, range_end, zone):
    rs = int(range_start.timestamp() * 1000)
    re_ = int(range_end.timestamp() * 1000)
    end = max(occ["end_ms"], occ["start_ms"] + 1)
    return occ["start_ms"] < re_ and end > rs


# --------------------------------------------------------------------------
# Writing


def fold(line):
    data = line.encode("utf-8")
    if len(data) <= 75:
        return line
    out, chunk, size = [], [], 0
    limit = 75
    for ch in line:
        b = len(ch.encode("utf-8"))
        if size + b > limit:
            out.append("".join(chunk))
            chunk, size, limit = [], 0, 74
        chunk.append(ch)
        size += b
    out.append("".join(chunk))
    return "\r\n ".join(out)


def _fmt_utc(value):
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def build_event(title, start, end, all_day=False, location="", description="",
                reminders_minutes=(), uid=None, now=None):
    """A complete VCALENDAR with one VEVENT. Times are written in UTC so no
    VTIMEZONE is needed; all-day events use VALUE=DATE."""
    uid = uid or (uuid.uuid4().hex + "@dadoux01.agenda")
    now = now or dt.datetime.now(UTC)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//dadoux01//Agenda for Omarchy//EN",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        "UID:" + uid,
        "DTSTAMP:" + _fmt_utc(now),
        "CREATED:" + _fmt_utc(now),
        "LAST-MODIFIED:" + _fmt_utc(now),
    ]
    if all_day:
        lines.append("DTSTART;VALUE=DATE:" + start.strftime("%Y%m%d"))
        lines.append("DTEND;VALUE=DATE:" + end.strftime("%Y%m%d"))
    else:
        lines.append("DTSTART:" + _fmt_utc(start))
        lines.append("DTEND:" + _fmt_utc(end))
    lines.append("SUMMARY:" + escape_text(title))
    if location:
        lines.append("LOCATION:" + escape_text(location))
    if description:
        lines.append("DESCRIPTION:" + escape_text(description))
    for minutes in sorted(set(int(m) for m in reminders_minutes if 0 <= int(m) <= 40320)):
        lines += [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            "DESCRIPTION:" + escape_text(title or "Reminder"),
            "TRIGGER:-PT%dM" % minutes,
            "END:VALARM",
        ]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return uid, "\r\n".join(fold(line) for line in lines) + "\r\n"


def add_exdate(text, uid, recurrence_id):
    """Exclude one occurrence from a series: add an EXDATE to the master,
    written in the same form as its DTSTART, and drop any override for it."""
    uid = str(uid or "").strip()[:500]
    recurrence_id = str(recurrence_id or "").strip()
    root = parse(text)
    vtz = _vtimezone_aliases(root)
    target_key = None
    override_params = None
    for cal in root.children:
        if cal.name != "VCALENDAR":
            continue
        for comp in cal.children:
            if comp.name == "VEVENT" and comp.value("UID").strip()[:500] == uid:
                rid = comp.get("RECURRENCE-ID")
                if rid is not None and rid.value.strip() == recurrence_id:
                    try:
                        target_key = occurrence_key(parse_date_value(rid.value, rid.params, vtz))
                        override_params = dict(rid.params)
                        break
                    except IcsError:
                        pass
        if target_key is not None:
            break

    found = False
    for cal in root.children:
        if cal.name != "VCALENDAR":
            continue
        keep = []
        for comp in cal.children:
            if comp.name == "VEVENT" and comp.value("UID").strip()[:500] == uid:
                rid = comp.get("RECURRENCE-ID")
                if rid is None:
                    start = comp.get("DTSTART")
                    if start is None:
                        raise IcsError("no_dtstart")
                    params = override_params if override_params is not None else dict(start.params)
                    try:
                        if target_key is None:
                            target_key = occurrence_key(parse_date_value(
                                recurrence_id, _rid_params(params, recurrence_id), vtz))
                    except IcsError:
                        raise IcsError("bad_recurrence_id")
                    head = "EXDATE"
                    if len(recurrence_id) == 8:
                        head += ";VALUE=DATE"
                    elif "TZID" in params and not recurrence_id.endswith("Z"):
                        head += ";TZID=" + _quote_param(params["TZID"])
                    line = head + ":" + recurrence_id
                    comp.props.append(Prop("EXDATE", {}, recurrence_id, line))
                    found = True
                else:
                    try:
                        key = occurrence_key(parse_date_value(rid.value, rid.params, vtz))
                    except IcsError:
                        key = None
                    if target_key is None:
                        try:
                            target_key = occurrence_key(parse_date_value(
                                recurrence_id, _rid_params(rid.params, recurrence_id), vtz))
                        except IcsError:
                            pass
                    if key is not None and key == target_key:
                        continue
            keep.append(comp)
        cal.children = keep
    if not found:
        raise IcsError("master_not_found")
    return serialize(root)


def _rid_params(params, value):
    out = {}
    if "TZID" in params and not value.endswith("Z"):
        out["TZID"] = params["TZID"]
    if len(value) == 8:
        out["VALUE"] = "DATE"
    return out


def _quote_param(value):
    cleaned = str(value).replace("\r", "").replace("\n", "").replace('"', "")
    return '"' + cleaned + '"' if any(c in cleaned for c in ":;,") else cleaned


def serialize(root):
    lines = []

    def walk(comp):
        if comp.name != "ROOT":
            lines.append("BEGIN:" + comp.name)
        for p in comp.props:
            lines.append(fold(p.raw))
        for child in comp.children:
            walk(child)
        if comp.name != "ROOT":
            lines.append("END:" + comp.name)

    walk(root)
    return "\r\n".join(lines) + "\r\n"
