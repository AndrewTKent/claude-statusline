#!/usr/bin/env python3
"""ccx — lossless Claude Code account vault and headroom board.

Claude Code keeps ONE credential slot (Keychain item "Claude Code-credentials"),
so /login clobbers whichever account was active. ccx snapshots the slot into a
per-account vault (one Keychain item per account) so no login is ever lost, and
ranks accounts by remaining rate-limit headroom (reset-aware, from
~/.claude/account-resets.json).

ccx only ever READS the live slot. It never writes it: macOS pins the slot's
partition list to whoever writes it, so a programmatic swap makes every reader
storm password prompts (2026-07-17 incident). Switching accounts is /login's
job; `ccx switch` just says which account to pick. Only identity metadata
lands in ~/.claude/ccx-vault.json.

Commands:
  enroll          snapshot the active account into the vault (one-shot)
  mirror          loop: re-snapshot the slot whenever it changes (run as daemon)
  ls              vaulted accounts + reset-aware headroom estimates
  best            print the vaulted account with the most headroom
  switch TARGET   print which account to pick in /login (advisory; never writes)
  selftest        keychain round-trip on a scratch item (never touches live/vault)
"""

from __future__ import annotations

import argparse
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
LIVE_SERVICE = "Claude Code-credentials"
VAULT_SERVICE = "Claude Code-cred-vault"
SELFTEST_SERVICE = "Claude Code-cred-vault-selftest"
META_PATH = HOME / ".claude" / "ccx-vault.json"
LOCK_PATH = HOME / ".claude" / "ccx.lock"
RESETS_PATH = HOME / ".claude" / "account-resets.json"
CLAUDE_JSON = HOME / ".claude.json"
CONF_PATH = HOME / ".claude" / "statusline.conf"
MIRROR_LOG = HOME / ".claude" / "ccx-mirror.log"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
STALE_AFTER_S = 3 * 3600


class CcxError(RuntimeError):
    pass


def die(msg: str) -> None:
    print(f"ccx: {msg}", file=sys.stderr)
    sys.exit(1)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ── keychain ──────────────────────────────────────────────────────────────


def kc_read(service: str, account: str | None = None) -> str | None:
    cmd = ["security", "find-generic-password", "-s", service]
    if account is not None:
        cmd += ["-a", account]
    cmd.append("-w")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return r.stdout.rstrip("\n")


def kc_vault_put(account: str, secret: str, service: str = VAULT_SERVICE) -> None:
    """Write a vault item with an allow-all ACL (`-A`) so ccx reads it back
    without a keychain prompt — matching the live "Claude Code-credentials"
    item's posture (a local attacker reads the live token anyway, so gating
    the copy stricter than the original only adds friction).

    `-A` requires `-w` (argv), which is fine here where `security -i` is not:
    the value is one exec arg in list form (no shell), well under ARG_MAX, and
    only ephemerally in `ps` — an acceptable trade for an allow-all item. This
    also sidesteps the `-i` line-buffer truncation that mangled MCP-bearing
    blobs. delete-then-add guarantees the ACL even when replacing a
    previously-restricted item."""
    subprocess.run(
        ["security", "delete-generic-password", "-s", service, "-a", account],
        capture_output=True,
        text=True,
    )
    r = subprocess.run(
        ["security", "add-generic-password", "-A", "-s", service, "-a", account, "-w", secret],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise CcxError(f"vault write failed: {r.stderr.strip()[:200]}")




def kc_delete(service: str, account: str) -> None:
    subprocess.run(
        ["security", "delete-generic-password", "-s", service, "-a", account],
        capture_output=True,
        text=True,
    )




# ── identity ──────────────────────────────────────────────────────────────


def blob_access_token(blob: str) -> str | None:
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    inner = data.get("claudeAiOauth")
    if isinstance(inner, dict) and inner.get("accessToken"):
        return str(inner["accessToken"])
    if data.get("accessToken"):
        return str(data["accessToken"])
    return None


def blob_refresh_expiry(blob: str) -> int | None:
    """Refresh-token expiry (epoch seconds) from the blob, or None.

    The access token expires hourly and self-refreshes; the *refresh* token
    expiring is what actually kills the cred and forces a re-login. Stored so
    the statusline can flag dead accounts without a network call. Value in the
    blob is epoch-ms."""
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data.get("claudeAiOauth"), dict) else data
    raw = oauth.get("refreshTokenExpiresAt") if isinstance(oauth, dict) else None
    try:
        return int(raw) // 1000 if raw is not None else None
    except (TypeError, ValueError):
        return None


def blob_access_expiry(blob: str) -> int | None:
    """Access-token expiry (epoch seconds). A read against /api/oauth/usage
    needs a live access token — an expired one just 401s, so the poller skips
    it rather than trigger a refresh (which would rotate the refresh token)."""
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data.get("claudeAiOauth"), dict) else data
    raw = oauth.get("expiresAt") if isinstance(oauth, dict) else None
    try:
        return int(raw) // 1000 if raw is not None else None
    except (TypeError, ValueError):
        return None


def fetch_profile(access_token: str, timeout: float = 4.0) -> dict | None:
    req = urllib.request.Request(
        PROFILE_URL,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-code/2.1.34",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return None
    return data if isinstance(data, dict) and data.get("account") else None


def fetch_usage(access_token: str, timeout: float = 4.0) -> dict | None:
    """GET /api/oauth/usage as a pure read with the account's access token.
    Returns the parsed usage dict, or None on any failure (401 on an expired
    access token, network error, bad JSON). Never uses the refresh token, so
    it can't rotate a shared account's credential."""
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-code/2.1.34",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return None
    return data if isinstance(data, dict) and "five_hour" in data else None


def usage_to_reset_row(email: str, org_uuid: str, usage: dict, now_ts: int) -> dict:
    """Map an /api/oauth/usage response to an account-resets.json row — the
    exact schema statusline.sh writes for the active account, so a poller-written
    row is indistinguishable from a statusline-written one."""

    def weekly(field: str):
        for lim in usage.get("limits") or []:
            if isinstance(lim, dict) and lim.get("kind") == "weekly_scoped":
                if field == "label":
                    return ((lim.get("scope") or {}).get("model") or {}).get("display_name")
                return lim.get(field)
        return None

    five = usage.get("five_hour") or {}
    seven = usage.get("seven_day") or {}
    return {
        "email": email,
        "org_uuid": org_uuid,
        "five_hour_reset": five.get("resets_at"),
        "five_hour_pct": five.get("utilization") or 0,
        "seven_day_reset": seven.get("resets_at"),
        "seven_day_pct": seven.get("utilization") or 0,
        "fable_pct": weekly("percent"),
        "fable_reset": weekly("resets_at"),
        "fable_label": weekly("label"),
        "last_seen": now_ts,
    }


def identity_from_profile(profile: dict) -> dict:
    acc = profile.get("account") or {}
    org = profile.get("organization") or {}
    return {
        "uuid": acc.get("uuid"),
        "email": acc.get("email"),
        "org_uuid": org.get("uuid"),
        "org_type": org.get("organization_type"),
        "rate_limit_tier": org.get("rate_limit_tier"),
    }


def vault_key(ident: dict) -> str | None:
    """Vault key = accountUuid|orgUuid. One email can belong to several orgs
    (e.g. a personal Max plan and a company seat), each with its own org-scoped
    token — keying on account alone would collide them into one entry."""
    uuid, org = ident.get("uuid"), ident.get("org_uuid")
    return f"{uuid}|{org}" if uuid and org else None


def short_key(key: str) -> str:
    """8-char account|org breadcrumb for display (account alone is not unique)."""
    parts = key.split("|")
    return "|".join(p[:8] for p in parts)


def synthesize_oauth_account(ident: dict, profile: dict | None) -> dict:
    """Minimal oauthAccount for ~/.claude.json when the full one wasn't captured.

    Claude Code refreshes profile details itself; the load-bearing fields are the
    three identifiers."""
    acc = (profile or {}).get("account") or {}
    org = (profile or {}).get("organization") or {}
    return {
        "accountUuid": ident.get("uuid"),
        "emailAddress": ident.get("email"),
        "organizationUuid": ident.get("org_uuid"),
        "hasExtraUsageEnabled": bool(org.get("has_extra_usage_enabled", False)),
        "billingType": org.get("billing_type"),
        "accountCreatedAt": acc.get("created_at"),
        "subscriptionCreatedAt": org.get("subscription_created_at"),
        "ccOnboardingFlags": {},
        "claudeCodeTrialEndsAt": org.get("claude_code_trial_ends_at"),
        "claudeCodeTrialDurationDays": org.get("claude_code_trial_duration_days"),
        "seatTier": org.get("seat_tier"),
        "displayName": acc.get("display_name"),
        "profileFetchedAt": int(time.time() * 1000),
        "organizationRole": None,
        "workspaceRole": None,
        "organizationName": org.get("name"),
        "organizationType": ident.get("org_type"),
        "organizationRateLimitTier": ident.get("rate_limit_tier"),
        "userRateLimitTier": None,
    }


# ── metadata / config ─────────────────────────────────────────────────────


def load_meta() -> dict:
    if META_PATH.exists():
        try:
            return json.loads(META_PATH.read_text())
        except json.JSONDecodeError:
            corrupt = META_PATH.with_suffix(f".corrupt.{int(time.time())}")
            shutil.copy2(META_PATH, corrupt)
    return {"version": 1, "accounts": {}, "last_live_sha": None}


def save_meta(meta: dict) -> None:
    META_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = META_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, META_PATH)


def _conf_var(name: str) -> str:
    if not CONF_PATH.exists():
        return ""
    try:
        out = subprocess.run(
            ["bash", "-c", f'source "{CONF_PATH}" >/dev/null 2>&1; printf %s "${name}"'],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.stdout
    except Exception:
        return ""


def load_label_pairs() -> list[tuple[str, str, str | None]]:
    pairs: list[tuple[str, str, str | None]] = []
    for pair in _conf_var("ACCOUNT_LABELS").split():
        if ":" not in pair:
            continue
        label, pattern = pair.split(":", 1)
        if "|" in pattern:
            email_pat, uuid = pattern.split("|", 1)
            pairs.append((label, email_pat, uuid))
        else:
            pairs.append((label, pattern, None))
    return pairs


def resolve_label(email: str | None, org_uuid: str | None, pairs) -> str:
    if not email:
        return "?"
    bare: str | None = None
    for label, email_pat, uuid in pairs:
        if uuid is not None:
            if fnmatch.fnmatch(email, email_pat) and org_uuid == uuid:
                return label
        elif fnmatch.fnmatch(email, email_pat) and bare is None:
            bare = label
    return bare or email.split("@", 1)[0]


def excluded_labels() -> set[str]:
    raw = os.environ.get("CCX_EXCLUDE") or _conf_var("CCX_EXCLUDE")
    return set(raw.split())


# ── headroom ──────────────────────────────────────────────────────────────


def load_resets() -> dict:
    try:
        return json.loads(RESETS_PATH.read_text())
    except Exception:
        return {}


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def effective_pcts(row: dict, now: datetime) -> dict:
    def eff(pct_key: str, reset_key: str) -> float | None:
        pct = row.get(pct_key)
        if pct is None:
            return None
        reset = parse_iso(row.get(reset_key))
        if reset is not None and now >= reset:
            return 0.0
        return float(pct)

    return {
        "five_hour": eff("five_hour_pct", "five_hour_reset"),
        "seven_day": eff("seven_day_pct", "seven_day_reset"),
        "fable": eff("fable_pct", "fable_reset"),
    }


def headroom_rank(effs: dict) -> tuple:
    def key(name: str) -> float:
        v = effs.get(name)
        return float("inf") if v is None else v

    return (key("five_hour"), key("seven_day"), key("fable"))


def resets_row(resets: dict, email: str | None, org_uuid: str | None) -> dict:
    return resets.get(f"{email}|{org_uuid}", {})


# ── core ops ──────────────────────────────────────────────────────────────


@contextmanager
def locked(blocking: bool = True):
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_PATH, "w")
    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(handle, flags)
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def read_claude_json() -> dict:
    try:
        return json.loads(CLAUDE_JSON.read_text())
    except Exception:
        return {}




def snapshot_live(meta: dict, *, force: bool = False) -> tuple[str, dict] | None:
    """Park the live slot's cred into the vault. Returns (uuid, entry) or None if unchanged."""
    blob = kc_read(LIVE_SERVICE)
    if blob is None:
        raise CcxError(f'no live credential in keychain slot "{LIVE_SERVICE}"')
    blob_sha = sha256(blob)
    if not force and meta.get("last_live_sha") == blob_sha:
        return None

    # Attribution comes from the profile endpoint ONLY: it answers for the token
    # actually in the slot. ~/.claude.json can lag it and would mis-file the blob.
    token = blob_access_token(blob)
    if not token:
        raise CcxError("live credential blob has no access token; not vaulting")
    profile = fetch_profile(token)
    if not profile:
        raise CcxError("profile fetch failed; cannot attribute live credential (will retry)")
    ident = identity_from_profile(profile)
    key = vault_key(ident)
    if key is None:
        raise CcxError("profile response missing account/org uuid; not vaulting")

    kc_vault_put(key, blob)
    if kc_read(VAULT_SERVICE, key) != blob:
        raise CcxError("vault write verification failed (read-back mismatch)")

    entry = meta["accounts"].get(key, {})
    oa = read_claude_json().get("oauthAccount") or {}
    # Trust ~/.claude.json's oauthAccount only when it is for this exact
    # account AND org — one email can hold several org-scoped tokens.
    if oa.get("accountUuid") == ident["uuid"] and oa.get("organizationUuid") == ident["org_uuid"]:
        entry["oauth_account"] = oa
    elif "oauth_account" not in entry:
        entry["oauth_account"] = synthesize_oauth_account(ident, profile)
    entry.update(
        {
            "email": ident.get("email"),
            "org_uuid": ident.get("org_uuid"),
            "org_type": ident.get("org_type"),
            "rate_limit_tier": ident.get("rate_limit_tier"),
            "vaulted_at": now_utc().isoformat(),
            "blob_sha256": blob_sha,
            "refresh_expires_at": blob_refresh_expiry(blob),
        }
    )
    meta["accounts"][key] = entry
    meta["last_live_sha"] = blob_sha
    save_meta(meta)
    return key, entry


def resolve_target(meta: dict, needle: str, pairs) -> tuple[str, dict]:
    matches: list[tuple[str, dict]] = []
    for uuid, entry in meta["accounts"].items():
        label = resolve_label(entry.get("email"), entry.get("org_uuid"), pairs)
        if needle in (uuid, entry.get("email")) or needle == label:
            matches.append((uuid, entry))
    if not matches:
        die(
            f"'{needle}' is not in the vault. Vaulted: "
            + (", ".join(sorted(vault_labels(meta, pairs))) or "(none)")
            + ". Log into it once with the mirror running (or run `ccx enroll` while "
            "it is active) and it will be captured."
        )
    if len(matches) > 1:
        labels = ", ".join(
            sorted(resolve_label(e.get("email"), e.get("org_uuid"), pairs) for _, e in matches)
        )
        die(f"'{needle}' is ambiguous ({len(matches)} accounts share it); use a label: {labels}")
    return matches[0]


def vault_labels(meta: dict, pairs) -> list[str]:
    return [
        resolve_label(e.get("email"), e.get("org_uuid"), pairs)
        for e in meta["accounts"].values()
    ]




def log_line(msg: str) -> None:
    stamp = now_utc().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    line = f"[{stamp}] {msg}\n"
    sys.stdout.write(line)
    sys.stdout.flush()
    try:
        if MIRROR_LOG.exists() and MIRROR_LOG.stat().st_size > 1_000_000:
            MIRROR_LOG.rename(MIRROR_LOG.with_suffix(".log.1"))
        with open(MIRROR_LOG, "a") as f:
            f.write(line)
    except OSError:
        pass


# ── commands ──────────────────────────────────────────────────────────────


def cmd_enroll(_args) -> None:
    pairs = load_label_pairs()
    with locked():
        meta = load_meta()
        result = snapshot_live(meta, force=True)
    if result is None:
        print("nothing to enroll")
        return
    key, entry = result
    label = resolve_label(entry.get("email"), entry.get("org_uuid"), pairs)
    print(f"vaulted {label} ({entry.get('email')}) [{short_key(key)}]")


def merge_reset_rows(rows: dict[str, dict]) -> None:
    """Write freshly-polled rows into account-resets.json, preserving every row
    we didn't poll. Re-reads under the ccx lock so a concurrent statusline write
    (its own atomic mv of the active row) is not lost. Atomic rename."""
    if not rows:
        return
    with locked():
        current = load_resets()
        current.update(rows)
        RESETS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = RESETS_PATH.with_suffix(".ccx-tmp")
        tmp.write_text(json.dumps(current, indent=1) + "\n")
        os.replace(tmp, RESETS_PATH)


def poll_all_usage(meta: dict, *, skip_active: bool = True) -> int:
    """Refresh account-resets.json for every vaulted account with a live access
    token — the statusline board only auto-polls the active account, so the
    other rows would otherwise sit frozen. Pure reads (access token only): no
    refresh, no rotation. Returns the number of rows refreshed.

    The active account is skipped by default: statusline owns its row at 60s
    with interpolation, and letting each writer own disjoint rows avoids a
    two-writer fight over the same one."""
    now = int(time.time())
    live_sha = meta.get("last_live_sha")
    fresh: dict[str, dict] = {}
    for key, entry in meta["accounts"].items():
        if skip_active and entry.get("blob_sha256") == live_sha:
            continue
        blob = kc_read(VAULT_SERVICE, key)
        if not blob:
            continue
        access_exp = blob_access_expiry(blob)
        if access_exp is not None and now >= access_exp:
            continue  # expired access token would 401; leave the row reset-aware
        token = blob_access_token(blob)
        if not token:
            continue
        usage = fetch_usage(token)
        if usage is None:
            continue
        fresh[f"{entry.get('email')}|{entry.get('org_uuid')}"] = usage_to_reset_row(
            entry.get("email"), entry.get("org_uuid"), usage, now
        )
    merge_reset_rows(fresh)
    return len(fresh)


def cmd_poll(_args) -> None:
    """Refresh the usage board for every vaulted account now (includes the
    active one, so a manual `ccx poll` fully repaints)."""
    n = poll_all_usage(load_meta(), skip_active=False)
    print(f"refreshed usage for {n} account(s)")


# ── token vault + router (zero keychain) ──────────────────────────────────
# Long-lived per-account tokens minted by `claude setup-token`, stored in a
# 0600 file OUTSIDE ~/.claude (the nightly archival chain mirrors ~/.claude
# to remote-host in plaintext — tokens must never land there). Routing injects
# CLAUDE_CODE_OAUTH_TOKEN into the child env, which outranks keychain auth;
# the live keychain slot stays 100% Claude-Code-owned.

TOKEN_VAULT_PATH = HOME / ".ccx" / "vault.json"
TOKEN_LIFETIME_S = 364 * 24 * 3600
TOKEN_RE = re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")


def load_token_vault() -> dict:
    try:
        return json.loads(TOKEN_VAULT_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "tokens": {}}


def save_token_vault(vault: dict) -> None:
    TOKEN_VAULT_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(TOKEN_VAULT_PATH.parent, 0o700)
    tmp = TOKEN_VAULT_PATH.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(vault, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, TOKEN_VAULT_PATH)


def token_for(vault: dict, label: str, now_ts: float) -> str | None:
    entry = (vault.get("tokens") or {}).get(label)
    if not entry:
        return None
    if now_ts >= entry.get("expires_at", 0):
        return None
    return entry.get("token")


def pick_route(rows: list[dict], vault: dict, excludes: set[str], now_ts: float, pin: str | None):
    """Best-headroom row that has a live minted token. `pin` forces a label.
    Returns (label, token) or None."""
    for row in rows:
        if pin is not None and row["label"] != pin:
            continue
        if pin is None and (row["label"] in excludes or row["expired"]):
            continue
        token = token_for(vault, row["label"], now_ts)
        if token:
            return row["label"], token
    return None


def cmd_mint(args) -> None:
    """Run `claude setup-token` and pipe the minted token straight into the
    vault — it is never displayed and never transits a transcript. The browser
    flow picks the account; the live keychain login is undisturbed."""
    label = args.label
    print(f"minting a long-lived token for '{label}' — approve in the browser...")
    r = subprocess.run(["claude", "setup-token"], stdout=subprocess.PIPE, text=True)
    m = TOKEN_RE.search(r.stdout or "")
    if r.returncode != 0 or not m:
        die("setup-token did not produce a token (browser flow cancelled?)")
    vault = load_token_vault()
    now = time.time()
    vault.setdefault("tokens", {})[label] = {
        "token": m.group(0),
        "minted_at": int(now),
        "expires_at": int(now + TOKEN_LIFETIME_S),
    }
    save_token_vault(vault)
    print(f"vaulted token for {label} (expires in ~1 year); it was not displayed")


def cmd_tokens(_args) -> None:
    vault = load_token_vault()
    tokens = vault.get("tokens") or {}
    if not tokens:
        print("no minted tokens — run `ccx mint <label>` per account you want routable")
        return
    now = time.time()
    for label in sorted(tokens):
        e = tokens[label]
        days = int((e.get("expires_at", 0) - now) / 86400)
        state = f"{days}d left" if days > 0 else "EXPIRED — re-mint"
        print(f"  {label:<12} minted {datetime.fromtimestamp(e.get('minted_at', 0)).date()}  {state}")


def cmd_pick_env(_args) -> None:
    """Emit `export` lines for the best routable account (consumed by the
    `claude` shell wrapper). Prints nothing when there is no routable token —
    the wrapper then falls back to Claude Code's native keychain auth. Never
    fails: routing must never block launching claude."""
    try:
        pick = pick_route(
            _account_rows(load_meta()),
            load_token_vault(),
            excluded_labels(),
            time.time(),
            os.environ.get("CCX_ACCOUNT") or None,
        )
    except Exception:
        return
    if pick is None:
        return
    label, token = pick
    print(f"export CLAUDE_CODE_OAUTH_TOKEN='{token}'")
    print(f"export CCX_ROUTED_LABEL='{label}'")


def cmd_mirror(args) -> None:
    """Capture + poll only. Each tick: vault the live slot if it changed
    (/login or token rotation), and refresh the all-account usage board on the
    poll sub-cadence. Never writes the live keychain slot — switching accounts
    is /login's job (see cmd_switch)."""
    poll_every = args.poll_all_sec
    log_line(
        f"ccx mirror started (interval {args.interval}s; poll-all every {poll_every}s; "
        "capture-only)"
    )
    last_poll = 0.0
    while True:
        try:
            with locked(blocking=False):
                meta = load_meta()
                result = snapshot_live(meta)
            if result is not None:
                key, entry = result
                pairs = load_label_pairs()
                label = resolve_label(entry.get("email"), entry.get("org_uuid"), pairs)
                log_line(f"vaulted {label} ({entry.get('email')}) [{short_key(key)}]")
        except BlockingIOError:
            pass  # another ccx holds the lock; catch the change on the next tick
        except CcxError as exc:
            log_line(f"warn: {exc}")
        except Exception as exc:  # daemon must survive anything
            log_line(f"error: {type(exc).__name__}: {exc}")

        if poll_every > 0 and (time.monotonic() - last_poll) >= poll_every:
            try:
                n = poll_all_usage(load_meta())
                if n:
                    log_line(f"polled usage for {n} account(s)")
            except Exception as exc:
                log_line(f"poll-all error: {type(exc).__name__}: {exc}")
            last_poll = time.monotonic()

        if args.once:
            return
        time.sleep(args.interval)


def _account_rows(meta: dict) -> list[dict]:
    pairs = load_label_pairs()
    resets = load_resets()
    now = now_utc()
    live_sha = meta.get("last_live_sha")
    rows = []
    for uuid, entry in meta["accounts"].items():
        row = resets_row(resets, entry.get("email"), entry.get("org_uuid"))
        effs = effective_pcts(row, now)
        last_seen = row.get("last_seen")
        stale = bool(last_seen) and (time.time() - last_seen) > STALE_AFTER_S
        rows.append(
            {
                "uuid": uuid,
                "email": entry.get("email"),
                "label": resolve_label(entry.get("email"), entry.get("org_uuid"), pairs),
                "effs": effs,
                "rank": headroom_rank(effs),
                "stale": stale,
                "active": entry.get("blob_sha256") == live_sha,
                "vaulted_at": entry.get("vaulted_at"),
                "expired": cred_expired(entry.get("refresh_expires_at")),
            }
        )
    # Dead creds sort last regardless of headroom — you can't route to them.
    rows.sort(key=lambda r: (r["expired"], r["rank"]))
    return rows


def cred_expired(refresh_expires_at: int | None) -> bool:
    """True when the vaulted refresh token is past expiry — switching to this
    account would force a re-login. None (older entry, unknown) is not expired."""
    return refresh_expires_at is not None and time.time() >= refresh_expires_at


def _fmt_pct(value: float | None, stale: bool) -> str:
    if value is None:
        return "—"
    return f"{value:.0f}%{'~' if stale else ''}"


def cmd_ls(_args) -> None:
    meta = load_meta()
    rows = _account_rows(meta)
    if not rows:
        print("vault is empty — run `ccx enroll` (or start the mirror) on each account once")
        return
    excludes = excluded_labels()
    print(f"{'':2}{'label':<12} {'email':<32} {'5h':>6} {'7d':>6} {'fable':>6}")
    any_expired = False
    for r in rows:
        mark = "* " if r["active"] else "  "
        if r["expired"]:
            suffix = "  EXPIRED — /login to refresh"
            any_expired = True
        elif r["label"] in excludes:
            suffix = "  [excluded]"
        else:
            suffix = ""
        print(
            f"{mark}{r['label']:<12} {r['email'] or '?':<32} "
            f"{_fmt_pct(r['effs']['five_hour'], r['stale']):>6} "
            f"{_fmt_pct(r['effs']['seven_day'], r['stale']):>6} "
            f"{_fmt_pct(r['effs']['fable'], r['stale']):>6}{suffix}"
        )
    print("\n* = matches live slot   ~ = estimate stale (>3h since that account was live)")
    print("pcts are USED (0% = full headroom), reset-aware; sorted best-first")
    if any_expired:
        print("EXPIRED = vaulted refresh token dead; switch to it needs a fresh /login")


def cmd_best(_args) -> None:
    meta = load_meta()
    rows = [
        r
        for r in _account_rows(meta)
        if r["label"] not in excluded_labels() and not r["expired"]
    ]
    if not rows:
        die("no usable account (vault empty, all excluded, or all expired)")
    top = rows[0]
    effs = top["effs"]
    print(
        f"{top['label']} ({top['email']}) — 5h {_fmt_pct(effs['five_hour'], top['stale'])}, "
        f"7d {_fmt_pct(effs['seven_day'], top['stale'])}, fable {_fmt_pct(effs['fable'], top['stale'])}"
    )


def _org_hint(entry: dict) -> str:
    """Disambiguator for the /login org picker when one email spans orgs."""
    org_type = entry.get("org_type") or ""
    if org_type == "claude_max":
        return "the Max-plan organization"
    if org_type == "claude_team":
        return "the Team organization"
    return f"org {short_key(entry.get('org_uuid') or '?')}"


def cmd_switch(args) -> None:
    """Advisory only — ccx never writes the live keychain slot. macOS pins the
    slot's partition list to whoever writes it, so a programmatic swap makes
    every reader (Claude Code, statusline) storm password prompts (2026-07-17
    incident). Switching goes through Claude Code's own /login; this command
    just tells you which account to pick."""
    if args.best == bool(args.target):
        die("switch needs exactly one of TARGET or --best")
    pairs = load_label_pairs()
    meta = load_meta()
    if args.best:
        candidates = [
            r
            for r in _account_rows(meta)
            if r["label"] not in excluded_labels() and not r["expired"] and not r["active"]
        ]
        if not candidates:
            die("no candidate account (vault empty, all excluded, or all expired)")
        top = candidates[0]
        key, entry, label = top["uuid"], meta["accounts"][top["uuid"]], top["label"]
    else:
        key, entry = resolve_target(meta, args.target, pairs)
        label = resolve_label(entry.get("email"), entry.get("org_uuid"), pairs)

    print(f"to land on {label}:")
    print("  1. in Claude Code, run /login")
    print(f"  2. sign in as {entry.get('email')} and pick {_org_hint(entry)}")
    print("(ccx does not switch accounts itself: writing the live keychain slot")
    print(" poisons its ACL and storms password prompts — 2026-07-17 incident)")


def cmd_selftest(_args) -> None:
    marker = secrets.token_hex(8)
    fake = json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": f"sk-ant-st-{marker}",
                "refreshToken": f"sk-ant-rt-{marker}",
            },
            # Realistic shape: live blobs also carry MCP OAuth entries and run
            # to kilobytes — the length is what broke the `security -i` path.
            "https://mcp.example.test": {"accessToken": f"mcp-{marker}"},
            "padding": secrets.token_hex(4096),
        },
        separators=(",", ":"),
    )
    try:
        # Exercise the real vault write path (allow-all): write, read-back,
        # overwrite, read-back. Allow-all means the read never prompts.
        kc_vault_put("selftest", fake, service=SELFTEST_SERVICE)
        first = kc_read(SELFTEST_SERVICE, "selftest")
        kc_vault_put("selftest", fake + "2", service=SELFTEST_SERVICE)
        second = kc_read(SELFTEST_SERVICE, "selftest")
    finally:
        kc_delete(SELFTEST_SERVICE, "selftest")
    if first != fake or second != fake + "2":
        die("selftest FAILED: keychain round-trip mismatch")
    print("selftest OK: allow-all vault write/read/overwrite/delete verified (scratch item only)")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ccx", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("enroll", help="vault the active account now").set_defaults(fn=cmd_enroll)

    p_mirror = sub.add_parser("mirror", help="watch the live slot and vault every change")
    p_mirror.add_argument("--interval", type=float, default=20.0)
    p_mirror.add_argument("--once", action="store_true", help="single pass (for cron/tests)")
    p_mirror.add_argument(
        "--poll-all-sec",
        type=float,
        default=15.0,
        help="refresh usage for all non-active accounts this often (0 disables)",
    )
    p_mirror.set_defaults(fn=cmd_mirror)

    sub.add_parser(
        "poll", help="refresh the usage board for all vaulted accounts now"
    ).set_defaults(fn=cmd_poll)

    p_mint = sub.add_parser("mint", help="mint + vault a 1-year token via claude setup-token")
    p_mint.add_argument("label", help="account label (from ACCOUNT_LABELS)")
    p_mint.set_defaults(fn=cmd_mint)

    sub.add_parser("tokens", help="list minted tokens and expiry").set_defaults(fn=cmd_tokens)

    sub.add_parser(
        "pick-env", help="emit env exports for the best routable account (wrapper hook)"
    ).set_defaults(fn=cmd_pick_env)

    sub.add_parser("ls", help="list vaulted accounts with headroom").set_defaults(fn=cmd_ls)
    sub.add_parser("best", help="print the highest-headroom account").set_defaults(fn=cmd_best)

    p_switch = sub.add_parser("switch", help="advise which account to /login into (never writes)")
    p_switch.add_argument(
        "target", nargs="?", help="label (from ACCOUNT_LABELS), email, or account uuid"
    )
    p_switch.add_argument("--best", action="store_true", help="pick highest-headroom account")
    p_switch.set_defaults(fn=cmd_switch)

    sub.add_parser("selftest", help="scratch keychain round-trip").set_defaults(fn=cmd_selftest)

    args = parser.parse_args(argv)
    try:
        args.fn(args)
    except CcxError as exc:
        die(str(exc))


if __name__ == "__main__":
    main()
