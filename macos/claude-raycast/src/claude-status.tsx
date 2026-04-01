import { List, Color, Icon } from "@raycast/api";
import { getDashboardState } from "./lib/data";

function pctColor(pct: number): Color {
  if (pct >= 90) return Color.Red;
  if (pct >= 70) return Color.Yellow;
  if (pct >= 50) return Color.Orange;
  return Color.Green;
}

function pctBar(pct: number, width = 10): string {
  const filled = Math.round((pct / 100) * width);
  return "●".repeat(filled) + "○".repeat(width - filled);
}

export default function ClaudeStatus() {
  const state = getDashboardState();

  return (
    <List>
      <List.Section title="Session">
        <List.Item
          icon={Icon.Monitor}
          title="Model"
          accessories={[{ text: state.model }]}
        />
        <List.Item
          icon={Icon.BankNote}
          title="Session Cost"
          accessories={[{ text: `$${state.sessionCost.toFixed(2)}` }]}
        />
        <List.Item
          icon={Icon.Calendar}
          title="Daily Cost"
          accessories={[
            { text: `$${state.dailyCost.toFixed(2)}` },
            ...(state.sessionCount > 1
              ? [{ text: `${state.sessionCount} sessions`, color: Color.SecondaryText }]
              : []),
          ]}
        />
      </List.Section>

      <List.Section title="Rate Limits">
        <List.Item
          icon={Icon.Clock}
          title="5-Hour"
          subtitle={pctBar(state.fiveHourPct)}
          accessories={[
            { text: `${state.fiveHourPct}%`, color: pctColor(state.fiveHourPct) },
          ]}
        />
        <List.Item
          icon={Icon.Calendar}
          title="Weekly"
          subtitle={pctBar(state.sevenDayPct)}
          accessories={[
            { text: `${state.sevenDayPct}%`, color: pctColor(state.sevenDayPct) },
          ]}
        />
        {state.extraLimit > 0 && (
          <List.Item
            icon={Icon.CreditCard}
            title="Extra Credits"
            subtitle={pctBar(state.extraPct)}
            accessories={[
              { text: `$${state.extraUsed.toFixed(2)} / $${state.extraLimit.toFixed(2)}` },
            ]}
          />
        )}
      </List.Section>

      <List.Section title="Context">
        <List.Item
          icon={Icon.Document}
          title="Context Window"
          subtitle={pctBar(state.contextPct)}
          accessories={[
            { text: `${state.contextPct}%`, color: pctColor(state.contextPct) },
          ]}
        />
      </List.Section>

      {state.accountEmail && (
        <List.Section title="Account">
          <List.Item
            icon={Icon.Person}
            title={state.accountName || state.accountEmail}
            accessories={[
              ...(state.accountName ? [{ text: state.accountEmail, color: Color.SecondaryText }] : []),
            ]}
          />
        </List.Section>
      )}
    </List>
  );
}
