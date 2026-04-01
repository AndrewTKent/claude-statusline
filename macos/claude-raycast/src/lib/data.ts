import { readFileSync, existsSync } from "fs";
import { join } from "path";
import { homedir } from "os";

const CLAUDE_DIR = join(homedir(), ".claude");
const TMP_DIR = "/tmp/claude";

interface RateWindow {
  utilization?: number;
  resets_at?: string;
}

interface ExtraUsage {
  is_enabled?: boolean;
  utilization?: number;
  used_credits?: number;
  monthly_limit?: number;
}

interface UsageCache {
  five_hour?: RateWindow;
  seven_day?: RateWindow;
  extra_usage?: ExtraUsage;
}

interface ProfileCache {
  account?: {
    email?: string;
    full_name?: string;
  };
}

interface SessionEntry {
  baseline: number;
  current: number;
}

interface DailyCostLedger {
  date?: string;
  sessions?: Record<string, SessionEntry>;
}

interface RawStatus {
  model?: { display_name?: string };
  cost?: { total_cost_usd?: number; total_duration_ms?: number };
  context_window?: { used_percentage?: number };
  session_id?: string;
}

export interface DashboardState {
  model: string;
  sessionCost: number;
  dailyCost: number;
  contextPct: number;
  fiveHourPct: number;
  fiveHourReset: string;
  sevenDayPct: number;
  sevenDayReset: string;
  extraPct: number;
  extraUsed: number;
  extraLimit: number;
  accountEmail: string;
  accountName: string;
  sessionCount: number;
}

function readJSON<T>(path: string): T | null {
  if (!existsSync(path)) return null;
  try {
    return JSON.parse(readFileSync(path, "utf-8")) as T;
  } catch {
    return null;
  }
}

export function getDashboardState(): DashboardState {
  const state: DashboardState = {
    model: "—",
    sessionCost: 0,
    dailyCost: 0,
    contextPct: 0,
    fiveHourPct: 0,
    fiveHourReset: "",
    sevenDayPct: 0,
    sevenDayReset: "",
    extraPct: 0,
    extraUsed: 0,
    extraLimit: 0,
    accountEmail: "",
    accountName: "",
    sessionCount: 0,
  };

  // Rate limits
  const usage = readJSON<UsageCache>(join(TMP_DIR, "statusline-usage-cache.json"));
  if (usage) {
    state.fiveHourPct = Math.round(usage.five_hour?.utilization ?? 0);
    state.fiveHourReset = usage.five_hour?.resets_at ?? "";
    state.sevenDayPct = Math.round(usage.seven_day?.utilization ?? 0);
    state.sevenDayReset = usage.seven_day?.resets_at ?? "";
    if (usage.extra_usage?.is_enabled) {
      state.extraPct = Math.round(usage.extra_usage.utilization ?? 0);
      state.extraUsed = (usage.extra_usage.used_credits ?? 0) / 100;
      state.extraLimit = (usage.extra_usage.monthly_limit ?? 0) / 100;
    }
  }

  // Profile
  const profile = readJSON<ProfileCache>(join(TMP_DIR, "statusline-profile-cache.json"));
  if (profile?.account) {
    state.accountEmail = profile.account.email ?? "";
    state.accountName = profile.account.full_name ?? "";
  }

  // Raw status
  const raw = readJSON<RawStatus>(join(TMP_DIR, "statusline-raw.json"));
  if (raw) {
    state.model = raw.model?.display_name ?? "—";
    state.sessionCost = raw.cost?.total_cost_usd ?? 0;
    state.contextPct = Math.round(raw.context_window?.used_percentage ?? 0);
  }

  // Daily cost
  const ledger = readJSON<DailyCostLedger>(join(CLAUDE_DIR, "daily-cost.json"));
  if (ledger?.sessions) {
    const today = new Date().toISOString().slice(0, 10);
    if (ledger.date === today) {
      state.dailyCost = Object.values(ledger.sessions).reduce(
        (sum, s) => sum + (s.current - s.baseline),
        0
      );
      state.sessionCount = Object.keys(ledger.sessions).length;
    }
  }

  return state;
}
