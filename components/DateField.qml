import QtQuick
import QtQuick.Controls as QQC
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// A date typed as dd/mm/yyyy, or picked from a month grid.
//
// Typing stays the fast path. The calendar button (or Down / Alt+Down in
// the field) opens the grid: arrows move by day and week, Page Up / Page
// Down by month, Home goes back to today, Enter picks, Esc closes.
Item {
  id: root

  property var panel
  property alias text: field.text
  property alias placeholderText: field.placeholderText
  readonly property alias fieldActiveFocus: field.activeFocus

  signal accepted()
  signal escaped()

  implicitHeight: field.implicitHeight

  function forceActiveFocus() { field.forceActiveFocus() }

  // The day the typed text names, or NaN.
  function typedDay() { return Model.parseDateField(field.text) }

  function openPicker() {
    var d = root.typedDay()
    picker.selected = isNaN(d) ? Model.startOfDay(Date.now()) : d
    picker.showMonth(picker.selected)
    popup.open()
  }

  function pick(dayMs) {
    field.text = Model.dateFieldText(dayMs)
    popup.close()
    field.forceActiveFocus()
  }

  TextField {
    id: field
    anchors.left: parent.left
    anchors.right: button.left
    anchors.rightMargin: Style.space(4)
    foreground: root.panel.fg
    font.family: root.panel.fontFamily
    maximumLength: 10
    inputMethodHints: Qt.ImhDate
    onActiveFocusChanged: if (activeFocus) root.panel.editing = true
    onAccepted: root.accepted()
    Keys.onEscapePressed: function(event) { event.accepted = true; root.escaped() }
    Keys.onDownPressed: function(event) { event.accepted = true; root.openPicker() }
  }

  Button {
    id: button
    anchors.right: parent.right
    anchors.verticalCenter: field.verticalCenter
    width: height
    height: field.height
    iconText: "\u{F00ED}"
    foreground: root.panel.fg
    fontFamily: root.panel.fontFamily
    bordered: true
    activeFocusOnTab: false
    onClicked: popup.opened ? popup.close() : root.openPicker()
  }

  QQC.Popup {
    id: popup
    y: field.height + Style.space(2)
    x: Math.max(0, root.width - width)
    width: 7 * picker.cell + leftPadding + rightPadding
    padding: Style.space(6)
    closePolicy: QQC.Popup.CloseOnPressOutsideParent | QQC.Popup.CloseOnEscape
    focus: true

    background: Rectangle {
      color: Commons.Color.popups.background
      border.color: Commons.Color.popups.border
      border.width: Style.normalBorderWidth
      radius: Style.cornerRadius
    }

    onOpened: grid.forceActiveFocus()

    contentItem: Column {
      id: picker
      spacing: Style.space(4)

      // Local midnight of the highlighted day, and the month shown.
      property double selected: 0
      property int year: 2000
      property int month: 0
      readonly property real cell: Style.space(30)
      readonly property int weekStart: root.panel.locale.firstDayOfWeek % 7
      readonly property double today: Model.startOfDay(Date.now())
      // The first cell of the 6-week grid.
      readonly property double gridStart: {
        var first = new Date(year, month, 1)
        var back = (first.getDay() - weekStart + 7) % 7
        return new Date(year, month, 1 - back).getTime()
      }

      function showMonth(dayMs) {
        var d = new Date(dayMs)
        year = d.getFullYear()
        month = d.getMonth()
      }

      function step(days) {
        selected = Model.addDays(selected, days)
        showMonth(selected)
      }

      function moveMonths(n) {
        var d = new Date(selected)
        var target = new Date(d.getFullYear(), d.getMonth() + n, 1)
        var last = new Date(target.getFullYear(), target.getMonth() + 1, 0).getDate()
        selected = new Date(target.getFullYear(), target.getMonth(), Math.min(d.getDate(), last)).getTime()
        showMonth(selected)
      }

      Item {
        width: grid.width
        height: Style.space(26)

        Button {
          id: prev
          anchors.left: parent.left
          width: height
          height: parent.height
          iconText: "\u{F0141}"
          foreground: root.panel.fg
          fontFamily: root.panel.fontFamily
          activeFocusOnTab: false
          onClicked: picker.moveMonths(-1)
        }
        PlainLabel {
          anchors.centerIn: parent
          text: new Date(picker.year, picker.month, 1).toLocaleDateString(root.panel.locale, "MMMM yyyy")
          color: root.panel.fg
          font.family: root.panel.fontFamily
          font.bold: true
        }
        Button {
          anchors.right: parent.right
          width: height
          height: parent.height
          iconText: "\u{F0142}"
          foreground: root.panel.fg
          fontFamily: root.panel.fontFamily
          activeFocusOnTab: false
          onClicked: picker.moveMonths(1)
        }
      }

      Row {
        Repeater {
          model: 7
          PlainLabel {
            required property int index
            width: picker.cell
            horizontalAlignment: Text.AlignHCenter
            text: root.panel.locale.dayName((picker.weekStart + index) % 7, Locale.ShortFormat).slice(0, 2)
            color: Util.alpha(root.panel.fg, 0.55)
            font.family: root.panel.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }

      Grid {
        id: grid
        columns: 7
        focus: true

        Keys.onPressed: function(event) {
          var k = event.key
          if (k === Qt.Key_Left || event.text === "h") picker.step(-1)
          else if (k === Qt.Key_Right || event.text === "l") picker.step(1)
          else if (k === Qt.Key_Up || event.text === "k") picker.step(-7)
          else if (k === Qt.Key_Down || event.text === "j") picker.step(7)
          else if (k === Qt.Key_PageUp) picker.moveMonths(-1)
          else if (k === Qt.Key_PageDown) picker.moveMonths(1)
          else if (k === Qt.Key_Home) { picker.selected = picker.today; picker.showMonth(picker.today) }
          else if (k === Qt.Key_Return || k === Qt.Key_Enter || k === Qt.Key_Space) root.pick(picker.selected)
          else if (k === Qt.Key_Escape) { popup.close(); field.forceActiveFocus() }
          else return
          event.accepted = true
        }

        Repeater {
          model: 42
          Rectangle {
            id: dayCell
            required property int index
            readonly property double day: Model.addDays(picker.gridStart, index)
            readonly property var date: new Date(day)
            readonly property bool inMonth: date.getMonth() === picker.month
            readonly property bool isSelected: day === picker.selected
            readonly property bool isToday: day === picker.today

            width: picker.cell
            height: picker.cell
            radius: Style.cornerRadius
            color: isSelected ? Commons.Color.accent
                 : (hover.hovered ? Style.hoverFillFor(root.panel.fg, Commons.Color.accent) : "transparent")
            border.width: isToday && !isSelected ? Style.normalBorderWidth : 0
            border.color: Commons.Color.accent

            PlainLabel {
              anchors.centerIn: parent
              text: dayCell.date.getDate()
              color: dayCell.isSelected ? Commons.Color.popups.background
                   : Util.alpha(root.panel.fg, dayCell.inMonth ? 1 : 0.35)
              font.family: root.panel.fontFamily
              font.bold: dayCell.isSelected || dayCell.isToday
            }

            HoverHandler { id: hover; cursorShape: Qt.PointingHandCursor }
            TapHandler { onTapped: root.pick(dayCell.day) }
          }
        }
      }
    }
  }
}
