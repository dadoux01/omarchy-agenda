// node --test tests/model.test.mjs   (TZ is pinned so results are stable)
import { test } from "node:test"
import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { fileURLToPath } from "node:url"
import path from "node:path"

process.env.TZ = "Europe/Paris"
const here = path.dirname(fileURLToPath(import.meta.url))
const source = readFileSync(path.join(here, "..", "Model.js"), "utf8").replace(/^\.pragma library\s*$/m, "")
const names = [...source.matchAll(/^function (\w+)/gm)].map(m => m[1])
const M = new Function(source + "\nreturn {" + names.join(",") + "}")()

const NOW = new Date(2026, 10, 25, 12, 5).getTime() // Wed 25 Nov 2026 12:05
const fmt = ms => { const d = new Date(ms); return `${d.getDate()}/${d.getMonth() + 1} ${d.getHours()}:${String(d.getMinutes()).padStart(2, "0")}` }

test("quick add: french relative day and range", () => {
  const r = M.parseQuickAdd("Dentiste demain 14h-15h", NOW)
  assert.equal(r.title, "Dentiste")
  assert.equal(fmt(r.start), "26/11 14:00")
  assert.equal(fmt(r.end), "26/11 15:00")
})

test("quick add: english with meridiem", () => {
  const r = M.parseQuickAdd("Lunch tomorrow 1pm-2:30pm", NOW)
  assert.equal(r.title, "Lunch")
  assert.equal(fmt(r.start), "26/11 13:00")
  assert.equal(fmt(r.end), "26/11 14:30")
  const s = M.parseQuickAdd("Call tomorrow 1-2pm", NOW)
  assert.equal(fmt(s.start), "26/11 13:00")
})

test("quick add: explicit date and single time defaults to one hour", () => {
  const r = M.parseQuickAdd("Réunion 28/11 10:30", NOW)
  assert.equal(r.title, "Réunion")
  assert.equal(fmt(r.start), "28/11 10:30")
  assert.equal(r.end - r.start, 3600000)
})

test("quick add: date only is all day; past date rolls to next year", () => {
  const r = M.parseQuickAdd("Vacances 24/12", NOW)
  assert.equal(r.allDay, true)
  assert.equal(fmt(r.start), "24/12 0:00")
  const p = M.parseQuickAdd("Bilan 02/01", NOW)
  assert.equal(new Date(p.start).getFullYear(), 2027)
})

test("quick add: weekday with connector", () => {
  const r = M.parseQuickAdd("Call vendredi à 9h", NOW)
  assert.equal(r.title, "Call")
  assert.equal(fmt(r.start), "27/11 9:00")
  const same = M.parseQuickAdd("Sport mercredi 18h", NOW)
  assert.equal(fmt(same.start), "2/12 18:00", "same weekday means next week")
})

test("quick add: time already past today goes to tomorrow", () => {
  const r = M.parseQuickAdd("Pharmacie 9h", NOW)
  assert.equal(fmt(r.start), "26/11 9:00")
  const later = M.parseQuickAdd("Pharmacie 17h", NOW)
  assert.equal(fmt(later.start), "25/11 17:00")
})

test("quick add: plain numbers in titles are not times", () => {
  assert.equal(M.parseQuickAdd("Acheter 3 baguettes", NOW), null)
  const r = M.parseQuickAdd("Rendu projet 42 demain", NOW)
  assert.equal(r.title, "Rendu projet 42")
  assert.equal(r.allDay, true)
})

test("quick add: no title or nothing to parse", () => {
  assert.equal(M.parseQuickAdd("demain 14h", NOW), null)
  assert.equal(M.parseQuickAdd("", NOW), null)
  assert.equal(M.parseQuickAdd("31/02 Rien", NOW), null)
})

test("quick add: range past midnight", () => {
  const r = M.parseQuickAdd("Soirée samedi 22h-2h", NOW)
  assert.equal(fmt(r.start), "28/11 22:00")
  assert.equal(fmt(r.end), "29/11 2:00")
})

const ev = (title, s, e, extra = {}) => ({ title, start: s, end: e, allDay: false, ...extra })
const H = 3600000

test("focus event: running, next, about-to-start beats running", () => {
  const evs = [ev("Block", NOW - H, NOW + 3 * H), ev("Standup", NOW + 10 * 60000, NOW + 25 * 60000),
    ev("Later", NOW + 5 * H, NOW + 6 * H), { ...ev("Holiday", NOW - H, NOW + 20 * H), allDay: true }]
  assert.equal(M.focusEvent(evs, NOW, 12 * H).event.title, "Standup")
  assert.equal(M.focusEvent(evs.slice(0, 1), NOW, 12 * H).running, true)
  assert.equal(M.focusEvent([evs[2]], NOW, 4 * H), null)
  assert.equal(M.focusEvent([evs[3]], NOW, 12 * H), null)
})

test("groupByDay keeps today, drops empty days, all-day first", () => {
  const today = M.startOfDay(NOW)
  const evs = [ev("B", today + 9 * H, today + 10 * H), { ...ev("A", today, today + 24 * H), allDay: true },
    ev("C", M.addDays(today, 3) + 8 * H, M.addDays(today, 3) + 9 * H)]
  const g = M.groupByDay(evs, NOW, 7)
  assert.deepEqual(g.map(d => d.offset), [0, 3])
  assert.deepEqual(g[0].events.map(e => e.title), ["A", "B"])
  assert.deepEqual(M.groupByDay([], NOW, 7).map(d => d.offset), [0])
})

test("form field parsing", () => {
  assert.equal(fmt(M.parseDateField("28/11/2026")), "28/11 0:00")
  assert.equal(fmt(M.parseDateField("2026-11-28")), "28/11 0:00")
  assert.ok(Number.isNaN(M.parseDateField("31/11/2026")))
  assert.deepEqual(M.parseTimeField("9h30"), { h: 9, m: 30 })
  assert.deepEqual(M.parseTimeField("14"), { h: 14, m: 0 })
  assert.deepEqual(M.parseTimeField("2pm"), { h: 14, m: 0 })
  assert.equal(M.parseTimeField("25:00"), null)
  assert.equal(fmt(M.defaultStart(NOW)), "25/11 12:30")
})

test("meeting url and colors are validated", () => {
  assert.equal(M.safeMeetingUrl("https://meet.google.com/abc"), "https://meet.google.com/abc")
  assert.equal(M.safeMeetingUrl("javascript:alert(1)"), "")
  assert.equal(M.safeMeetingUrl("https://x.com/a b"), "")
  assert.equal(M.isHexColor("#a1b2c3"), true)
  assert.equal(M.isHexColor("red;"), false)
})

test("neutralize removes markup characters", () => {
  const out = M.neutralize('<img src="https://evil.invalid/t">&amp;\u0007')
  assert.ok(!/[<>&]/.test(out))
  assert.equal(M.neutralize("Perso"), "Perso")
})

test("bar label fits the budget and keeps the time part", () => {
  const long = M.fitLabel("%1 dans %2 min", ["Réunion trimestrielle avec toute l'équipe produit", 6], 0, 24)
  assert.ok(long.length <= 24, long)
  assert.ok(long.endsWith(" dans 6 min"), long)
  assert.ok(long.includes("…"))
  assert.equal(M.fitLabel("%1 %2", ["14:30", "Dentiste"], 1, 24), "14:30 Dentiste")
  const tiny = M.fitLabel("%1 · jusqu'à %2", ["Bloc de travail", "18:00"], 0, 10)
  assert.ok(tiny.startsWith("Blo"), tiny)
  const dollar = M.fitLabel("%1 in %2 min", ["Fix $& $$ $1 bug", 5], 0, 40)
  assert.equal(dollar, "Fix $& $$ $1 bug in 5 min")
})

test("dst overnight range and null guards", () => {
  // Europe/Paris springs forward on 2027-03-28 at 02:00 -> 03:00
  const dstEve = new Date(2027, 2, 27, 12, 0).getTime()
  const r = M.parseQuickAdd("Party 27/03/2027 23h-04h", dstEve)
  assert.equal(fmt(r.start), "27/3 23:00")
  assert.equal(fmt(r.end), "28/3 4:00")
  assert.equal(M.focusEvent(null, NOW, H), null)
  assert.equal(M.countOn(null, NOW), 0)
  assert.deepEqual(M.groupByDay(null, NOW, 3).map(d => d.offset), [0])
})
