import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons

// The part of Agenda that runs for the whole session, whatever the bar does.
//
// Omarchy builds one bar per monitor, so anything that must happen once
// (talking to the calendar servers, reminders, the daily recap) lives here
// rather than in the widget. The work itself is done by a Python helper,
// backend/agenda_backend.py, spoken to as JSON lines over stdin/stdout:
// passwords travel that way too, never on a command line.
Item {
  id: root
  width: 0
  height: 0
  visible: false

  // Injected by the shell.
  property var shell: null
  property var manifest: null
  property var pluginRegistry: null
  property var barWidgetRegistry: null
  property string omarchyPath: ""

  // Mirrors of the helper's state, pushed after every change.
  property var accounts: []
  property var events: []
  property var settings: ({})
  property bool syncing: false
  property double syncedAt: 0
  property bool backendUp: false
  property string version: ""

  signal openRequested(string page)

  readonly property string pluginDir: String(Qt.resolvedUrl(".")).replace(/^file:\/\//, "").replace(/\/$/, "")

  Commons.Translations { id: strings; directory: Qt.resolvedUrl("i18n") }

  // ------------------------------------------------------------------ calls

  property int nextId: 1
  property var callbacks: ({})
  property int activeCallbackCount: 0

  function request(op, params, callback) {
    if (!helper.running) {
      if (callback) callback({ ok: false, error: { code: "backend_down", detail: "" } })
      return
    }
    var id = nextId++
    if (callback) {
      var isLogin = op === "connect" || op === "discover" || op === "reconnect"
      callbacks[id] = { cb: callback, deadline: Date.now() + (isLogin ? 360000 : 90000) }
      activeCallbackCount = Object.keys(callbacks).length
    }
    helper.write(JSON.stringify({ id: id, op: op, params: params || {} }) + "\n")
  }

  function sync(force) { request("sync", { force: !!force }, null) }

  function handleLine(line) {
    if (!line || line.length > 64 * 1024 * 1024) return
    var msg
    try { msg = JSON.parse(line) } catch (e) { return }
    if (!msg || typeof msg !== "object") return
    if (msg.event === "state") applyState(msg.state)
    else if (msg.event === "open") root.openRequested("agenda")
    else if (msg.id !== undefined) {
      var entry = callbacks[msg.id]
      if (entry) {
        delete callbacks[msg.id]
        activeCallbackCount = Object.keys(callbacks).length
        try { entry.cb(msg) } catch (e2) { console.warn("agenda: callback failed", e2) }
      }
    }
  }

  function applyState(s) {
    if (!s || typeof s !== "object") return
    root.accounts = Array.isArray(s.accounts) ? s.accounts : []
    root.events = Array.isArray(s.events) ? s.events : []
    root.settings = s.settings && typeof s.settings === "object" ? s.settings : ({})
    root.syncing = s.syncing === true
    root.syncedAt = Number(s.syncedAt) || 0
    root.version = String(s.version || "")
  }

  // ------------------------------------------------------------- the helper

  property int restartDelay: 1000
  property bool destroying: false

  Process {
    id: helper
    command: ["python3", "-I", "-B", root.pluginDir + "/backend/agenda_backend.py", "serve"]
    stdinEnabled: true
    running: false

    stdout: SplitParser {
      splitMarker: "\n"
      onRead: function(data) { root.handleLine(data) }
    }
    stderr: SplitParser {
      splitMarker: "\n"
      onRead: function(data) { console.warn("agenda helper:", String(data).slice(0, 500)) }
    }

    onRunningChanged: {
      root.backendUp = running
      if (running) {
        root.restartDelay = 1000
        Qt.callLater(root.configure)
      } else {
        // Fail every pending call rather than leave the UI waiting forever.
        var pending = root.callbacks
        root.callbacks = ({})
        root.activeCallbackCount = 0
        for (var id in pending) {
          try { pending[id].cb({ ok: false, error: { code: "backend_down", detail: "" } }) } catch (e) {}
        }
        if (root.destroying) return
        restartTimer.interval = root.restartDelay
        root.restartDelay = Math.min(60000, root.restartDelay * 2)
        restartTimer.start()
      }
    }
  }

  Timer {
    id: restartTimer
    repeat: false
    onTriggered: if (!root.destroying) helper.running = true
  }

  Timer {
    id: requestTimeoutTimer
    interval: 5000
    repeat: true
    running: !root.destroying && root.activeCallbackCount > 0
    onTriggered: {
      var now = Date.now()
      var expired = []
      for (var id in root.callbacks) {
        if (root.callbacks[id].deadline <= now) expired.push(id)
      }
      if (expired.length === 0) return
      for (var i = 0; i < expired.length; i++) {
        var entry = root.callbacks[expired[i]]
        delete root.callbacks[expired[i]]
        try { entry.cb({ ok: false, error: { code: "timeout", detail: "" } }) } catch (e) {}
      }
      root.activeCallbackCount = Object.keys(root.callbacks).length
    }
  }

  Component.onCompleted: helper.running = true
  Component.onDestruction: {
    root.destroying = true
    restartTimer.stop()
    configureTimer.stop()
    helper.running = false
  }

  // Notification text is written by the helper, in the shell's language.
  readonly property var notificationKeys: [
    "app", "inMinutes", "inHours", "inHoursMinutes", "inDays", "startsNow", "late", "allDay",
    "privateTitle", "join", "snooze", "open", "recapTitle", "recapEmpty", "recapMore"
  ]
  readonly property var catalog: strings.strings
  onCatalogChanged: configureTimer.restart()

  Timer {
    id: configureTimer
    interval: 300
    onTriggered: root.configure()
  }

  function configure() {
    var out = {}
    for (var i = 0; i < notificationKeys.length; i++) {
      var key = "notify." + notificationKeys[i]
      var value = strings.t(key, "")
      if (value) out[notificationKeys[i]] = value
    }
    var fmt = Qt.locale(Commons.I18n.formatLocale).timeFormat(Locale.ShortFormat)
    request("configure", { strings: out, use24h: !/a/i.test(fmt) }, function(msg) {
      if (msg.ok) root.applyState(msg.result)
    })
  }

  // omarchy-shell dadoux01.agenda toggle | open | add | sync
  ShellIpc {
    target: "dadoux01.agenda"

    function open(): void { root.openRequested("agenda") }
    function toggle(): void { root.openRequested("toggle") }
    function add(): void { root.openRequested("add") }
    function sync(): void { root.sync(true) }
  }
}
