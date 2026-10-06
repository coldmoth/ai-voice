import AppKit
import Foundation
import WebKit
import UserNotifications
import Carbon.HIToolbox

private enum GlassMode: String { case native = "native-glass", css = "css-glass" }

private struct BackendLayout {
    let resources: URL
    let root: URL
    let bundledPython: URL
    let packaged: Bool

    static func resolve() -> BackendLayout {
        let bundle = Bundle.main
        let resources = bundle.resourceURL ?? bundle.bundleURL
        let bundledPython = resources.appendingPathComponent("python/bin/python3")
        return BackendLayout(resources: resources, root: bundle.bundleURL.deletingLastPathComponent(),
                             bundledPython: bundledPython,
                             packaged: FileManager.default.isExecutableFile(atPath: bundledPython.path))
    }

    var dataHome: URL {
        if packaged {
            return FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
                .appendingPathComponent("AI Voice", isDirectory: true)
        }
        return root.appendingPathComponent("state", isDirectory: true)
    }

    var locales: URL {
        return packaged ? resources.appendingPathComponent("app/ai_voice/locales")
                        : root.appendingPathComponent("src/ai_voice/locales")
    }
}

private struct Strings {
    let lang: String
    let dict: [String: String]
    let fallback: [String: String]

    static func load(override: String? = nil) -> Strings {
        let layout = BackendLayout.resolve()
        let preferences = layout.dataHome.appendingPathComponent("desktop.json")
        let lang: String
        if let override = override, override == "en" || override == "ru" {
            lang = override
        } else if !FileManager.default.fileExists(atPath: preferences.path) {
            lang = "en"
        } else if let data = try? Data(contentsOf: preferences),
                  let object = try? JSONSerialization.jsonObject(with: data),
                  let values = object as? [String: Any],
                  let stored = values["language"] as? String, stored == "en" || stored == "ru" {
            lang = stored
        } else {
            lang = "ru"
        }
        func read(_ language: String) -> [String: String] {
            let url = layout.locales.appendingPathComponent(language + ".json")
            guard let data = try? Data(contentsOf: url),
                  let object = try? JSONSerialization.jsonObject(with: data),
                  let values = object as? [String: String] else { return [:] }
            return values
        }
        let fallback = read("en")
        return Strings(lang: lang, dict: lang == "en" ? fallback : read(lang), fallback: fallback)
    }

    func t(_ key: String) -> String {
        return dict[key] ?? fallback[key] ?? key
    }
}

private func resolveGlassMode() -> GlassMode {
    if ProcessInfo.processInfo.environment["AI_VOICE_FORCE_CSS_GLASS"] == "1" { return .css }
    #if compiler(>=6.2)
    if #available(macOS 26.0, *), NSClassFromString("NSGlassEffectView") != nil { return .native }
    #endif
    return .css
}

/// Carbon delivers hot key press/release events on the main thread; the app delegate forwards them to JS.
private func hotKeyEventHandler(_ next: EventHandlerCallRef?, _ event: EventRef?, _ userData: UnsafeMutableRawPointer?) -> OSStatus {
    guard let event, let userData else { return OSStatus(eventNotHandledErr) }
    let app = Unmanaged<VoiceApp>.fromOpaque(userData).takeUnretainedValue()
    app.hotKeyEvent(pressed: GetEventKind(event) == UInt32(kEventHotKeyPressed))
    return noErr
}

final class VoiceApp: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    private var L = Strings.load()
    private static let externalHosts: Set<String> = ["fish.audio", "huggingface.co", "github.com",
                                                     "docs.fish.audio", "coldmoth.github.io", "existential.audio"]
    private var window: NSWindow!
    private var webView: WKWebView!
    private var backend: Process?
    private var exiting = false
    private var glassModeName = GlassMode.css.rawValue
    private var notificationRequested = false
    private var hotKeyRef: EventHotKeyRef?
    private var hotKeyHandler: EventHandlerRef?
    private var hotKeyDown = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        installMenu()
        let mode = resolveGlassMode()
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1100, height: 720),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = "AI Voice"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        let toolbar = NSToolbar(identifier: "main")
        toolbar.showsBaselineSeparator = false
        window.toolbar = toolbar
        if #available(macOS 11.0, *) {
            window.toolbarStyle = .unified
            window.titlebarSeparatorStyle = .none
        }
        window.minSize = NSSize(width: 820, height: 560)
        window.isOpaque = false
        window.backgroundColor = .clear
        if !window.setFrameUsingName("AI Voice Desktop") { window.center() }
        window.setFrameAutosaveName("AI Voice Desktop")
        let configuration = WKWebViewConfiguration()
        let js = "document.documentElement.classList.add('\(mode.rawValue)');document.documentElement.dataset.glass='\(mode.rawValue)';document.documentElement.style.setProperty('--titlebar-h','52px');"
        configuration.userContentController.addUserScript(
            WKUserScript(source: js, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        // The web view covers the transparent title bar, so dragging is forwarded to AppKit.
        let dragJS = "document.addEventListener('mousedown',function(e){if(e.button!==0)return;var t=e.target;if(!t||!t.closest||!t.closest('.n-toolbar,.n-traffic-spacer'))return;if(t.closest('button,input,select,textarea,a'))return;window.webkit.messageHandlers.drag.postMessage(1);},true);"
        configuration.userContentController.addUserScript(
            WKUserScript(source: dragJS, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        configuration.userContentController.add(self, name: "drag")
        configuration.userContentController.add(self, name: "notify")
        configuration.userContentController.add(self, name: "hotkey")
        configuration.userContentController.add(self, name: "language")
        let bounds = window.contentView!.bounds
        webView = WKWebView(frame: bounds, configuration: configuration)
        webView.autoresizingMask = [.width, .height]
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.setValue(false, forKey: "drawsBackground")
        if #available(macOS 12.0, *) { webView.underPageBackgroundColor = .clear }
        installContainer(mode: mode, bounds: bounds)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        webView.loadHTMLString("<html class='\(glassModeName)'><body style='background:transparent;color:#8a909b;font:15px -apple-system;padding:40px'>\(L.t("common.opening"))</body></html>", baseURL: nil)
        launchBackend()
    }

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        if message.name == "language", let language = message.body as? String,
           language == "en" || language == "ru" {
            L = Strings.load(override: language)
            installMenu()
        }
        if message.name == "drag", let event = NSApp.currentEvent { window.performDrag(with: event) }
        if message.name == "hotkey", let values = message.body as? [String: Any] {
            if values["action"] as? String == "register",
               let code = (values["key_code"] as? NSNumber)?.uint32Value,
               let modifiers = (values["modifiers"] as? NSNumber)?.uint32Value {
                if !registerHotKey(code: code, modifiers: modifiers) {
                    webView.evaluateJavaScript("window.aiVoiceHotkeyError&&aiVoiceHotkeyError()")
                }
            } else if values["action"] as? String == "unregister" {
                unregisterHotKey()
            }
        }
        if message.name == "notify", let values = message.body as? [String: String],
           let title = values["title"], let body = values["body"] {
            let center = UNUserNotificationCenter.current()
            let deliver = {
                guard !NSApp.isActive else { return }
                let content = UNMutableNotificationContent()
                content.title = title
                content.body = body
                center.add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
            }
            if !notificationRequested {
                notificationRequested = true
                center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
                    if granted { DispatchQueue.main.async(execute: deliver) }
                }
            } else {
                deliver()
            }
        }
    }

    private func installHotKeyHandler() -> Bool {
        guard hotKeyHandler == nil else { return true }
        var types = [EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed)),
                     EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyReleased))]
        let status = InstallEventHandler(GetApplicationEventTarget(), hotKeyEventHandler, types.count, &types,
                                         Unmanaged.passUnretained(self).toOpaque(), &hotKeyHandler)
        guard status == noErr else { hotKeyHandler = nil; return false }
        return true
    }

    /// RegisterEventHotKey needs no Accessibility permission. A new registration replaces the old one.
    private func registerHotKey(code: UInt32, modifiers: UInt32) -> Bool {
        unregisterHotKey()
        guard installHotKeyHandler() else { return false }
        let id = EventHotKeyID(signature: OSType(0x41495648), id: 1)  // 'AIVH'
        var ref: EventHotKeyRef?
        let status = RegisterEventHotKey(code, modifiers, id, GetApplicationEventTarget(), 0, &ref)
        guard status == noErr, let ref else { return false }
        hotKeyRef = ref
        return true
    }

    private func unregisterHotKey() {
        if let ref = hotKeyRef { UnregisterEventHotKey(ref) }
        hotKeyRef = nil
        hotKeyDown = false
    }

    fileprivate func hotKeyEvent(pressed: Bool) {
        // Ignore a repeated press without a release in between.
        if pressed == hotKeyDown { return }
        hotKeyDown = pressed
        webView.evaluateJavaScript("window.aiVoiceHotkey&&aiVoiceHotkey('\(pressed ? "down" : "up")')")
    }

    private func installContainer(mode: GlassMode, bounds: NSRect) {
        glassModeName = mode.rawValue
        #if compiler(>=6.2)
        if mode == .native, #available(macOS 26.0, *) {
            let glass = NSGlassEffectView(frame: bounds)
            glass.autoresizingMask = [.width, .height]
            glass.cornerRadius = 0
            glass.contentView = webView
            window.contentView = glass
            return
        }
        #endif
        let fx = NSVisualEffectView(frame: bounds)
        fx.material = .underWindowBackground
        fx.blendingMode = .behindWindow
        fx.state = .followsWindowActiveState
        fx.autoresizingMask = [.width, .height]
        webView.frame = fx.bounds
        fx.addSubview(webView)
        window.contentView = fx
    }

    private func installMenu() {
        let menu = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu(title: "AI Voice")
        let settingsItem = NSMenuItem(title: L.t("menu.settings"), action: #selector(openSettings), keyEquivalent: ",")
        settingsItem.target = self
        appMenu.addItem(settingsItem)
        let keyItem = NSMenuItem(title: L.t("menu.fish_key"), action: #selector(openFishKeySettings), keyEquivalent: "")
        keyItem.target = self
        appMenu.addItem(keyItem)
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: L.t("menu.quit"), action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        menu.addItem(appItem)

        let editItem = NSMenuItem()
        let editMenu = NSMenu(title: L.t("menu.edit"))
        // A nil target routes editing actions to the focused WebKit responder.
        for (title, action, key) in [
            (L.t("menu.undo"), "undo:", "z"),
            (L.t("menu.cut"), "cut:", "x"),
            (L.t("menu.copy"), "copy:", "c"),
            (L.t("menu.paste"), "paste:", "v"),
            (L.t("menu.select_all"), "selectAll:", "a")
        ] {
            editMenu.addItem(withTitle: title, action: Selector(action), keyEquivalent: key)
        }
        editItem.submenu = editMenu
        menu.addItem(editItem)
        NSApp.mainMenu = menu
    }

    /// Packaged app: Python, code, UI and the speech helper live inside the bundle,
    /// user data in ~/Library/Application Support/AI Voice. A bare binary next to
    /// the project checkout (development) falls back to the repository layout.
    private func launchBackend() {
        let bundle = Bundle.main
        let layout = BackendLayout.resolve()
        let resources = layout.resources
        let bundledPython = layout.bundledPython
        let process = Process()
        var environment = ProcessInfo.processInfo.environment
        if layout.packaged {
            let support = layout.dataHome
            try? FileManager.default.createDirectory(at: support, withIntermediateDirectories: true)
            let appDir = resources.appendingPathComponent("app")
            environment["AI_VOICE_RESOURCES"] = appDir.path
            environment["AI_VOICE_WEB"] = resources.appendingPathComponent("web").path
            environment["AI_VOICE_HELPER"] = bundle.bundleURL.appendingPathComponent("Contents/Helpers/SpeechHelper.app").path
            environment["AI_VOICE_HOME"] = support.path
            environment["AI_VOICE_VC_RUNTIME"] = support.appendingPathComponent("vc-runtime").path
            environment["PYTHONPATH"] = appDir.path
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            environment["PYTHONNOUSERSITE"] = "1"
            process.executableURL = bundledPython
            process.currentDirectoryURL = support
        } else {
            let root = layout.root
            process.executableURL = root.appendingPathComponent(".venv/bin/python")
            process.currentDirectoryURL = root
        }
        process.environment = environment
        process.arguments = ["-u", "-m", "ai_voice.desktop", "--parent-pid", String(ProcessInfo.processInfo.processIdentifier)]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        process.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                guard let self, !self.exiting else { return }
                self.showError(self.L.t("errors.shell_backend_exited"))
            }
        }
        backend = process
        do {
            try process.run()
        } catch {
            showError(L.t("errors.shell_python_failed"))
            return
        }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            var data = Data()
            while !data.contains(10) {
                let chunk = pipe.fileHandleForReading.availableData
                if chunk.isEmpty { break }
                data.append(chunk)
                if data.count > 8192 { break }
            }
            let firstLine = data.split(separator: 10).first.map { Data($0) } ?? Data()
            let object = try? JSONSerialization.jsonObject(with: firstLine) as? [String: String]
            let url = object?["url"].flatMap(URL.init(string:))
            DispatchQueue.main.async {
                guard let self, !self.exiting else { return }
                guard let url, url.host == "127.0.0.1", url.scheme == "http" else {
                    self.showError(self.L.t("errors.shell_ui_failed"))
                    return
                }
                self.webView.load(URLRequest(url: url))
            }
        }
    }

    @objc private func openSettings() {
        webView.evaluateJavaScript("window.aiVoiceOpenSettings&&window.aiVoiceOpenSettings()")
    }

    @objc private func openFishKeySettings() {
        webView.evaluateJavaScript("window.aiVoiceOpenSettings&&window.aiVoiceOpenSettings('api')")
    }

    private func showError(_ message: String) {
        webView.loadHTMLString("<html class='\(glassModeName)'><body style='background:transparent;color:#8a909b;font:15px -apple-system;padding:40px'><h2>AI Voice</h2><p>\(message)</p></body></html>", baseURL: nil)
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { decisionHandler(.cancel); return }
        if url.scheme == "about" || url.host == "127.0.0.1" {
            decisionHandler(.allow)
        } else if url.scheme == "x-apple.systempreferences" {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
        } else {
            if url.scheme == "https", let host = url.host, Self.externalHosts.contains(host) { NSWorkspace.shared.open(url) }
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url, url.scheme == "x-apple.systempreferences" {
            NSWorkspace.shared.open(url)
        } else if let url = action.request.url, url.scheme == "https", let host = url.host, Self.externalHosts.contains(host) {
            NSWorkspace.shared.open(url)
        }
        return nil
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) {
        exiting = true
        unregisterHotKey()
        if let backend, backend.isRunning { backend.terminate() }
    }
}

let app = NSApplication.shared
let delegate = VoiceApp()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
