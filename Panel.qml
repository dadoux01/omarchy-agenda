import QtQuick
import Quickshell
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "components"
import "pages"
import "Model.js" as Model

// The Agenda popup. BarWidget.qml owns the bar label and hands this panel
// the button to hang from and the session service to talk to.
Panel {
  id: root
  moduleName: "dadoux01.agenda"
  ipcTarget: "dadoux01.agenda"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  property var service: null
  readonly property var barIdentity: hostWidget || root

  Commons.Translations { id: translations; directory: Qt.resolvedUrl("i18n") }
  readonly property alias strings: translations

  // ------------------------------------------------------------ look

  readonly property color fg: bar ? bar.foreground : Commons.Color.foreground
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property bool use24h: !/a/i.test(Qt.locale(Commons.I18n.formatLocale).timeFormat(Locale.ShortFormat))
  readonly property var locale: Qt.locale(Commons.I18n.formatLocale)

  // ------------------------------------------------------------ state

  readonly property bool backendUp: service ? service.backendUp : false
  readonly property var accounts: service ? service.accounts : []
  readonly property var events: service ? service.events : []
  readonly property bool syncing: service ? service.syncing : false
  readonly property double syncedAt: service ? service.syncedAt : 0

  property string page: "agenda"
  property bool editing: false
  property int busyCount: 0
  readonly property bool busy: busyCount > 0
  property string selectedKey: ""
  property string expandedKey: ""
  property double now: Date.now()

  function settingsValue(key, fallback) {
    var s = service ? service.settings : null
    return s && s[key] !== undefined && s[key] !== null ? s[key] : fallback
  }

  readonly property int agendaDays: Number(settingsValue("agendaDays", 7)) || 7
  readonly property var groups: Model.groupByDay(events, now, agendaDays)

  readonly property var flatRows: {
    var out = []
    for (var i = 0; i < groups.length; i++)
      for (var j = 0; j < groups[i].events.length; j++)
        out.push({ key: rowKey(groups[i], groups[i].events[j]), event: groups[i].events[j] })
    return out
  }

  function rowKey(group, ev) { return group.day + ":" + ev.key }

  readonly property var writableCalendars: {
    var out = []
    for (var i = 0; i < accounts.length; i++) {
      var a = accounts[i]
      var cals = a && Array.isArray(a.calendars) ? a.calendars : []
      for (var j = 0; j < cals.length; j++) {
        var c = cals[j]
        if (c.readOnly || !c.enabled) continue
        out.push({ key: a.id + "|" + c.id, accountId: a.id, calendarId: c.id,
                   label: accounts.length > 1 ? a.label + " › " + c.name : c.name })
      }
    }
    return out
  }

  readonly property bool multipleCalendars: {
    var n = 0
    for (var i = 0; i < accounts.length; i++) {
      var cals = accounts[i] && Array.isArray(accounts[i].calendars) ? accounts[i].calendars : []
      for (var j = 0; j < cals.length; j++)
        if (cals[j].enabled) n++
    }
    return n > 1
  }

  readonly property string defaultCalendarKey: {
    var wanted = String(settingsValue("defaultCalendar", ""))
    for (var i = 0; i < writableCalendars.length; i++)
      if (writableCalendars[i].key === wanted) return wanted
    return writableCalendars.length > 0 ? writableCalendars[0].key : ""
  }

  readonly property string defaultCalendarName: {
    var c = calendarByKey(defaultCalendarKey)
    return c ? c.label : ""
  }

  function calendarByKey(key) {
    for (var i = 0; i < writableCalendars.length; i++)
      if (writableCalendars[i].key === key) return writableCalendars[i]
    return null
  }

  readonly property int syncErrorCount: {
    var n = 0
    for (var i = 0; i < accounts.length; i++) if (accounts[i].error) n++
    return n
  }

  property string flash: ""

  readonly property string statusText: {
    if (flash) return flash
    if (syncing) return strings.t("panel.syncing")
    if (syncErrorCount > 0) return strings.tn("panel.syncErrors", syncErrorCount).arg(syncErrorCount)
    if (syncedAt <= 0) return strings.t("panel.neverSynced")
    return strings.t("panel.synced").arg(Model.timeText(syncedAt, use24h))
  }

  Timer {
    id: flashTimer
    interval: 3500
    onTriggered: root.flash = ""
  }

  function showFlash(text) {
    flash = text
    flashTimer.restart()
  }

  function dayTitle(group) {
    if (group.offset === 0) return strings.t("panel.today") + "  ·  " + new Date(group.day).toLocaleDateString(locale, "d MMMM")
    if (group.offset === 1) return strings.t("panel.tomorrow") + "  ·  " + new Date(group.day).toLocaleDateString(locale, "d MMMM")
    return new Date(group.day).toLocaleDateString(locale, "dddd d MMMM")
  }

  function errorText(code, detail) {
    if (!code) return ""
    var key = "error." + code
    var text = strings.t(key, "")
    if (text) return text.indexOf("%1") !== -1 ? text.arg(detail || code) : text
    if (code === "backend_down") return strings.t("panel.backendDown")
    return strings.t("error.generic").arg(detail || code)
  }

  // ------------------------------------------------------------ lifecycle

  function open() {
    root.now = Date.now()
    root.controller.show()
    if (service && (Date.now() - syncedAt) > 60000) service.sync(false)
    Qt.callLater(function() {
      if (root.opened) setCenterHoverRevealSuppressed(true)
    })
  }

  function close() {
    setCenterHoverRevealSuppressed(false)
    root.controller.hide()
    page = "agenda"
    editing = false
    expandedKey = ""
    confirm.close()
  }

  function toggle() {
    if (root.opened) root.close()
    else root.open()
  }

  function openPage(p) {
    if (p === "form" && writableCalendars.length === 0) p = accounts.length === 0 ? "add" : "agenda"
    if (!root.opened) root.open()
    editing = false
    var alreadyForm = page === "form"
    page = p
    var content = panel.loadedContent
    if (content && content.scrollerItem) content.scrollerItem.contentY = 0
    if (p === "form") {
      if (alreadyForm) {
        Qt.callLater(function() {
          var f = pageItem("form")
          if (f) f.reset(root.pendingPreset)
          root.pendingPreset = null
        })
      }
    } else if (p === "add") {
      // A finished or abandoned flow starts over; one waiting on the browser
      // or on the choice of calendars is picked up where it was.
      if (addFlow.step !== "browser" && addFlow.step !== "calendars") addFlow = freshFlow
    } else Qt.callLater(focusList)
  }

  function pageItem(name) {
    var c = panel.loadedContent
    if (!c) return null
    var loader = name === "agenda" ? c.agendaLoader : name === "form" ? c.formLoader : name === "add" ? c.addLoader : null
    return loader ? loader.item : null
  }

  function focusList() {
    editing = false
    if (panel.loadedContent) panel.loadedContent.forceActiveFocus()
  }

  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function")
      return root.bar.switchPanelFrom(root.barIdentity, direction)
    return false
  }

  function setCenterHoverRevealSuppressed(value) {
    if (root.bar && typeof root.bar.setCenterHoverRevealSuppressed === "function")
      root.bar.setCenterHoverRevealSuppressed(value)
  }

  Timer {
    interval: 20000
    repeat: true
    running: root.opened
    triggeredOnStart: true
    onTriggered: root.now = Date.now()
  }

  // ------------------------------------------------------------ agenda keys

  function moveSelection(dy) {
    if (flatRows.length === 0) return
    var idx = -1
    for (var i = 0; i < flatRows.length; i++) if (flatRows[i].key === selectedKey) idx = i
    if (idx === -1) idx = dy > 0 ? 0 : flatRows.length - 1
    else idx = Math.max(0, Math.min(flatRows.length - 1, idx + dy))
    selectedKey = flatRows[idx].key
  }

  function selectedEvent() {
    for (var i = 0; i < flatRows.length; i++) if (flatRows[i].key === selectedKey) return flatRows[i].event
    return null
  }

  function toggleExpanded(key) {
    selectedKey = key
    expandedKey = expandedKey === key ? "" : key
  }

  function join(ev) {
    var url = Model.safeMeetingUrl(ev.meetingUrl)
    if (url === "") return
    Qt.openUrlExternally(url)
    close()
  }

  // ------------------------------------------------------------ calls

  function call(op, params, done) {
    if (!service) { if (done) done(false, errorText("backend_down", ""), "backend_down"); return }
    busyCount++
    service.request(op, params, function(msg) {
      busyCount = Math.max(0, busyCount - 1)
      var code = msg.ok ? "" : (msg.error ? String(msg.error.code) : "internal")
      var detail = !msg.ok && msg.error && msg.error.detail ? String(msg.error.detail) : ""
      if (done) done(msg.ok === true, msg.ok ? msg.result : errorText(code, detail), code)
    })
  }

  function syncNow() { if (service) service.sync(true) }

  property var pendingPreset: null

  function quickAdd(text) {
    var parsed = Model.parseQuickAdd(text, Date.now())
    if (!parsed) { quickToForm(text); showFlash(strings.t("panel.quickAddFailed")); return }
    var target = calendarByKey(defaultCalendarKey)
    if (!target) return
    var m = Number(settingsValue("defaultReminderMinutes", 10))
    createEvent({
      accountId: target.accountId, calendarId: target.calendarId,
      title: parsed.title, start: parsed.start, end: parsed.end, allDay: parsed.allDay,
      reminders: parsed.allDay || m < 0 ? [] : [m]
    }, function(err) { showFlash(err) })
  }

  function quickToForm(text) {
    var parsed = Model.parseQuickAdd(text, Date.now())
    pendingPreset = parsed || { title: String(text || "").trim() }
    openPage("form")
  }

  function createEvent(params, onError) {
    call("createEvent", params, function(ok, result) {
      if (!ok) { if (onError) onError(result); return }
      var agenda = pageItem("agenda")
      if (agenda) agenda.quickField.text = ""
      showFlash(strings.t("form.saved"))
      openPage("agenda")
    })
  }

  function testNotification() { call("testNotification", {}, null) }

  function saveSettings(values) { call("setSettings", { settings: values }, null) }

  function setCalendarEnabled(accountId, calendarId, enabled) {
    call("updateAccount", { id: accountId, calendars: [{ id: calendarId, enabled: enabled }] }, null)
  }

  // ------------------------------------------------------------ add account
  //
  // Kept here rather than in AddAccountPage: a Google sign-in happens in the
  // browser, clicking there closes the popup and unloads the page, and the
  // flow must still be where it was when the browser hands back.

  readonly property var freshFlow: ({ step: "provider", kind: "", error: "", token: "", found: [], chosen: ({}), label: "" })
  property var addFlow: freshFlow
  property var clientFiles: []
  property int loginAttemptId: 0

  function setAddFlow(patch) {
    var next = {}
    for (var k in addFlow) next[k] = addFlow[k]
    for (var p in patch) next[p] = patch[p]
    addFlow = next
  }

  function loadClientFiles() {
    if (!service) return
    service.request("googleClientFiles", {}, function(msg) {
      root.clientFiles = msg.ok && msg.result && Array.isArray(msg.result.files) ? msg.result.files : []
    })
  }

  function connectAccount(params) {
    var browser = params.kind === "google"
    var attempt = ++root.loginAttemptId
    setAddFlow({ error: "", step: browser ? "browser" : addFlow.step })
    call("connect", params, function(ok, result, code) {
      if (attempt !== root.loginAttemptId) return
      if (!ok) {
        setAddFlow({ step: "credentials", error: code === "cancelled" ? "" : result })
        return
      }
      var chosen = {}
      for (var i = 0; i < result.calendars.length; i++)
        chosen[result.calendars[i].id] = result.calendars[i].enabled !== false
      setAddFlow({ step: "calendars", token: result.token, found: result.calendars,
                   chosen: chosen, label: result.label, error: "" })
      // Back from the browser: bring the choice of calendars to the user.
      if (browser && (!root.opened || root.page !== "add")) openPage("add")
    })
  }

  function cancelLogin() {
    root.loginAttemptId++
    if (service) service.request("cancelLogin", {}, null)
    setAddFlow({ step: "credentials", error: "" })
  }

  function toggleFound(id) {
    var c = {}
    for (var k in addFlow.chosen) c[k] = addFlow.chosen[k]
    c[id] = !(c[id] === true)
    setAddFlow({ chosen: c })
  }

  function saveFoundAccount() {
    var selection = []
    for (var i = 0; i < addFlow.found.length; i++)
      selection.push({ id: addFlow.found[i].id, enabled: addFlow.chosen[addFlow.found[i].id] === true })
    call("saveAccount", { token: addFlow.token, label: addFlow.label, calendars: selection }, function(ok, result) {
      if (!ok) { setAddFlow({ step: "credentials", error: result }); return }
      if (result.secretStore === "@file") showFlash(strings.t("accounts.secretFile"))
      addFlow = freshFlow
      openPage("agenda")
    })
  }

  function reconnect(acc) {
    showFlash(strings.t("accounts.reconnecting"))
    call("reconnect", { id: acc.id }, function(ok, result) {
      showFlash(ok ? strings.t("accounts.reconnected") : result)
    })
  }

  // ------------------------------------------------------------ confirmations

  function askDelete(ev) {
    var choices = []
    if (ev.recurring && ev.recurrenceId) {
      choices.push({ id: "occurrence", label: strings.t("panel.deleteOccurrence"), destructive: true })
      choices.push({ id: "series", label: strings.t("panel.deleteSeries"), destructive: true })
    } else {
      choices.push({ id: "series", label: strings.t("panel.deleteSingle"), destructive: true })
    }
    choices.push({ id: "cancel", label: strings.t("panel.cancel"), destructive: false })
    confirm.ask(strings.t("panel.deleteConfirm").arg(ev.title || "…"), choices, function(choice) {
      if (choice === "cancel") return
      call("deleteEvent", { key: ev.key, scope: choice }, function(ok, result) {
        if (!ok) showFlash(result)
        expandedKey = ""
      })
    })
  }

  function askRemoveAccount(acc) {
    confirm.ask(strings.t("accounts.removeConfirm").arg(acc.label || acc.username || "…"), [
      { id: "remove", label: strings.t("accounts.remove"), destructive: true },
      { id: "cancel", label: strings.t("panel.cancel"), destructive: false }
    ], function(choice) {
      if (choice === "remove") call("removeAccount", { id: acc.id }, null)
    })
  }

  QtObject {
    id: confirm
    property bool opened: false
    property string message: ""
    property var choices: []
    property var callback: null

    function ask(message, choices, callback) {
      confirm.message = message
      confirm.choices = choices
      confirm.callback = callback
      confirm.opened = true
    }
    function answer(id) {
      var cb = confirm.callback
      confirm.close()
      if (cb) cb(id)
      Qt.callLater(root.focusList)
    }
    function close() {
      confirm.opened = false
      confirm.callback = null
    }
  }

  // ------------------------------------------------------------ the popup

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    centerOnBar: false
    focusTarget: panel.loadedContent
    contentWidth: panel.fittedContentWidth(Style.space(440))
    contentHeight: panel.fittedContentHeight(panel.loadedContentHeight)

    content: PanelKeyCatcher {
      id: keys
      // The content is its own component: Panel reaches the pages through these.
      property alias agendaLoader: agendaLoader
      property alias formLoader: formLoader
      property alias addLoader: addLoader
      property alias scrollerItem: scroller
      implicitHeight: Math.min(pages.implicitHeight, Style.space(760))
      anchors.fill: parent
      blocked: root.page !== "agenda" || root.editing || confirm.opened

      onMoveRequested: function(dx, dy) { if (dy !== 0) root.moveSelection(dy) }
      onActivateRequested: { if (root.selectedKey) root.toggleExpanded(root.selectedKey) }
      onCloseRequested: root.close()
      onDeleteRequested: {
        var ev = root.selectedEvent()
        if (ev && !ev.readOnly) root.askDelete(ev)
      }
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === "n" || t === "+") root.openPage("form")
        else if (t === "s" || t === "r") root.syncNow()
        else if (t === "a") root.openPage("accounts")
        else if (t === ",") root.openPage("settings")
        else if (t === "/" || t === "q") {
          if (agendaLoader.item && agendaLoader.item.quickField.visible) agendaLoader.item.quickField.forceActiveFocus()
        } else if (t === "o" || t === "J") {
          var ev = root.selectedEvent()
          if (ev) root.join(ev)
        }
      }

      // Escape on a sub-page goes back to the agenda; on the agenda the
      // catcher above closes the popup.
      Keys.onEscapePressed: function(event) {
        if (confirm.opened) confirm.answer("cancel")
        else if (root.page !== "agenda") root.openPage("agenda")
        else root.close()
        event.accepted = true
      }

      Flickable {
        id: scroller
        anchors.fill: parent
        contentWidth: width
        contentHeight: pages.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height

        Column {
          id: pages
          width: scroller.width

          Loader {
            id: agendaLoader
            width: parent.width
            active: true
            visible: root.page === "agenda"
            sourceComponent: AgendaPage { panel: root; width: agendaLoader.width }
          }
          Loader {
            id: formLoader
            width: parent.width
            active: root.page === "form"
            visible: active
            sourceComponent: EventForm { panel: root; width: formLoader.width }
            onLoaded: {
              item.reset(root.pendingPreset)
              root.pendingPreset = null
            }
          }
          Loader {
            id: accountsLoader
            width: parent.width
            active: root.page === "accounts"
            visible: active
            sourceComponent: AccountsPage { panel: root; width: accountsLoader.width }
          }
          Loader {
            id: addLoader
            width: parent.width
            active: root.page === "add"
            visible: active
            sourceComponent: AddAccountPage { panel: root; width: addLoader.width }
          }
          Loader {
            id: settingsLoader
            width: parent.width
            active: root.page === "settings"
            visible: active
            sourceComponent: SettingsPage { panel: root; width: settingsLoader.width }
          }
        }
      }

      ChoiceDialog {
        anchors.fill: parent
        z: 10
        panel: root
        opened: confirm.opened
        message: confirm.message
        choices: confirm.choices
        onChosen: function(id) { confirm.answer(id) }
      }
    }
  }
}
