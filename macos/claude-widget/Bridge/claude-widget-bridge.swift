#!/usr/bin/env swift
// Claude Widget Bridge
// Reads statusline JSON files and writes a consolidated snapshot
// for consumption by WidgetKit or other external tools.
//
// Usage: swift claude-widget-bridge.swift
// Run via launchd every 30s for widget auto-refresh.

import Foundation

// MARK: - Input Models

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

struct DailyCostLedger: Codable {
    let date: String?
    let sessions: [String: SessionEntry]?
}

struct SessionEntry: Codable {
    let baseline: Double
    let current: Double
}

struct RawStatus: Codable {
    let model: ModelInfo?
    let cost: CostInfo?
    let contextWindow: ContextInfo?
    enum CodingKeys: String, CodingKey {
        case model, cost
        case contextWindow = "context_window"
    }
}

struct ModelInfo: Codable {
    let displayName: String?
    enum CodingKeys: String, CodingKey { case displayName = "display_name" }
}

struct CostInfo: Codable {
    let totalCostUsd: Double?
    enum CodingKeys: String, CodingKey { case totalCostUsd = "total_cost_usd" }
}

struct ContextInfo: Codable {
    let usedPercentage: Double?
    enum CodingKeys: String, CodingKey { case usedPercentage = "used_percentage" }
}

// MARK: - Output Model (for widget consumption)

struct WidgetSnapshot: Codable {
    let timestamp: String
    let model: String
    let sessionCost: Double
    let dailyCost: Double
    let contextPct: Int
    let fiveHourPct: Int
    let fiveHourReset: String
    let sevenDayPct: Int
    let sevenDayReset: String
    let extraPct: Int
    let extraUsed: Double
    let extraLimit: Double
    let sessionCount: Int
    let costHistory: [CostPoint]

    enum CodingKeys: String, CodingKey {
        case timestamp, model
        case sessionCost = "session_cost"
        case dailyCost = "daily_cost"
        case contextPct = "context_pct"
        case fiveHourPct = "five_hour_pct"
        case fiveHourReset = "five_hour_reset"
        case sevenDayPct = "seven_day_pct"
        case sevenDayReset = "seven_day_reset"
        case extraPct = "extra_pct"
        case extraUsed = "extra_used"
        case extraLimit = "extra_limit"
        case sessionCount = "session_count"
        case costHistory = "cost_history"
    }
}

struct CostPoint: Codable {
    let hour: String
    let cost: Double
}

// MARK: - Helpers

func readJSON<T: Decodable>(_ path: String) -> T? {
    guard let data = FileManager.default.contents(atPath: path) else { return nil }
    return try? JSONDecoder().decode(T.self, from: data)
}

// MARK: - Main

let home = NSHomeDirectory()
let claudeDir = "\(home)/.claude"
let tmpDir = "/tmp/claude"
let outputPath = "\(claudeDir)/widget-snapshot.json"

// Read sources
let usage: UsageCache? = readJSON("\(tmpDir)/statusline-usage-cache.json")
let ledger: DailyCostLedger? = readJSON("\(claudeDir)/daily-cost.json")
let raw: RawStatus? = readJSON("\(tmpDir)/statusline-raw.json")

// Compute daily cost
let today = ISO8601DateFormatter().string(from: Date()).prefix(10)
var dailyCost = 0.0
var sessionCount = 0
if ledger?.date == String(today), let sessions = ledger?.sessions {
    dailyCost = sessions.values.reduce(0) { $0 + ($1.current - $1.baseline) }
    sessionCount = sessions.count
}

// Load existing cost history and append current hour
var costHistory: [CostPoint] = []
if let existing: WidgetSnapshot = readJSON(outputPath) {
    costHistory = existing.costHistory
}

let hourFormatter = DateFormatter()
hourFormatter.dateFormat = "yyyy-MM-dd'T'HH:00:00"
let currentHour = hourFormatter.string(from: Date())

// Update or append the current hour's cost
if let lastIdx = costHistory.lastIndex(where: { $0.hour == currentHour }) {
    costHistory[lastIdx] = CostPoint(hour: currentHour, cost: dailyCost)
} else {
    costHistory.append(CostPoint(hour: currentHour, cost: dailyCost))
}

// Keep last 24 hours
if costHistory.count > 24 {
    costHistory = Array(costHistory.suffix(24))
}

// Build snapshot
let isoFormatter = ISO8601DateFormatter()
let snapshot = WidgetSnapshot(
    timestamp: isoFormatter.string(from: Date()),
    model: raw?.model?.displayName ?? "—",
    sessionCost: raw?.cost?.totalCostUsd ?? 0,
    dailyCost: dailyCost,
    contextPct: Int(raw?.contextWindow?.usedPercentage ?? 0),
    fiveHourPct: Int(usage?.fiveHour?.utilization ?? 0),
    fiveHourReset: usage?.fiveHour?.resetsAt ?? "",
    sevenDayPct: Int(usage?.sevenDay?.utilization ?? 0),
    sevenDayReset: usage?.sevenDay?.resetsAt ?? "",
    extraPct: Int(usage?.extraUsage?.utilization ?? 0),
    extraUsed: (usage?.extraUsage?.usedCredits ?? 0) / 100,
    extraLimit: (usage?.extraUsage?.monthlyLimit ?? 0) / 100,
    sessionCount: sessionCount,
    costHistory: costHistory
)

// Write atomically
let encoder = JSONEncoder()
encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
if let data = try? encoder.encode(snapshot) {
    let tmpPath = outputPath + ".tmp"
    try? data.write(to: URL(fileURLWithPath: tmpPath))
    try? FileManager.default.moveItem(atPath: tmpPath, toPath: outputPath)
}

print("Updated \(outputPath)")
