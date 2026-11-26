import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../components"
import "../Model.js" as Model

// Add an account: provider, then credentials (or a browser sign-in), then
// the calendars to show. The flow's state lives on the panel (panel.add*),
// not here: signing in to Google means clicking into the browser, which
// closes the popup, and the flow has to be where the user left it after.
Column {
  id: root

  property var panel
  readonly property var flow: panel.addFlow

  spacing: Style.space(10)

  function reset() {
    userField.text = ""
    passField.text = ""
    serverField.text = ""
    labelField.text = ""
    urlField.text = ""
    pathField.text = ""
  }

  function pick(kind) {
    root.panel.setAddFlow({ step: "credentials", kind: kind, error: "" })
    if (kind === "google") root.panel.loadClientFiles()
    Qt.callLater(function() {
      if (kind === "ics") urlField.forceActiveFocus()
      else if (kind !== "google") userField.forceActiveFocus()
    })
  }

  function connectPassword() {
    var password = passField.text
    passField.text = ""
    root.panel.connectAccount({
      kind: flow.kind,
      server: flow.kind === "icloud" ? "" : serverField.text,
      username: userField.text,
      password: password,
      label: labelField.text
    })
  }

  function connectIcs() {
    root.panel.connectAccount({ kind: "ics", url: urlField.text, label: labelField.text })
  }

  function connectGoogle(path) {
    root.panel.connectAccount({ kind: "google", clientFile: path, label: labelField.text })
  }

  PageHeader {
    panel: root.panel
    title: root.panel.strings.t("add.title")
  }

  // ---- Step 1: provider
  Repeater {
    model: root.flow.step !== "provider" ? [] : [
      { id: "icloud", title: root.panel.strings.t("add.icloud"), hint: root.panel.strings.t("add.icloudHint"), glyph: "\u{F0035}" },
      { id: "google", title: root.panel.strings.t("add.google"), hint: root.panel.strings.t("add.googleHint"), glyph: "\u{F02AD}" },
      { id: "caldav", title: root.panel.strings.t("add.caldav"), hint: root.panel.strings.t("add.caldavHint"), glyph: "\u{F00ED}" },
      { id: "ics", title: root.panel.strings.t("add.ics"), hint: root.panel.strings.t("add.icsHint"), glyph: "\u{F0337}" },
      { id: "microsoft", title: root.panel.strings.t("add.microsoft"), hint: root.panel.strings.t("add.soon"), glyph: "\u{F05AA}", soon: true }
    ]

    Rectangle {
      id: providerCard
      required property var modelData
      readonly property bool ready: modelData.soon !== true
      width: root.width
      height: providerColumn.implicitHeight + Style.space(16)
      radius: Style.cornerRadius
      color: providerMouse.containsMouse && ready ? Util.alpha(root.panel.fg, 0.08) : Util.alpha(root.panel.fg, 0.03)
      opacity: ready ? 1 : 0.45

      MouseArea {
        id: providerMouse
        anchors.fill: parent
        hoverEnabled: true
        enabled: providerCard.ready
        cursorShape: Qt.PointingHandCursor
        onClicked: root.pick(providerCard.modelData.id)
      }

      PlainLabel {
        id: providerGlyph
        x: Style.space(12)
        anchors.verticalCenter: parent.verticalCenter
        text: providerCard.modelData.glyph
        color: root.panel.fg
        font.family: root.panel.fontFamily
        font.pixelSize: Style.font.icon
      }

      Column {
        id: providerColumn
        anchors.left: providerGlyph.right
        anchors.leftMargin: Style.space(12)
        anchors.right: parent.right
        anchors.rightMargin: Style.space(12)
        anchors.verticalCenter: parent.verticalCenter
        spacing: Style.space(2)

        PlainLabel {
          width: parent.width
          text: providerCard.modelData.title
          color: root.panel.fg
          font.family: root.panel.fontFamily
        }
        PlainLabel {
          width: parent.width
          text: providerCard.modelData.hint
          color: Util.alpha(root.panel.fg, 0.55)
          font.family: root.panel.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.Wrap
        }
      }
    }
  }

  component Field: TextField {
    width: root.width
    foreground: root.panel.fg
    font.family: root.panel.fontFamily
    Keys.onEscapePressed: function(event) {
      event.accepted = true
      root.reset()
      root.panel.setAddFlow({ step: "provider", error: "" })
    }
  }

  component Hint: PlainLabel {
    width: root.width
    color: Util.alpha(root.panel.fg, 0.55)
    font.family: root.panel.fontFamily
    font.pixelSize: Style.font.caption
    wrapMode: Text.Wrap
  }

  component Heading: PlainLabel {
    width: root.width
    color: root.panel.fg
    font.family: root.panel.fontFamily
    font.bold: true
  }

  // ---- Step 2a: user name + password (iCloud, CalDAV)
  Column {
    visible: root.flow.step === "credentials" && (root.flow.kind === "icloud" || root.flow.kind === "caldav")
    width: root.width
    spacing: Style.space(8)

    Heading { text: root.flow.kind === "icloud" ? root.panel.strings.t("add.icloud") : root.panel.strings.t("add.caldav") }
    Field {
      id: userField
      placeholderText: root.flow.kind === "icloud" ? root.panel.strings.t("add.email") : root.panel.strings.t("add.username")
      maximumLength: 320
      inputMethodHints: Qt.ImhEmailCharactersOnly | Qt.ImhNoAutoUppercase
      onAccepted: passField.forceActiveFocus()
    }
    Field {
      id: serverField
      visible: root.flow.kind !== "icloud"
      placeholderText: root.panel.strings.t("add.server")
      maximumLength: 500
      inputMethodHints: Qt.ImhUrlCharactersOnly | Qt.ImhNoAutoUppercase
    }
    Field {
      id: passField
      password: true
      placeholderText: root.flow.kind === "icloud" ? root.panel.strings.t("add.password") : root.panel.strings.t("add.passwordGeneric")
      maximumLength: 1024
      inputMethodHints: Qt.ImhSensitiveData | Qt.ImhNoPredictiveText
      onAccepted: if (!root.panel.busy && userField.text.trim() !== "" && passField.text !== "") root.connectPassword()
    }
    Field {
      id: labelField
      placeholderText: root.panel.strings.t("add.label")
      maximumLength: 80
    }
    Hint { text: root.flow.kind === "icloud" ? root.panel.strings.t("add.guideIcloud") : root.panel.strings.t("add.guideCaldav") }
    Button {
      width: parent.width
      text: root.panel.busy ? root.panel.strings.t("add.connecting") : root.panel.strings.t("add.connect")
      enabled: !root.panel.busy && userField.text.trim() !== "" && passField.text !== ""
      foreground: Commons.Color.accent
      fontFamily: root.panel.fontFamily
      bordered: true
      onClicked: root.connectPassword()
    }
  }

  // ---- Step 2b: a calendar link
  Column {
    visible: root.flow.step === "credentials" && root.flow.kind === "ics"
    width: root.width
    spacing: Style.space(8)

    Heading { text: root.panel.strings.t("add.ics") }
    Field {
      id: urlField
      placeholderText: root.panel.strings.t("add.icsUrl")
      maximumLength: 4000
      inputMethodHints: Qt.ImhUrlCharactersOnly | Qt.ImhNoAutoUppercase | Qt.ImhSensitiveData
      onAccepted: if (!root.panel.busy && text.trim()) root.connectIcs()
    }
    Hint { text: root.panel.strings.t("add.guideIcs") }
    Button {
      width: parent.width
      text: root.panel.busy ? root.panel.strings.t("add.connecting") : root.panel.strings.t("add.connect")
      enabled: !root.panel.busy && urlField.text.trim() !== ""
      foreground: Commons.Color.accent
      fontFamily: root.panel.fontFamily
      bordered: true
      onClicked: root.connectIcs()
    }
  }

  // ---- Step 2c: Google, with the user's own OAuth client
  Column {
    visible: root.flow.step === "credentials" && root.flow.kind === "google"
    width: root.width
    spacing: Style.space(8)

    Heading { text: root.panel.strings.t("add.google") }
    Hint { text: root.panel.strings.t("add.guideGoogle") }

    PlainLabel {
      visible: root.panel.clientFiles.length > 0
      width: parent.width
      text: root.panel.strings.t("add.googleFound")
      color: root.panel.fg
      font.family: root.panel.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    Repeater {
      model: root.panel.clientFiles

      Button {
        required property var modelData
        width: root.width
        leftAlign: true
        text: Model.neutralize(modelData.name)
        iconText: "\u{F0214}"
        enabled: !root.panel.busy
        foreground: root.panel.fg
        fontFamily: root.panel.fontFamily
        bordered: true
        onClicked: root.connectGoogle(modelData.path)
      }
    }

    Field {
      id: pathField
      placeholderText: root.panel.strings.t("add.googlePath")
      maximumLength: 1000
      inputMethodHints: Qt.ImhNoAutoUppercase | Qt.ImhNoPredictiveText
      onAccepted: if (!root.panel.busy && text.trim()) root.connectGoogle(text.trim())
    }

    Row {
      width: parent.width
      spacing: Style.space(6)

      Button {
        width: (parent.width - Style.space(6)) / 2
        text: root.panel.strings.t("add.googleRefresh")
        iconText: "\u{F0450}"
        enabled: !root.panel.busy
        foreground: root.panel.fg
        fontFamily: root.panel.fontFamily
        bordered: true
        onClicked: root.panel.loadClientFiles()
      }
      Button {
        width: (parent.width - Style.space(6)) / 2
        text: root.panel.strings.t("add.googleSignIn")
        iconText: "\u{F02AD}"
        enabled: !root.panel.busy && pathField.text.trim() !== ""
        foreground: Commons.Color.accent
        fontFamily: root.panel.fontFamily
        bordered: true
        onClicked: root.connectGoogle(pathField.text.trim())
      }
    }
  }

  // ---- Waiting for the browser
  Column {
    visible: root.flow.step === "browser"
    width: root.width
    spacing: Style.space(10)
    topPadding: Style.space(6)

    PlainLabel {
      width: parent.width
      horizontalAlignment: Text.AlignHCenter
      text: root.panel.strings.t("add.googleWaiting")
      color: root.panel.fg
      font.family: root.panel.fontFamily
      wrapMode: Text.Wrap
    }
    Hint {
      horizontalAlignment: Text.AlignHCenter
      text: root.panel.strings.t("add.googleWaitingHint")
    }
    Button {
      anchors.horizontalCenter: parent.horizontalCenter
      text: root.panel.strings.t("panel.cancel")
      foreground: root.panel.fg
      fontFamily: root.panel.fontFamily
      bordered: true
      onClicked: root.panel.cancelLogin()
    }
  }

  // ---- Step 3: calendars
  Column {
    visible: root.flow.step === "calendars"
    width: root.width
    spacing: Style.space(6)

    Heading { text: root.flow.label || "" }
    PlainLabel {
      width: parent.width
      text: root.panel.strings.t("add.choose")
      color: Util.alpha(root.panel.fg, 0.7)
      font.family: root.panel.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    Repeater {
      model: root.flow.found || []

      Item {
        id: foundRow
        required property var modelData
        width: root.width
        height: Math.max(foundName.implicitHeight, foundToggle.height) + Style.space(4)

        Rectangle {
          id: foundSwatch
          x: Style.space(6)
          anchors.verticalCenter: parent.verticalCenter
          width: Style.space(10)
          height: width
          radius: width / 2
          color: Model.isHexColor(foundRow.modelData.color) ? foundRow.modelData.color : Commons.Color.accent
        }
        PlainLabel {
          id: foundName
          anchors.left: foundSwatch.right
          anchors.leftMargin: Style.space(10)
          anchors.right: foundToggle.left
          anchors.rightMargin: Style.space(8)
          anchors.verticalCenter: parent.verticalCenter
          text: foundRow.modelData.name + (foundRow.modelData.readOnly ? "  ·  " + root.panel.strings.t("panel.readOnly") : "")
          color: root.panel.fg
          font.family: root.panel.fontFamily
          font.pixelSize: Style.font.bodySmall
        }
        ToggleSwitch {
          id: foundToggle
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          checked: root.flow.chosen[foundRow.modelData.id] === true
          foreground: root.panel.fg
          onToggled: root.panel.toggleFound(foundRow.modelData.id)
        }
      }
    }

    Button {
      width: parent.width
      text: root.panel.busy ? root.panel.strings.t("panel.working") : root.panel.strings.t("add.save")
      iconText: "\u{F012C}"
      enabled: !root.panel.busy
      foreground: Commons.Color.accent
      fontFamily: root.panel.fontFamily
      bordered: true
      onClicked: root.panel.saveFoundAccount()
    }
  }

  PlainLabel {
    visible: (root.flow.error || "") !== ""
    width: root.width
    text: root.flow.error || ""
    color: Commons.Color.urgent
    font.family: root.panel.fontFamily
    font.pixelSize: Style.font.bodySmall
    wrapMode: Text.Wrap
  }
}
