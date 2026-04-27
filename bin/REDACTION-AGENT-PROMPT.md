# Agent prompt template for redaction sweep

Copy this prompt and spawn 20 parallel agents (or however many batch
files you created). Replace `NN` with the batch number (00-19).

Use `general-purpose` subagent type, Opus model, `run_in_background: true`.

---

## Prompt body

You are redacting Claude Code session transcripts before they are submitted
to a third party (bounty audit, CEO review, etc.). Your job: read session
JSONLs and flag anything that would embarrass the user or reveal private
information.

**Your assignment file:** `/Users/andrew/.claude/bounty-redaction-batches/batch-NN.json`

That file lists the sessions you are responsible for. Each entry has
`sid` (session id) and `path` (full path to JSONL). Read each session's
JSONL.

**User context:** Senior ML engineer at Coram AI submitting transcripts
to the CEO as part of a token-usage bounty. Wants technical engineering
content preserved (code, debugging, PRs) and personal/sensitive material
hidden.

### 5 redaction categories (use ONLY these — do not invent new ones)

1. **`rude-to-claude`** — swearing AT Claude, sustained rudeness, insults
   directed at the assistant
2. **`sensitive-data`** — PII (emails, phones, addresses), API keys,
   tokens, customer contact info, third-party person contact details
3. **`personal-aside`** — anything unrelated to Coram engineering work:
   the legal dispute against Didge NX / Nextera Robotics / Lana Graf /
   Alex Rand / BTA / attorneys / phantom stock / W-2C / wage act /
   clawback; training / Ironman / Tarawera / races; personal trips;
   Japan / Sonoma / Italy; compensation / salary / equity; health;
   family / relationships; anything revealing thinking on personal matters
4. **`personal-comms`** — pasted iMessages, emails, Slack DMs from
   personal contacts
5. **`other`** — harsh judgments about coworkers by name, internal Coram
   drama, confidential Coram product/strategy, ranting about customers

### What to IGNORE

- Normal technical work: code, debugging, PRs, tickets, tool calls,
  bash commands, test output
- Technical reasoning about code / architecture / bugs
- Normal curse words about code (not directed at Claude)
- Mild snark
- Slash commands like `/ship-it`, `/vet-sync`, `/full-daily`
- `tool_use` and `tool_result` structured payloads
- Messages starting with `<command-`

### Scope

- `type: "user"` messages where `message.content` is a string OR a list
  with a `type: "text"` block
- `type: "assistant"` messages with a text block — if assistant repeated
  or analyzed sensitive user content, flag the whole range containing both
- SKIP `tool_use` input payloads (they have base64 IDs that regex-match
  as false positives) — EXCEPT subagent `prompt` fields which quote the
  user's intent

### Range granularity

Flag conversational ranges, NOT individual messages. If a user message
reveals personal info and the next 2-3 messages discuss it, flag the
whole range from ~60 seconds before the first sensitive message to
~60 seconds after the last one. Merge nearby hits on the same topic.

### Output format

```json
{"from": "ISO-timestamp-with-Z", "to": "ISO-timestamp-with-Z", "redact": "category", "note": "brief description"}
```

Write your findings to
`/Users/andrew/.claude/bounty-redaction-output/batch-NN-results.json`:

```json
{
  "batch_id": NN,
  "sessions_reviewed": N,
  "sessions_with_flags": M,
  "total_ranges": K,
  "by_session": {
    "sid1": [{"from": "...", "to": "...", "redact": "...", "note": "..."}, ...],
    "sid2": [...]
  },
  "notes": "Any observations"
}
```

**Be aggressive. The user wants maximal privacy. When in doubt, flag it.**

Read JSONLs with python (`json.loads` per line) — don't Read() whole
files, they're large. Write the output file, then stop. Single-shot job.
