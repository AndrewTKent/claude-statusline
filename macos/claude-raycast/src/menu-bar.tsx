import { MenuBarExtra, Icon, Color } from "@raycast/api";
import { getDashboardState } from "./lib/data";

function pctColor(pct: number): Color {
  if (pct >= 90) return Color.Red;
  if (pct >= 70) return Color.Yellow;
  if (pct >= 50) return Color.Orange;
  return Color.Green;
}

export default function MenuBar() {
  const state = getDashboardState();

  const title = `$${state.sessionCost.toFixed(2)} | ${state.fiveHourPct}%`;

  return (
    <MenuBarExtra icon={Icon.Terminal} title={title}>
      <MenuBarExtra.Section title="Cost">
        <MenuBarExtra.Item title={`Session: $${state.sessionCost.toFixed(2)}`} />
        <MenuBarExtra.Item title={`Today: $${state.dailyCost.toFixed(2)}`} />
        {state.sessionCount > 1 && (
          <MenuBarExtra.Item title={`${state.sessionCount} active sessions`} />
        )}
      </MenuBarExtra.Section>

      <MenuBarExtra.Section title="Rate Limits">
        <MenuBarExtra.Item
          title={`5-Hour: ${state.fiveHourPct}%`}
          icon={{ source: Icon.Clock, tintColor: pctColor(state.fiveHourPct) }}
        />
        <MenuBarExtra.Item
          title={`Weekly: ${state.sevenDayPct}%`}
          icon={{ source: Icon.Calendar, tintColor: pctColor(state.sevenDayPct) }}
        />
        {state.extraLimit > 0 && (
          <MenuBarExtra.Item
            title={`Extra: $${state.extraUsed.toFixed(2)} / $${state.extraLimit.toFixed(2)}`}
          />
        )}
      </MenuBarExtra.Section>

      <MenuBarExtra.Section title="Context">
        <MenuBarExtra.Item
          title={`Context: ${state.contextPct}%`}
          icon={{ source: Icon.Document, tintColor: pctColor(state.contextPct) }}
        />
      </MenuBarExtra.Section>

      <MenuBarExtra.Section>
        <MenuBarExtra.Item title={state.model} icon={Icon.Monitor} />
      </MenuBarExtra.Section>
    </MenuBarExtra>
  );
}
