import datetime as dt
import os
import sys
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from agenda import ics  # noqa: E402

PARIS = ZoneInfo("Europe/Paris")
UTC = dt.timezone.utc


def window(y1, m1, d1, y2, m2, d2, zone=PARIS):
    return dt.datetime(y1, m1, d1, tzinfo=zone), dt.datetime(y2, m2, d2, tzinfo=zone)


def wrap(*vevents, extra=""):
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + extra + "".join(vevents) + "END:VCALENDAR\r\n"


def vevent(body):
    return "BEGIN:VEVENT\r\n" + body.strip().replace("\n", "\r\n") + "\r\nEND:VEVENT\r\n"


def starts(occs, zone=PARIS):
    return [dt.datetime.fromtimestamp(o["start_ms"] / 1000, zone).strftime("%Y-%m-%d %H:%M") for o in occs]


class ParseTests(unittest.TestCase):
    def test_folding_and_escapes(self):
        text = wrap(vevent("""UID:1
DTSTART:20261125T090000Z
DTEND:20261125T100000Z
SUMMARY:Réunion\\, équipe\\; point
  hebdo
DESCRIPTION:ligne1\\nligne2"""))
        occ = ics.events_from_calendar(text, *window(2026, 11, 24, 2026, 11, 27))
        self.assertEqual(occ[0]["title"], "Réunion, équipe; point hebdo")
        self.assertEqual(occ[0]["description"], "ligne1\nligne2")

    def test_quoted_param_with_colon(self):
        name, params, value = ics.parse_line('ATTENDEE;CN="Doe: John";ROLE=REQ:mailto:a@b.c')
        self.assertEqual(name, "ATTENDEE")
        self.assertEqual(params["CN"], "Doe: John")
        self.assertEqual(value, "mailto:a@b.c")

    def test_markup_is_kept_as_text_and_control_chars_removed(self):
        text = wrap(vevent("""UID:x
DTSTART:20261125T090000Z
SUMMARY:<img src="https://evil.invalid/t">\x07hi"""))
        occ = ics.events_from_calendar(text, *window(2026, 11, 24, 2026, 11, 27))
        self.assertEqual(occ[0]["title"], '<img src="https://evil.invalid/t">hi')

    def test_title_is_capped(self):
        text = wrap(vevent("UID:x\nDTSTART:20261125T090000Z\nSUMMARY:" + "a" * 5000))
        occ = ics.events_from_calendar(text, *window(2026, 11, 24, 2026, 11, 27))
        self.assertLessEqual(len(occ[0]["title"]), 301)

    def test_tzid_and_windows_zone(self):
        text = wrap(vevent("""UID:a
DTSTART;TZID=Romance Standard Time:20261125T140000
DTEND;TZID=Romance Standard Time:20261125T150000
SUMMARY:A"""), vevent("""UID:b
DTSTART;TZID=America/New_York:20261125T090000
SUMMARY:B"""))
        occ = ics.events_from_calendar(text, *window(2026, 11, 24, 2026, 11, 27))
        self.assertEqual(starts(occ), ["2026-11-25 14:00", "2026-11-25 15:00"])

    def test_all_day(self):
        text = wrap(vevent("""UID:a
DTSTART;VALUE=DATE:20261125
DTEND;VALUE=DATE:20261126
SUMMARY:Anniv"""))
        occ = ics.events_from_calendar(text, *window(2026, 11, 24, 2026, 11, 27), zone=PARIS)
        self.assertTrue(occ[0]["all_day"])
        self.assertEqual(occ[0]["end_ms"] - occ[0]["start_ms"], 86400000)

    def test_cancelled_is_skipped(self):
        text = wrap(vevent("UID:a\nDTSTART:20261125T090000Z\nSTATUS:CANCELLED\nSUMMARY:x"))
        self.assertEqual(ics.events_from_calendar(text, *window(2026, 11, 24, 2026, 11, 27)), [])

    def test_doctype_is_irrelevant_but_size_capped(self):
        with self.assertRaises(ics.IcsError):
            ics.parse("A" * (ics.MAX_ICS_BYTES + 1))

    def test_alarms(self):
        text = wrap(vevent("""UID:a
DTSTART:20261125T090000Z
SUMMARY:x
BEGIN:VALARM
ACTION:DISPLAY
TRIGGER:-PT10M
END:VALARM
BEGIN:VALARM
ACTION:AUDIO
TRIGGER;RELATED=START:-P1DT2H
END:VALARM
BEGIN:VALARM
ACTION:EMAIL
TRIGGER:-PT5M
END:VALARM
BEGIN:VALARM
ACTION:DISPLAY
TRIGGER;VALUE=DATE-TIME:20261125T080000Z
END:VALARM"""))
        occ = ics.events_from_calendar(text, *window(2026, 11, 24, 2026, 11, 27))[0]
        self.assertEqual(occ["alarms_before"], [600, 93600])
        self.assertEqual(occ["alarms_at"], [int(dt.datetime(2026, 11, 25, 8, tzinfo=UTC).timestamp() * 1000)])

    def test_meeting_url(self):
        self.assertEqual(ics.safe_meeting_url("Join: https://meet.google.com/abc-defg-hij."),
                         "https://meet.google.com/abc-defg-hij")
        self.assertEqual(ics.safe_meeting_url("https://us02web.zoom.us/j/123?pwd=x"),
                         "https://us02web.zoom.us/j/123?pwd=x")
        self.assertEqual(ics.safe_meeting_url("http://meet.google.com/x"), "")
        self.assertEqual(ics.safe_meeting_url("https://meet.google.com.evil.io/x"), "")
        self.assertEqual(ics.safe_meeting_url("javascript:alert(1)"), "")


class RecurrenceTests(unittest.TestCase):
    def occ(self, body, w, zone=PARIS):
        return starts(ics.events_from_calendar(wrap(vevent(body)), *w, zone=zone), zone)

    def test_weekly_byday_keeps_wall_time_across_dst(self):
        got = self.occ("""UID:r
DTSTART;TZID=Europe/Paris:20261019T090000
DTEND;TZID=Europe/Paris:20261019T093000
RRULE:FREQ=WEEKLY;BYDAY=MO,WE""", window(2026, 10, 19, 2026, 11, 3))
        self.assertEqual(got, ["2026-10-19 09:00", "2026-10-21 09:00", "2026-10-26 09:00",
                               "2026-10-28 09:00", "2026-11-02 09:00"])

    def test_daily_count(self):
        got = self.occ("""UID:r
DTSTART:20261125T080000Z
RRULE:FREQ=DAILY;COUNT=3""", window(2026, 11, 1, 2026, 12, 31))
        self.assertEqual(len(got), 3)

    def test_count_counts_from_dtstart_not_window(self):
        got = self.occ("""UID:r
DTSTART:20261101T080000Z
RRULE:FREQ=DAILY;COUNT=5""", window(2026, 11, 4, 2026, 12, 31))
        self.assertEqual(got, ["2026-11-04 09:00", "2026-11-05 09:00"])

    def test_until(self):
        got = self.occ("""UID:r
DTSTART;TZID=Europe/Paris:20261123T100000
RRULE:FREQ=DAILY;UNTIL=20261125T090000Z""", window(2026, 11, 20, 2026, 12, 31))
        self.assertEqual(got, ["2026-11-23 10:00", "2026-11-24 10:00", "2026-11-25 10:00"])

    def test_monthly_last_friday(self):
        got = self.occ("""UID:r
DTSTART;TZID=Europe/Paris:20260130T170000
RRULE:FREQ=MONTHLY;BYDAY=-1FR""", window(2026, 11, 1, 2027, 1, 31))
        self.assertEqual(got, ["2026-11-27 17:00", "2026-12-25 17:00", "2027-01-29 17:00"])

    def test_monthly_31st_skips_short_months(self):
        got = self.occ("""UID:r
DTSTART;VALUE=DATE:20260131
RRULE:FREQ=MONTHLY""", window(2026, 1, 1, 2026, 6, 1))
        self.assertEqual([g[:10] for g in got], ["2026-01-31", "2026-03-31", "2026-05-31"])

    def test_monthly_bysetpos_last_weekday(self):
        got = self.occ("""UID:r
DTSTART;TZID=Europe/Paris:20261130T090000
RRULE:FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1""", window(2026, 11, 1, 2027, 1, 31))
        self.assertEqual(got, ["2026-11-30 09:00", "2026-12-31 09:00", "2027-01-29 09:00"])

    def test_yearly_birthday_all_day(self):
        got = self.occ("""UID:r
DTSTART;VALUE=DATE:19900312
RRULE:FREQ=YEARLY""", window(2026, 1, 1, 2027, 12, 31))
        self.assertEqual([g[:10] for g in got], ["2026-03-12", "2027-03-12"])

    def test_yearly_thanksgiving(self):
        got = self.occ("""UID:r
DTSTART;VALUE=DATE:20201126
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=4TH""", window(2026, 1, 1, 2027, 12, 31))
        self.assertEqual([g[:10] for g in got], ["2026-11-26", "2027-11-25"])

    def test_old_daily_series_skips_ahead(self):
        got = self.occ("""UID:r
DTSTART;TZID=Europe/Paris:20000101T073000
RRULE:FREQ=DAILY;INTERVAL=2""", window(2026, 11, 24, 2026, 11, 29))
        # 2000-01-01 + even number of days
        for g in got:
            d = dt.date.fromisoformat(g[:10])
            self.assertEqual((d - dt.date(2000, 1, 1)).days % 2, 0)
        self.assertEqual(len(got), 3)

    def test_weekly_interval_two_from_old_start(self):
        got = self.occ("""UID:r
DTSTART;TZID=Europe/Paris:20240101T100000
RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO""", window(2026, 11, 1, 2026, 12, 1))
        for g in got:
            d = dt.date.fromisoformat(g[:10])
            self.assertEqual((d - dt.date(2024, 1, 1)).days % 14, 0)
        self.assertEqual([g[:10] for g in got], ["2026-11-02", "2026-11-16", "2026-11-30"])

    def test_exdate_and_override(self):
        text = wrap(vevent("""UID:s
DTSTART;TZID=Europe/Paris:20261123T090000
DTEND;TZID=Europe/Paris:20261123T100000
RRULE:FREQ=DAILY;COUNT=5
EXDATE;TZID=Europe/Paris:20261124T090000
SUMMARY:Standup"""), vevent("""UID:s
RECURRENCE-ID;TZID=Europe/Paris:20261125T090000
DTSTART;TZID=Europe/Paris:20261125T113000
DTEND;TZID=Europe/Paris:20261125T120000
SUMMARY:Standup (moved)"""), vevent("""UID:s
RECURRENCE-ID;TZID=Europe/Paris:20261126T090000
DTSTART;TZID=Europe/Paris:20261126T090000
STATUS:CANCELLED"""))
        occ = ics.events_from_calendar(text, *window(2026, 11, 20, 2026, 12, 1))
        self.assertEqual(starts(occ), ["2026-11-23 09:00", "2026-11-25 11:30", "2026-11-27 09:00"])
        self.assertEqual(occ[1]["title"], "Standup (moved)")
        self.assertTrue(all(o["recurring"] for o in occ))
        self.assertEqual(occ[0]["recurrence_id"], "20261123T090000")

    def test_running_event_from_before_window_counts(self):
        got = self.occ("""UID:r
DTSTART;TZID=Europe/Paris:20261124T230000
DTEND;TZID=Europe/Paris:20261125T020000
RRULE:FREQ=WEEKLY""", window(2026, 11, 25, 2026, 11, 26))
        self.assertEqual(got, ["2026-11-24 23:00"])

    def test_instance_cap(self):
        got = self.occ("""UID:r
DTSTART:20261101T000000Z
RRULE:FREQ=DAILY""", window(2026, 11, 1, 2030, 1, 1))
        self.assertEqual(len(got), ics.MAX_INSTANCES_PER_EVENT)

    def test_rdate_comma_bomb_is_bounded(self):
        # Issue #15910: huge comma-separated RDATE/EXDATE lists must stay bounded.
        rdates = ",".join("202611%02dT090000Z" % (1 + (i % 28)) for i in range(60_000))
        many_in_win = ",".join(
            (dt.datetime(2026, 11, 1, 9, tzinfo=UTC) + dt.timedelta(minutes=i)).strftime("%Y%m%dT%H%M%SZ")
            for i in range(6_000)
        )
        exdates = ",".join("20261101T090%d00Z" % (i % 10) for i in range(20_000))
        text = wrap(vevent(f"UID:bomb\nDTSTART:20261101T090000Z\nRDATE:{rdates}\nRDATE:{many_in_win}\nEXDATE:{exdates}\nSUMMARY:Boom"))
        occ = ics.events_from_calendar(text, *window(2026, 11, 1, 2026, 12, 1))
        self.assertLessEqual(len(occ), ics.MAX_INSTANCES_PER_EVENT)
        self.assertNotIn("2026-11-01 10:00", starts(occ))

    def test_rrule_rdate_and_overrides_share_instance_cap(self):
        base = vevent("UID:shared\nDTSTART:20261101T090000Z\nRRULE:FREQ=DAILY\nSUMMARY:Master")
        overrides = "".join(
            vevent(
                "UID:shared\n"
                f"RECURRENCE-ID:{(dt.datetime(2027, 6, 1, 9, tzinfo=UTC) + dt.timedelta(days=i)).strftime('%Y%m%dT%H%M%SZ')}\n"
                f"DTSTART:{(dt.datetime(2027, 6, 1, 10, tzinfo=UTC) + dt.timedelta(days=i)).strftime('%Y%m%dT%H%M%SZ')}\n"
                f"SUMMARY:Ov {i}"
            )
            for i in range(600)
        )
        occ = ics.events_from_calendar(wrap(base, overrides), *window(2026, 11, 1, 2032, 1, 1))
        self.assertEqual(len(occ), ics.MAX_INSTANCES_PER_EVENT)

    def test_line_cap_and_rfc5545_edge_cases(self):
        with self.assertRaises(ics.IcsError):
            ics.parse("X\n" * (ics.MAX_LINES + 1))

        text = wrap(vevent("""UID:rfc
DTSTART;VALUE=date:20261125
RDATE;VALUE=DATE-TIME:20261126T000000Z/PT1H
RRULE:FREQ=WEEKLY;BYDAY=MO,MO
SUMMARY:Multi"""))
        occ = ics.events_from_calendar(text, *window(2026, 11, 24, 2026, 12, 8), zone=PARIS)
        # DTSTART (Wed 25) is included per RFC 5545, duplicate BYDAY=MO deduplicated, PERIOD ignored.
        self.assertEqual([g[:10] for g in starts(occ, PARIS)], ["2026-11-25", "2026-11-30", "2026-12-07"])

        with self.assertRaises(ics.IcsError):
            ics.parse_date_value("20260230")

    def test_rdate_exdate_budget(self):
        # A huge RDATE list (every minute for ~6 months) and a huge EXDATE list:
        # occurrences stay capped and the lists are read only up to the budget.
        base = dt.datetime(2026, 11, 1, tzinfo=UTC)
        dates = ",".join((base + dt.timedelta(minutes=i)).strftime("%Y%m%dT%H%M%SZ")
                         for i in range(250_000))
        text = wrap(vevent("UID:big\nDTSTART:20261101T000000Z\nRRULE:FREQ=DAILY\n"
                           "RDATE:" + dates + "\nEXDATE:" + dates + "\nSUMMARY:Flood"))
        calls = []
        real = ics.parse_date_value
        with mock.patch.object(ics, "parse_date_value",
                               side_effect=lambda *a, **k: calls.append(1) or real(*a, **k)):
            occ = ics.events_from_calendar(text, *window(2026, 11, 1, 2028, 1, 1))
        self.assertLessEqual(len(occ), ics.MAX_INSTANCES_PER_EVENT)
        self.assertLessEqual(len(calls), ics.MAX_DATE_VALUES_PER_EVENT + 10)

    def test_rdate_dedup_and_exdate(self):
        got = self.occ("""UID:r
DTSTART:20261123T090000Z
RRULE:FREQ=DAILY;COUNT=2
RDATE:20261124T090000Z,20261126T090000Z,20261126T090000Z
RDATE:20261127T090000Z
EXDATE:20261123T090000Z,20261127T090000Z""", window(2026, 11, 20, 2026, 12, 1, UTC), UTC)
        self.assertEqual(got, ["2026-11-24 09:00", "2026-11-26 09:00"])


class WriteTests(unittest.TestCase):
    def test_build_event_round_trip(self):
        start = dt.datetime(2026, 11, 26, 14, 0, tzinfo=PARIS)
        uid, text = ics.build_event("Dentiste, Dr; X", start, start + dt.timedelta(hours=1),
                                    location="12 rue " + "très longue " * 10, reminders_minutes=[10, 60])
        self.assertTrue(all(len(line.encode()) <= 75 for line in text.split("\r\n")))
        occ = ics.events_from_calendar(text, *window(2026, 11, 25, 2026, 11, 28))[0]
        self.assertEqual(occ["uid"], uid)
        self.assertEqual(occ["title"], "Dentiste, Dr; X")
        self.assertEqual(starts([occ]), ["2026-11-26 14:00"])
        self.assertEqual(occ["alarms_before"], [600, 3600])

    def test_build_all_day(self):
        _, text = ics.build_event("Off", dt.date(2026, 12, 24), dt.date(2026, 12, 26), all_day=True)
        self.assertIn("DTSTART;VALUE=DATE:20261224", text)
        occ = ics.events_from_calendar(text, *window(2026, 12, 1, 2027, 1, 1))[0]
        self.assertTrue(occ["all_day"])

    def test_add_exdate_tzid(self):
        text = wrap(vevent("""UID:s
DTSTART;TZID=Europe/Paris:20261123T090000
RRULE:FREQ=DAILY;COUNT=3
SUMMARY:S"""), vevent("""UID:s
RECURRENCE-ID;TZID=Europe/Paris:20261124T090000
DTSTART;TZID=Europe/Paris:20261124T100000
SUMMARY:S moved"""))
        updated = ics.add_exdate(text, "s", "20261124T090000")
        self.assertIn("EXDATE;TZID=Europe/Paris:20261124T090000", updated)
        occ = ics.events_from_calendar(updated, *window(2026, 11, 20, 2026, 12, 1))
        self.assertEqual(starts(occ), ["2026-11-23 09:00", "2026-11-25 09:00"])

    def test_add_exdate_utc_and_date(self):
        text = wrap(vevent("UID:u\nDTSTART:20261123T080000Z\nRRULE:FREQ=DAILY;COUNT=3"))
        occ = ics.events_from_calendar(text, *window(2026, 11, 20, 2026, 12, 1))
        updated = ics.add_exdate(text, "u", occ[1]["recurrence_id"])
        self.assertEqual(len(ics.events_from_calendar(updated, *window(2026, 11, 20, 2026, 12, 1))), 2)
        text = wrap(vevent("UID:d\nDTSTART;VALUE=DATE:20261123\nRRULE:FREQ=DAILY;COUNT=3"))
        updated = ics.add_exdate(text, "d", "20261124")
        self.assertIn("EXDATE;VALUE=DATE:20261124", updated)
        self.assertEqual(len(ics.events_from_calendar(updated, *window(2026, 11, 20, 2026, 12, 1))), 2)

    def test_add_exdate_unknown_uid(self):
        with self.assertRaises(ics.IcsError):
            ics.add_exdate(wrap(vevent("UID:a\nDTSTART:20261123T080000Z")), "zzz", "20261123T080000Z")

    def test_add_exdate_override_with_different_tzid(self):
        text = wrap(
            vevent("UID:m\nDTSTART;TZID=Europe/Paris:20261123T090000\nRRULE:FREQ=DAILY;COUNT=2\nSUMMARY:M"),
            vevent("UID:m\nRECURRENCE-ID;TZID=America/New_York:20261124T030000\n"
                   "DTSTART;TZID=Europe/Paris:20261124T110000\nSUMMARY:Moved"),
        )
        updated = ics.add_exdate(text, "m", "20261124T030000")
        self.assertIn("EXDATE;TZID=America/New_York:20261124T030000", updated)
        occ = ics.events_from_calendar(updated, *window(2026, 11, 20, 2026, 12, 1))
        self.assertEqual(starts(occ), ["2026-11-23 09:00"])

    def test_long_uid_matches_add_exdate(self):
        uid = "u" * 300
        text = wrap(vevent(f"UID:{uid}\nDTSTART:20261123T080000Z\nRRULE:FREQ=DAILY;COUNT=2\nSUMMARY:Long"))
        occ = ics.events_from_calendar(text, *window(2026, 11, 20, 2026, 12, 1))
        self.assertEqual(occ[0]["uid"], uid)
        updated = ics.add_exdate(text, occ[0]["uid"], occ[1]["recurrence_id"])
        self.assertEqual(len(ics.events_from_calendar(updated, *window(2026, 11, 20, 2026, 12, 1))), 1)


if __name__ == "__main__":
    unittest.main()
