const TOKEN_SERIES = [
  ["input_tokens", "Input", "#5aa9ff"],
  ["cached_input_tokens", "Cache read", "#54d7c7"],
  ["cache_create_tokens", "Cache write", "#a68cff"],
  ["output_tokens", "Output excluding reasoning", "#ffac5a"],
  ["reasoning_tokens", "Reasoning", "#f06fa8"],
];
const TIMELINE_TOKEN_KEYS = new Set([...TOKEN_SERIES.map(([key]) => key), "total_tokens"]);
const ACCOUNT_COLORS = ["#54d7c7", "#5aa9ff", "#a68cff", "#ffac5a", "#f06fa8", "#75d58a", "#d7c56a"];
const filterNames = ["provider", "account", "model", "effort", "session", "agent"];
let latestTimeline = [];
let latestBucketMinutes = 1;
let capabilityToken = "";
if (typeof window !== "undefined") {
  const fragmentToken = new URLSearchParams(window.location.hash.slice(1)).get("token");
  if (fragmentToken) {
    window.sessionStorage.setItem("agent-metrics-capability", fragmentToken);
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
  }
  capabilityToken = window.sessionStorage.getItem("agent-metrics-capability") || "";
}

function compact(value) {
  const number = Number(value || 0);
  if (number >= 1e9) return `${(number / 1e9).toFixed(2)}B`;
  if (number >= 1e6) return `${(number / 1e6).toFixed(2)}M`;
  if (number >= 1e3) return `${(number / 1e3).toFixed(1)}k`;
  return number.toLocaleString();
}

function duration(ms) {
  if (ms == null) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60000).toFixed(1)}m`;
}

function percentChange(current, previous) {
  const before = Number(previous || 0);
  if (!before) return null;
  return (Number(current || 0) - before) / before * 100;
}

function changeText(value) {
  if (value == null) return "No prior activity";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(1)}%`;
}

function normalizeTimelineRows(rows, bucketMinutes) {
  return rows.map(row => Object.fromEntries(
    Object.entries(row).map(([key, value]) => [
      key,
      TIMELINE_TOKEN_KEYS.has(key)
        ? Number(value || 0) / bucketMinutes
        : value,
    ])
  ));
}

function changeClass(value) {
  if (value == null || Math.abs(value) < 0.05) return "delta-flat";
  return value > 0 ? "delta-up" : "delta-down";
}

function escaped(value) {
  const node = document.createElement("span");
  node.textContent = String(value ?? "");
  return node.innerHTML;
}

function queryString() {
  const params = new URLSearchParams();
  const range = Number(document.getElementById("range").value);
  if (range) params.set("since", String(Date.now() - range));
  for (const name of filterNames) {
    const value = document.getElementById(name).value;
    if (value) params.set(name, value);
  }
  return params.toString();
}

function setFilterOptions(data) {
  const mapping = { provider: "providers", account: "accounts", model: "models", effort: "efforts", session: "sessions", agent: "agents" };
  for (const [elementName, payloadName] of Object.entries(mapping)) {
    const select = document.getElementById(elementName);
    const selected = select.value;
    const options = [new Option("All", "")];
    for (const value of data.filters[payloadName]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = elementName === "account" ? (data.account_labels[value] || value.slice(0, 10)) : value;
      options.push(option);
    }
    select.replaceChildren(...options);
    if (options.some(option => option.value === selected)) select.value = selected;
  }
}

function seriesValue(row, key) {
  const value = Number(row[key] || 0);
  if (key !== "output_tokens") return value;
  return Math.max(0, value - Number(row.reasoning_tokens || 0));
}

function movingAverageRows(rows, windowMinutes) {
  if (windowMinutes <= 1) return rows.map(row => ({ ...row }));
  const windowMs = windowMinutes * 60000;
  let start = 0;
  return rows.map((row, index) => {
    const at = Number(row.minute);
    while (start < index && Number(rows[start].minute) < at - windowMs + 60000) start += 1;
    const actual = rows.slice(start, index + 1);
    const smoothed = { ...row };
    for (const [key] of TOKEN_SERIES) {
      smoothed[key] = actual.reduce((sum, sample) => sum + Number(sample[key] || 0), 0) / actual.length;
    }
    return smoothed;
  });
}

function hourlyDayRows(rows) {
  if (!rows.length) return [];
  const cutoff = Number(rows[rows.length - 1].minute) - 24 * 60 * 60000;
  const buckets = new Map();
  for (const row of rows) {
    if (Number(row.minute) < cutoff) continue;
    const hour = Math.floor(Number(row.minute) / 3600000) * 3600000;
    const bucket = buckets.get(hour) || { hour, total_tokens: 0 };
    bucket.total_tokens += Number(row.total_tokens || 0);
    buckets.set(hour, bucket);
  }
  let cumulative = 0;
  return [...buckets.values()].sort((a, b) => a.hour - b.hour).map(bucket => {
    cumulative += bucket.total_tokens;
    return { ...bucket, cumulative_tokens: cumulative };
  });
}

function selectedTokenSeries() {
  const checked = new Set([...document.querySelectorAll('#series-controls input:checked')].map(input => input.value));
  return TOKEN_SERIES.filter(([key]) => checked.has(key));
}

function axisLabel(timestamp, spanMs) {
  const date = new Date(timestamp);
  const time = date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (spanMs <= 36 * 60 * 60 * 1000) return time;
  return `${date.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
}

function renderOverview(data, backlog) {
  const cards = [
    ["Total", data.total_tokens, "tokens"],
    ["Input", data.input_tokens, "uncached"],
    ["Cache read", data.cached_input_tokens, "tokens"],
    ["Cache write", data.cache_create_tokens, "tokens"],
    ["Output", data.output_tokens, "tokens"],
    ["Reasoning", data.reasoning_tokens, "tokens"],
    ["Pending files", backlog.files, `${compact(backlog.bytes)} bytes`],
  ];
  document.getElementById("overview").innerHTML = cards.map(([label, value, suffix]) =>
    `<article class="card"><p class="eyebrow">${label}</p><div class="value">${compact(value)}</div><small>${suffix}</small></article>`
  ).join("");
}

function renderAnalysis(analysis) {
  const today = analysis.today;
  const yesterday = analysis.yesterday_same_time;
  const todayFresh = Number(today.input_tokens || 0) + Number(today.output_tokens || 0);
  const yesterdayFresh = Number(yesterday.input_tokens || 0) + Number(yesterday.output_tokens || 0);
  const totalChange = percentChange(today.total_tokens, yesterday.total_tokens);
  const freshChange = percentChange(todayFresh, yesterdayFresh);
  const cards = [
    ["Today so far", compact(today.total_tokens), "all token traffic, including cache", ""],
    ["Fresh tokens", compact(todayFresh), `${changeText(freshChange)} vs yesterday now`, changeClass(freshChange)],
    ["Cache read", compact(today.cached_input_tokens), "reused context served from cache", ""],
    ["Traffic change", changeText(totalChange), `${compact(yesterday.total_tokens)} by this time yesterday`, changeClass(totalChange)],
  ];
  document.getElementById("analysis-summary").innerHTML = cards.map(([label, value, note, css]) =>
    `<article class="analysis-card"><p class="eyebrow">${label}</p><div class="value ${css}">${value}</div><small>${note}</small></article>`
  ).join("");
  document.title = `Agent Metrics · ${compact(today.total_tokens)} today`;
}

function renderTimeline(rows, bucketMinutes = 1) {
  const target = document.getElementById("timeline");
  if (!rows.length) {
    target.innerHTML = '<div class="empty">Run <code>agent-metrics sync</code> to ingest local events.</div>';
    return;
  }
  const windowMinutes = Number(document.getElementById("smoothing").value);
  const normalizedRows = normalizeTimelineRows(rows, bucketMinutes);
  const displayedRows = movingAverageRows(normalizedRows, windowMinutes);
  const tokenSeries = selectedTokenSeries();
  const resolution = bucketMinutes === 1 ? "1m raw" : `${bucketMinutes}m server buckets`;
  document.getElementById("timeline-mode").textContent = windowMinutes === 1
    ? `${resolution} · tokens/min`
    : `${windowMinutes}m trailing average · ${resolution} · tokens/min`;
  const width = 1200, height = 265, left = 55, right = 12, top = 10, bottom = 28;
  const plotWidth = width - left - right, plotHeight = height - top - bottom;
  const max = Math.max(...displayedRows.map(row => tokenSeries.reduce((sum, [key]) => sum + seriesValue(row, key), 0)), 1);
  const peak = displayedRows.reduce((highest, row) =>
    Number(row.total_tokens || 0) > Number(highest.total_tokens || 0) ? row : highest
  );
  const cached = Number(peak.cached_input_tokens || 0) + Number(peak.cache_create_tokens || 0);
  const cachedShare = Number(peak.total_tokens || 0) ? cached / Number(peak.total_tokens) * 100 : 0;
  document.getElementById("timeline-insight").textContent =
    `Largest minute: ${compact(peak.total_tokens)} · ${cachedShare.toFixed(0)}% cache traffic`;
  const barWidth = Math.max(1, plotWidth / displayedRows.length - 1);
  let svg = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">`;
  for (let i = 0; i <= 4; i++) {
    const y = top + plotHeight * i / 4;
    svg += `<line class="grid-line" x1="${left}" x2="${width - right}" y1="${y}" y2="${y}"/>`;
    svg += `<text class="axis" x="0" y="${y + 3}">${compact(max * (4 - i) / 4)}</text>`;
  }
  displayedRows.forEach((row, index) => {
    const x = left + index * plotWidth / displayedRows.length;
    let y = top + plotHeight;
    for (const [key, , color] of tokenSeries) {
      const h = seriesValue(row, key) / max * plotHeight;
      y -= h;
      if (h > 0) svg += `<rect x="${x}" y="${y}" width="${barWidth}" height="${h}" fill="${color}" rx="1"/>`;
    }
  });
  const spanMs = Number(displayedRows[displayedRows.length - 1].minute) - Number(displayedRows[0].minute);
  for (let tick = 0; tick < 5; tick += 1) {
    const index = Math.round((displayedRows.length - 1) * tick / 4);
    const x = left + plotWidth * tick / 4;
    const anchor = tick === 0 ? "start" : tick === 4 ? "end" : "middle";
    svg += `<line class="time-grid" x1="${x}" x2="${x}" y1="${top}" y2="${top + plotHeight}"/>`;
    svg += `<text class="axis" text-anchor="${anchor}" x="${x}" y="${height - 5}">${axisLabel(displayedRows[index].minute, spanMs)}</text>`;
  }
  svg += "</svg>";
  target.innerHTML = svg;
}

function renderDayView(rows) {
  const target = document.getElementById("day-view");
  const hours = hourlyDayRows(rows);
  if (!hours.length) {
    target.innerHTML = '<div class="empty">No minute buckets in the selected range.</div>';
    return;
  }
  const width = 1200, height = 220, left = 55, right = 58, top = 12, bottom = 28;
  const plotWidth = width - left - right, plotHeight = height - top - bottom;
  const maxHourly = Math.max(...hours.map(row => row.total_tokens), 1);
  const maxCumulative = Math.max(hours[hours.length - 1].cumulative_tokens, 1);
  const barWidth = Math.max(3, plotWidth / hours.length - 3);
  let points = "";
  let svg = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">`;
  hours.forEach((row, index) => {
    const x = left + index * plotWidth / Math.max(hours.length - 1, 1);
    const barHeight = row.total_tokens / maxHourly * plotHeight;
    svg += `<rect class="hour-bar" x="${x - barWidth / 2}" y="${top + plotHeight - barHeight}" width="${barWidth}" height="${barHeight}" rx="2"/>`;
    const lineY = top + plotHeight - row.cumulative_tokens / maxCumulative * plotHeight;
    points += `${x},${lineY} `;
  });
  svg += `<polyline class="cumulative-line" points="${points.trim()}"/>`;
  svg += `<text class="axis" x="0" y="${top + 4}">${compact(maxHourly)}/h</text>`;
  svg += `<text class="axis" text-anchor="end" x="${width}" y="${top + 4}">${compact(maxCumulative)} total</text>`;
  svg += `<text class="axis" x="${left}" y="${height - 5}">${new Date(hours[0].hour).toLocaleString()}</text>`;
  svg += `<text class="axis" text-anchor="end" x="${width - right}" y="${height - 5}">${new Date(hours[hours.length - 1].hour).toLocaleString()}</text></svg>`;
  target.innerHTML = svg;
}

function renderDailyAccounts(rows, daily, labels) {
  const target = document.getElementById("daily-accounts");
  const totals = new Map();
  for (const row of rows) totals.set(row.account_id, (totals.get(row.account_id) || 0) + Number(row.total_tokens || 0));
  const accounts = [...totals].sort((a, b) => b[1] - a[1]).map(([account]) => account).slice(0, ACCOUNT_COLORS.length);
  const byDay = new Map(daily.map(row => [row.day, new Map()]));
  for (const row of rows) {
    if (byDay.has(row.day)) byDay.get(row.day).set(row.account_id, Number(row.total_tokens || 0));
  }
  if (!accounts.length) {
    target.innerHTML = '<div class="empty">No account-attributed usage in the last 14 days.</div>';
    return;
  }
  const width = 1200, height = 250, left = 58, right = 12, top = 18, bottom = 36;
  const plotWidth = width - left - right, plotHeight = height - top - bottom;
  const dayTotals = daily.map(row => accounts.reduce(
    (sum, account) => sum + Number((byDay.get(row.day) || new Map()).get(account) || 0),
    0,
  ));
  const max = Math.max(...dayTotals, 1);
  const slot = plotWidth / daily.length;
  const barWidth = Math.max(4, slot - 8);
  let svg = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">`;
  for (let tick = 0; tick <= 4; tick += 1) {
    const y = top + plotHeight * tick / 4;
    svg += `<line class="grid-line" x1="${left}" x2="${width - right}" y1="${y}" y2="${y}"/>`;
    svg += `<text class="axis" x="0" y="${y + 3}">${compact(max * (4 - tick) / 4)}</text>`;
  }
  daily.forEach((day, index) => {
    const values = byDay.get(day.day) || new Map();
    const x = left + index * slot + (slot - barWidth) / 2;
    let y = top + plotHeight;
    accounts.forEach((account, accountIndex) => {
      const h = Number(values.get(account) || 0) / max * plotHeight;
      y -= h;
      if (h > 0) svg += `<rect x="${x}" y="${y}" width="${barWidth}" height="${h}" fill="${ACCOUNT_COLORS[accountIndex]}" rx="2"/>`;
    });
    if (index % 2 === 0 || index === daily.length - 1) {
      svg += `<text class="axis" text-anchor="middle" x="${x + barWidth / 2}" y="${height - 8}">${new Date(`${day.day}T12:00:00`).toLocaleDateString([], { month: "short", day: "numeric" })}</text>`;
    }
  });
  svg += "</svg>";
  const legend = accounts.map((account, index) =>
    `<span class="chart-key"><i style="background:${ACCOUNT_COLORS[index]}"></i>${escaped(labels[account] || (account ? `unmapped ${account.slice(0, 8)}` : "unattributed"))}</span>`
  ).join("");
  target.innerHTML = `<div class="chart-legend">${legend}</div>${svg}`;
}

function renderDailyTable(analysis) {
  const rows = analysis.daily;
  document.querySelector("#daily-table tbody").innerHTML = rows.map((row, index) => {
    const fresh = Number(row.input_tokens || 0) + Number(row.output_tokens || 0);
    let previous = index ? rows[index - 1].total_tokens : 0;
    if (index === rows.length - 1) previous = analysis.yesterday_same_time.total_tokens;
    const delta = percentChange(row.total_tokens, previous);
    return `<tr><td>${new Date(`${row.day}T12:00:00`).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })}${index === rows.length - 1 ? ' <span class="muted">so far</span>' : ""}</td><td>${compact(fresh)}</td><td>${compact(row.cached_input_tokens)}</td><td>${compact(row.cache_create_tokens)}</td><td>${compact(row.total_tokens)}</td><td class="${changeClass(delta)}">${changeText(delta)}</td></tr>`;
  }).join("");
}

function renderQuotaDrawdown(rows, labels) {
  const target = document.getElementById("quota-drawdown");
  if (!rows.length) {
    target.innerHTML = '<div class="empty">No fresh five-hour observations in this range yet.</div>';
    return;
  }
  const groups = new Map();
  for (const row of rows) {
    const group = groups.get(row.account_id) || [];
    group.push(row);
    groups.set(row.account_id, group);
  }
  const accounts = [...groups].sort((a, b) => (labels[a[0]] || a[0]).localeCompare(labels[b[0]] || b[0]));
  const allTimes = rows.map(row => Number(row.observed_minute));
  const start = Math.min(...allTimes), end = Math.max(...allTimes);
  const span = Math.max(end - start, 1);
  const width = 1200, height = 250, left = 48, right = 12, top = 18, bottom = 32;
  const plotWidth = width - left - right, plotHeight = height - top - bottom;
  let svg = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">`;
  for (let value = 0; value <= 100; value += 25) {
    const y = top + plotHeight * (100 - value) / 100;
    svg += `<line class="grid-line" x1="${left}" x2="${width - right}" y1="${y}" y2="${y}"/>`;
    svg += `<text class="axis" x="0" y="${y + 3}">${value}%</text>`;
  }
  accounts.forEach(([account, points], index) => {
    const color = ACCOUNT_COLORS[index % ACCOUNT_COLORS.length];
    let path = "";
    let previousAt = null;
    for (const point of points) {
      const at = Number(point.observed_minute);
      const x = left + (at - start) / span * plotWidth;
      const y = top + (100 - Number(point.remaining_percent)) / 100 * plotHeight;
      path += `${previousAt == null || at - previousAt > 20 * 60_000 ? "M" : "L"}${x},${y} `;
      previousAt = at;
    }
    svg += `<path class="account-line" stroke="${color}" d="${path.trim()}"/>`;
    const latest = points[points.length - 1];
    const latestX = left + (Number(latest.observed_minute) - start) / span * plotWidth;
    const latestY = top + (100 - Number(latest.remaining_percent)) / 100 * plotHeight;
    svg += `<circle class="account-dot" cx="${latestX}" cy="${latestY}" r="3" fill="${color}"/>`;
  });
  for (let tick = 0; tick < 5; tick += 1) {
    const x = left + plotWidth * tick / 4;
    const at = start + span * tick / 4;
    const anchor = tick === 0 ? "start" : tick === 4 ? "end" : "middle";
    svg += `<text class="axis" text-anchor="${anchor}" x="${x}" y="${height - 6}">${axisLabel(at, span)}</text>`;
  }
  svg += "</svg>";
  const legend = accounts.map(([account], index) =>
    `<span class="chart-key"><i style="background:${ACCOUNT_COLORS[index % ACCOUNT_COLORS.length]}"></i>${escaped(labels[account] || `unmapped ${account.slice(0, 8)}`)}</span>`
  ).join("");
  target.innerHTML = `<div class="chart-legend">${legend}</div>${svg}`;
}

function renderAccountStatus(rows, labels) {
  const sorted = [...rows].sort((a, b) => Number(b.today_tokens || 0) - Number(a.today_tokens || 0));
  document.querySelector("#account-status-table tbody").innerHTML = sorted.map(row => {
    const label = labels[row.account_id] || row.account_label || (row.account_id ? `unmapped ${row.account_id.slice(0, 8)}` : "unattributed");
    const remaining = row.remaining_percent == null ? "—" : `${Number(row.remaining_percent).toFixed(0)}%`;
    const drawdown = row.drawdown_percent == null ? "—" : `${Number(row.drawdown_percent) >= 0 ? "−" : "+"}${Math.abs(Number(row.drawdown_percent)).toFixed(0)} pts`;
    const reset = row.resets_at ? new Date(Number(row.resets_at)).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }) : "—";
    return `<tr><td>${escaped(label)}</td><td>${compact(row.today_tokens)}</td><td>${compact(row.today_fresh_tokens)}</td><td>${remaining}</td><td>${drawdown}</td><td class="muted">${reset}</td></tr>`;
  }).join("");
}

function renderBars(element, rows, nameKey, labels = {}) {
  const max = Math.max(...rows.map(row => Number(row.total_tokens || 0)), 1);
  document.getElementById(element).innerHTML = rows.length ? rows.map(row => {
    const raw = row[nameKey] || "Unknown";
    const name = labels[raw] || raw;
    return `<div class="bar-row"><span title="${escaped(raw)}">${escaped(name)}</span><div class="bar-track"><div class="bar-fill" style="width:${Number(row.total_tokens || 0) / max * 100}%"></div></div><span class="bar-value">${compact(row.total_tokens)}</span></div>`;
  }).join("") : '<div class="empty">No data</div>';
}

function renderTables(data) {
  document.querySelector("#sessions-table tbody").innerHTML = data.sessions.map(row => {
    const child = row.parent_session_id ? `<br><span class="muted">child of ${escaped(row.parent_session_id.slice(0, 12))}</span>` : "";
    return `<tr><td>${escaped(row.provider)}</td><td>${escaped((row.agent_id || row.session_id).slice(0, 18))}${child}</td><td>${escaped(row.model || "—")}</td><td>${escaped(row.reasoning_effort || "—")}</td><td>${compact(row.total_tokens)}</td><td class="muted">${new Date(row.last_at).toLocaleString()}</td></tr>`;
  }).join("");
  document.querySelector("#tools-table tbody").innerHTML = data.tools.map(row =>
    `<tr><td>${escaped(row.tool_name || "unknown")}</td><td>${escaped(row.tool_status)}</td><td>${compact(row.calls)}</td><td class="muted">${duration(row.average_duration_ms)}</td></tr>`
  ).join("");
}

function renderDetails(data) {
  document.getElementById("latency").innerHTML = `<div class="value">${duration(data.latency.p95_ms)}</div><small>p95 · ${duration(data.latency.average_ms)} average · ${data.latency.count} turns</small>`;
  document.getElementById("compactions").innerHTML = `<div class="value">${compact(data.overview.compactions)}</div><small>in selected event range</small>`;
  document.getElementById("quota").innerHTML = data.quota.length ? data.quota.slice(0, 4).map(row =>
    `<div class="quota-row"><span>${escaped(row.provider)} · ${escaped(row.quota_name)} · ${row.quota_window_minutes || "?"}m</span><span>${Number(row.quota_used_percent).toFixed(1)}%</span></div>`
  ).join("") : '<div class="empty">Not exposed by local events</div>';
}

function renderQuotaCapacity(rows, labels) {
  document.querySelector("#capacity-table tbody").innerHTML = rows.length ? rows.map(row => {
    const account = labels[row.account_id] || row.account_label || row.account_id.slice(0, 10);
    const dispersion = row.estimated_tokens_at_100_pct ? row.standard_deviation / row.estimated_tokens_at_100_pct * 100 : 0;
    return `<tr><td>${escaped(account)}</td><td>${escaped(row.plan_cohort || "unmapped")}</td><td>${escaped(row.model || "—")}</td><td>${escaped(row.reasoning_effort || "—")}</td><td>${row.sample_count}</td><td>${compact(row.estimated_tokens_at_100_pct)}</td><td>${compact(row.minimum_tracked_tokens)}–${compact(row.maximum_tracked_tokens)}</td><td>${dispersion.toFixed(1)}%</td></tr>`;
  }).join("") : '<tr><td colspan="8" class="empty">No eligible same-window observations yet.</td></tr>';
}

async function refresh() {
  try {
    const response = await fetch(`/api/dashboard?${queryString()}`, {
      cache: "no-store",
      headers: { Authorization: `Bearer ${capabilityToken}` },
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    setFilterOptions(data);
    latestTimeline = data.timeline;
    latestBucketMinutes = Number(data.timeline_bucket_minutes || 1);
    renderAnalysis(data.analysis);
    renderDailyAccounts(data.analysis.daily_accounts, data.analysis.daily, data.account_labels);
    renderDailyTable(data.analysis);
    renderQuotaDrawdown(data.analysis.quota_history, data.account_labels);
    renderAccountStatus(data.analysis.account_status, data.account_labels);
    renderOverview(data.overview, data.backlog);
    renderTimeline(data.timeline, latestBucketMinutes);
    renderDayView(data.timeline);
    renderBars("accounts", data.accounts, "account_id", data.account_labels);
    renderBars("models", data.models, "model");
    renderTables(data);
    renderDetails(data);
    renderQuotaCapacity(data.quota_capacity, data.account_labels);
    document.getElementById("updated").textContent = new Date(data.generated_at).toLocaleTimeString();
  } catch (error) {
    document.getElementById("timeline").innerHTML = `<div class="empty">Dashboard read failed: ${escaped(error.message)}</div>`;
  }
}

function initializeDashboard() {
  document.getElementById("series-controls").innerHTML = TOKEN_SERIES.map(([key, label, color]) =>
    `<label class="series-option" style="--swatch:${color}"><input type="checkbox" value="${key}" checked><span>${label}</span></label>`
  ).join("");
  for (const name of ["range", ...filterNames]) document.getElementById(name).addEventListener("change", refresh);
  document.getElementById("smoothing").addEventListener("change", () => renderTimeline(latestTimeline, latestBucketMinutes));
  document.getElementById("series-controls").addEventListener("change", () => renderTimeline(latestTimeline, latestBucketMinutes));
  refresh();
  setInterval(() => {
    if (!document.hidden) refresh();
  }, 60000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refresh();
  });
}

if (typeof document !== "undefined") initializeDashboard();
if (typeof module !== "undefined") module.exports = { hourlyDayRows, movingAverageRows, normalizeTimelineRows, seriesValue };
