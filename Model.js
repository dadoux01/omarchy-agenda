.pragma library

// Pure helpers for the widget: grouping, the bar label, quick add.
// No QML types in here, so tests/model.test.mjs can run it under node.

var MINUTE = 60 * 1000
var HOUR = 60 * MINUTE
var DAY = 24 * HOUR

function startOfDay(ms) {
  var d = new Date(ms)
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()
}

function addDays(dayMs, n) {
  var d = new Date(dayMs)
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n).getTime()
}

function dayKey(ms) {
  var d = new Date(ms)
  return d.getFullYear() + "-" + (d.getMonth() + 1) + "-" + d.getDate()
}

function overlapsDay(ev, dayStart, dayEnd) {
  var end = Math.max(ev.end, ev.start + 1)
  return ev.start < dayEnd && end > dayStart
}

// Days from today, each with the events touching it. Ended events of today
// stay in the list (dimmed by the panel); empty days are dropped except today.
function groupByDay(events, nowMs, days) {
  events = Array.isArray(events) ? events : []
  var out = []
  var today = startOfDay(nowMs)
  for (var i = 0; i < days; i++) {
    var ds = addDays(today, i)
    var de = addDays(today, i + 1)
    var list = []
    for (var j = 0; j < events.length; j++)
      if (overlapsDay(events[j], ds, de)) list.push(events[j])
    list.sort(function(a, b) {
      if (a.allDay !== b.allDay) return a.allDay ? -1 : 1
      return a.start - b.start || String(a.title).localeCompare(String(b.title))
    })
    if (list.length > 0 || i === 0) out.push({ day: ds, offset: i, events: list })
  }
  return out
}

// The event the bar should talk about: one running now, else the next one
// starting within `horizonMs`. All-day events never take the bar.
function focusEvent(events, nowMs, horizonMs) {
  if (!Array.isArray(events) || events.length === 0) return null
  var running = null
  var next = null
  for (var i = 0; i < events.length; i++) {
    var e = events[i]
    if (e.allDay) continue
    if (e.start <= nowMs && e.end > nowMs) {
      if (!running || e.start > running.start) running = e
    } else if (e.start > nowMs && e.start - nowMs <= horizonMs) {
      if (!next || e.start < next.start) next = e
    }
  }
  // A meeting about to start matters more than the long block it sits in.
  if (running && next && next.start - nowMs <= 15 * MINUTE) return { event: next, running: false }
  if (running) return { event: running, running: true }
  if (next) return { event: next, running: false }
  return null
}

function countOn(events, dayMs) {
  if (!Array.isArray(events)) return 0
  var de = addDays(dayMs, 1)
  var n = 0
  for (var i = 0; i < events.length; i++) if (overlapsDay(events[i], dayMs, de)) n++
  return n
}

function minutesUntil(ms, nowMs) {
  return Math.max(0, Math.ceil((ms - nowMs) / MINUTE))
}

function truncate(text, max) {
  text = String(text || "")
  if (text.length <= max) return text
  return text.slice(0, Math.max(1, max - 1)).replace(/\s+$/, "") + "…"
}

function pad2(n) { return (n < 10 ? "0" : "") + n }

function timeText(ms, use24h) {
  var d = new Date(ms)
  if (use24h) return pad2(d.getHours()) + ":" + pad2(d.getMinutes())
  var h = d.getHours() % 12 || 12
  return h + ":" + pad2(d.getMinutes()) + (d.getHours() < 12 ? " AM" : " PM")
}

function isHexColor(value) {
  return /^#[0-9a-fA-F]{6}$/.test(String(value || ""))
}

function safeMeetingUrl(url) {
  url = String(url || "")
  return /^https:\/\/[A-Za-z0-9.-]+(:\d+)?(\/[^\s"'<>`]*)?$/.test(url) && url.length <= 2000 ? url : ""
}

// ------------------------------------------------------------------ quick add
//
// "Dentiste demain 14h-15h", "Lunch tomorrow 1pm-2pm", "Réunion 28/11 10:30",
// "Vacances 24/12" (all day), "Call vendredi à 9h". Returns null when no date
// or time was found, so the form can take over.

var WEEKDAYS = {
  "dimanche": 0, "lundi": 1, "mardi": 2, "mercredi": 3, "jeudi": 4, "vendredi": 5, "samedi": 6,
  "sunday": 0, "monday": 1, "tuesday": 2, "wednesday": 3, "thursday": 4, "friday": 5, "saturday": 6,
  "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6
}

var RELATIVE = {
  "aujourd'hui": 0, "aujourdhui": 0, "auj": 0, "today": 0,
  "demain": 1, "tomorrow": 1, "tmrw": 1,
  "après-demain": 2, "apres-demain": 2, "après demain": 2
}

var FILLERS = ["à", "a", "at", "de", "du", "from", "le", "on", "le", "pour", "for", "et", "to", "jusqu'à"]

function parseTime(token) {
  var t = String(token).toLowerCase()
  var m = t.match(/^(\d{1,2})(?:[h:](\d{2})?)?\s*(am|pm)?$/)
  if (!m) return null
  if (!m[2] && !m[3] && t.indexOf("h") === -1 && t.indexOf(":") === -1) return null
  var h = parseInt(m[1], 10)
  var mi = m[2] ? parseInt(m[2], 10) : 0
  if (m[3]) {
    if (h < 1 || h > 12) return null
    if (m[3] === "pm" && h !== 12) h += 12
    if (m[3] === "am" && h === 12) h = 0
  }
  if (h > 23 || mi > 59) return null
  return { h: h, m: mi }
}

function parseQuickAdd(text, nowMs) {
  var src = String(text || "").replace(/\s+/g, " ").trim()
  if (!src) return null
  var now = new Date(nowMs)
  var date = null
  var times = []
  var used = []

  function take(re, fn) {
    var m = src.match(re)
    if (!m) return false
    if (fn(m) === false) return false
    used.push(m[0])
    src = (src.slice(0, m.index) + " " + src.slice(m.index + m[0].length)).replace(/\s+/g, " ").trim()
    return true
  }

  // Time ranges first, so "14h-15h" is not read as two loose times.
  take(/(?:^|\s)(?:de\s+|from\s+)?(\d{1,2}(?:[h:]\d{0,2})?\s*(?:am|pm)?)\s*(?:-|–|à|a|to|jusqu'à)\s*(\d{1,2}(?:[h:]\d{0,2})?\s*(?:am|pm)?)(?=\s|$)/i, function(m) {
    var a = parseTime(m[1].replace(/\s+/g, "")), b = parseTime(m[2].replace(/\s+/g, ""))
    if (!b) return false
    if (!a) {
      // "1-2pm": the meridiem written once applies to both ends.
      var bare = m[1].match(/^(\d{1,2})$/)
      var mer = m[2].toLowerCase().match(/(am|pm)$/)
      if (bare && mer) a = parseTime(bare[1] + mer[1])
    }
    if (!a) return false
    times = [a, b]
  })
  if (times.length === 0) {
    take(/(?:^|\s)(?:à\s+|a\s+|at\s+)?(\d{1,2}(?:[h:]\d{0,2})?\s*(?:am|pm)?)(?=\s|$)/i, function(m) {
      var a = parseTime(m[1].replace(/\s+/g, ""))
      if (!a) return false
      times = [a]
    })
  }

  // Dates.
  var lower = src.toLowerCase()
  for (var word in RELATIVE) {
    var idx = lower.search(new RegExp("(^|\\s)" + word.replace(/[-']/g, "[-' ]?") + "(?=\\s|$)"))
    if (idx !== -1) {
      date = new Date(now.getFullYear(), now.getMonth(), now.getDate() + RELATIVE[word])
      var re = new RegExp("(^|\\s)" + word.replace(/[-']/g, "[-' ]?") + "(?=\\s|$)", "i")
      src = src.replace(re, " ").replace(/\s+/g, " ").trim()
      break
    }
  }
  if (!date) {
    take(/(?:^|\s)(?:le\s+|on\s+)?(\d{1,2})[\/.](\d{1,2})(?:[\/.](\d{2,4}))?(?=\s|$)/i, function(m) {
      var d = parseInt(m[1], 10), mo = parseInt(m[2], 10) - 1
      var y = m[3] ? parseInt(m[3], 10) : now.getFullYear()
      if (y < 100) y += 2000
      var candidate = new Date(y, mo, d)
      if (candidate.getMonth() !== mo || candidate.getDate() !== d) return false
      if (!m[3] && candidate.getTime() < startOfDay(nowMs)) candidate = new Date(y + 1, mo, d)
      date = candidate
    })
  }
  if (!date) {
    take(/(?:^|\s)(\d{4})-(\d{2})-(\d{2})(?=\s|$)/, function(m) {
      var candidate = new Date(parseInt(m[1], 10), parseInt(m[2], 10) - 1, parseInt(m[3], 10))
      if (candidate.getDate() !== parseInt(m[3], 10)) return false
      date = candidate
    })
  }
  if (!date) {
    take(/(?:^|\s)(?:ce\s+|this\s+|next\s+|le\s+|on\s+)?(dimanche|lundi|mardi|mercredi|jeudi|vendredi|samedi|sunday|monday|tuesday|wednesday|thursday|friday|saturday)(?:\s+prochain)?(?=\s|$)/i, function(m) {
      var target = WEEKDAYS[m[1].toLowerCase()]
      var delta = (target - now.getDay() + 7) % 7
      if (delta === 0) delta = 7
      date = new Date(now.getFullYear(), now.getMonth(), now.getDate() + delta)
    })
  }

  if (!date && times.length === 0) return null
  if (!date) {
    date = new Date(now.getFullYear(), now.getMonth(), now.getDate())
    // A time already past today means tomorrow.
    var probe = new Date(date.getFullYear(), date.getMonth(), date.getDate(), times[0].h, times[0].m)
    if (probe.getTime() < nowMs - 5 * MINUTE) date = new Date(date.getFullYear(), date.getMonth(), date.getDate() + 1)
  }

  // What is left is the title, minus dangling connector words.
  var words = src.split(" ").filter(function(w) { return w.length > 0 })
  while (words.length && FILLERS.indexOf(words[words.length - 1].toLowerCase()) !== -1) words.pop()
  while (words.length && FILLERS.indexOf(words[0].toLowerCase()) !== -1) words.shift()
  var title = words.join(" ").trim()
  if (!title) return null

  if (times.length === 0) {
    var s = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime()
    return { title: title, allDay: true, start: s, end: addDays(s, 1) }
  }
  var start = new Date(date.getFullYear(), date.getMonth(), date.getDate(), times[0].h, times[0].m).getTime()
  var end
  if (times.length > 1) {
    end = new Date(date.getFullYear(), date.getMonth(), date.getDate(), times[1].h, times[1].m).getTime()
    if (end <= start) end = new Date(date.getFullYear(), date.getMonth(), date.getDate() + 1, times[1].h, times[1].m).getTime()
  } else {
    end = start + HOUR
  }
  return { title: title, allDay: false, start: start, end: end }
}

// "2026-11-25" / "25/11/2026" -> day start ms, or NaN.
function parseDateField(text) {
  var t = String(text || "").trim()
  var m = t.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/)
  var y, mo, d
  if (m) { y = +m[1]; mo = +m[2] - 1; d = +m[3] }
  else {
    m = t.match(/^(\d{1,2})[\/.](\d{1,2})[\/.](\d{2,4})$/)
    if (!m) return NaN
    d = +m[1]; mo = +m[2] - 1; y = +m[3]
    if (y < 100) y += 2000
  }
  var date = new Date(y, mo, d)
  if (date.getMonth() !== mo || date.getDate() !== d) return NaN
  return date.getTime()
}

function parseTimeField(text) {
  var t = parseTime(String(text || "").trim().toLowerCase().replace(/\s+/g, ""))
  if (!t) {
    var bare = String(text || "").trim().match(/^(\d{1,2})$/)
    if (bare && +bare[1] < 24) t = { h: +bare[1], m: 0 }
  }
  return t
}

function dateFieldText(ms) {
  var d = new Date(ms)
  return pad2(d.getDate()) + "/" + pad2(d.getMonth() + 1) + "/" + d.getFullYear()
}

function timeFieldText(ms) {
  var d = new Date(ms)
  return pad2(d.getHours()) + ":" + pad2(d.getMinutes())
}

function at(dayMs, time) {
  var d = new Date(dayMs)
  return new Date(d.getFullYear(), d.getMonth(), d.getDate(), time.h, time.m).getTime()
}

// Next half hour from now, for a fresh form.
function defaultStart(nowMs) {
  var d = new Date(nowMs)
  d.setSeconds(0, 0)
  d.setMinutes(d.getMinutes() < 30 ? 30 : 60)
  return d.getTime()
}

// For text from a server that has to go through an Omarchy component whose
// Text format the plugin cannot set (Dropdown options, Button labels): swap
// the characters markup is made of for look-alikes, so AutoText never sees a
// tag or an entity.
function neutralize(text) {
  return String(text || "")
    .replace(/</g, "‹")
    .replace(/>/g, "›")
    .replace(/&/g, "＆")
    .replace(/[\u0000-\u001f\u007f]/g, " ")
}

// "%1 in %2 min" with ["Standup", 6] -> "Standup in 6 min".
function fill(template, args) {
  return String(template).replace(/%(\d)/g, function(m, n) {
    var v = args[Number(n) - 1]
    return v === undefined ? m : String(v)
  })
}

// The bar label, never longer than `max` characters: the title gives way so
// the time part ("in 6 min", "14:30") always stays whole.
function fitLabel(template, args, titleIndex, max) {
  var marker = "\u0001"
  var a = args.slice()
  a[titleIndex] = marker
  var base = fill(template, a)
  var budget = Math.max(4, max - (base.length - 1))
  return base.replace(marker, function() { return truncate(args[titleIndex], budget) })
}
