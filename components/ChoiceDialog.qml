import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// A modal question with any number of answers, the last one being the safe
// way out. Omarchy's ConfirmDialog has exactly two buttons, and deleting a
// repeating event needs three: this one, the whole series, or neither.
// The message is plain text: it quotes event titles.
Item {
  id: root

  property var panel
  property bool opened: false
  property string message: ""
  // [{ id, label, destructive }]
  property var choices: []
  property int selectedIndex: 0

  signal chosen(string id)

  visible: opened
  onOpenedChanged: if (opened) { selectedIndex = 0; forceActiveFocus() }

  Keys.onPressed: function(event) {
    if (event.key === Qt.Key_Escape) {
      root.chosen("cancel")
    } else if (root.choices.length === 0) {
      return
    } else if (event.key === Qt.Key_Up || event.key === Qt.Key_Left || event.key === Qt.Key_Backtab) {
      root.selectedIndex = (root.selectedIndex + root.choices.length - 1) % root.choices.length
    } else if (event.key === Qt.Key_Down || event.key === Qt.Key_Right || event.key === Qt.Key_Tab) {
      root.selectedIndex = (root.selectedIndex + 1) % root.choices.length
    } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
      if (root.choices[root.selectedIndex]) root.chosen(root.choices[root.selectedIndex].id)
    } else {
      return
    }
    event.accepted = true
  }

  Rectangle {
    anchors.fill: parent
    color: Util.alpha(Commons.Color.background, 0.8)

    MouseArea { anchors.fill: parent; onClicked: root.chosen("cancel") }

    Rectangle {
      id: card
      width: Math.min(parent.width - Style.space(24), Style.space(360))
      height: column.implicitHeight + Style.space(32)
      anchors.centerIn: parent
      color: Commons.Color.background
      border.color: Commons.Color.accent
      border.width: Math.max(1, Style.normalBorderWidth)
      radius: Style.cornerRadius

      MouseArea { anchors.fill: parent }

      Column {
        id: column
        anchors.fill: parent
        anchors.margins: Style.space(16)
        spacing: Style.space(10)

        PlainLabel {
          width: parent.width
          text: root.message
          color: root.panel.fg
          font.family: root.panel.fontFamily
          wrapMode: Text.Wrap
          elide: Text.ElideNone
        }

        Repeater {
          model: root.choices

          Button {
            required property var modelData
            required property int index
            width: column.width
            text: modelData.label
            selected: root.selectedIndex === index
            hasCursor: root.selectedIndex === index
            foreground: modelData.destructive ? Commons.Color.urgent : root.panel.fg
            fontFamily: root.panel.fontFamily
            bordered: true
            onClicked: root.chosen(modelData.id)
            onHovered: function(h) { if (h) root.selectedIndex = index }
          }
        }
      }
    }
  }
}
