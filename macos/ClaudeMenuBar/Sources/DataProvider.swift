import Foundation
import Combine

class DataProvider: ObservableObject {
    @Published var state = DashboardState()

    private var timer: Timer?
    private let decoder = JSONDecoder()

    private let usageCachePath = "/tmp/claude/statusline-usage-cache.json"
    private let profileCachePath = "/tmp/claude/statusline-profile-cache.json"
    private let rawStatusPath = "/tmp/claude/statusline-raw.json"
    private var dailyCostPath: String { "\(NSHomeDirectory())/.claude/daily-cost.json" }

    init() {
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            self?.refresh()
        }
    }

    deinit {
        timer?.invalidate()
    }

    func refresh() {
        var newState = DashboardState()

        // Rate limits
        if let data = readFile(usageCachePath),
           let usage = try? decoder.decode(UsageCache.self, from: data) {
            newState.fiveHourPct = Int(usage.fiveHour?.utilization ?? 0)
            newState.fiveHourReset = usage.fiveHour?.resetsAt ?? ""
            newState.sevenDayPct = Int(usage.sevenDay?.utilization ?? 0)
            newState.sevenDayReset = usage.sevenDay?.resetsAt ?? ""

            if usage.extraUsage?.isEnabled == true {
                newState.extraPct = Int(usage.extraUsage?.utilization ?? 0)
                newState.extraUsed = (usage.extraUsage?.usedCredits ?? 0) / 100
                newState.extraLimit = (usage.extraUsage?.monthlyLimit ?? 0) / 100
            }
        }

        // Profile
        if let data = readFile(profileCachePath),
           let profile = try? decoder.decode(ProfileCache.self, from: data) {
            newState.accountEmail = profile.account?.email ?? ""
            newState.accountName = profile.account?.fullName ?? ""
        }

        // Raw status (model, cost, context)
        if let data = readFile(rawStatusPath),
           let raw = try? decoder.decode(RawStatus.self, from: data) {
            newState.model = raw.model?.displayName ?? "—"
            newState.sessionCost = raw.cost?.totalCostUsd ?? 0
            newState.contextPct = Int(raw.contextWindow?.usedPercentage ?? 0)
        }

        // Daily cost
        if let data = readFile(dailyCostPath),
           let ledger = try? decoder.decode(DailyCostLedger.self, from: data) {
            let today = ISO8601DateFormatter().string(from: Date()).prefix(10)
            if ledger.date == String(today) {
                let total = ledger.sessions?.values.reduce(0.0) { $0 + ($1.current - $1.baseline) } ?? 0
                newState.dailyCost = total
                newState.sessionCount = ledger.sessions?.count ?? 0
            }
        }

        DispatchQueue.main.async {
            self.state = newState
        }
    }

    private func readFile(_ path: String) -> Data? {
        try? Data(contentsOf: URL(fileURLWithPath: path))
    }
}
