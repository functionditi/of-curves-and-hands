import AppKit
import AVFoundation
import Carbon.HIToolbox
import GameController
import WebKit

final class BorderlessFullscreenWindow: NSWindow {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}

final class InteractionBlockerView: NSView {
    override var acceptsFirstResponder: Bool { true }

    override func hitTest(_ point: NSPoint) -> NSView? {
        self
    }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool {
        true
    }
}

struct LaunchConfiguration {
    let url: URL
    let title: String
    let screenNumber: Int?
    let requiresCamera: Bool
    let sessionName: String?
    let tmuxPath: String?

    static func parse(arguments: [String]) -> LaunchConfiguration? {
        var urlString: String?
        var title = "Plotter Window"
        var screenNumber: Int?
        var requiresCamera = false
        var sessionName: String?
        var tmuxPath: String?

        var index = 0
        while index < arguments.count {
            let argument = arguments[index]
            switch argument {
            case "--title":
                index += 1
                guard index < arguments.count else {
                    return nil
                }
                title = arguments[index]
            case "--screen":
                index += 1
                guard index < arguments.count, let value = Int(arguments[index]), value > 0 else {
                    return nil
                }
                screenNumber = value
            case "--camera":
                requiresCamera = true
            case "--session-name":
                index += 1
                guard index < arguments.count else {
                    return nil
                }
                sessionName = arguments[index]
            case "--tmux-path":
                index += 1
                guard index < arguments.count else {
                    return nil
                }
                tmuxPath = arguments[index]
            default:
                guard !argument.hasPrefix("--"), urlString == nil else {
                    return nil
                }
                urlString = argument
            }
            index += 1
        }

        guard let urlString, let url = URL(string: urlString) else {
            return nil
        }

        return LaunchConfiguration(
            url: url,
            title: title,
            screenNumber: screenNumber,
            requiresCamera: requiresCamera,
            sessionName: sessionName,
            tmuxPath: tmuxPath
        )
    }
}

final class PlotterWindowAppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate {
    private enum WindowMode {
        case compact
        case fullscreen
    }

    private enum ShortcutAction {
        case compact
        case fullscreen
        case quit
    }

    private let configuration: LaunchConfiguration
    private var window: NSWindow?
    private var webView: WKWebView?
    private var keyMonitor: Any?
    private var globalKeyMonitor: Any?
    private var hotKeyRef: EventHotKeyRef?
    private var hotKeyHandlerRef: EventHandlerRef?
    private var controllerConnectObserver: NSObjectProtocol?
    private var controllerDisconnectObserver: NSObjectProtocol?
    private var previousPresentationOptions: NSApplication.PresentationOptions = []
    private var activeControllerID: ObjectIdentifier?
    private var activePlotterIndex: Int?
    private var isImmersivePresentationActive = false
    private var isCursorHidden = false
    private let allowedHosts = Set(["localhost", "127.0.0.1"])
    private let controlledBundleIdentifiers = [
        "com.ofcurvesandhands.plotterclient",
        "com.ofcurvesandhands.plotterdashboard",
    ]
    private let qKeyCode: UInt16 = 12

    init(configuration: LaunchConfiguration) {
        self.configuration = configuration
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildWindow()
        installKeyMonitor()
        installGameControllerMonitoring()
        applyWindowMode(.fullscreen, animated: false)
        if configuration.requiresCamera {
            requestCameraAccessAndLoad()
        } else {
            loadClient()
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let keyMonitor {
            NSEvent.removeMonitor(keyMonitor)
            self.keyMonitor = nil
        }
        if let globalKeyMonitor {
            NSEvent.removeMonitor(globalKeyMonitor)
            self.globalKeyMonitor = nil
        }
        if let hotKeyRef {
            UnregisterEventHotKey(hotKeyRef)
            self.hotKeyRef = nil
        }
        if let hotKeyHandlerRef {
            RemoveEventHandler(hotKeyHandlerRef)
            self.hotKeyHandlerRef = nil
        }
        if let controllerConnectObserver {
            NotificationCenter.default.removeObserver(controllerConnectObserver)
            self.controllerConnectObserver = nil
        }
        if let controllerDisconnectObserver {
            NotificationCenter.default.removeObserver(controllerDisconnectObserver)
            self.controllerDisconnectObserver = nil
        }
        leaveImmersivePresentationMode()
        showCursor()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    private func enterImmersivePresentationMode() {
        guard !isImmersivePresentationActive else {
            return
        }
        previousPresentationOptions = NSApp.presentationOptions
        NSApp.presentationOptions = [.hideDock, .hideMenuBar]
        isImmersivePresentationActive = true
    }

    private func leaveImmersivePresentationMode() {
        guard isImmersivePresentationActive else {
            return
        }
        NSApp.presentationOptions = previousPresentationOptions
        isImmersivePresentationActive = false
    }

    private func hideCursor() {
        guard !isCursorHidden else {
            return
        }
        NSCursor.hide()
        isCursorHidden = true
    }

    private func showCursor() {
        guard isCursorHidden else {
            return
        }
        NSCursor.unhide()
        isCursorHidden = false
    }

    private func orderedScreens() -> [NSScreen] {
        let allScreens = NSScreen.screens
        guard let mainScreen = NSScreen.main else {
            return allScreens.sorted { screenNumber(for: $0) < screenNumber(for: $1) }
        }

        let otherScreens = allScreens
            .filter { $0 !== mainScreen }
            .sorted { screenNumber(for: $0) < screenNumber(for: $1) }
        return [mainScreen] + otherScreens
    }

    private func targetScreen() -> NSScreen? {
        let screens = orderedScreens()
        guard let screenNumber = configuration.screenNumber else {
            return screens.first ?? NSScreen.main
        }

        let index = screenNumber - 1
        guard index >= 0, index < screens.count else {
            return screens.first ?? NSScreen.main
        }
        return screens[index]
    }

    private func screenNumber(for screen: NSScreen) -> Int {
        let value = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber
        return value?.intValue ?? 0
    }

    private func buildWindow() {
        let targetScreen = targetScreen()
        let initialFrame = targetScreen?.frame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let webConfiguration = WKWebViewConfiguration()
        webConfiguration.websiteDataStore = .default()
        webConfiguration.defaultWebpagePreferences.allowsContentJavaScript = true

        let webView = WKWebView(frame: initialFrame, configuration: webConfiguration)
        webView.uiDelegate = self
        webView.navigationDelegate = self
        webView.autoresizingMask = [.width, .height]

        let window = BorderlessFullscreenWindow(
            contentRect: initialFrame,
            styleMask: [.borderless],
            backing: .buffered,
            defer: false
        )
        window.backgroundColor = .black
        window.hasShadow = false
        window.isMovable = false
        window.level = .normal
        window.title = configuration.title
        window.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle]
        window.isReleasedWhenClosed = false
        if let targetScreen {
            window.setFrame(targetScreen.frame, display: true)
        }
        let containerView = NSView(frame: initialFrame)
        containerView.autoresizingMask = [.width, .height]

        webView.frame = containerView.bounds
        containerView.addSubview(webView)

        let blockerView = InteractionBlockerView(frame: containerView.bounds)
        blockerView.autoresizingMask = [.width, .height]
        containerView.addSubview(blockerView)

        window.contentView = containerView
        window.orderFrontRegardless()
        window.makeKeyAndOrderFront(nil)
        window.makeFirstResponder(blockerView)

        self.window = window
        self.webView = webView

        NSApp.activate(ignoringOtherApps: true)
    }

    private func installKeyMonitor() {
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: [.keyDown]) { [weak self] event in
            guard let self else {
                return event
            }

            self.handleShortcutAction(for: event)

            return nil
        }

        globalKeyMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.keyDown]) { [weak self] event in
            guard let self, let action = self.shortcutAction(for: event) else {
                return
            }

            DispatchQueue.main.async {
                self.performShortcutAction(action)
            }
        }

        installCarbonHotKey()
    }

    private func installGameControllerMonitoring() {
        guard configuration.requiresCamera else {
            return
        }

        controllerConnectObserver = NotificationCenter.default.addObserver(
            forName: NSNotification.Name.GCControllerDidConnect,
            object: nil,
            queue: .main
        ) { [weak self] notification in
            guard let self, let controller = notification.object as? GCController else {
                return
            }
            self.configureGameController(controller)
            self.pushStatusToClient("Controller connected. Press X, B, Y, or A to choose a plotter.")
        }

        controllerDisconnectObserver = NotificationCenter.default.addObserver(
            forName: NSNotification.Name.GCControllerDidDisconnect,
            object: nil,
            queue: .main
        ) { [weak self] notification in
            guard let self else {
                return
            }
            if let controller = notification.object as? GCController,
               self.activeControllerID == ObjectIdentifier(controller) {
                self.activeControllerID = nil
            }
            self.pushStatusToClient("Controller disconnected.")
        }

        for controller in GCController.controllers() {
            configureGameController(controller)
        }
    }

    private func configureGameController(_ controller: GCController) {
        guard let gamepad = controller.extendedGamepad else {
            return
        }

        activeControllerID = ObjectIdentifier(controller)

        gamepad.buttonX.pressedChangedHandler = { [weak self, weak controller] _, _, pressed in
            guard pressed, let self, let controller else {
                return
            }
            self.selectPlotter(index: 1, buttonLabel: "X", controller: controller)
        }
        gamepad.buttonB.pressedChangedHandler = { [weak self, weak controller] _, _, pressed in
            guard pressed, let self, let controller else {
                return
            }
            self.selectPlotter(index: 2, buttonLabel: "B", controller: controller)
        }
        gamepad.buttonY.pressedChangedHandler = { [weak self, weak controller] _, _, pressed in
            guard pressed, let self, let controller else {
                return
            }
            self.selectPlotter(index: 3, buttonLabel: "Y", controller: controller)
        }
        gamepad.buttonA.pressedChangedHandler = { [weak self, weak controller] _, _, pressed in
            guard pressed, let self, let controller else {
                return
            }
            self.selectPlotter(index: 4, buttonLabel: "A", controller: controller)
        }
        gamepad.leftTrigger.pressedChangedHandler = { [weak self, weak controller] _, _, pressed in
            guard pressed, let self, let controller else {
                return
            }
            self.sendServoToggleCommand(buttonLabel: "B6", controller: controller)
        }
        gamepad.dpad.up.pressedChangedHandler = { [weak self, weak controller] _, _, pressed in
            guard pressed, let self, let controller else {
                return
            }
            self.sendPenCommand(path: "/controller/pen-up", successPrefix: "Pen up", controller: controller)
        }
        gamepad.dpad.down.pressedChangedHandler = { [weak self, weak controller] _, _, pressed in
            guard pressed, let self, let controller else {
                return
            }
            self.sendPenCommand(path: "/controller/pen-down", successPrefix: "Pen down", controller: controller)
        }
        gamepad.buttonHome?.pressedChangedHandler = { [weak self, weak controller] _, _, pressed in
            guard pressed, let self, let controller else {
                return
            }
            self.quitFromController(buttonLabel: "B16", controller: controller)
        }
    }

    private func activateController(_ controller: GCController) {
        activeControllerID = ObjectIdentifier(controller)
    }

    private func plotterLabel(for index: Int) -> String {
        guard index >= 1 && index <= 26,
              let scalar = UnicodeScalar(64 + index) else {
            return "#\(index)"
        }
        return String(scalar)
    }

    private func selectPlotter(index: Int, buttonLabel: String, controller: GCController) {
        activateController(controller)
        activePlotterIndex = index
        postBridgeCommand(
            path: "/controller/select-plotter",
            body: [
                "plotter_index": index,
                "source": "native-gamecontroller",
            ],
            successPrefix: "Controller selection via \(buttonLabel)"
        )
    }

    private func sendPenCommand(path: String, successPrefix: String, controller: GCController) {
        activateController(controller)
        guard let plotterIndex = activePlotterIndex else {
            pushStatusToClient("Pick a plotter first with X, B, Y, or A.")
            return
        }

        let plotterLabel = plotterLabel(for: plotterIndex)
        postBridgeCommand(
            path: path,
            body: [
                "plotter_index": plotterIndex,
                "source": "native-gamecontroller",
            ],
            successPrefix: "\(successPrefix) on Plotter \(plotterLabel)"
        )
    }

    private func sendServoToggleCommand(buttonLabel: String, controller: GCController) {
        activateController(controller)
        guard let plotterIndex = activePlotterIndex else {
            pushStatusToClient("Pick a plotter first with X, B, Y, or A.")
            return
        }

        let plotterLabel = plotterLabel(for: plotterIndex)
        postBridgeCommand(
            path: "/controller/servo-toggle",
            body: [
                "plotter_index": plotterIndex,
                "source": "native-gamecontroller",
            ],
            successPrefix: "Servo toggle via \(buttonLabel) on Plotter \(plotterLabel)"
        )
    }

    private func quitFromController(buttonLabel: String, controller: GCController) {
        activateController(controller)
        pushStatusToClient("Quitting via \(buttonLabel)")
        terminateControlledWindows()
    }

    private func bridgeCommandURL(path: String) -> URL? {
        guard var components = URLComponents(url: configuration.url, resolvingAgainstBaseURL: false) else {
            return nil
        }
        components.path = path.hasPrefix("/") ? path : "/\(path)"
        components.query = nil
        components.fragment = nil
        return components.url
    }

    private func postBridgeCommand(path: String, body: [String: Any], successPrefix: String) {
        guard let url = bridgeCommandURL(path: path) else {
            pushStatusToClient("\(successPrefix): could not build bridge URL")
            return
        }

        let payload: Data
        do {
            payload = try JSONSerialization.data(withJSONObject: body, options: [])
        } catch {
            pushStatusToClient("\(successPrefix): could not encode request")
            return
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 3.0
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = payload

        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            guard let self else {
                return
            }

            if let error {
                self.pushStatusToClient("\(successPrefix): could not reach plotter bridge (\(error.localizedDescription))")
                return
            }

            let httpResponse = response as? HTTPURLResponse
            let responsePayload = (data.flatMap { try? JSONSerialization.jsonObject(with: $0, options: []) }) as? [String: Any]
            let message = (responsePayload?["message"] as? String) ?? "Bridge error (\(httpResponse?.statusCode ?? -1))"
            self.pushStatusToClient("\(successPrefix): \(message)")
        }.resume()
    }

    private func pushStatusToClient(_ message: String) {
        let escaped = message
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "'", with: "\\'")
            .replacingOccurrences(of: "\n", with: "\\n")

        DispatchQueue.main.async { [weak self] in
            self?.webView?.evaluateJavaScript(
                "window.ofCurvesAndHandsNativeStatus && window.ofCurvesAndHandsNativeStatus('\(escaped)');",
                completionHandler: nil
            )
        }
    }

    private func shortcutAction(for event: NSEvent) -> ShortcutAction? {
        let relevantFlags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        guard relevantFlags.isEmpty else {
            return nil
        }
        if event.keyCode == qKeyCode {
            return .quit
        }
        switch event.charactersIgnoringModifiers?.lowercased() {
        case "s":
            return .compact
        case "f":
            return .fullscreen
        default:
            return nil
        }
    }

    private func handleShortcutAction(for event: NSEvent) {
        guard let action = shortcutAction(for: event) else {
            return
        }
        performShortcutAction(action)
    }

    private func performShortcutAction(_ action: ShortcutAction) {
        switch action {
        case .compact:
            applyWindowMode(.compact, animated: true)
        case .fullscreen:
            applyWindowMode(.fullscreen, animated: true)
        case .quit:
            terminateControlledWindows()
        }
    }

    private func compactFrame(for screen: NSScreen?) -> NSRect {
        let referenceFrame = screen?.visibleFrame ?? NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let width = min(max(referenceFrame.width * 0.78, 960), 1440)
        let height = min(max(referenceFrame.height * 0.78, 600), 900)
        let originX = referenceFrame.midX - (width / 2)
        let originY = referenceFrame.midY - (height / 2)
        return NSRect(x: originX, y: originY, width: width, height: height).integral
    }

    private func applyWindowMode(_ mode: WindowMode, animated: Bool) {
        guard let window else {
            return
        }

        let screen = targetScreen() ?? window.screen ?? NSScreen.main
        let frame: NSRect

        switch mode {
        case .fullscreen:
            enterImmersivePresentationMode()
            hideCursor()
            frame = screen?.frame ?? window.frame
        case .compact:
            leaveImmersivePresentationMode()
            showCursor()
            frame = compactFrame(for: screen)
        }

        if let screen {
            window.setFrame(frame, display: true, animate: animated)
            window.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle]
            if mode == .fullscreen {
                window.setFrame(screen.frame, display: true, animate: animated)
            }
        } else {
            window.setFrame(frame, display: true, animate: animated)
        }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func shouldHandleQuitEvent(_ event: NSEvent) -> Bool {
        shortcutAction(for: event) == .quit
    }

    private func installCarbonHotKey() {
        var eventType = EventTypeSpec(
            eventClass: OSType(kEventClassKeyboard),
            eventKind: UInt32(kEventHotKeyPressed)
        )
        let userData = UnsafeMutableRawPointer(Unmanaged.passUnretained(self).toOpaque())

        let installStatus = InstallEventHandler(
            GetApplicationEventTarget(),
            { _, _, userData in
                guard let userData else {
                    return noErr
                }
                let delegate = Unmanaged<PlotterWindowAppDelegate>.fromOpaque(userData).takeUnretainedValue()
                delegate.terminateControlledWindows()
                return noErr
            },
            1,
            &eventType,
            userData,
            &hotKeyHandlerRef
        )
        guard installStatus == noErr else {
            return
        }

        let hotKeyID = EventHotKeyID(signature: OSType(0x504C5452), id: 1)
        let registerStatus = RegisterEventHotKey(
            UInt32(qKeyCode),
            0,
            hotKeyID,
            GetApplicationEventTarget(),
            0,
            &hotKeyRef
        )
        if registerStatus != noErr {
            hotKeyRef = nil
        }
    }

    private func terminateControlledWindows() {
        killTmuxSessionIfNeeded()
        for bundleIdentifier in controlledBundleIdentifiers {
            let applications = NSRunningApplication.runningApplications(withBundleIdentifier: bundleIdentifier)
            for application in applications {
                application.terminate()
            }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
            NSApp.terminate(nil)
        }
    }

    private func killTmuxSessionIfNeeded() {
        guard let sessionName = configuration.sessionName,
              !sessionName.isEmpty,
              let tmuxPath = configuration.tmuxPath,
              !tmuxPath.isEmpty else {
            return
        }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: tmuxPath)
        process.arguments = ["kill-session", "-t", sessionName]
        process.standardOutput = Pipe()
        process.standardError = Pipe()
        try? process.run()
    }

    private func requestCameraAccessAndLoad() {
        loadPlaceholder(
            title: "Starting Plotter Client",
            body: "Requesting camera access and loading the hand-tracking client..."
        )

        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized:
            loadClient()
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .video) { granted in
                DispatchQueue.main.async {
                    if granted {
                        self.loadClient()
                    } else {
                        self.showCameraAccessInstructions()
                    }
                }
            }
        case .denied, .restricted:
            showCameraAccessInstructions()
        @unknown default:
            showCameraAccessInstructions()
        }
    }

    private func showCameraAccessInstructions() {
        loadPlaceholder(
            title: "Camera Access Needed",
            body: """
            Plotter Client needs camera access for hand tracking.

            Open System Settings > Privacy & Security > Camera and allow access for Plotter Client, then relaunch.
            """
        )
    }

    private func loadClient() {
        webView?.load(URLRequest(url: configuration.url))
    }

    private func loadPlaceholder(title: String, body: String) {
        let escapedTitle = title
            .replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
        let escapedBody = body
            .replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
            .replacingOccurrences(of: "\n", with: "<br>")

        let html = """
        <!doctype html>
        <html lang="en">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>\(escapedTitle)</title>
          <style>
            :root {
              color-scheme: light;
              font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
            }
            body {
              margin: 0;
              min-height: 100vh;
              display: grid;
              place-items: center;
              background: #0156fb;
              color: #ffffff;
            }
            main {
              width: min(720px, calc(100vw - 48px));
              padding: 32px;
              border: 1px solid rgba(255, 255, 255, 0.28);
              background: rgba(255, 255, 255, 0.1);
              backdrop-filter: blur(12px);
            }
            h1 {
              margin: 0 0 16px;
              font-size: 2.2rem;
              line-height: 1;
            }
            p {
              margin: 0;
              line-height: 1.6;
              font-size: 1.05rem;
            }
          </style>
        </head>
        <body>
          <main>
            <h1>\(escapedTitle)</h1>
            <p>\(escapedBody)</p>
          </main>
        </body>
        </html>
        """

        webView?.loadHTMLString(html, baseURL: nil)
    }

    func webView(
        _ webView: WKWebView,
        requestMediaCapturePermissionFor origin: WKSecurityOrigin,
        initiatedByFrame frame: WKFrameInfo,
        type: WKMediaCaptureType,
        decisionHandler: @escaping (WKPermissionDecision) -> Void
    ) {
        guard configuration.requiresCamera else {
            decisionHandler(.deny)
            return
        }

        let normalizedHost = origin.host.lowercased()
        guard allowedHosts.contains(normalizedHost) else {
            decisionHandler(.deny)
            return
        }

        switch type {
        case .camera:
            decisionHandler(.grant)
        case .microphone, .cameraAndMicrophone:
            decisionHandler(.deny)
        @unknown default:
            decisionHandler(.deny)
        }
    }

    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        if let url = navigationAction.request.url {
            NSWorkspace.shared.open(url)
        }
        return nil
    }
}

guard let configuration = LaunchConfiguration.parse(arguments: Array(CommandLine.arguments.dropFirst())) else {
    let alert = NSAlert()
    alert.messageText = "Plotter Window"
    alert.informativeText = "Usage: PlotterWindowApp <url> [--title <title>] [--screen <1-based number>] [--camera] [--session-name <name>] [--tmux-path <path>]"
    alert.runModal()
    exit(1)
}

let application = NSApplication.shared
let delegate = PlotterWindowAppDelegate(configuration: configuration)
application.setActivationPolicy(.regular)
application.delegate = delegate
application.run()
