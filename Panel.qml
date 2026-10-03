import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Ui
import qs.Commons

// XR Head Aim: XR glasses (via XRLinuxDriver) -> xr-head-aim.service -> the
// controller's right stick while one is connected, otherwise the mouse; only while a
// game is focused. Left click opens the panel, middle click turns head aim on/off,
// right click recenters. Everything writes ~/.config/xr-head-aim/settings.json through
// `bin/xr-head-aim`, which the service applies within a second.
Panel {
  id: root
  moduleName: "io.github.r88800.xr-head-aim"
  ipcTarget: "io.github.r88800.xr-head-aim"

  readonly property string tool: String(Qt.resolvedUrl("bin/xr-head-aim")).replace(/^file:\/\//, "")

  property bool installed: true
  property var status: ({})
  property bool showAdvanced: false
  readonly property bool aimOn: root.overrides.enabled !== undefined ? root.overrides.enabled === 1 : status.enabled !== false
  readonly property bool aimActive: !!status.running && aimOn
  readonly property string statusText: !installed ? "Not set up: run install.sh in the plugin folder"
    : !status.running ? "Service not running: rerun install.sh"
    : !aimOn ? "Off"
    : !status.glasses ? "On · waiting for glasses"
    : status.game ? "On · aiming with the " + (status.output === "controller" ? "controller stick" : "mouse")
    : "On · idle until a game is focused"
  property var tuning: ({})

  // Values written but not yet confirmed by a file reload, so knobs don't
  // snap back during the round trip.
  property var overrides: ({})
  property var pending: ({})

  // Keyboard cursor: -1 = on/off row, 0..n-1 = slider rows.
  property int cursor: -1
  property bool cursorActive: false

  // The essentials first; everything else is under Advanced. Defaults are tuned on
  // recorded play, so most people never open it.
  readonly property var allSliders: [
    { key: "gain", needsBridge: true, label: "Controller sensitivity",
      hint: "View degrees per head degree", min: 1, max: 10, step: 0.05, unit: "×", digits: 2 },
    { key: "mouse_sensitivity", label: "Mouse sensitivity",
      hint: "Mouse counts per head degree (match your in-game mouse sensitivity)", min: 2, max: 200, step: 1, unit: "", digits: 0 },
    { key: "vertical_ratio", label: "Vertical ratio",
      hint: "Up/down speed relative to left/right", min: 0.3, max: 1.5, step: 0.05, unit: "×", digits: 2 },
    { key: "precision", advanced: true, section: "PRECISION", label: "Slow-motion precision",
      hint: "Share of sensitivity for small, slow head moves", min: 0.2, max: 1, step: 0.05, unit: "%", digits: 0, percent: true },
    { key: "precision_to", advanced: true, section: "PRECISION", label: "Full sensitivity from",
      hint: "Head speed where precision ends", min: 1, max: 30, step: 0.5, unit: "°/s", digits: 1 },
    { key: "still_from", advanced: true, section: "DEAD ZONE", label: "Dead zone",
      hint: "Head speed below which the view stays still", min: 0, max: 2, step: 0.05, unit: "°/s", digits: 2 },
    { key: "still_to", advanced: true, section: "DEAD ZONE", label: "Fade-in end",
      hint: "Head speed where output reaches full strength", min: 0.1, max: 4, step: 0.05, unit: "°/s", digits: 2 },
    { key: "smooth_ms", advanced: true, section: "SMOOTHING", label: "Tremor smoothing",
      hint: "Averaging for slow movements (adds lag there)", min: 0, max: 80, step: 1, unit: " ms", digits: 0 },
    { key: "smooth_from", advanced: true, section: "SMOOTHING", label: "Smoothing fades from",
      hint: "Head speed where smoothing starts to drop", min: 0, max: 5, step: 0.1, unit: "°/s", digits: 1 },
    { key: "smooth_to", advanced: true, section: "SMOOTHING", label: "No smoothing above",
      hint: "Head speed with zero smoothing (zero lag)", min: 0.1, max: 10, step: 0.1, unit: "°/s", digits: 1 },
    { key: "predict", advanced: true, section: "SMOOTHING", label: "Lag removal",
      hint: "Fast moves use the newest head rate (~4 ms less lag)", min: 0, max: 1, step: 0.05, unit: "%", digits: 0, percent: true },
    { key: "game_full_rate", advanced: true, needsBridge: true, section: "GAME (CONTROLLER)", label: "Game turn speed",
      hint: "How fast the game turns at full stick", min: 60, max: 600, step: 5, unit: "°/s", digits: 0 },
    { key: "game_deadzone", advanced: true, needsBridge: true, section: "GAME (CONTROLLER)", label: "Game stick dead zone",
      hint: "Set it to the game's look dead zone", min: 0, max: 0.3, step: 0.01, unit: "%", digits: 0, percent: true }
  ]
  readonly property var sliders: allSliders.filter(function(spec) {
    return (!spec.needsBridge || root.status.bridge) && (!spec.advanced || root.showAdvanced)
  })

  // Pairs that must stay ordered (lower key, upper key).
  readonly property var orderedPairs: [
    ["still_from", "still_to"],
    ["smooth_from", "smooth_to"]
  ]

  function value(key) {
    if (overrides[key] !== undefined) return overrides[key]
    var v = tuning[key]
    return v === undefined ? 0 : Number(v)
  }

  function format(spec, v) {
    if (spec.percent) return Math.round(v * 100) + "%"
    return Number(v).toFixed(spec.digits) + spec.unit
  }

  function snap(key, v) {
    if (key === "invert_y") return v
    for (var i = 0; i < sliders.length; i++) {
      var spec = sliders[i]
      if (spec.key !== key) continue
      v = Math.round(v / spec.step) * spec.step
      return Number(Math.max(spec.min, Math.min(spec.max, v)).toFixed(4))
    }
    return v
  }

  function setValue(key, v) {
    v = snap(key, v)
    var o = Object.assign({}, overrides)
    var p = Object.assign({}, pending)
    o[key] = v; p[key] = v
    for (var i = 0; i < orderedPairs.length; i++) {
      var lo = orderedPairs[i][0], hi = orderedPairs[i][1]
      if (key === lo && value(hi) <= v) { o[hi] = snap(hi, v + 0.1); p[hi] = o[hi] }
      if (key === hi && value(lo) >= v) { o[lo] = snap(lo, v - 0.1); p[lo] = o[lo] }
    }
    overrides = o
    pending = p
    writeDebounce.restart()
  }

  function flush() {
    if (tuneProc.running) return
    var keys = Object.keys(pending)
    if (keys.length === 0) return
    var cmd = [root.tool, "tune"]
    for (var i = 0; i < keys.length; i++) cmd.push(keys[i] + "=" + pending[keys[i]])
    pending = ({})
    tuneProc.command = cmd
    tuneProc.running = true
  }

  function resetTuning() {
    pending = ({})
    tuneProc.command = [root.tool, "tune", "reset"]
    tuneProc.running = true
  }

  function toggleAim() {
    var o = Object.assign({}, overrides); o.enabled = aimOn ? 0 : 1; overrides = o
    toggleProc.running = true
  }

  function toggleInvert() {
    setValue("invert_y", value("invert_y") ? 0 : 1)
  }

  function refreshState() {
    if (!stateProc.running) stateProc.running = true
  }

  function adjustCursor(dx) {
    if (cursor < 0) return
    var spec = sliders[cursor]
    setValue(spec.key, value(spec.key) + dx * spec.step)
  }

  function ensureCursorVisible(item) {
    if (!item || !scrollArea) return
    var flick = scrollArea.contentItem
    if (!flick || flick.contentY === undefined) return
    var pt = item.mapToItem(flick.contentItem || flick, 0, 0)
    var margin = 6
    if (pt.y < flick.contentY + margin) flick.contentY = Math.max(0, pt.y - margin)
    else if (pt.y + item.height > flick.contentY + flick.height - margin)
      flick.contentY = pt.y + item.height + margin - flick.height
  }

  onOpenedChanged: if (opened) refreshState()

  function applyState(s) {
    root.installed = s.installed !== false
    root.status = s
    if (!s.settings) return
    root.tuning = s.settings
    // Drop overrides the file now confirms.
    var o = {}
    for (var k in root.overrides) {
      var want = root.overrides[k]
      var have = root.tuning[k]
      var same = typeof want === "number" ? Math.abs(Number(have) - want) <= 1e-6 : have === want
      if (!same || root.pending[k] !== undefined) o[k] = want
    }
    root.overrides = o
  }

  Timer {
    id: writeDebounce
    interval: 120
    onTriggered: root.flush()
  }

  Process {
    id: tuneProc
    onExited: {
      root.refreshState()
      root.flush()
    }
  }

  Process {
    id: toggleProc
    command: [root.tool, "toggle"]
    onExited: root.refreshState()
  }

  Process {
    id: recenterProc
    command: [root.tool, "recenter"]
  }

  Process {
    id: stateProc
    command: [root.tool, "state"]
    stdout: StdioCollector {
      onStreamFinished: {
        try {
          root.applyState(JSON.parse(text))
        } catch (e) {}
      }
    }
  }

  Timer {
    interval: root.opened ? 1500 : 4000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refreshState()
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰊪"
    active: root.aimActive
    tooltipText: "Head aim: " + root.statusText
      + "\n• Left-click: tune\n• Middle-click: on/off\n• Right-click: recenter"
    onPressed: function(b) {
      if (b === Qt.MiddleButton) root.toggleAim()
      else if (b === Qt.RightButton) recenterProc.running = true
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(400))
    contentHeight: panel.fittedContentHeight(panelColumn.implicitHeight, Style.space(640))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        if (dy !== 0) root.cursor = Math.max(-1, Math.min(root.sliders.length - 1, root.cursor + dy))
        else if (dx !== 0) root.adjustCursor(dx)
      }
      onActivateRequested: if (root.cursorActive && root.cursor === -1) root.toggleAim()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      ScrollView {
        id: scrollArea
        anchors.fill: parent
        clip: true
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical.policy: panelColumn.implicitHeight > height ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff
        Binding {
          target: scrollArea.contentItem
          property: "interactive"
          value: panelColumn.implicitHeight > scrollArea.height
        }

        Column {
          id: panelColumn
          width: scrollArea.availableWidth
          spacing: Style.space(10)

          // ---------- Hero: on/off ----------
          CursorSurface {
            id: heroRow
            width: parent.width
            height: Math.max(heroLabels.implicitHeight, aimSwitch.implicitHeight) + Style.space(12)
            hasCursor: root.cursorActive && root.cursor === -1
            onHasCursorChanged: if (hasCursor) root.ensureCursorVisible(heroRow)
            foreground: root.bar.foreground
            outline: true

            Text {
              id: heroIcon
              text: "󰊪"
              color: root.bar.foreground
              opacity: root.aimActive ? 1 : 0.45
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.display
              anchors.left: parent.left
              anchors.leftMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
            }

            Column {
              id: heroLabels
              anchors.left: heroIcon.right
              anchors.leftMargin: Style.space(12)
              anchors.right: aimSwitch.left
              anchors.rightMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(2)

              Text {
                text: "HEAD AIM"
                color: root.bar.foreground
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.subtitle
                font.bold: true
              }
              Text {
                width: parent.width
                elide: Text.ElideRight
                textFormat: Text.PlainText
                text: root.statusText
                color: Qt.darker(root.bar.foreground, 1.4)
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
              }
            }

            ToggleSwitch {
              id: aimSwitch
              anchors.right: parent.right
              anchors.rightMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
              checked: root.aimOn
              foreground: root.bar.foreground
              onToggled: root.toggleAim()
            }

            HoverHandler {
              onHoveredChanged: if (hovered) { root.cursorActive = true; root.cursor = -1 }
            }
          }

          // ---------- Actions ----------
          Row {
            spacing: Style.space(8)
            anchors.horizontalCenter: parent.horizontalCenter

            Button {
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
              text: "Recenter"
              enabled: root.aimActive
              onClicked: recenterProc.running = true
            }
            Button {
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
              text: "Invert up/down"
              bordered: true
              active: root.value("invert_y") === 1
              onClicked: root.toggleInvert()
            }
          }

          // ---------- Sliders ----------
          Repeater {
            model: root.sliders

            Column {
              id: sliderBlock
              required property var modelData
              required property int index
              readonly property bool firstInSection: !!modelData.section
                && (index === 0 || root.sliders[index - 1].section !== modelData.section)
              width: panelColumn.width
              spacing: Style.space(4)

              PanelSeparator {
                visible: sliderBlock.firstInSection
                foreground: root.bar.foreground
              }

              PanelSectionHeader {
                visible: sliderBlock.firstInSection
                text: sliderBlock.modelData.section
                foreground: root.bar.foreground
                fontFamily: root.bar.fontFamily
              }

              Item {
                width: parent.width
                implicitHeight: Math.max(sliderLabel.implicitHeight, sliderValue.implicitHeight)

                Text {
                  id: sliderLabel
                  text: sliderBlock.modelData.label
                  color: root.bar.foreground
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.body
                  anchors.left: parent.left
                  anchors.leftMargin: Style.space(6)
                  anchors.verticalCenter: parent.verticalCenter
                }

                Text {
                  id: sliderValue
                  textFormat: Text.PlainText
                  text: root.format(sliderBlock.modelData,
                                    slider.dragging ? slider.liveValue : root.value(sliderBlock.modelData.key))
                  color: Qt.darker(root.bar.foreground, 1.4)
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: true
                  anchors.right: parent.right
                  anchors.rightMargin: Style.space(6)
                  anchors.verticalCenter: parent.verticalCenter
                }
              }

              CursorSurface {
                id: sliderRow
                width: parent.width
                height: slider.implicitHeight + Style.spacing.controlGap
                hasCursor: root.cursorActive && root.cursor === sliderBlock.index
                onHasCursorChanged: if (hasCursor) root.ensureCursorVisible(sliderRow)
                foreground: root.bar.foreground
                outline: true

                PanelSlider {
                  id: slider
                  bar: root.bar
                  anchors.fill: parent
                  anchors.leftMargin: Style.space(6)
                  anchors.rightMargin: Style.space(6)
                  minimum: sliderBlock.modelData.min
                  maximum: sliderBlock.modelData.max
                  step: sliderBlock.modelData.step
                  value: root.value(sliderBlock.modelData.key)
                  onMoved: function(v) { root.setValue(sliderBlock.modelData.key, v) }
                  onReleased: function(v) { root.setValue(sliderBlock.modelData.key, v) }
                }

                HoverHandler {
                  onHoveredChanged: if (hovered) { root.cursorActive = true; root.cursor = sliderBlock.index }
                }
              }

              Text {
                width: parent.width
                wrapMode: Text.WordWrap
                text: sliderBlock.modelData.hint
                color: Qt.darker(root.bar.foreground, 1.6)
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
                leftPadding: Style.space(6)
              }
            }
          }

          // ---------- Advanced ----------
          PanelSeparator { foreground: root.bar.foreground }
          Row {
            spacing: Style.space(8)
            anchors.horizontalCenter: parent.horizontalCenter
            Button {
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
              text: root.showAdvanced ? "Hide advanced" : "Advanced tuning"
              onClicked: root.showAdvanced = !root.showAdvanced
            }
            Button {
              visible: root.showAdvanced
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
              text: "Reset to defaults"
              onClicked: root.resetTuning()
            }
          }
        }
      }
    }
  }
}
