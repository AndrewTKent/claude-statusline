import Foundation

// MARK: - Rate Limit Usage Cache
struct UsageCache: Codable {
    let fiveHour: RateWindow?
    let sevenDay: RateWindow?
    let extraUsage: ExtraUsage?

    enum CodingKeys: String, CodingKey {
        case fiveHour = "five_hour"
        case sevenDay = "seven_day"
        case extraUsage = "extra_usage"
    }
}

struct RateWindow: Codable {
    let utilization: Double?
    let resetsAt: String?

    enum CodingKeys: String, CodingKey {
        case utilization
        case resetsAt = "resets_at"
    }
}

struct ExtraUsage: Codable {
    let isEnabled: Bool?
    let utilization: Double?
    let usedCredits: Double?
    let monthlyLimit: Double?

    enum CodingKeys: String, CodingKey {
        case isEnabled = "is_enabled"
        case utilization
        case usedCredits = "used_credits"
        case monthlyLimit = "monthly_limit"
    }
}

// MARK: - Profile Cache
struct ProfileCache: Codable {
    let account: Account?
}

struct Account: Codable {
    let email: String?
    let fullName: String?

    enum CodingKeys: String, CodingKey {
        case email
        case fullName = "full_name"
    }
}

// MARK: - Daily Cost Ledger
struct DailyCostLedger: Codable {
    let date: String?
    let sessions: [String: SessionEntry]?
}

struct SessionEntry: Codable {
    let baseline: Double
    let current: Double
}

// MARK: - Raw Status (from Claude Code stdin JSON)
struct RawStatus: Codable {
    let model: ModelInfo?
    let cost: CostInfo?
    let contextWindow: ContextInfo?
    let sessionId: String?

    enum CodingKeys: String, CodingKey {
        case model
        case cost
        case contextWindow = "context_window"
        case sessionId = "session_id"
    }
}

struct ModelInfo: Codable {
    let displayName: String?
    enum CodingKeys: String, CodingKey {
        case displayName = "display_name"
    }
}

struct CostInfo: Codable {
    let totalCostUsd: Double?
    let totalDurationMs: Double?
    enum CodingKeys: String, CodingKey {
        case totalCostUsd = "total_cost_usd"
        case totalDurationMs = "total_duration_ms"
    }
}

struct ContextInfo: Codable {
    let usedPercentage: Double?
    enum CodingKeys: String, CodingKey {
        case usedPercentage = "used_percentage"
    }
}

// MARK: - Aggregated Dashboard State
struct DashboardState {
    var model: String = "—"
    var sessionCost: Double = 0
    var dailyCost: Double = 0
    var contextPct: Int = 0
    var fiveHourPct: Int = 0
    var fiveHourReset: String = ""
    var sevenDayPct: Int = 0
    var sevenDayReset: String = ""
    var extraPct: Int = 0
    var extraUsed: Double = 0
    var extraLimit: Double = 0
    var accountEmail: String = ""
    var accountName: String = ""
    var sessionCount: Int = 0

    var overallHealth: Health {
        if fiveHourPct >= 90 || contextPct >= 90 { return .critical }
        if fiveHourPct >= 70 || contextPct >= 70 || sevenDayPct >= 80 { return .warning }
        return .ok
    }

    enum Health {
        case ok, warning, critical
    }
}
