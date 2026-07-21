#!/usr/bin/env python3
"""accounts — Claude Code multi-account router and headroom board.

cc's credential store (decompiled 2.1.214) is 'keychain-with-plaintext-fallback',
re-read on a ~30s TTL: the Keychain item "Claude Code-credentials" wins whenever
it exists; ~/.claude/.credentials.json is read only when it doesn't. On refresh,
cc recreates the keychain slot itself and deletes the file. accounts therefore stores
every account's full OAuth blob in ~/.accounts/blobs.json and switches by writing the
FILE then DELETING the keychain slot — cc's next ~30s re-read lands on the file,
mid-session. Accounts are ranked by remaining rate-limit headroom (reset-aware).

accounts NEVER adds or modifies the Keychain live slot. macOS pins the slot's
partition list to whoever writes it, so a programmatic Keychain write makes
every reader storm password prompts (2026-07-17 incident). Deleting the slot
re-pins nothing — cc recreates it itself — and is cc's own logout primitive.

Router commands:
  route           daemon: poll all accounts, switch the creds file to the
                  freshest when the live one runs low (AUTO) or hold a pin (SET)
  set LABEL       pin the live account to LABEL and hold it
  auto            hand routing back to the daemon
  status          mode + per-account 5h headroom + ⚠login flags
  poll / migrate  repaint the board / seed blobs.json from the legacy vault
Legacy Keychain-vault board (never writes the live slot): enroll, ls, best, switch, selftest.
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
META_PATH = HOME / ".claude" / "accounts-vault.json"
LOCK_PATH = HOME / ".claude" / "accounts.lock"
RESETS_PATH = HOME / ".claude" / "account-resets.json"
CLAUDE_JSON = HOME / ".claude.json"
CONF_PATH = HOME / ".claude" / "statusline.conf"
MIRROR_LOG = HOME / ".claude" / "accounts-mirror.log"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # public Claude Code OAuth client (PKCE, no secret)
STALE_AFTER_S = 3 * 3600


class AccountsError(RuntimeError):
    pass


class TokenRefreshError(AccountsError):
    """Non-200 from the OAuth token endpoint. `.code` is the HTTP status string
    ("429" = the edge in front of the endpoint is throttling this client)."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"token endpoint returned {code}")


def die(msg: str) -> None:
    print(f"accounts: {msg}", file=sys.stderr)
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
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        return None  # wedged securityd must not hang the caller (it may hold the flock)
    if r.returncode != 0:
        return None
    return r.stdout.rstrip("\n")


def kc_slot_status(service: str) -> str:
    """'present' | 'absent' | 'unknown' — metadata-only probe (no secret read, so
    it works even when the keychain is locked and a -w read would be refused)."""
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", service],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return "unknown"
    if r.returncode == 0:
        return "present"
    if r.returncode == 44:  # errSecItemNotFound
        return "absent"
    return "unknown"


def kc_vault_put(account: str, secret: str, service: str = VAULT_SERVICE) -> None:
    """Write a vault item with an allow-all ACL (`-A`) so accounts reads it back
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
        raise AccountsError(f"vault write failed: {r.stderr.strip()[:200]}")




def kc_delete(service: str, account: str | None = None) -> None:
    cmd = ["security", "delete-generic-password", "-s", service]
    if account is not None:
        cmd += ["-a", account]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        pass  # caller re-probes the slot; a hang must not wedge the flock holder




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


def refresh_blob_access(blob: str, timeout: float = 15.0) -> str | None:
    """Exchange the blob's refresh token for a fresh access token; return the
    updated blob string. The OAuth refresh ROTATES the refresh token, so the
    caller MUST persist the returned blob — dropping it bricks the account (it
    would need a browser /login). Returns None when the blob has no refresh
    token or the response is malformed; raises TokenRefreshError on a non-200
    (429 = the edge fronting the token endpoint is throttling this client).
    Deliberately not called from poll_blobs_usage: polling stays a pure read so
    the statusline can repaint on a timer without rotating anyone's credential."""
    try:
        outer = json.loads(blob)
    except json.JSONDecodeError:
        return None
    oauth = outer.get("claudeAiOauth") if isinstance(outer.get("claudeAiOauth"), dict) else outer
    if not isinstance(oauth, dict) or not oauth.get("refreshToken"):
        return None
    body = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": oauth["refreshToken"],
            "client_id": OAUTH_CLIENT_ID,
        }
    )
    # curl, not urllib: the edge fronting the token endpoint 429s urllib's client
    # fingerprint but lets a normal one through. The refresh token is piped in on
    # stdin (--data @-), never argv, so it can't surface in the process list.
    proc = subprocess.run(
        [
            "curl", "-sS", "-m", str(int(timeout)),
            "-w", "\n%{http_code}",
            "-X", "POST", TOKEN_URL,
            "-H", "Content-Type: application/json",
            "-H", "Accept: application/json",
            "-H", "User-Agent: claude-cli/2.1.34 (external, cli)",
            "--data", "@-",
        ],
        input=body,
        capture_output=True,
        text=True,
    )
    resp_text, _, code = proc.stdout.rpartition("\n")
    if code.strip() != "200":
        raise TokenRefreshError(code.strip() or f"curl-exit-{proc.returncode}")
    try:
        payload = json.loads(resp_text)
    except json.JSONDecodeError:
        return None
    access, refresh = payload.get("access_token"), payload.get("refresh_token")
    if not (access and refresh):
        return None
    now_ms = int(time.time() * 1000)
    oauth["accessToken"] = access
    oauth["refreshToken"] = refresh
    ttl = payload.get("expires_in")
    if isinstance(ttl, (int, float)):
        oauth["expiresAt"] = now_ms + int(ttl) * 1000
    rt_ttl = payload.get("refresh_token_expires_in")
    if isinstance(rt_ttl, (int, float)):
        oauth["refreshTokenExpiresAt"] = now_ms + int(rt_ttl) * 1000
    else:
        # cc's own rotation omits this field, and blob_expired reads missing as alive.
        oauth.pop("refreshTokenExpiresAt", None)
    if "claudeAiOauth" in outer:
        outer["claudeAiOauth"] = oauth
    else:
        outer = oauth
    return json.dumps(outer)


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
    raw = os.environ.get("ACCOUNTS_EXCLUDE") or _conf_var("ACCOUNTS_EXCLUDE")
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
            # A passed reset means the window is empty ONLY if we polled after
            # it. A stale row (access token lapsed, so the poller skipped it and
            # never advanced this reset) must NOT read as replenished — that
            # showed false headroom and stranded switches onto spent accounts.
            last_seen = row.get("last_seen")
            if last_seen is not None:
                seen = datetime.fromtimestamp(last_seen, tz=timezone.utc)
                if seen >= reset:
                    return 0.0
            return float(pct)
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


_lock_depth = 0  # accounts is single-threaded; nested locked() must not re-flock


@contextmanager
def locked(blocking: bool = True):
    # Reentrant within this process. macOS flock ties the lock to the open file
    # description, so a second open()+flock from the SAME process (route_once
    # holds it → poll's merge_reset_rows re-takes it) blocks forever. Only the
    # outermost frame flocks; nested calls are no-ops.
    global _lock_depth
    if _lock_depth > 0:
        _lock_depth += 1
        try:
            yield
        finally:
            _lock_depth -= 1
        return
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_PATH, "w")
    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(handle, flags)
        _lock_depth = 1
        yield
    finally:
        _lock_depth = 0
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
        raise AccountsError(f'no live credential in keychain slot "{LIVE_SERVICE}"')
    blob_sha = sha256(blob)
    if not force and meta.get("last_live_sha") == blob_sha:
        return None

    # Attribution comes from the profile endpoint ONLY: it answers for the token
    # actually in the slot. ~/.claude.json can lag it and would mis-file the blob.
    token = blob_access_token(blob)
    if not token:
        raise AccountsError("live credential blob has no access token; not vaulting")
    profile = fetch_profile(token)
    if not profile:
        raise AccountsError("profile fetch failed; cannot attribute live credential (will retry)")
    ident = identity_from_profile(profile)
    key = vault_key(ident)
    if key is None:
        raise AccountsError("profile response missing account/org uuid; not vaulting")

    kc_vault_put(key, blob)
    if kc_read(VAULT_SERVICE, key) != blob:
        raise AccountsError("vault write verification failed (read-back mismatch)")

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
            + ". Run `accounts enroll` while it is active and it will be captured."
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
    we didn't poll. Re-reads under the accounts lock (serializes accounts writers only —
    the statusline writes this file without the lock). Atomic rename."""
    if not rows:
        return
    with locked():
        current = load_resets()
        current.update(rows)
        RESETS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = RESETS_PATH.with_suffix(".accounts-tmp")
        tmp.write_text(json.dumps(current, indent=1) + "\n")
        os.replace(tmp, RESETS_PATH)


def cmd_poll(_args) -> None:
    """Query every stored account's remaining limits and repaint the board."""
    n = poll_blobs_usage(load_blobs())
    print(f"refreshed usage for {n} account(s)")


def cmd_refresh(args) -> None:
    """Re-auth stale accounts without a browser: swap each expired access token
    for a fresh one via its own refresh token, in place. On-demand and explicit —
    `poll` never does this, since it runs on the statusline's timer and would
    rotate a credential on every repaint. Default target set is every account
    whose access token has expired but whose refresh token is still alive; pass a
    label to force one."""
    now = time.time()
    accounts = load_blobs().get("accounts") or {}
    if args.label:
        if args.label not in accounts:
            raise AccountsError(f"no stored blob for '{args.label}'")
        targets = [args.label]
    else:
        targets = []
        for label, e in accounts.items():
            blob = e.get("blob", "")
            acc_exp = blob_access_expiry(blob)
            access_stale = acc_exp is None or now >= acc_exp
            if access_stale and not blob_expired(blob, now):
                targets.append(label)
    if not targets:
        print("nothing to refresh — every access token is current")
        return
    revived = 0
    for label in targets:
        try:
            new_blob = refresh_blob_access(accounts[label].get("blob", ""))
        except TokenRefreshError as e:
            hint = "token endpoint throttled, retry in a minute" if e.code == "429" else "not refreshed"
            print(f"  {label}: HTTP {e.code} — {hint}")
            continue
        except Exception as e:  # noqa: BLE001 - report and move on, never brick a blob
            print(f"  {label}: {type(e).__name__} — not refreshed")
            continue
        if not new_blob:
            print(f"  {label}: no usable refresh token — needs a browser /login")
            continue
        # Persist now: the old refresh token is already dead server-side.
        with locked():
            fresh = load_blobs()
            fresh.setdefault("accounts", {}).setdefault(label, {})["blob"] = new_blob
            save_blobs(fresh)
        revived += 1
        print(f"  {label}: refreshed")
    if revived:
        poll_blobs_usage(load_blobs())
        print(f"repainted board for {revived} refreshed account(s)")


# ── token vault + router (zero keychain) ──────────────────────────────────
# Long-lived per-account tokens minted by `claude setup-token`, stored in a
# 0600 file OUTSIDE ~/.claude (the nightly archival chain mirrors ~/.claude
# session data to hound in plaintext — long-lived tokens must never land in an
# archived path). Routing injects
# CLAUDE_CODE_OAUTH_TOKEN into the child env, which outranks keychain auth;
# the live keychain slot stays 100% Claude-Code-owned.

TOKEN_VAULT_PATH = HOME / ".accounts" / "vault.json"
TOKEN_LIFETIME_S = 364 * 24 * 3600
TOKEN_RE = re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")

# The switch: cc reads ~/.claude/.credentials.json (verified on macOS). Writing
# it is a plain FILE write — no keychain, no ACL/partition, so it can never
# storm. We write the account's static setup-token blob; cc adopts that account.
# The system, plainly: full OAuth blobs for every account live in a 0600 FILE
# (~/.accounts/blobs.json). The daemon polls each blob's usage, and when the active
# account runs low it OVERWRITES the single file cc reads (~/.claude/
# .credentials.json) with the best account's blob — cc re-reads it and switches.
# Every write is a plain file write: no keychain, no ACL/partition, cannot storm.
# Full blobs (not setup-tokens) because only they carry the refresh token cc
# needs and the scope the usage endpoint needs.
CRED_FILE = HOME / ".claude" / ".credentials.json"
MODE_PATH = HOME / ".accounts" / "mode.json"
BLOBS_PATH = HOME / ".accounts" / "blobs.json"


def load_mode() -> dict:
    try:
        m = json.loads(MODE_PATH.read_text())
        if m.get("mode") in ("auto", "set", "fable"):
            return m
    except (OSError, json.JSONDecodeError):
        pass
    return {"mode": "auto", "label": None}


def save_mode(mode: str, label: str | None) -> None:
    MODE_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = MODE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"mode": mode, "label": label}) + "\n")
    os.replace(tmp, MODE_PATH)


def _write_0600(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".accounts-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def _preserve_corrupt(path: Path) -> None:
    """Copy an unparseable store aside so a bad read can't silently destroy it."""
    try:
        content = path.read_bytes()
        newest = max(path.parent.glob(f"{path.name}.corrupt.*"), default=None)
        if newest is not None and newest.read_bytes() == content:
            return  # already preserved; don't pile up one copy per daemon pass
        backup = path.with_name(f"{path.name}.corrupt.{int(time.time())}")
        n = 0
        while backup.exists():  # same-second different corruption: never clobber a backup
            n += 1
            backup = path.with_name(f"{path.name}.corrupt.{int(time.time())}.{n}")
        shutil.copy2(path, backup)
        log_line(f"warn: unreadable {path.name} preserved as {backup.name}")
    except OSError:
        pass


def load_blobs() -> dict:
    try:
        raw = BLOBS_PATH.read_text()
    except FileNotFoundError:
        return {"version": 1, "accounts": {}}
    except OSError as exc:
        # Present but unreadable (perms, I/O): treating it as empty would let the
        # next capture overwrite the store with one account. Refuse instead.
        raise AccountsError(f"{BLOBS_PATH} unreadable ({exc}) — refusing to treat as empty") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Readable but corrupt: preserve it before any capture overwrites the store,
        # so a bad parse never silently destroys the other accounts' stored logins.
        _preserve_corrupt(BLOBS_PATH)
        return {"version": 1, "accounts": {}}
    if not isinstance(parsed, dict):
        _preserve_corrupt(BLOBS_PATH)
        return {"version": 1, "accounts": {}}
    return parsed


def save_blobs(blobs: dict) -> None:
    _write_0600(BLOBS_PATH, json.dumps(blobs, indent=2, sort_keys=True) + "\n")


def migrate_vault_to_blobs() -> int:
    """One-time seed: copy each account's full blob out of the keychain vault
    into the 0600 file store. Keychain READS only (allow-all vault items are
    silent); nothing is written to the keychain."""
    meta = load_meta()
    pairs = load_label_pairs()
    blobs = load_blobs()
    n = 0
    for key, entry in meta.get("accounts", {}).items():
        label = resolve_label(entry.get("email"), entry.get("org_uuid"), pairs)
        blob = kc_read(VAULT_SERVICE, key)
        if not blob:
            continue
        blobs.setdefault("accounts", {})[label] = {
            "blob": blob,
            "email": entry.get("email"),
            "org_uuid": entry.get("org_uuid"),
            "org_type": entry.get("org_type"),
        }
        n += 1
    save_blobs(blobs)
    return n


def write_live_creds(blob: str) -> None:
    """Atomic 0600 write of ~/.claude/.credentials.json. FILE ONLY; the live
    keychain slot is never added/modified (that write is what stormed
    2026-07-17 — deletion is the only sanctioned keychain-slot operation)."""
    _write_0600(CRED_FILE, blob)


def live_cred() -> tuple[str | None, str | None]:
    """The credential cc is actually using, mirroring cc's own read order
    (its store is literally 'keychain-with-plaintext-fallback', re-read on a
    ~30s TTL): the keychain slot when it exists, else .credentials.json.
    Returns (blob, 'keychain'|'file') or (None, None)."""
    kc = kc_read(LIVE_SERVICE)
    if kc:
        return kc, "keychain"
    try:
        return CRED_FILE.read_text(), "file"
    except OSError:
        return None, None


def capture_live_to_blobs(blobs: dict) -> str | None:
    """Fold cc's current credential back into the store so the account stays
    pollable and its next restore is current. cc refreshes into the KEYCHAIN
    (recreating the slot and deleting the file), so the live source oscillates
    keychain<->file. Returns the active label if identified."""
    live, _src = live_cred()
    if live is None:
        return None
    tok = blob_access_token(live)
    if not tok:
        return None
    for label, e in (blobs.get("accounts") or {}).items():
        if blob_access_token(e.get("blob", "")) == tok:
            return label  # unchanged
    # token changed (cc refreshed) — attribute via profile and update its entry
    prof = fetch_profile(tok)
    if not prof:
        return None
    ident = identity_from_profile(prof)
    label = resolve_label(ident.get("email"), ident.get("org_uuid"), load_label_pairs())
    acct = blobs.setdefault("accounts", {}).setdefault(label, {})
    acct.update({"blob": live, "email": ident.get("email"), "org_uuid": ident.get("org_uuid")})
    save_blobs(blobs)
    return label


def apply_account(label: str, blobs: dict) -> bool:
    """Make `label` the live account. cc reads keychain-first with the file as
    fallback, so the switch is: write the file, then DELETE the keychain slot —
    cc's next ~30s re-read misses the keychain and picks up the file, mid-
    session. Deletion never re-pins ACLs (there is nothing left to pin) and cc
    recreates the slot itself on its next refresh; the slot is never
    added/modified from here. Returns False if we hold no blob for `label`."""
    e = (blobs.get("accounts") or {}).get(label)
    if not e or not e.get("blob"):
        return False
    write_live_creds(e["blob"])
    status = kc_slot_status(LIVE_SERVICE)
    if status == "present":
        kc_delete(LIVE_SERVICE)
        status = kc_slot_status(LIVE_SERVICE)
    if status != "absent":  # survived delete, or unreadable (locked keychain / wedged securityd)
        log_line(f"warn: keychain live slot {status} after switch; cc may keep reading it")
    return True


def blob_expired(blob: str, now_ts: float) -> bool:
    """True when the stored credential can no longer be used to switch — no
    refresh token at all, or a known refresh expiry in the past. cc's rotation
    rewrites carry refreshToken but OMIT refreshTokenExpiresAt (only fresh
    /login blobs have it), so a missing expiry is alive, not dead. This is the
    'needs a fresh /login' state the statusline flags ⚠login."""
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return True
    oauth = data.get("claudeAiOauth") if isinstance(data.get("claudeAiOauth"), dict) else data
    if not isinstance(oauth, dict) or not oauth.get("refreshToken"):
        return True
    exp = blob_refresh_expiry(blob)
    return exp is not None and now_ts >= exp


def poll_blobs_usage(blobs: dict) -> int:
    """Query each stored account's remaining limits with its OWN access token
    and write them to the board (account-resets.json). Pure reads. Skips blobs
    whose access token has expired (would 401) — those show the reset-aware
    estimate until the account is next active and its blob refreshes."""
    now = int(time.time())
    fresh: dict[str, dict] = {}
    for label, e in (blobs.get("accounts") or {}).items():
        blob = e.get("blob", "")
        exp = blob_access_expiry(blob)
        if exp is not None and now >= exp:
            continue
        token = blob_access_token(blob)
        if not token:
            continue
        usage = fetch_usage(token)
        if usage is None:
            continue
        fresh[f"{e.get('email')}|{e.get('org_uuid')}"] = usage_to_reset_row(
            e.get("email"), e.get("org_uuid"), usage, now
        )
    merge_reset_rows(fresh)
    return len(fresh)


def route_rows(blobs: dict, active_label: str | None, now_ts: float) -> list[dict]:
    """One row per stored account: label, effective 5h% (reset-aware, from the
    board the poll just refreshed), whether its cred is expired, whether it's
    the live account. Sorted best-first (lowest 5h)."""
    resets = load_resets()
    rows = []
    for label, e in (blobs.get("accounts") or {}).items():
        row = resets_row(resets, e.get("email"), e.get("org_uuid"))
        effs = effective_pcts(row, now_utc())
        rows.append(
            {
                "label": label,
                "email": e.get("email"),
                "five_hour": effs["five_hour"],
                "seven_day": effs["seven_day"],
                "fable": effs["fable"],
                "expired": blob_expired(e.get("blob", ""), now_ts),
                "active": label == active_label,
            }
        )
    rows.sort(key=lambda r: (float("inf") if r["five_hour"] is None else r["five_hour"]))
    return rows


def route_pick(rows: list[dict], excludes: set[str]) -> str | None:
    """Best switchable account: lowest 5h, not active, not excluded, cred live."""
    for r in rows:
        if r["active"] or r["expired"] or r["label"] in excludes:
            continue
        return r["label"]
    return None


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


HOUND_HOSTS = ("hound", "hound-ts")
HOUND_VAULT = "~/.accounts/vault.json"


def hound_host(timeout: int = 3) -> str | None:
    for host in HOUND_HOSTS:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}", host, "true"],
            capture_output=True,
        )
        if r.returncode == 0:
            return host
    return None


def merge_token_vaults(a: dict, b: dict) -> dict:
    """Union by label; the newer mint wins. Both sides keep the full set."""
    out: dict = {"version": 1, "tokens": {}}
    for src in (a, b):
        for label, entry in (src.get("tokens") or {}).items():
            cur = out["tokens"].get(label)
            if cur is None or entry.get("minted_at", 0) > cur.get("minted_at", 0):
                out["tokens"][label] = entry
    return out


def sync_with_hound(quiet: bool = False) -> bool:
    """Converge the token vault with hound: pull, merge (newest mint per label
    wins), write local, push the merged set back — both machines end up with
    the full vault. Best-effort: hound unreachable leaves local untouched.
    Tokens transit ssh stdio only, never argv."""
    host = hound_host()
    if host is None:
        if not quiet:
            print("hound unreachable — vault stays local-only (run `accounts sync` later)")
        return False
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host, f"cat {HOUND_VAULT} 2>/dev/null || true"],
        capture_output=True,
        text=True,
    )
    try:
        remote = json.loads(r.stdout) if r.stdout.strip() else {"version": 1, "tokens": {}}
    except json.JSONDecodeError:
        remote = {"version": 1, "tokens": {}}
    merged = merge_token_vaults(load_token_vault(), remote)
    save_token_vault(merged)
    push = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            host,
            f"mkdir -p ~/.accounts && chmod 700 ~/.accounts && cat > {HOUND_VAULT} && chmod 600 {HOUND_VAULT}",
        ],
        input=json.dumps(merged, indent=2, sort_keys=True) + "\n",
        capture_output=True,
        text=True,
    )
    ok = push.returncode == 0
    if not quiet:
        if ok:
            print(f"synced with {host}: {len(merged['tokens'])} token(s) on both sides")
        else:
            print(f"pulled from {host} but push failed: {push.stderr.strip()[:120]}")
    return ok


def cmd_sync(_args) -> None:
    """Converge ~/.accounts/vault.json between this machine and hound."""
    sync_with_hound()


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
    sync_with_hound(quiet=False)


def cmd_tokens(_args) -> None:
    vault = load_token_vault()
    tokens = vault.get("tokens") or {}
    if not tokens:
        print("no minted tokens — run `accounts mint <label>` per account you want routable")
        return
    now = time.time()
    for label in sorted(tokens):
        e = tokens[label]
        days = int((e.get("expires_at", 0) - now) / 86400)
        state = f"{days}d left" if days > 0 else "EXPIRED — re-mint"
        print(f"  {label:<12} minted {datetime.fromtimestamp(e.get('minted_at', 0)).date()}  {state}")


def _fable_first(rows: list[dict]) -> list[dict]:
    """Reorder for fable mode: fable-eligible accounts first (least fable used),
    everything else keeps its headroom_rank order. pick_route then takes the first
    with a live token, so fallback (no eligible or no-token fable account) is
    automatic. Stable sort preserves the headroom order inside each group."""

    def key(r: dict) -> tuple:
        effs = r["effs"]
        if fable_eligible(effs["five_hour"], effs["seven_day"], effs["fable"]):
            return (0, effs["fable"])
        return (1, 0.0)

    return sorted(rows, key=key)


def cmd_pick_env(_args) -> None:
    """Emit `export` lines for the best routable account (consumed by the
    `claude` shell wrapper). Prints nothing when there is no routable token —
    the wrapper then falls back to Claude Code's native keychain auth. Never
    fails: routing must never block launching claude."""
    try:
        rows = _account_rows(load_meta())
        if load_mode().get("mode") == "fable":
            rows = _fable_first(rows)
        pick = pick_route(
            rows,
            load_token_vault(),
            excluded_labels(),
            time.time(),
            os.environ.get("ACCOUNTS_PIN") or None,
        )
    except Exception:
        return
    if pick is None:
        return
    label, token = pick
    print(f"export CLAUDE_CODE_OAUTH_TOKEN='{token}'")
    print(f"export ACCOUNTS_ROUTED_LABEL='{label}'")


ROUTE_AT_DEFAULT = 80.0  # switch when the active account's 5h usage crosses this
ROUTE_HYSTERESIS_PCT = 10.0  # below cap: only switch for a headroom gain this large
ROUTE_CAP_PCT = 95.0  # at/above this the live account is ~capped: escape to a usable account
ROUTE_MIN_DWELL_S = 300.0  # >= this long between AUTO switches (each cold-starts a prompt cache)
FABLE_CAP_PCT = 95.0  # fable weekly window ~exhausted; separate axis from ROUTE_CAP_PCT (5h)
FABLE_HYSTERESIS_PCT = 10.0  # fable mode: only chase a meaningfully fresher account (no near-tie churn)

# Monotonic so an NTP step-back can't wedge routing; None = never switched this
# process. Daemon is one long-lived process, so no persistence needed.
_last_switch_ts: float | None = None


def notify(text: str, title: str = "accounts") -> None:
    safe = text.replace('"', "'")
    subprocess.run(
        ["osascript", "-e", f'display notification "{safe}" with title "{title}"'],
        capture_output=True,
        text=True,
    )


def fable_eligible(
    five_hour: float | None, seven_day: float | None, fable: float | None
) -> bool:
    """Usable for Fable work: headroom on the fable window AND both rate windows
    (5h and 7d). Fable headroom is worthless if either rate window is capped — you
    can't make requests at all (a maxed weekly is why a fresh-fable account can
    still be unusable). fable None (tier has no weekly_scoped limit), or any of the
    three axes None or at/over cap → ineligible."""
    return (
        fable is not None
        and fable < FABLE_CAP_PCT
        and five_hour is not None
        and five_hour < ROUTE_CAP_PCT
        and seven_day is not None
        and seven_day < ROUTE_CAP_PCT
    )


def route_pick_fable(rows: list[dict], excludes: set[str]) -> str | None:
    """Best OTHER account to switch to for Fable: eligible on both axes, not
    active/expired/excluded, least fable used first (5h as tiebreak). None when no
    other account qualifies — the caller then holds or falls back to the 5h router."""
    eligible = [
        r
        for r in rows
        if not r["active"]
        and not r["expired"]
        and r["label"] not in excludes
        and fable_eligible(r["five_hour"], r["seven_day"], r["fable"])
    ]
    if not eligible:
        return None
    eligible.sort(key=lambda r: (r["fable"], r["five_hour"]))  # both non-None by eligibility
    return eligible[0]["label"]


def _auto_switch(
    rows: list[dict],
    by_label: dict,
    active: str | None,
    blobs: dict,
    threshold: float,
    excludes: set[str],
) -> str | None:
    """AUTO decision: switch to the freshest 5h account when the live one is past
    `threshold`, with cap/hysteresis/dwell anti-thrash. Shared by AUTO and the
    fable-mode fallback. Caller holds the lock."""
    global _last_switch_ts
    cur = by_label.get(active) if active else None
    if cur is None or cur["five_hour"] is None or cur["five_hour"] < threshold:
        return None
    pick = route_pick(rows, excludes)
    if pick is None:
        return None
    cur_pct = cur["five_hour"]
    pick_pct = by_label[pick]["five_hour"]
    if pick_pct is None:  # no board row for the pick: never switch (also no log crash)
        return None
    if cur_pct >= ROUTE_CAP_PCT:
        # Live account is ~capped. Only worth escaping to one with real headroom
        # (below the cap); if every account is capped there's nothing better, so
        # hold — never thrash between two dead accounts (that was the flap).
        if pick_pct >= ROUTE_CAP_PCT:
            return None
    elif pick_pct >= cur_pct - ROUTE_HYSTERESIS_PCT:
        # Not capped: hold unless a MEANINGFULLY fresher account exists.
        return None
    # Each switch cold-starts the new account's prompt cache, so cap the rate at
    # one per dwell regardless of how the board drifts under usage feedback.
    if _last_switch_ts is not None and time.monotonic() - _last_switch_ts < ROUTE_MIN_DWELL_S:
        return None
    if apply_account(pick, blobs):
        _last_switch_ts = time.monotonic()
        return f"ROUTED {active} → {pick} (5h {cur_pct:.0f}% → {pick_pct:.0f}%)"
    return None


def route_once(threshold: float) -> str | None:
    """One pass of the whole system: fold in any cc refresh, poll every stored
    account's limits, then — in AUTO — if the live account is past `threshold`
    and a better one exists, overwrite .credentials.json with it. In SET, keep
    the pinned account live. Returns a log line if it switched, else None.
    Only ever writes files; never the keychain."""
    global _last_switch_ts
    with locked():
        blobs = load_blobs()
        capture_live_to_blobs(blobs)  # fold any cc refresh from before this pass
        poll_blobs_usage(blobs)  # seconds of network per account
        # cc may have refreshed (and rotated) the live token during the poll;
        # re-capture so we never overwrite an unsaved rotation below.
        active = capture_live_to_blobs(blobs)
        now = time.time()
        rows = route_rows(blobs, active, now)
        by_label = {r["label"]: r for r in rows}
        mode = load_mode()

        if mode["mode"] == "set":
            target = mode.get("label")
            if not target or target == active:
                return None
            # A keychain blob carrying refreshTokenExpiresAt is a fresh /login (cc's
            # rotations omit it): the user picked an account — move the pin, don't clobber.
            if active is not None:
                live, src = live_cred()
                if src == "keychain" and live and blob_refresh_expiry(live) is not None:
                    save_mode("set", active)
                    return f"ADOPT /login {target} → {active} (pin follows the login)"
            if (by_label.get(target) or {}).get("expired"):
                return None
            # active is None both for "no live cred anywhere" (seed it) and
            # "live cred present but profile fetch failed" (unattributed). Don't
            # overwrite an unattributed live token — it may be a cc-rotated one
            # we'd clobber.
            if active is None and live_cred()[0] is not None:
                return None
            if apply_account(target, blobs):
                return f"SET {active or '?'} → {target}"
            return None

        excludes = excluded_labels()

        if mode["mode"] == "fable":
            cur = by_label.get(active) if active else None
            if cur is None:
                return None  # unattributed/absent live cred — don't clobber (mirrors AUTO/SET)
            pick = route_pick_fable(rows, excludes)  # best OTHER eligible account
            if pick is None:
                # No other Fable account. Stay if live is still Fable-usable; else the
                # Fable window is spent everywhere → hand to the normal 5h router.
                if fable_eligible(cur["five_hour"], cur["seven_day"], cur["fable"]):
                    return None
                return _auto_switch(rows, by_label, active, blobs, threshold, excludes)
            pick_fable = by_label[pick]["fable"]
            # Live still usable → move only for a MEANINGFULLY fresher account (weekly
            # fable drifts slowly, so this holds most passes). Live NOT usable → switch.
            if (
                fable_eligible(cur["five_hour"], cur["seven_day"], cur["fable"])
                and pick_fable >= cur["fable"] - FABLE_HYSTERESIS_PCT
            ):
                return None
            if _last_switch_ts is not None and time.monotonic() - _last_switch_ts < ROUTE_MIN_DWELL_S:
                return None
            if apply_account(pick, blobs):
                _last_switch_ts = time.monotonic()
                cur_f = f"{cur['fable']:.0f}%" if cur["fable"] is not None else "—"
                return f"FABLE {active} → {pick} (fable {cur_f} → {pick_fable:.0f}%)"
            return None

        return _auto_switch(rows, by_label, active, blobs, threshold, excludes)


def cmd_route(args) -> None:
    """The router daemon. Every --interval seconds: capture cc's own refreshes,
    poll all stored accounts' limits, and (AUTO) switch the live .credentials
    .json to the freshest account when the current one runs low, or (SET) hold
    the pinned account. All file writes — cannot storm."""
    log_line(
        f"accounts route started (interval {args.interval}s; switch at ≥{args.at:.0f}% 5h; "
        f"mode={load_mode()['mode']})"
    )
    while True:
        try:
            line = route_once(args.at)
            if line:
                log_line(line)
                notify(line.split(" (")[0], "accounts")
        except AccountsError as exc:
            log_line(f"warn: {exc}")
        except Exception as exc:  # daemon must survive anything
            log_line(f"error: {type(exc).__name__}: {exc}")
        if args.once:
            return
        time.sleep(args.interval)


def cmd_set(args) -> None:
    """Pin the live account to <label> and hold it there (manual mode)."""
    with locked():
        blobs = load_blobs()
        e = (blobs.get("accounts") or {}).get(args.label)
        if not e:
            die(f"'{args.label}' has no stored blob — run `accounts migrate` or mint/login it first")
        if blob_expired(e.get("blob", ""), time.time()):
            die(f"'{args.label}' refresh token is expired — /login it first (won't write a dead cred)")
        active = capture_live_to_blobs(blobs)  # fold any unsaved rotation of the outgoing account
        # Mirror route_once's SET guard: live creds present but unattributable
        # (offline / profile fetch failed) may be a cc-rotated token not yet in the
        # store — overwriting would lose that account's login.
        if active is None and live_cred()[0] is not None:
            die(
                "live creds present but unattributable (offline, or profile fetch failed) — "
                "retry online before forcing a switch"
            )
        save_mode("set", args.label)
        ok = apply_account(args.label, blobs)
    print(f"SET → {args.label}" + ("" if ok else " (mode set; blob missing, not applied)"))
    print("new + restarted sessions use it; a running session picks it up on its next creds re-read")


def cmd_auto(_args) -> None:
    """Hand routing back to the daemon (switch to freshest when the live account runs low)."""
    save_mode("auto", None)
    with locked():
        blobs = load_blobs()
        active = capture_live_to_blobs(blobs)
        rows = route_rows(blobs, active, time.time())
    pick = route_pick(rows, excluded_labels())
    print("AUTO — daemon routes to the freshest account when the live one runs low")
    print(f"  live: {active or '?'} · would pick now: {pick or '(none free)'}")


def cmd_fable(_args) -> None:
    """Prefer an account with Fable (premium weekly) headroom; degrade to normal
    5h routing when none can do Fable. The daemon does the live switch."""
    save_mode("fable", None)
    with locked():
        blobs = load_blobs()
        active = capture_live_to_blobs(blobs)
        rows = route_rows(blobs, active, time.time())
    excludes = excluded_labels()
    # Display-only: best eligible INCLUDING the active account (route_pick_fable excludes it).
    usable = sorted(
        (
            r
            for r in rows
            if r["label"] not in excludes
            and not r["expired"]
            and fable_eligible(r["five_hour"], r["seven_day"], r["fable"])
        ),
        key=lambda r: (r["fable"], r["five_hour"]),
    )
    print("FABLE — daemon keeps you on a Fable-capable account; normal routing when none is")
    if not usable:
        print(
            f"  live: {active or '?'} · no Fable headroom anywhere right now — "
            f"routing normally (would pick {route_pick(rows, excludes) or '(none free)'})"
        )
    elif usable[0]["label"] == active:
        print(f"  live: {active} · already on the best Fable account (fable {usable[0]['fable']:.0f}%)")
    else:
        print(
            f"  live: {active or '?'} · will switch to {usable[0]['label']} "
            f"(fable {usable[0]['fable']:.0f}%) within ~30s"
        )


def cmd_status(_args) -> None:
    mode = load_mode()
    with locked():
        blobs = load_blobs()
        active = capture_live_to_blobs(blobs)
        rows = route_rows(blobs, active, time.time())
    if mode["mode"] == "set":
        tag = f"SET → {mode['label']}"
    elif mode["mode"] == "fable":
        tag = "FABLE"
    else:
        tag = "AUTO"
    print(f"mode: {tag}   live: {active or '?'}")
    for r in rows:
        mark = "*" if r["active"] else " "
        pct = "—" if r["five_hour"] is None else f"{r['five_hour']:.0f}%"
        fpct = "—" if r["fable"] is None else f"{r['fable']:.0f}%"
        flag = "  ⚠login" if r["expired"] else ""
        print(f" {mark} {r['label']:<12} 5h {pct:>5}  fable {fpct:>5}{flag}")


def cmd_migrate(_args) -> None:
    with locked():
        n = migrate_vault_to_blobs()
    print(f"seeded {n} account blob(s) into {BLOBS_PATH}")


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
        print("vault is empty — run `accounts enroll` on each account once")
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
    """Advisory only — accounts never writes the live keychain slot. macOS pins the
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
    print("(accounts does not switch accounts itself: writing the live keychain slot")
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
    parser = argparse.ArgumentParser(prog="accounts", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("enroll", help="vault the active account now").set_defaults(fn=cmd_enroll)

    p_route = sub.add_parser("route", help="router daemon: poll all, auto-switch the live account")
    p_route.add_argument("--interval", type=float, default=30.0)
    p_route.add_argument("--at", type=float, default=ROUTE_AT_DEFAULT, help="switch at this 5h%%")
    p_route.add_argument("--once", action="store_true")
    p_route.set_defaults(fn=cmd_route)

    p_set = sub.add_parser("set", help="pin the live account to <label> (manual mode)")
    p_set.add_argument("label")
    p_set.set_defaults(fn=cmd_set)

    sub.add_parser("auto", help="hand routing back to the daemon").set_defaults(fn=cmd_auto)
    sub.add_parser(
        "fable", help="prefer a Fable-capable account; fall back to normal routing"
    ).set_defaults(fn=cmd_fable)
    sub.add_parser("status", help="mode + per-account 5h + ⚠login flags").set_defaults(fn=cmd_status)
    sub.add_parser("migrate", help="seed ~/.accounts/blobs.json from the keychain vault").set_defaults(
        fn=cmd_migrate
    )

    sub.add_parser(
        "poll", help="refresh the usage board for all stored accounts now"
    ).set_defaults(fn=cmd_poll)

    p_mint = sub.add_parser("mint", help="mint + vault a 1-year token via claude setup-token")
    p_mint.add_argument("label", help="account label (from ACCOUNT_LABELS)")
    p_mint.set_defaults(fn=cmd_mint)

    sub.add_parser("tokens", help="list minted tokens and expiry").set_defaults(fn=cmd_tokens)

    p_refresh = sub.add_parser(
        "refresh", help="re-auth stale blobs via their refresh token (no browser)"
    )
    p_refresh.add_argument(
        "label", nargs="?", help="account to refresh (default: all stale-but-refreshable)"
    )
    p_refresh.set_defaults(fn=cmd_refresh)

    sub.add_parser("sync", help="converge the token vault with hound").set_defaults(fn=cmd_sync)

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
    except AccountsError as exc:
        die(str(exc))


if __name__ == "__main__":
    main()
