# Session Redaction Pipeline

Redact sensitive content from Claude Code session JSONLs before submitting
them to third parties (bounty audits, code reviews, employer inspection).

**Source files are never modified.** All redaction happens in an export
copy. Originals stay in `~/.claude/projects/`. A `_vault/` directory
preserves original text for every redacted message so nothing is lost.

---

## Pipeline overview

```
                                                       ┌──────────────────────┐
  ~/.claude/projects/<proj>/<sid>.jsonl       ────►    │  scan-tokens-export  │
                                                       │   (applies ranges)    │
  ~/.claude/token-scan-overrides.json         ────►    │                       │
      (merged from all sources below)                  └─────────┬─────────────┘
                                                                 │
  Sources that populate the overrides file:                      ▼
                                                       <output-dir>/
  1. Manual curation                                   ├── _vault/<rid>.json   (originals)
  2. scan-tokens-autoflag.py  (regex PII/legal)        ├── MANIFEST.json
  3. scan-tokens-coworker-scrub.py  (name matches)     └── <proj>/<sid>.jsonl  (redacted copy)
  4. 20 parallel agents via Agent tool
                                         │
                                         ▼
                      scan-tokens-merge.py (dedup + backup + write)
```

---

## Re-running the full pipeline

### 1. Back up the current overrides file

The merge script makes a backup automatically, but belt-and-suspenders:

```bash
cp ~/.claude/token-scan-overrides.json \
   ~/.claude/token-scan-overrides.json.bak-$(date +%Y%m%d-%H%M%S)
```

### 2. Deterministic passes (fast, reversible)

```bash
# Regex pass for legal/personal/PII patterns — writes to
# ~/.claude/token-scan-autoflag-staging.json
python3 ~/personal/code/claude-statusline/bin/scan-tokens-autoflag.py

# Coworker-name pass — writes to
# ~/.claude/token-scan-coworker-staging.json
python3 ~/personal/code/claude-statusline/bin/scan-tokens-coworker-scrub.py
```

Both scripts only **stage** their output — they never touch the live
overrides file. Review the staging JSONs before merging.

### 3. Agent passes (slow, costs tokens)

Agents handle subjective judgment: rude-to-claude, strategy reveals,
coworker drama. They cannot be a deterministic regex.

Build session batches (distribute ~324 main sessions across 20 agents):

```bash
python3 << 'EOF'
import json
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"
CUTOFF = "2026-03-01T00:00:00Z"  # adjust as needed

sessions = []
for p in PROJECTS.rglob("*.jsonl"):
    if "subagent" in str(p):
        continue
    with open(p) as f:
        first_ts = None
        user_chars = 0
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            ts = d.get("timestamp", "")
            if ts and first_ts is None:
                first_ts = ts
                if ts < CUTOFF:
                    break
            if d.get("type") == "user":
                msg = d.get("message", {}).get("content", "")
                if isinstance(msg, str) and not msg.startswith("<command-"):
                    user_chars += len(msg)
                elif isinstance(msg, list):
                    for c in msg:
                        if isinstance(c, dict) and c.get("type") == "text":
                            user_chars += len(c.get("text", ""))
    if first_ts and first_ts >= CUTOFF:
        sessions.append({"sid": p.stem, "path": str(p), "user_chars": user_chars})

sessions.sort(key=lambda s: -s["user_chars"])
batch_dir = Path.home() / ".claude" / "bounty-redaction-batches"
batch_dir.mkdir(exist_ok=True)
BATCHES = 20
batches = [[] for _ in range(BATCHES)]
for i, s in enumerate(sessions):
    batches[i % BATCHES].append(s)
for i, b in enumerate(batches):
    with open(batch_dir / f"batch-{i:02d}.json", "w") as f:
        json.dump({"batch_id": i, "sessions": b}, f, indent=2)
print(f"Wrote {BATCHES} batches covering {len(sessions)} sessions")
EOF
```

Then spawn 20 agents using the prompt template in `REDACTION-AGENT-PROMPT.md`,
each pointed at one batch file. Agents write results to
`~/.claude/bounty-redaction-output/batch-XX-results.json`.

### 4. Merge all sources

Dry-run first:

```bash
python3 ~/personal/code/claude-statusline/bin/scan-tokens-merge.py
```

Then apply:

```bash
python3 ~/personal/code/claude-statusline/bin/scan-tokens-merge.py --write
```

Each new range gets a `source` field (e.g., `autoflag`, `coworker-scan`,
`agent-batch-07`) so it's traceable. Revert a bad source with:

```bash
python3 ~/personal/code/claude-statusline/bin/scan-tokens-merge.py \
    --revert-source agent-batch-07
```

### 5. Export the redacted copy

```bash
python3 ~/personal/code/claude-statusline/bin/scan-tokens-export.py \
    ~/Desktop/redacted-export
```

Output directory mirrors `~/.claude/projects/` structure. Includes:

- Redacted session JSONLs (user + assistant + subagent files)
- `_vault/<rid>.json` — originals for recovery
- `MANIFEST.json` — aggregate counts only (no notes, no categories)

---

## What gets redacted

The export script redacts messages whose timestamp falls inside a flagged
range and which contain real user-authored or assistant-authored text:

| Message type     | Redacted?                              |
|------------------|----------------------------------------|
| User (string)    | Yes (full content → `[REDACTED:rid]`)  |
| User (text block)| Yes (text field → placeholder)         |
| Tool results     | No — pass through                      |
| Command wrappers | No — pass through                      |
| Assistant text   | Yes                                    |
| Assistant tool_use.input.prompt       | Yes (subagent prompts leak user content) |
| Assistant tool_use.input.description  | Yes                                    |
| Other tool_use input fields           | No                                     |
| System messages  | No                                     |
| Subagent JSONLs  | Yes — processed recursively per parent |

The placeholder is bare `[REDACTED:rid]` — **no category is included**
in the output, to avoid leaking the reason for redaction. Category is
preserved internally in the vault file for your recovery.

---

## Categories (5 total — do NOT add more)

The export script hard-codes these:

| Category         | Use for                                           |
|------------------|---------------------------------------------------|
| `rude-to-claude` | Swearing AT Claude, sustained rudeness            |
| `sensitive-data` | PII, API keys, customer contacts                  |
| `personal-aside` | Legal dispute, training, trips, comp, health, family |
| `personal-comms` | Pasted iMessages/emails/personal DMs              |
| `other`          | Harsh coworker judgments, internal drama, confidential strategy |

Agents and scripts must use one of these five. The merge script coerces
invalid categories to `other` and warns.

---

## Files and locations

| Path | Purpose |
|------|---------|
| `~/.claude/projects/` | Source JSONLs (never modified) |
| `~/.claude/token-scan-overrides.json` | Live overrides — ranges + categories |
| `~/.claude/token-scan-overrides.json.bak-*` | Timestamped backups (auto) |
| `~/.claude/token-scan-autoflag-staging.json` | Regex PII pass output |
| `~/.claude/token-scan-coworker-staging.json` | Coworker-name pass output |
| `~/.claude/bounty-redaction-batches/` | Per-agent session assignments |
| `~/.claude/bounty-redaction-output/` | Per-agent flag results |
| `~/personal/code/claude-statusline/bin/scan-tokens-autoflag.py` | Regex scanner |
| `~/personal/code/claude-statusline/bin/scan-tokens-coworker-scrub.py` | Coworker name scanner |
| `~/personal/code/claude-statusline/bin/scan-tokens-redactions.py` | Read-only reporter |
| `~/personal/code/claude-statusline/bin/scan-tokens-merge.py` | Merges sources into overrides |
| `~/personal/code/claude-statusline/bin/scan-tokens-export.py` | Applies redactions to an export copy |

---

## Recovery

1. **Wrong range flagged** — edit `~/.claude/token-scan-overrides.json`
   directly, or re-run with `--revert-source <source-name>`.
2. **Export needs to change** — delete the output directory and re-run
   `scan-tokens-export.py`. Idempotent.
3. **Restore a redacted message** — look up the redact_id from the
   placeholder string (`[REDACTED:r_abcd]`), read
   `<output-dir>/_vault/r_abcd.json` for the original text.
4. **Undo a merge** — restore from the timestamped backup
   (`~/.claude/token-scan-overrides.json.bak-*`).

---

## Tuning the coworker scan

`scan-tokens-coworker-scrub.py` has three name lists at the top:

- `UNCOMMON_FIRST` — matched bare (high confidence)
- `COMMON_FIRST` — only matches if a work-context word is within 200 chars
- `FULL_NAMES` — always match
- `LAST_NAMES` — matched bare
- `HANDLES` — matched bare

Add or remove names as needed. The `WORK_CONTEXT` regex is the
false-positive throttle for common first names — widen it if real
coworker mentions are slipping through, tighten it if normal engineering
content is being over-redacted.
