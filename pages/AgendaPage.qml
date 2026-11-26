import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../components"
import "../Model.js" as Model

// The default page: the next days, grouped, with quick add on top.
Column {
  id: root

  property var panel
  property alias quickField: quick

  spacing: Style.space(8)

  // ---- Header
  Item {
    width: parent.width
    height: Math.max(heading.implicitHeight, actions.height)

    PanelSectionHeader {
      id: heading
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      text: root.panel.strings.t("panel.title")
      foreground: root.panel.fg
      fontFamily: root.panel.fontFamily
    }

    Row {
      id: actions
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      spacing: Style.space(2)

      PanelActionButton {
        iconText: "\u{F0415}"
        tooltipText: root.panel.strings.t("panel.add")
        foreground: root.panel.fg
        fontFamily: root.panel.fontFamily
        enabled: root.panel.writableCalendars.length > 0
        onClicked: root.panel.openPage("form")
      }
      PanelActionButton {
        iconText: "\u{F0450}"
        tooltipText: root.panel.strings.t("panel.sync")
        foreground: root.panel.fg
        fontFamily: root.panel.fontFamily
        enabled: root.panel.accounts.length > 0
        rotation: root.panel.syncing ? spin.angle : 0
        onClicked: root.panel.syncNow()
      }
      PanelActionButton {
        iconText: "\u{F000E}"
        tooltipText: root.panel.strings.t("panel.accounts")
        foreground: root.panel.fg
        fontFamily: root.panel.fontFamily
        onClicked: root.panel.openPage("accounts")
      }
      PanelActionButton {
        iconText: "\u{F0493}"
        tooltipText: root.panel.strings.t("panel.settings")
        foreground: root.panel.fg
        fontFamily: root.panel.fontFamily
        onClicked: root.panel.openPage("settings")
      }
    }

    QtObject {
      id: spin
      property real angle: 0
      property NumberAnimation anim: NumberAnimation {
        target: spin
        property: "angle"
        from: 0
        to: 360
        duration: 900
        loops: Animation.Infinite
        running: root.panel.syncing && root.panel.opened
      }
    }
  }

  // ---- No account yet
  Column {
    visible: root.panel.accounts.length === 0
    width: parent.width
    spacing: Style.space(10)
    topPadding: Style.space(10)
    bottomPadding: Style.space(10)

    PlainLabel {
      width: parent.width
      horizontalAlignment: Text.AlignHCenter
      text: root.panel.backendUp ? root.panel.strings.t("panel.noAccounts") : root.panel.strings.t("panel.backendDown")
      color: Util.alpha(root.panel.fg, 0.7)
      font.family: root.panel.fontFamily
      wrapMode: Text.Wrap
    }
    Button {
      visible: root.panel.backendUp
      anchors.horizontalCenter: parent.horizontalCenter
      text: root.panel.strings.t("panel.connect")
      iconText: "\u{F0415}"
      foreground: Commons.Color.accent
      fontFamily: root.panel.fontFamily
      bordered: true
      onClicked: root.panel.openPage("add")
    }
  }

  // ---- Quick add
  TextField {
    id: quick
    visible: root.panel.writableCalendars.length > 0
    width: parent.width
    placeholderText: root.panel.strings.t("panel.quickAdd")
    foreground: root.panel.fg
    font.family: root.panel.fontFamily
    maximumLength: 300
    onActiveFocusChanged: root.panel.editing = activeFocus
    Keys.onEscapePressed: function(event) { event.accepted = true; text = ""; root.panel.focusList() }
    Keys.onTabPressed: root.panel.quickToForm(text)
    onAccepted: if (!root.panel.busy && text.trim() !== "") root.panel.quickAdd(text)
  }

  PlainLabel {
    visible: quick.activeFocus && quick.text.length > 0
    width: parent.width
    text: root.panel.strings.t("panel.quickAddHint").arg(root.panel.defaultCalendarName)
    color: Util.alpha(root.panel.fg, 0.55)
    font.family: root.panel.fontFamily
    font.pixelSize: Style.font.caption
  }

  // ---- Days
  Repeater {
    model: root.panel.accounts.length > 0 ? root.panel.groups : []

    Column {
      id: dayColumn
      required property var modelData
      width: root.width
      spacing: Style.space(2)

      Item {
        width: parent.width
        height: dayLabel.implicitHeight + Style.space(6)

        PlainLabel {
          id: dayLabel
          anchors.bottom: parent.bottom
          anchors.bottomMargin: Style.space(2)
          text: root.panel.dayTitle(dayColumn.modelData)
          color: dayColumn.modelData.offset === 0 ? Commons.Color.accent : Util.alpha(root.panel.fg, 0.75)
          font.family: root.panel.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
          font.capitalization: Font.AllUppercase
        }
      }

      PlainLabel {
        visible: dayColumn.modelData.events.length === 0
        leftPadding: Style.space(18)
        text: root.panel.strings.t("panel.empty")
        color: Util.alpha(root.panel.fg, 0.5)
        font.family: root.panel.fontFamily
        font.pixelSize: Style.font.bodySmall
      }

      Repeater {
        model: dayColumn.modelData.events

        EventRow {
          required property var modelData
          panel: root.panel
          event: modelData
          dayStart: dayColumn.modelData.day
          selected: root.panel.selectedKey === root.panel.rowKey(dayColumn.modelData, modelData)
          expanded: root.panel.expandedKey === root.panel.rowKey(dayColumn.modelData, modelData)
          onClicked: root.panel.toggleExpanded(root.panel.rowKey(dayColumn.modelData, modelData))
          onJoinRequested: root.panel.join(modelData)
          onDeleteRequested: root.panel.askDelete(modelData)
        }
      }
    }
  }

  // ---- Status
  PlainLabel {
    visible: root.panel.accounts.length > 0
    width: parent.width
    topPadding: Style.space(4)
    text: root.panel.statusText
    color: root.panel.syncErrorCount > 0 ? Commons.Color.urgent : Util.alpha(root.panel.fg, 0.45)
    font.family: root.panel.fontFamily
    font.pixelSize: Style.font.caption
    wrapMode: Text.Wrap
  }
}
