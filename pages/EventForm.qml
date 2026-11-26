import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../components"
import "../Model.js" as Model

// New event. Dates are typed (dd/mm/yyyy) rather than picked: the panel is
// keyboard-first, and Tab walks the fields in reading order.
Column {
  id: root

  property var panel
  property string error: ""

  spacing: Style.space(8)

  readonly property var reminderChoices: [-1, 0, 5, 10, 15, 30, 60, 120, 1440, 2880]

  function reminderLabel(m) {
    var s = root.panel.strings
    if (m < 0) return s.t("form.reminderNone")
    if (m === 0) return s.t("form.reminderAtStart")
    if (m % 1440 === 0) return s.tn("form.reminderDays", m / 1440).arg(m / 1440)
    if (m % 60 === 0) return s.tn("form.reminderHours", m / 60).arg(m / 60)
    return s.tn("form.reminderMinutes", m).arg(m)
  }

  // Fill from quick add or a fresh default.
  function reset(preset) {
    error = ""
    var now = Date.now()
    var start = preset && preset.start ? preset.start : Model.defaultStart(now)
    var end = preset && preset.end ? preset.end : start + Model.HOUR
    titleField.text = preset && preset.title ? preset.title : ""
    allDay.checked = !!(preset && preset.allDay)
    dateField.text = Model.dateFieldText(start)
    endDateField.text = Model.dateFieldText(allDay.checked ? end - 1 : end)
    startField.text = Model.timeFieldText(start)
    endField.text = Model.timeFieldText(end)
    locationField.text = ""
    notesField.text = ""
    var def = Number(root.panel.settingsValue("defaultReminderMinutes", 10))
    reminder.value = String(root.reminderChoices.indexOf(def) !== -1 ? def : 10)
    calendar.value = root.panel.defaultCalendarKey
    Qt.callLater(function() { titleField.forceActiveFocus(); titleField.cursorPosition = titleField.text.length })
  }

  function submit() {
    error = ""
    var day = Model.parseDateField(dateField.text)
    if (isNaN(day)) { error = root.panel.strings.t("form.badDate"); return }
    var start, end
    if (allDay.checked) {
      var last = Model.parseDateField(endDateField.text || dateField.text)
      if (isNaN(last)) { error = root.panel.strings.t("form.badDate"); return }
      if (last < day) { error = root.panel.strings.t("form.endBeforeStart"); return }
      start = day
      end = Model.addDays(last, 1)
    } else {
      var st = Model.parseTimeField(startField.text)
      var et = Model.parseTimeField(endField.text)
      if (!st || !et) { error = root.panel.strings.t("form.badTime"); return }
      start = Model.at(day, st)
      end = Model.at(day, et)
      if (end <= start) end = Model.at(Model.addDays(day, 1), et)
    }
    var target = root.panel.calendarByKey(calendar.value)
    if (!target) { error = root.panel.strings.t("form.noCalendar"); return }
    var m = Number(reminder.value)
    root.panel.createEvent({
      accountId: target.accountId,
      calendarId: target.calendarId,
      title: titleField.text,
      start: start,
      end: end,
      allDay: allDay.checked,
      location: locationField.text,
      description: notesField.text,
      reminders: m >= 0 ? [m] : []
    }, function(err) { root.error = err })
  }

  PageHeader {
    panel: root.panel
    title: root.panel.strings.t("form.title")
  }

  component Field: TextField {
    width: root.width
    foreground: root.panel.fg
    font.family: root.panel.fontFamily
    onActiveFocusChanged: if (activeFocus) root.panel.editing = true
    Keys.onEscapePressed: function(event) { event.accepted = true; root.panel.openPage("agenda") }
    onAccepted: if (!root.panel.busy && titleField.text.trim() !== "") root.submit()
  }

  component Caption: PlainLabel {
    color: Util.alpha(root.panel.fg, 0.6)
    font.family: root.panel.fontFamily
    font.pixelSize: Style.font.caption
  }

  Field {
    id: titleField
    placeholderText: root.panel.strings.t("form.name")
    maximumLength: 300
  }

  Toggle {
    id: allDay
    width: root.width
    label: root.panel.strings.t("form.allDay")
    foreground: root.panel.fg
    fontFamily: root.panel.fontFamily
    onClicked: checked = !checked
  }

  Row {
    width: root.width
    spacing: Style.space(8)

    Column {
      width: allDay.checked ? (root.width - Style.space(8)) / 2 : root.width * 0.44
      spacing: Style.space(3)
      Caption { text: root.panel.strings.t("form.date") }
      Field { id: dateField; width: parent.width; maximumLength: 10; inputMethodHints: Qt.ImhDate }
    }
    Column {
      visible: allDay.checked
      width: (root.width - Style.space(8)) / 2
      spacing: Style.space(3)
      Caption { text: root.panel.strings.t("form.endDate") }
      Field { id: endDateField; width: parent.width; maximumLength: 10 }
    }
    Column {
      visible: !allDay.checked
      width: (root.width * 0.56 - Style.space(16)) / 2
      spacing: Style.space(3)
      Caption { text: root.panel.strings.t("form.start") }
      Field { id: startField; width: parent.width; maximumLength: 8 }
    }
    Column {
      visible: !allDay.checked
      width: (root.width * 0.56 - Style.space(16)) / 2
      spacing: Style.space(3)
      Caption { text: root.panel.strings.t("form.end") }
      Field { id: endField; width: parent.width; maximumLength: 8 }
    }
  }

  Field {
    id: locationField
    placeholderText: root.panel.strings.t("form.location")
    maximumLength: 300
  }

  Field {
    id: notesField
    placeholderText: root.panel.strings.t("form.description")
    maximumLength: 2000
  }

  Dropdown {
    id: calendar
    width: root.width
    label: root.panel.strings.t("form.calendar")
    options: root.panel.writableCalendars.map(function(c) {
      return { value: c.key, label: Model.neutralize(c.label) }
    })
    fontFamily: root.panel.fontFamily
    onChanged: function(v) { calendar.value = v }
  }

  Dropdown {
    id: reminder
    width: root.width
    label: root.panel.strings.t("form.reminder")
    options: root.reminderChoices.map(function(m) { return { value: String(m), label: root.reminderLabel(m) } })
    fontFamily: root.panel.fontFamily
    onChanged: function(v) { reminder.value = v }
  }

  PlainLabel {
    visible: root.error !== ""
    width: root.width
    text: root.error
    color: Commons.Color.urgent
    font.family: root.panel.fontFamily
    font.pixelSize: Style.font.bodySmall
    wrapMode: Text.Wrap
  }

  Button {
    width: root.width
    text: root.panel.busy ? root.panel.strings.t("panel.working") : root.panel.strings.t("form.save")
    iconText: "\u{F012C}"
    enabled: !root.panel.busy && titleField.text.trim() !== ""
    foreground: Commons.Color.accent
    fontFamily: root.panel.fontFamily
    bordered: true
    onClicked: root.submit()
  }
}
