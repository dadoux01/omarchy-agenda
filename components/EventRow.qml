import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// One event in the agenda. Collapsed: colour, time, title, place. Expanded
// (click or Enter): notes, and the Join / Delete actions.
Item {
  id: root

  property var panel
  property var event: ({})
  property bool selected: false
  property bool expanded: false
  property double dayStart: 0

  signal clicked()
  signal joinRequested()
  signal deleteRequested()

  readonly property bool ended: !event.allDay && event.end <= panel.now
  readonly property bool running: !event.allDay && event.start <= panel.now && event.end > panel.now
  readonly property color dot: Model.isHexColor(event.color) ? event.color : Commons.Color.accent
  readonly property real dim: ended ? 0.45 : 1.0
  readonly property string meeting: Model.safeMeetingUrl(event.meetingUrl)

  readonly property string timeLabel: {
    if (event.allDay) return panel.strings.t("panel.allDay")
    // An event that began on an earlier day shows when it ends instead.
    if (event.start < dayStart) return panel.strings.t("panel.until").arg(Model.timeText(event.end, panel.use24h))
    return Model.timeText(event.start, panel.use24h)
  }

  readonly property string secondLine: {
    var parts = []
    if (running) parts.push(panel.strings.t("panel.now"))
    else if (!event.allDay && event.start >= dayStart) parts.push(Model.timeText(event.start, panel.use24h) + " – " + Model.timeText(event.end, panel.use24h))
    if (event.location) parts.push(event.location)
    if (panel.multipleCalendars && event.calendarName) parts.push(event.calendarName)
    return parts.join("  ·  ")
  }

  width: parent ? parent.width : 0
  height: body.implicitHeight + Style.space(10)

  Rectangle {
    anchors.fill: parent
    radius: Style.cornerRadius
    color: root.selected || mouse.containsMouse
      ? Util.alpha(root.panel.fg, root.selected ? 0.09 : 0.05)
      : "transparent"
  }

  MouseArea {
    id: mouse
    anchors.fill: parent
    hoverEnabled: true
    cursorShape: Qt.PointingHandCursor
    onClicked: root.clicked()
  }

  Rectangle {
    id: stripe
    x: Style.space(4)
    y: Style.space(6)
    width: Style.space(4)
    height: parent.height - Style.space(12)
    radius: width / 2
    color: root.dot
    opacity: root.dim
  }

  Column {
    id: body
    x: stripe.x + stripe.width + Style.space(10)
    y: Style.space(5)
    width: parent.width - x - Style.space(6)
    spacing: Style.space(3)
    opacity: root.dim

    Row {
      width: parent.width
      spacing: Style.space(10)

      PlainLabel {
        id: timeText
        width: Style.space(62)
        text: root.timeLabel
        color: root.running ? Commons.Color.accent : root.panel.fg
        font.family: root.panel.fontFamily
        font.pixelSize: Style.font.bodySmall
        font.bold: root.running
      }

      PlainLabel {
        id: titleText
        width: parent.width - timeText.width - icons.width - (icons.width > 0 ? Style.space(20) : Style.space(10))
        text: String(root.event.title || "…").replace(/[\r\n]+/g, " ")
        color: root.panel.fg
        font.family: root.panel.fontFamily
      }

      Row {
        id: icons
        spacing: Style.space(4)

        PlainLabel {
          visible: root.meeting !== ""
          text: "\u{F0567}"
          color: Commons.Color.accent
          font.family: root.panel.fontFamily
          font.pixelSize: Style.font.bodySmall
        }
        PlainLabel {
          visible: root.event.recurring === true
          text: "\u{F0456}"
          color: Util.alpha(root.panel.fg, 0.6)
          font.family: root.panel.fontFamily
          font.pixelSize: Style.font.bodySmall
        }
        PlainLabel {
          visible: (root.event.alarms || []).length > 0
          text: "\u{F009A}"
          color: Util.alpha(root.panel.fg, 0.6)
          font.family: root.panel.fontFamily
          font.pixelSize: Style.font.bodySmall
        }
      }
    }

    PlainLabel {
      visible: root.secondLine !== ""
      x: timeText.width + Style.space(10)
      width: parent.width - x
      text: root.secondLine
      color: Util.alpha(root.panel.fg, 0.65)
      font.family: root.panel.fontFamily
      font.pixelSize: Style.font.caption
    }

    // ---- Details
    Column {
      visible: root.expanded
      x: timeText.width + Style.space(10)
      width: parent.width - x
      spacing: Style.space(6)
      topPadding: Style.space(4)

      PlainLabel {
        visible: (root.event.description || "") !== ""
        width: parent.width
        text: root.event.description || ""
        color: Util.alpha(root.panel.fg, 0.8)
        font.family: root.panel.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.Wrap
        maximumLineCount: 6
      }

      PlainLabel {
        width: parent.width
        text: {
          var bits = []
          if (root.event.recurring) bits.push(root.panel.strings.t("panel.recurring"))
          var n = (root.event.alarms || []).length
          if (n > 0) bits.push(root.panel.strings.tn("panel.reminders", n).arg(Commons.I18n.formatNumber(n)))
          if (root.event.readOnly) bits.push(root.panel.strings.t("panel.readOnly"))
          if (root.event.accountLabel) bits.push(root.event.accountLabel + (root.event.calendarName ? " › " + root.event.calendarName : ""))
          return bits.join("  ·  ")
        }
        color: Util.alpha(root.panel.fg, 0.55)
        font.family: root.panel.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.Wrap
      }

      Row {
        spacing: Style.space(6)

        Button {
          visible: root.meeting !== ""
          text: root.panel.strings.t("panel.join")
          iconText: "\u{F0567}"
          foreground: Commons.Color.accent
          fontFamily: root.panel.fontFamily
          bordered: true
          onClicked: root.joinRequested()
        }
        Button {
          visible: !root.event.readOnly
          text: root.panel.strings.t("panel.delete")
          iconText: "\u{F01B4}"
          foreground: Commons.Color.urgent
          fontFamily: root.panel.fontFamily
          bordered: true
          onClicked: root.deleteRequested()
        }
      }
    }
  }
}
