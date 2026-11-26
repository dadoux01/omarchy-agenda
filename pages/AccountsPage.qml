import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../components"
import "../Model.js" as Model

// Connected accounts, the calendars in each one (shown or hidden), removal.
Column {
  id: root

  property var panel

  spacing: Style.space(10)

  PageHeader {
    panel: root.panel
    title: root.panel.strings.t("accounts.title")

    PanelActionButton {
      iconText: "\u{F0415}"
      tooltipText: root.panel.strings.t("accounts.add")
      foreground: root.panel.fg
      fontFamily: root.panel.fontFamily
      onClicked: root.panel.openPage("add")
    }
  }

  PlainLabel {
    visible: root.panel.accounts.length === 0
    width: root.width
    text: root.panel.strings.t("accounts.empty")
    color: Util.alpha(root.panel.fg, 0.6)
    font.family: root.panel.fontFamily
  }

  Repeater {
    model: root.panel.accounts

    Column {
      id: accountColumn
      required property var modelData
      width: root.width
      spacing: Style.space(4)

      Item {
        width: parent.width
        height: Math.max(nameColumn.implicitHeight, removeButton.height)

        Column {
          id: nameColumn
          anchors.left: parent.left
          anchors.right: removeButton.left
          anchors.rightMargin: Style.space(8)
          anchors.verticalCenter: parent.verticalCenter
          spacing: Style.space(1)

          PlainLabel {
            width: parent.width
            text: accountColumn.modelData.label || accountColumn.modelData.username || "…"
            color: root.panel.fg
            font.family: root.panel.fontFamily
            font.bold: true
          }
          PlainLabel {
            width: parent.width
            text: (accountColumn.modelData.username ? accountColumn.modelData.username + "  ·  " : "")
              + root.panel.strings.tn("accounts.calendars", accountColumn.modelData.calendars.length)
                  .arg(accountColumn.modelData.calendars.length)
            color: Util.alpha(root.panel.fg, 0.55)
            font.family: root.panel.fontFamily
            font.pixelSize: Style.font.caption
          }
          PlainLabel {
            visible: accountColumn.modelData.error !== ""
            width: parent.width
            text: root.panel.errorText(accountColumn.modelData.error)
            color: Commons.Color.urgent
            font.family: root.panel.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.Wrap
          }
          Button {
            visible: accountColumn.modelData.provider === "google"
              && accountColumn.modelData.error === "reauth_required"
            text: root.panel.strings.t("accounts.reconnect")
            iconText: "\u{F02AD}"
            enabled: !root.panel.busy
            foreground: Commons.Color.accent
            fontFamily: root.panel.fontFamily
            bordered: true
            onClicked: root.panel.reconnect(accountColumn.modelData)
          }
        }

        PanelActionButton {
          id: removeButton
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          iconText: "\u{F01B4}"
          tooltipText: root.panel.strings.t("accounts.remove")
          enabled: !root.panel.busy
          foreground: root.panel.fg
          hoverColor: Commons.Color.urgent
          fontFamily: root.panel.fontFamily
          onClicked: root.panel.askRemoveAccount(accountColumn.modelData)
        }
      }

      Repeater {
        model: accountColumn.modelData.calendars

        Item {
          id: calRow
          required property var modelData
          width: accountColumn.width
          height: Math.max(calName.implicitHeight, toggle.height) + Style.space(4)

          Rectangle {
            id: swatch
            x: Style.space(6)
            anchors.verticalCenter: parent.verticalCenter
            width: Style.space(10)
            height: width
            radius: width / 2
            color: Model.isHexColor(calRow.modelData.color) ? calRow.modelData.color : Commons.Color.accent
          }

          PlainLabel {
            id: calName
            anchors.left: swatch.right
            anchors.leftMargin: Style.space(10)
            anchors.right: toggle.left
            anchors.rightMargin: Style.space(8)
            anchors.verticalCenter: parent.verticalCenter
            text: calRow.modelData.name + (calRow.modelData.readOnly ? "  ·  " + root.panel.strings.t("panel.readOnly") : "")
            color: Util.alpha(root.panel.fg, calRow.modelData.enabled ? 0.9 : 0.5)
            font.family: root.panel.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          ToggleSwitch {
            id: toggle
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            checked: calRow.modelData.enabled
            enabled: !root.panel.busy
            foreground: root.panel.fg
            onToggled: root.panel.setCalendarEnabled(accountColumn.modelData.id, calRow.modelData.id, !calRow.modelData.enabled)
          }
        }
      }

      PanelSeparator {
        width: parent.width
        foreground: root.panel.fg
      }
    }
  }

  Button {
    width: root.width
    text: root.panel.strings.t("accounts.add")
    iconText: "\u{F0415}"
    foreground: Commons.Color.accent
    fontFamily: root.panel.fontFamily
    bordered: true
    onClicked: root.panel.openPage("add")
  }
}
