// Optimystery's own macOS Accessibility adapter. No coordinates, sockets or dependencies.
// Only executed by the user's explicit capture command. Bare `inspect` is read-only.
// `inspect --request-access` may ask macOS to display its Accessibility prompt.
import Cocoa
import ApplicationServices

enum Failure: Error, CustomStringConvertible {
    case message(String)
    var description: String { switch self { case .message(let s): return s } }
}
func attr(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success ? value : nil
}
func str(_ e: AXUIElement, _ name: String) -> String { attr(e, name) as? String ?? "" }
func children(_ e: AXUIElement) -> [AXUIElement] { attr(e, kAXChildrenAttribute) as? [AXUIElement] ?? [] }
func title(_ e: AXUIElement) -> String {
    let s = str(e, kAXTitleAttribute)
    return s.isEmpty ? str(e, kAXDescriptionAttribute) : s
}
func elements(_ root: AXUIElement, limit: Int = 12000) -> [AXUIElement] {
    var pending = [(root, 0)], result: [AXUIElement] = []
    while let (e, depth) = pending.popLast(), result.count < limit {
        result.append(e)
        if depth < 24 { pending.append(contentsOf: children(e).map { ($0, depth + 1) }) }
    }
    return result
}
func action(_ e: AXUIElement, _ name: String) throws {
    guard AXUIElementPerformAction(e, name as CFString) == .success else {
        throw Failure.message("Live did not accept the native action: " + name)
    }
}
func actions(_ e: AXUIElement) -> [String] {
    var value: CFArray?
    AXUIElementCopyActionNames(e, &value)
    return value as? [String] ?? []
}
func firstElement(_ root: AXUIElement, _ test: (AXUIElement) -> Bool) -> AXUIElement? {
    var pending = [(root, 0)], visited = 0
    while let (e, depth) = pending.popLast(), visited < 12000 {
        visited += 1
        if test(e) { return e }
        if depth < 24 { pending.append(contentsOf: children(e).map { ($0, depth + 1) }) }
    }
    return nil
}
func viewRoots(_ app: AXUIElement, named: String) -> [AXUIElement] {
    var pending = attr(app, kAXWindowsAttribute) as? [AXUIElement] ?? []
    var result: [AXUIElement] = [], index = 0
    while index < pending.count && index < 12000 {
        let e = pending[index]; index += 1
        let role = str(e, kAXRoleAttribute), label = title(e)
        if role == kAXGroupRole && label == named { result.append(e); continue }
        // These contain rows/content, not another application panel. Breadth-first
        // container discovery avoids walking thousands of Session/Browser cells.
        if [kAXOutlineRole, kAXTableRole, kAXRowRole, kAXCellRole].contains(role) { continue }
        if role == kAXGroupRole && ["Browser", "Clip Detail", "Slots"].contains(label) { continue }
        pending.append(contentsOf: children(e))
    }
    return result
}
func waitElement(_ root: AXUIElement, seconds: Double = 4, _ test: (AXUIElement) -> Bool) -> AXUIElement? {
    let end = Date().addingTimeInterval(seconds)
    repeat {
        if let e = firstElement(root, test) { return e }
        Thread.sleep(forTimeInterval: 0.08)
    } while Date() < end
    return nil
}
func pressKey(_ key: CGKeyCode) {
    CGEvent(keyboardEventSource: nil, virtualKey: key, keyDown: true)?.post(tap: .cghidEventTap)
    CGEvent(keyboardEventSource: nil, virtualKey: key, keyDown: false)?.post(tap: .cghidEventTap)
}
func emit(_ value: [String: Any]) throws {
    let bytes = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    print(String(data: bytes, encoding: .utf8)!)
}
func menu(_ app: AXUIElement, _ parent: String, _ item: String) throws -> AXUIElement {
    guard let bar = attr(app, kAXMenuBarAttribute) else { throw Failure.message("Live menu bar unavailable") }
    let barElement = unsafeBitCast(bar, to: AXUIElement.self)
    guard let group = children(barElement).first(where: { title($0) == parent }),
          let match = elements(group).first(where: { str($0, kAXRoleAttribute) == kAXMenuItemRole && title($0) == item }) else {
        throw Failure.message("Native menu unavailable: " + parent + " > " + item)
    }
    return match
}
func hasModal(_ app: AXUIElement) -> Bool {
    let windows = attr(app, kAXWindowsAttribute) as? [AXUIElement] ?? []
    return windows.contains { window in
        (attr(window, kAXModalAttribute) as? Bool == true) || elements(window, limit: 500).contains { str($0, kAXRoleAttribute) == kAXSheetRole }
    }
}
func sampleLabelMatches(role: String, title: String, value: String, actions: [String], stems: Set<String>) -> Bool {
    return role == kAXStaticTextRole && (stems.contains(title) || stems.contains(value)) && actions.contains(kAXShowMenuAction)
}
func sampleContexts(_ app: AXUIElement, stems: Set<String>) -> [AXUIElement] {
    // Live 12 exposes the sample filename as AXValue on static text inside Clip
    // Detail. Browser rows/cells repeat that filename and must not be candidates.
    let details = viewRoots(app, named: "Clip Detail")
    return details.flatMap { elements($0) }.filter {
        sampleLabelMatches(role: str($0, kAXRoleAttribute), title: title($0), value: str($0, kAXValueAttribute), actions: actions($0), stems: stems)
    }
}
func selectedBrowserRows(_ browser: AXUIElement) -> [AXUIElement] {
    var pending = [browser], rows: [AXUIElement] = [], count = 0
    while let e = pending.popLast(), count < 12000 {
        count += 1
        let role = str(e, kAXRoleAttribute)
        if role == kAXOutlineRole || role == kAXTableRole {
            if title(e) != "Browser Sidebar" {
                for row in attr(e, kAXSelectedRowsAttribute) as? [AXUIElement] ?? [] {
                    if !rows.contains(where: { CFEqual($0, row) }) { rows.append(row) }
                }
            }
            continue
        }
        pending.append(contentsOf: children(e))
    }
    return rows
}
func rename(_ app: AXUIElement, old: String, new: String, clip: String) throws {
    guard !hasModal(app) else { throw Failure.message("Close Live's dialog before Keep Take") }
    let stems = Set([old, (old as NSString).deletingPathExtension])
    var candidates: [AXUIElement] = []
    let contextDeadline = Date().addingTimeInterval(4)
    repeat {
        candidates = sampleContexts(app, stems: stems)
        if candidates.count == 1 { break }
        Thread.sleep(forTimeInterval: 0.1)
    } while Date() < contextDeadline
    guard candidates.count == 1 else {
        throw Failure.message("Cannot identify one Clip View sample context for " + old + " (found \(candidates.count)). No file was renamed.")
    }
    try action(candidates[0], kAXShowMenuAction)
    guard let reveal = waitElement(app, { str($0, kAXRoleAttribute) == kAXMenuItemRole && title($0) == "Show in Browser" }) else {
        pressKey(53)
        throw Failure.message("Live did not expose Show in Browser for this sample. No file was renamed.")
    }
    try action(reveal, kAXPressAction)
    // Show in Browser has completed; use its actual panel, not a global tree scan.
    var browsers = viewRoots(app, named: "Browser")
    let browserDeadline = Date().addingTimeInterval(4)
    while browsers.isEmpty && Date() < browserDeadline {
        Thread.sleep(forTimeInterval: 0.08); browsers = viewRoots(app, named: "Browser")
    }
    guard browsers.count == 1, let browser = browsers.first else {
        throw Failure.message("Live Browser unavailable")
    }
    var selected: [AXUIElement] = []
    let deadline = Date().addingTimeInterval(4)
    repeat {
        selected = selectedBrowserRows(browser)
            .filter { row in elements(row).contains { stems.contains(title($0)) || stems.contains(str($0, kAXValueAttribute)) } }
        if selected.count == 1 { break }
        Thread.sleep(forTimeInterval: 0.1)
    } while Date() < deadline
    guard selected.count == 1,
          elements(selected[0]).contains(where: { stems.contains(title($0)) || stems.contains(str($0, kAXValueAttribute)) }) else {
        throw Failure.message("The Browser selection is not the expected file: " + old)
    }
    try action(menu(app, "Edit", "Rename"), kAXPressAction)
    let filenameEditor: (AXUIElement) -> Bool = { e in
        str(e, kAXRoleAttribute) == kAXTextFieldRole && stems.contains(str(e, kAXValueAttribute)) && attr(e, kAXFocusedAttribute) as? Bool == true
    }
    guard let field = firstElement(browser, filenameEditor) ?? waitElement(app, filenameEditor) else {
        pressKey(53); throw Failure.message("Native filename editor did not match " + old)
    }
    let existing = str(field, kAXValueAttribute)
    let replacement = existing == old ? new : (new as NSString).deletingPathExtension
    guard AXUIElementSetAttributeValue(field, kAXValueAttribute as CFString, replacement as CFString) == .success else {
        pressKey(53); throw Failure.message("Live filename field is not writable through Accessibility")
    }
    pressKey(36)
    Thread.sleep(forTimeInterval: 0.2)
    if hasModal(app) { throw Failure.message("Live displayed a rename dialog; finish or cancel it before continuing") }
}

do {
    let args = Array(CommandLine.arguments.dropFirst())
    guard let command = args.first, ["inspect", "save", "rename"].contains(command) else {
        throw Failure.message("Usage: capture-native inspect | save | rename OLD NEW CLIP")
    }
    let apps = NSWorkspace.shared.runningApplications.filter { $0.bundleIdentifier == "com.ableton.live" }
    guard apps.count == 1 else { throw Failure.message("Open exactly one Ableton Live instance") }
    let requestAccess = command == "inspect" && args.dropFirst().contains("--request-access")
    let trusted = requestAccess
        ? AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary)
        : AXIsProcessTrusted()
    guard trusted else {
        let outcome = command == "inspect" ? "No capture was started." : "Operation stopped; check the capture recovery details."
        throw Failure.message("Accessibility permission is required. For opticapture in Live, enable \(apps[0].localizedName ?? "Ableton Live") in System Settings > Privacy & Security > Accessibility, then retry the action. If macOS names a different calling app in its prompt, enable that app. \(outcome)")
    }
    let app = AXUIElementCreateApplication(apps[0].processIdentifier)
    if command == "inspect" {
        let file = try menu(app, "File", "Save Live Set")
        try emit(["pid": Int(apps[0].processIdentifier), "save_command": title(file), "modal": hasModal(app)])
    } else {
        apps[0].activate(options: [.activateIgnoringOtherApps])
        guard !hasModal(app) else { throw Failure.message("Close the open Live dialog before capture") }
        if command == "save" {
            let item = try menu(app, "File", "Save Live Set")
            let enabled = attr(item, kAXEnabledAttribute) as? Bool ?? false
            if enabled { try action(item, kAXPressAction) }
            try emit(["invoked": enabled, "pid": Int(apps[0].processIdentifier)])
        } else {
            guard args.count == 4 else { throw Failure.message("rename needs old filename, new filename and clip name") }
            try rename(app, old: args[1], new: args[2], clip: args[3])
            try emit(["renamed": true])
        }
    }
} catch {
    FileHandle.standardError.write((String(describing: error) + "\n").data(using: .utf8)!)
    exit(1)
}
