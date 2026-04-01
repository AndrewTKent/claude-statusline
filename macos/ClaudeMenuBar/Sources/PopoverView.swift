import SwiftUI

struct PopoverView: View {
    @ObservedObject var data: DataProvider

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            // Header
            HStack {
                Text("Claude Code")
                    .font(.headline)
                Spacer()
                Text(data.state.model)
                    .font(.caption)
                    .foregroundColor(.secondary)
            }

            if !data.state.accountEmail.isEmpty {
                Text(data.state.accountName.isEmpty ? data.state.accountEmail : data.state.accountName)
                    .font(.caption)
                    .foregroundColor(.secondary)
            }

            Divider()

            // Cost
            HStack {
                Label("Session", systemImage: "dollarsign.circle")
                Spacer()
                Text(String(format: "$%.2f", data.state.sessionCost))
                    .fontWeight(.medium)
            }

            HStack {
                Label("Today", systemImage: "calendar")
                Spacer()
                Text(String(format: "$%.2f", data.state.dailyCost))
                    .foregroundColor(.secondary)
                if data.state.sessionCount > 1 {
                    Text("(\(data.state.sessionCount) sessions)")
                        .font(.caption2)
                        .foregroundColor(.secondary)
                }
            }

            Divider()

            // Context
            GaugeRow(
                label: "Context",
                icon: "doc.text",
                pct: data.state.contextPct
            )

            // Rate limits
            GaugeRow(
                label: "5-Hour",
                icon: "clock",
                pct: data.state.fiveHourPct
            )

            GaugeRow(
                label: "Weekly",
                icon: "calendar.badge.clock",
                pct: data.state.sevenDayPct
            )

            if data.state.extraLimit > 0 {
                HStack {
                    Label("Extra", systemImage: "creditcard")
                    Spacer()
                    Text(String(format: "$%.2f / $%.2f", data.state.extraUsed, data.state.extraLimit))
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
            }

            Divider()

            Button("Refresh") {
                data.refresh()
            }
            .buttonStyle(.borderless)
            .font(.caption)
        }
        .padding(16)
        .frame(width: 280)
    }
}

struct GaugeRow: View {
    let label: String
    let icon: String
    let pct: Int

    var color: Color {
        if pct >= 90 { return .red }
        if pct >= 70 { return .yellow }
        if pct >= 50 { return .orange }
        return .green
    }

    var body: some View {
        HStack {
            Label(label, systemImage: icon)
            Spacer()
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    RoundedRectangle(cornerRadius: 3)
                        .fill(Color.gray.opacity(0.2))
                    RoundedRectangle(cornerRadius: 3)
                        .fill(color)
                        .frame(width: geo.size.width * CGFloat(min(pct, 100)) / 100)
                }
            }
            .frame(width: 80, height: 8)
            Text("\(pct)%")
                .font(.caption)
                .frame(width: 36, alignment: .trailing)
        }
    }
}
