import Cocoa
import SwiftUI

@main
struct ClaudeMenuBarApp {
    static let data = DataProvider()
    static var statusItem: NSStatusItem!
    static var popover = NSPopover()
    static var eventMonitor: Any?

    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory) // No dock icon

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)

        if let button = statusItem.button {
            updateIcon(health: .ok, button: button)
            button.action = #selector(AppDelegate.togglePopover(_:))
        }

        popover.contentSize = NSSize(width: 280, height: 400)
        popover.behavior = .transient
        popover.contentViewController = NSHostingController(rootView: PopoverView(data: data))

        let delegate = AppDelegate()
        app.delegate = delegate

        // Watch for health changes
        Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { _ in
            if let button = statusItem.button {
                updateIcon(health: data.state.overallHealth, button: button)
            }
        }

        app.run()
    }

    static func updateIcon(health: DashboardState.Health, button: NSStatusBarButton) {
        let symbolName: String
        let color: NSColor

        switch health {
        case .ok:
            symbolName = "circle.fill"
            color = .systemGreen
        case .warning:
            symbolName = "exclamationmark.circle.fill"
            color = .systemYellow
        case .critical:
            symbolName = "exclamationmark.triangle.fill"
            color = .systemRed
        }

        if let image = NSImage(systemSymbolName: symbolName, accessibilityDescription: "Claude Status") {
            let config = NSImage.SymbolConfiguration(pointSize: 14, weight: .regular)
            let configured = image.withSymbolConfiguration(config)!
            button.image = configured
            button.contentTintColor = color
        }
    }
}

class AppDelegate: NSObject, NSApplicationDelegate {
    @objc func togglePopover(_ sender: AnyObject?) {
        if ClaudeMenuBarApp.popover.isShown {
            ClaudeMenuBarApp.popover.performClose(sender)
        } else if let button = ClaudeMenuBarApp.statusItem.button {
            ClaudeMenuBarApp.popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)

            // Close on outside click
            ClaudeMenuBarApp.eventMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { _ in
                ClaudeMenuBarApp.popover.performClose(nil)
            }
        }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {}
}
