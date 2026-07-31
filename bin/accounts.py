#!/usr/bin/env python3
"""accounts — Claude Code multi-account router and headroom board.

Interactive sessions use one native CLAUDE_CONFIG_DIR profile per account.
Credentials and entitlement caches stay isolated while projects, skills, settings,
and transcript state are shared. A supervisor routes each exact session by
reset-aware headroom and resumes it under another profile before quota exhaustion.

The setup-token vault remains separate for headless hound jobs.

Router commands:
  set LABEL       force every supervised session onto LABEL
  auto            route supervised sessions to the freshest account
  fable           run supervised sessions on Fable when headroom is available
  status / ls     mode + per-account 5h/7d/fable headroom, ⚠login flags
  poll / refresh  refresh the usage board / re-auth stale accounts (no browser)
  mint / tokens   mint a long-lived token for an account / list minted tokens
  sync            converge the minted-token vault with hound
  pick-env        emit env exports for the best routable account
"""

from __future__ import annotations

import argparse
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
LIVE_SERVICE = "Claude Code-credentials"
LOCK_PATH = HOME / ".claude" / "accounts.lock"
RESETS_PATH = HOME / ".claude" / "account-resets.json"
SESSION_LIMITS_PATH = HOME / ".accounts" / "session-limits.json"
NATIVE_REFRESH_LOCK_PATH = HOME / ".accounts" / "native-refresh.lock"
CONF_PATH = HOME / ".claude" / "statusline.conf"
MIRROR_LOG = HOME / ".claude" / "accounts-mirror.log"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # public Claude Code OAuth client (PKCE, no secret)
STALE_AFTER_S = 3 * 3600
SESSION_LIMIT_FALLBACK_S = 5 * 3600
NATIVE_DAEMON_MIN_VERSION = (2, 1, 220)
NATIVE_REFRESH_WAIT_S = 5.0
NATIVE_REFRESH_POLL_S = 0.1
LEGACY_ROUTE_AGENT_LABEL = "com.claude-accounts-route"
LEGACY_ROUTE_AGENT_PATH = HOME / "Library" / "LaunchAgents" / f"{LEGACY_ROUTE_AGENT_LABEL}.plist"


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


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def retire_legacy_route_agent() -> None:
    if sys.platform != "darwin":
        return
    try:
        LEGACY_ROUTE_AGENT_PATH.unlink(missing_ok=True)
    except OSError as exc:
        print(f"accounts: could not remove retired route agent plist: {exc}", file=sys.stderr)
    target = f"gui/{os.getuid()}/{LEGACY_ROUTE_AGENT_LABEL}"
    try:
        probe = subprocess.run(
            ["launchctl", "print", target],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"accounts: could not inspect retired route agent: {exc}", file=sys.stderr)
        return
    if probe.returncode != 0:
        return
    try:
        stopped = subprocess.run(
            ["launchctl", "bootout", target],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"accounts: could not stop retired route agent: {exc}", file=sys.stderr)
        return
    if stopped.returncode != 0:
        print("accounts: could not stop retired route agent; will retry", file=sys.stderr)


# ── keychain ──────────────────────────────────────────────────────────────


def kc_read(service: str, account: str | None = None) -> str | None:
    cmd = ["security", "find-generic-password", "-s", service]
    if account is not None:
        cmd += ["-a", account]
    cmd.append("-w")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.rstrip("\n")


def kc_delete(service: str) -> bool:
    try:
        r = subprocess.run(
            ["security", "delete-generic-password", "-s", service],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


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


# ── metadata / config ─────────────────────────────────────────────────────


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


def resets_row(resets: dict, email: str | None, org_uuid: str | None) -> dict:
    return resets.get(f"{email}|{org_uuid}", {})


# ── core ops ──────────────────────────────────────────────────────────────


_lock_depth = 0  # accounts is single-threaded; nested locked() must not re-flock


@contextmanager
def locked(blocking: bool = True):
    # A second open()+flock from the same process blocks on macOS.
    # Only the outermost frame flocks.
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


def mark_session_limit(
    email: str,
    org_uuid: str,
    *,
    now_ts: float | None = None,
) -> None:
    with locked():
        key = f"{email}|{org_uuid}"
        detected_at = time.time() if now_ts is None else now_ts
        row = resets_row(load_resets(), email, org_uuid)
        reset = parse_iso(row.get("five_hour_reset"))
        expires_at = (
            reset.timestamp()
            if reset is not None and reset.timestamp() > detected_at
            else detected_at + SESSION_LIMIT_FALLBACK_S
        )
        limits = load_session_limits(detected_at)
        limits[key] = {
            "detected_at": detected_at,
            "expires_at": expires_at,
        }
        _write_0600(
            SESSION_LIMITS_PATH,
            json.dumps(limits, indent=2, sort_keys=True) + "\n",
        )


def mark_fable_limit(
    email: str,
    org_uuid: str,
    *,
    now_ts: float | None = None,
) -> None:
    with locked():
        key = f"{email}|{org_uuid}|fable"
        detected_at = time.time() if now_ts is None else now_ts
        row = resets_row(load_resets(), email, org_uuid)
        reset = parse_iso(row.get("fable_reset"))
        expires_at = (
            reset.timestamp()
            if reset is not None and reset.timestamp() > detected_at
            else detected_at + SESSION_LIMIT_FALLBACK_S
        )
        limits = load_session_limits(detected_at)
        limits[key] = {
            "detected_at": detected_at,
            "expires_at": expires_at,
        }
        _write_0600(
            SESSION_LIMITS_PATH,
            json.dumps(limits, indent=2, sort_keys=True) + "\n",
        )


def load_session_limits(now_ts: float) -> dict[str, dict]:
    try:
        limits = json.loads(SESSION_LIMITS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(limits, dict):
        return {}
    return {
        key: value
        for key, value in limits.items()
        if isinstance(value, dict)
        and float(value.get("expires_at", 0)) > now_ts
    }


def cmd_poll(_args) -> None:
    """Query every stored account's remaining limits and repaint the board."""
    with locked():
        blobs = load_blobs()
        sync_profile_credentials(blobs, persist=True)
    refresh_dormant_profiles()
    with locked():
        blobs = load_blobs()
    n = poll_blobs_usage(blobs)
    print(f"refreshed usage for {n} account(s)")


def cmd_refresh(args) -> None:
    """Refresh stale file-backed credentials without a browser."""
    now = time.time()
    with locked():
        blobs = load_blobs()
        sync_profile_credentials(blobs, persist=True)
    accounts = blobs.get("accounts") or {}
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
        if kc_read(profile_keychain_service(label)):
            print(f"  {label}: active profile credential refreshes inside Claude")
            continue
        try:
            new_blob = refresh_blob_access(accounts[label].get("blob", ""))
        except TokenRefreshError as e:
            if e.code in ("400", "401", "403"):
                print(f"  {label}: HTTP {e.code} — refresh rejected, needs a browser /login")
                _persist_auth_dead(label)
            else:
                hint = (
                    "token endpoint throttled, retry in a minute"
                    if e.code == "429"
                    else "not refreshed"
                )
                print(f"  {label}: HTTP {e.code} — {hint}")
            continue
        except Exception as e:  # noqa: BLE001 - report and move on, never brick a blob
            print(f"  {label}: {type(e).__name__} — not refreshed")
            continue
        if not new_blob:
            print(f"  {label}: no usable refresh token — needs a browser /login")
            _persist_auth_dead(label)
            continue
        # Persist now: the old refresh token is already dead server-side.
        with locked():
            fresh = load_blobs()
            sync_profile_credentials(fresh, persist=False)
            set_entry_blob(fresh.setdefault("accounts", {}).setdefault(label, {}), new_blob)
            write_profile_credentials(label, new_blob)
            save_blobs(fresh)
        revived += 1
        print(f"  {label}: refreshed")
    if revived:
        poll_blobs_usage(load_blobs())
        print(f"repainted board for {revived} refreshed account(s)")


# ── token vault + native profile router ───────────────────────────────────
# Long-lived per-account tokens minted by `claude setup-token`, stored in a
# 0600 file OUTSIDE ~/.claude (the nightly archival chain mirrors ~/.claude
# session data to hound in plaintext — long-lived tokens must never land in an
# archived path). These tokens are for headless jobs, not interactive routing.

TOKEN_VAULT_PATH = HOME / ".accounts" / "vault.json"
TOKEN_LIFETIME_S = 364 * 24 * 3600
TOKEN_RE = re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")
PROFILES_PATH = HOME / ".accounts" / "profiles"
LEASES_PATH = HOME / ".accounts" / "leases.json"
CLAUDE_HOME = HOME / ".claude"
CLAUDE_STATE_PATH = HOME / ".claude.json"
LEASE_STALE_S = 30.0

PROFILE_SHARED_ENTRIES = (
    "CLAUDE.md",
    "agents",
    "certificate",
    "commands",
    "file-history",
    "history.jsonl",
    "hooks",
    "ide",
    "jobs",
    "parallel-agents.md",
    "paste-cache",
    "plans",
    "plugins",
    "pr-conventions.md",
    "projects",
    "remote-settings.json",
    "session-env",
    "session-history.jsonl",
    "sessions",
    "settings.json",
    "settings.local.json",
    "shell-snapshots",
    "skills",
    "slash-commands.json",
    "statusline.conf",
    "statusline.sh",
    "tasks",
    "workflows",
)

PROFILE_ACCOUNT_STATE_KEYS = {
    "additionalModelCostsCache",
    "additionalModelOptionsCache",
    "cachedDynamicConfigs",
    "cachedExperimentData",
    "cachedExperimentFeatures",
    "cachedExtraUsageDisabledReason",
    "cachedGrowthBookFeatures",
    "cachedGrowthBookFeaturesAt",
    "cachedStatsigGates",
    "clientDataCacheSlots",
    "fableOverageConsentV2",
    "modelAccessCache",
    "oauthAccount",
    "orgModelDefaultCache",
    "overageCreditGrantCache",
    "passesEligibilityCache",
    "penguinModeOrgEnabled",
    "s1mAccessCache",
    "userID",
}

# Default-profile credential source used to capture login and refresh state.
CRED_FILE = HOME / ".claude" / ".credentials.json"
MODE_PATH = HOME / ".accounts" / "mode.json"
BLOBS_PATH = HOME / ".accounts" / "blobs.json"


def native_profile_path(label: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", label):
        raise AccountsError(f"invalid account label: {label!r}")
    return PROFILES_PATH / label


def _seed_profile_state(profile: Path) -> None:
    state_path = profile / ".claude.json"
    if state_path.exists():
        return
    try:
        state = json.loads(CLAUDE_STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        state = {"hasCompletedOnboarding": True}
    for key in PROFILE_ACCOUNT_STATE_KEYS:
        state.pop(key, None)
    _write_0600(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")


def clear_profile_account_state(label: str) -> None:
    state_path = native_profile_path(label) / ".claude.json"
    try:
        state = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        state = {"hasCompletedOnboarding": True}
    if not isinstance(state, dict):
        state = {"hasCompletedOnboarding": True}
    for key in PROFILE_ACCOUNT_STATE_KEYS:
        state.pop(key, None)
    _write_0600(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")


def ensure_native_profile(label: str, entry: dict) -> Path:
    profile = native_profile_path(label)
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(PROFILES_PATH, 0o700)
    os.chmod(profile, 0o700)

    stored_blob = entry.get("blob", "")
    if not blob_access_token(stored_blob):
        raise AccountsError(f"'{label}' has no usable OAuth credential")
    credentials = profile / ".credentials.json"
    try:
        profile_blob = credentials.read_text()
    except OSError:
        profile_blob = ""
    if not blob_access_token(profile_blob):
        _write_0600(credentials, stored_blob)
    else:
        os.chmod(credentials, 0o600)

    _seed_profile_state(profile)
    for name in PROFILE_SHARED_ENTRIES:
        source = CLAUDE_HOME / name
        target = profile / name
        if not source.exists():
            continue
        if target.is_symlink():
            if target.resolve(strict=False) == source.resolve(strict=False):
                continue
            target.unlink()
        elif target.exists():
            continue
        target.symlink_to(source, target_is_directory=source.is_dir())
    return profile


def write_profile_credentials(label: str, blob: str) -> None:
    profile = native_profile_path(label)
    if profile.exists():
        _write_0600(profile / ".credentials.json", blob)


def _repin_known_profile_login(source_label: str, identity: dict, blobs: dict) -> None:
    mode = load_mode()
    if mode.get("mode") != "set" or mode.get("label") != source_label:
        return
    target_label = resolve_label(
        identity.get("email"),
        identity.get("org_uuid"),
        load_label_pairs(),
    )
    target = (blobs.get("accounts") or {}).get(target_label)
    if target_label == source_label or not target or entry_needs_login(target, time.time()):
        return
    save_mode("set", target_label)


def sync_profile_credentials(blobs: dict, *, persist: bool) -> set[str]:
    changed = False
    blocked_labels: set[str] = set()
    for label, entry in (blobs.get("accounts") or {}).items():
        credentials = native_profile_path(label) / ".credentials.json"
        profile_blob = profile_live_blob(label)
        if profile_blob is None:
            continue
        stored_blob = entry.get("blob", "")
        access_token = blob_access_token(profile_blob)
        if not access_token:
            if not blob_access_token(stored_blob):
                blocked_labels.add(label)
                mark_auth_dead(label, entry, time.time())
                changed = True
                continue
            keychain_service = profile_keychain_service(label)
            if kc_read(keychain_service) and not kc_delete(keychain_service):
                blocked_labels.add(label)
                mark_auth_dead(label, entry, time.time())
                changed = True
                continue
            _write_0600(credentials, stored_blob)
            clear_profile_account_state(label)
            set_entry_blob(entry, stored_blob)
            changed = True
            print(
                f"warning: repaired {label} unusable profile credential",
                file=sys.stderr,
            )
            continue
        if profile_blob == stored_blob:
            continue
        profile = fetch_profile(access_token)
        if not profile:
            # An expired candidate can't prove identity — that is staleness,
            # not a hijack. Leave the stored blob and keep the row routable;
            # verify-at-pick refreshes the live lineage when it's chosen.
            if blob_access_expiry(profile_blob) is not None and blob_access_expiry(
                profile_blob
            ) <= time.time():
                continue
            blocked_labels.add(label)
            continue
        identity = identity_from_profile(profile)
        expected_email = entry.get("email")
        expected_org_uuid = entry.get("org_uuid")
        if not expected_email or not expected_org_uuid:
            stored_access_token = blob_access_token(stored_blob)
            stored_profile = fetch_profile(stored_access_token) if stored_access_token else None
            if not stored_profile:
                blocked_labels.add(label)
                continue
            stored_identity = identity_from_profile(stored_profile)
            expected_email = expected_email or stored_identity["email"]
            expected_org_uuid = expected_org_uuid or stored_identity["org_uuid"]
        email_mismatch = identity["email"] != expected_email
        org_mismatch = identity["org_uuid"] != expected_org_uuid
        if email_mismatch or org_mismatch:
            if not blob_access_token(stored_blob):
                blocked_labels.add(label)
                mark_auth_dead(label, entry, time.time())
                print(
                    f"warning: {label} profile login does not match its stored identity; "
                    "blocked routing",
                    file=sys.stderr,
                )
                changed = True
                continue
            keychain_service = profile_keychain_service(label)
            if kc_read(keychain_service) and not kc_delete(keychain_service):
                blocked_labels.add(label)
                mark_auth_dead(label, entry, time.time())
                print(
                    f"warning: {label} profile login does not match its stored identity; "
                    "could not remove the mismatched profile credential",
                    file=sys.stderr,
                )
                changed = True
                continue
            _write_0600(credentials, stored_blob)
            clear_profile_account_state(label)
            set_entry_blob(entry, stored_blob)
            entry["email"] = expected_email
            entry["org_uuid"] = expected_org_uuid
            _repin_known_profile_login(label, identity, blobs)
            changed = True
            print(
                f"warning: repaired {label} profile login from its stored identity",
                file=sys.stderr,
            )
            continue
        set_entry_blob(entry, profile_blob)
        entry["email"] = identity["email"] or entry.get("email")
        entry["org_uuid"] = identity["org_uuid"] or entry.get("org_uuid")
        entry["org_type"] = identity["org_type"] or entry.get("org_type")
        changed = True
    if changed and persist:
        save_blobs(blobs)
    return blocked_labels


def load_mode_snapshot() -> tuple[dict, tuple[int, int] | None]:
    default = {"mode": "auto", "label": None}
    try:
        with MODE_PATH.open() as mode_file:
            stat = os.fstat(mode_file.fileno())
            try:
                mode = json.load(mode_file)
            except json.JSONDecodeError:
                mode = default
    except OSError:
        return default, None
    generation = (stat.st_ino, stat.st_mtime_ns)
    if isinstance(mode, dict) and mode.get("mode") in ("auto", "set", "fable"):
        return mode, generation
    return default, generation


def load_mode() -> dict:
    return load_mode_snapshot()[0]


def save_mode(mode: str, label: str | None) -> None:
    MODE_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = MODE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"mode": mode, "label": label}) + "\n")
    os.replace(tmp, MODE_PATH)


def _write_0600(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp.unlink(missing_ok=True)
        raise


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
            # cc is actively holding this credential — proof of life beats any
            # earlier rejected-refresh stamp (a lagged stored blob can 400 on
            # a refresh token cc has already rotated past).
            if e.get("auth_dead_at"):
                e.pop("auth_dead_at", None)
                save_blobs(blobs)
            return label  # unchanged
    # token changed (cc refreshed) — attribute via profile and update its entry
    prof = fetch_profile(tok)
    if not prof:
        return None
    ident = identity_from_profile(prof)
    label = resolve_label(ident.get("email"), ident.get("org_uuid"), load_label_pairs())
    acct = blobs.setdefault("accounts", {}).setdefault(label, {})
    set_entry_blob(acct, live)
    acct.update({"email": ident.get("email"), "org_uuid": ident.get("org_uuid")})
    save_blobs(blobs)
    return label


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


def entry_needs_login(entry: dict, now_ts: float) -> bool:
    """Routing/board verdict for one stored account: the blob's own metadata
    says it is unusable, or a live refresh attempt was rejected server-side
    (`auth_dead_at` — metadata can look alive while the server says no)."""
    return bool(entry.get("auth_dead_at")) or blob_expired(entry.get("blob", ""), now_ts)


def set_entry_blob(entry: dict, blob: str) -> None:
    """Every blob write comes through here so a fresh credential — a /login,
    a profile sync, a successful refresh — always clears the rejected flag."""
    entry["blob"] = blob
    entry.pop("auth_dead_at", None)


def mark_auth_dead(label: str, entry: dict, now_ts: float) -> None:
    """Record a server-rejected refresh; notify only on the transition."""
    first_time = not entry.get("auth_dead_at")
    entry["auth_dead_at"] = int(now_ts)
    if first_time:
        _notify_needs_login(label)


def _notify_needs_login(label: str) -> None:
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{label} needs /login — routing around it" '
                'with title "accounts"',
            ],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass


def _persist_auth_dead(label: str) -> None:
    """Stamp + save outside any existing lock (cmd_refresh's failure paths)."""
    with locked():
        fresh = load_blobs()
        entry = (fresh.get("accounts") or {}).get(label)
        if entry is not None:
            mark_auth_dead(label, entry, time.time())
            save_blobs(fresh)


def verify_entry_auth(label: str, entry: dict, now_ts: float) -> str:
    """'ok' | 'ok_rotated' | 'dead' | 'unavailable'. Exercises the refresh only
    when the access token is already expired — the one moment metadata can lie
    (a rotated blob carries a refreshToken the server may still reject). poll
    never does this; a session launch is rare enough to spend a rotation on.

    A profile-scoped keychain item is cc's own lineage and is cc-write-only
    here (rotating it from outside would strand the item above any fresher
    file in cc's read order). Item present → route and let cc prove it at
    launch, where a failure is visible immediately rather than mid-session."""
    item = kc_read(profile_keychain_service(label))
    if item:
        if blob_expired(item, now_ts):
            # cc will keep choosing this item by existence, not validity —
            # a lapsed refresh there is a real needs-/login, today.
            mark_auth_dead(label, entry, now_ts)
            return "dead"
        return "ok"
    blob = entry.get("blob", "")
    exp = blob_access_expiry(blob)
    if exp is not None and now_ts < exp:
        return "ok"
    try:
        new_blob = refresh_blob_access(blob)
    except TokenRefreshError as e:
        if e.code in ("400", "401", "403"):
            mark_auth_dead(label, entry, now_ts)
            return "dead"
        # Throttled or server-side trouble is not proof of death — just not now.
        return "unavailable"
    except Exception:
        return "unavailable"
    if not new_blob:
        mark_auth_dead(label, entry, now_ts)
        return "dead"
    set_entry_blob(entry, new_blob)
    write_profile_credentials(label, new_blob)
    return "ok_rotated"


def profile_keychain_service(label: str) -> str:
    """cc scopes its keychain item per config dir: the service name carries the
    first 8 hex of sha256 over the CLAUDE_CONFIG_DIR path."""
    digest = hashlib.sha256(str(native_profile_path(label)).encode()).hexdigest()[:8]
    return f"{LIVE_SERVICE}-{digest}"


def profile_live_blob(label: str) -> str | None:
    """The credential cc is actually using for this profile, mirroring cc's
    read order: the profile-scoped keychain item when it exists, else the
    profile's .credentials.json. The file is a one-shot seed — cc's first
    rotation moves the lineage into the keychain item, so the item is truth."""
    kc = kc_read(profile_keychain_service(label))
    if kc:
        return kc
    try:
        return (native_profile_path(label) / ".credentials.json").read_text()
    except OSError:
        return None


def native_claude_binary() -> str | None:
    binary = Path(
        os.environ.get("CLAUDE_REAL_BIN") or HOME / ".local/bin/claude"
    )
    if binary.is_file() and os.access(binary, os.X_OK):
        return str(binary)
    return None


def native_profile_refresh_supported(binary: str) -> bool:
    try:
        version = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", version.stdout)
    if (
        version.returncode != 0
        or match is None
        or tuple(map(int, match.groups())) < NATIVE_DAEMON_MIN_VERSION
    ):
        return False
    try:
        help_result = subprocess.run(
            [binary, "daemon", "run", "--help"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    help_text = help_result.stdout + help_result.stderr
    required = ("run [json-path]", "--json-path", "--log-file")
    return help_result.returncode == 0 and all(
        marker in help_text for marker in required
    )


@contextmanager
def try_native_refresh_lock():
    NATIVE_REFRESH_LOCK_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        handle = open(NATIVE_REFRESH_LOCK_PATH, "w")
    except OSError:
        yield None
        return
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        yield None
        return
    try:
        yield handle
    finally:
        # Native daemons inherit this descriptor, so the lock outlives this process.
        handle.close()


def refresh_keychain_profiles(
    labels: list[str],
    now_ts: float | None = None,
    *,
    wait_s: float = NATIVE_REFRESH_WAIT_S,
) -> int:
    if not labels:
        return 0
    with try_native_refresh_lock() as refresh_lock:
        if refresh_lock is None:
            return 0
        binary = native_claude_binary()
        if binary is None or not native_profile_refresh_supported(binary):
            return 0
        now_ts = time.time() if now_ts is None else now_ts
        before = {
            label: profile_live_blob(label) or ""
            for label in labels
        }
        processes: list[subprocess.Popen] = []
        launched: set[str] = set()
        for index, label in enumerate(labels):
            fd, json_name = tempfile.mkstemp(
                prefix=f"claude-auth-refresh-{os.getpid()}-{index}-",
                suffix=".json",
            )
            os.close(fd)
            json_path = Path(json_name)
            json_path.unlink()
            env = os.environ.copy()
            for name in (
                "ANTHROPIC_API_KEY",
                "ANTHROPIC_AUTH_TOKEN",
                "CLAUDE_CODE_OAUTH_TOKEN",
            ):
                env.pop(name, None)
            env["CLAUDE_CONFIG_DIR"] = str(native_profile_path(label))
            try:
                process = subprocess.Popen(
                    [
                        binary,
                        "daemon",
                        "run",
                        "--origin",
                        "transient",
                        "--json-path",
                        str(json_path),
                        "--log-file",
                        os.devnull,
                    ],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    pass_fds=(refresh_lock.fileno(),),
                )
            except OSError:
                continue
            processes.append(process)
            launched.add(label)

        refreshed: set[str] = set()
        deadline = time.monotonic() + wait_s
        while launched - refreshed:
            for label in launched - refreshed:
                blob = profile_live_blob(label) or ""
                expiry = blob_access_expiry(blob)
                if blob_access_token(blob) and (
                    blob != before[label]
                    or (expiry is not None and expiry > now_ts)
                ):
                    refreshed.add(label)
            for process in processes:
                process.poll()
            if not launched - refreshed or time.monotonic() >= deadline:
                break
            time.sleep(NATIVE_REFRESH_POLL_S)
        return len(refreshed)


def refresh_dormant_profiles() -> int:
    now = time.time()
    refreshed = 0
    changed = False
    keychain_labels: list[str] = []
    with locked():
        blobs = load_blobs()
        active_labels = {
            lease.get("label") for lease in load_session_leases()
        }
        excluded = excluded_labels()
        for label, entry in (blobs.get("accounts") or {}).items():
            if label in active_labels or label in excluded:
                continue
            blob = profile_live_blob(label) or entry.get("blob", "")
            access_expiry = blob_access_expiry(blob)
            if access_expiry is not None and access_expiry > now:
                continue
            if blob_expired(blob, now):
                continue
            keychain_service = profile_keychain_service(label)
            keychain_blob = kc_read(keychain_service)
            if keychain_blob:
                keychain_labels.append(label)
                continue
            try:
                new_blob = refresh_blob_access(blob)
            except TokenRefreshError as exc:
                if exc.code in ("400", "401", "403"):
                    mark_auth_dead(label, entry, now)
                    changed = True
                continue
            except Exception:
                continue
            if not new_blob:
                continue
            set_entry_blob(entry, new_blob)
            write_profile_credentials(label, new_blob)
            refreshed += 1
            changed = True
        if changed:
            save_blobs(blobs)
    return refreshed + refresh_keychain_profiles(keychain_labels, now)


def poll_blobs_usage(blobs: dict) -> int:
    """Query each stored account's remaining limits with its OWN access token
    and write them to the board (account-resets.json). Pure reads. Skips blobs
    whose access token has expired (would 401) — those show the reset-aware
    estimate until the account is next active and its blob refreshes."""
    now = int(time.time())
    fresh: dict[str, dict] = {}
    for label, e in (blobs.get("accounts") or {}).items():
        blob = profile_live_blob(label) or e.get("blob", "")
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
    """One row per stored account: label, effective pcts (reset-aware, from the
    board the poll just refreshed), staleness of that estimate, whether its
    cred is expired, whether it's the live account. Sorted best-first: most
    binding-window runway (lowest worst-of-5h/7d), freshest 5h as tiebreak;
    rows missing a rate axis are unpickable (_rate_eligible) and sort last."""
    resets = load_resets()
    session_limits = load_session_limits(now_ts)
    rows = []
    for label, e in (blobs.get("accounts") or {}).items():
        key = f"{e.get('email')}|{e.get('org_uuid')}"
        row = resets_row(resets, e.get("email"), e.get("org_uuid"))
        effs = effective_pcts(row, now_utc())
        if key in session_limits:
            effs["five_hour"] = 100.0
        if f"{key}|fable" in session_limits:
            effs["fable"] = 100.0
        last_seen = row.get("last_seen")
        rows.append(
            {
                "label": label,
                "email": e.get("email"),
                "five_hour": effs["five_hour"],
                "seven_day": effs["seven_day"],
                "fable": effs["fable"],
                "expired": entry_needs_login(e, now_ts),
                "active": label == active_label,
                "stale": not last_seen or (now_ts - last_seen) > STALE_AFTER_S,
            }
        )
    rows.sort(
        key=lambda r: (
            r["five_hour"] is None or r["seven_day"] is None,
            binding_pct(r["five_hour"], r["seven_day"]),
            float("inf") if r["five_hour"] is None else r["five_hour"],
            r["label"],
        )
    )
    return rows


def binding_pct(*pcts: float | None) -> float:
    """Worst usage across the windows that gate a session — the account's real
    runway is 100 minus this. Unknown axes are ignored; all-unknown sorts last."""
    known = [p for p in pcts if p is not None]
    return max(known) if known else float("inf")


def _rate_eligible(five_hour: float | None, seven_day: float | None) -> bool:
    """Usable for general work: headroom on BOTH rate windows. A maxed weekly
    blocks requests as hard as a maxed 5h, so an escape target must clear both
    or the switch just re-walls you (and, escaping onto the other axis's wall,
    ping-pongs). None on either axis is unknown → ineligible."""
    return (
        five_hour is not None
        and five_hour < RATE_CAP_PCT
        and seven_day is not None
        and seven_day < SEVEN_DAY_CAP_PCT
    )


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


def pick_profile_route(
    rows: list[dict],
    excludes: set[str],
    pin: str | None,
    *,
    require_fable: bool = False,
    force_pin: bool = False,
) -> str | None:
    if force_pin and pin is not None:
        for row in rows:
            if row["label"] != pin or row["expired"]:
                continue
            if require_fable and not fable_eligible(
                row["five_hour"],
                row["seven_day"],
                row["fable"],
            ):
                return None
            return row["label"]
        return None
    ordered = rows
    if pin is not None:
        ordered = sorted(rows, key=lambda row: row["label"] != pin)
    for row in ordered:
        if row["expired"]:
            continue
        if row.get("stale"):
            continue
        if row["label"] in excludes:
            continue
        if require_fable:
            if not fable_eligible(
                row["five_hour"],
                row["seven_day"],
                row["fable"],
            ):
                continue
        elif not _rate_eligible(row["five_hour"], row["seven_day"]):
            continue
        return row["label"]
    if require_fable:
        return None
    return _most_headroom(ordered, excludes, require_fable=False)


def _most_headroom(
    rows: list[dict],
    excludes: set[str],
    *,
    require_fable: bool,
) -> str | None:
    """Last resort when every account trips a ceiling: the one with the most
    runway still beats refusing to route. Only genuinely walled accounts
    (>= HARD_WALL_PCT on an axis they need) stay unpickable."""

    def usable(row: dict) -> bool:
        axes = [row["five_hour"], row["seven_day"]]
        if require_fable:
            axes.append(row["fable"])
        return all(pct is not None and pct < HARD_WALL_PCT for pct in axes)

    candidates = [
        row
        for row in rows
        if not row["expired"]
        and not row.get("stale")
        and row["label"] not in excludes
        and usable(row)
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda row: (
            binding_pct(row["five_hour"], row["seven_day"]),
            row["fable"] if require_fable and row["fable"] is not None else 0.0,
            row["label"],
        ),
    )["label"]


def rank_profile_rows(
    rows: list[dict],
    *,
    require_fable: bool = False,
) -> list[dict]:
    def score(row: dict) -> tuple:
        if require_fable:
            return _fable_rank(row)
        return (
            binding_pct(row["five_hour"], row["seven_day"]),
            float("inf") if row["five_hour"] is None else row["five_hour"],
            row["label"],
        )

    return sorted(rows, key=score)


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def load_session_leases(now_ts: float | None = None) -> list[dict]:
    now_ts = time.time() if now_ts is None else now_ts
    try:
        payload = json.loads(LEASES_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    leases = payload.get("leases") if isinstance(payload, dict) else None
    if not isinstance(leases, list):
        return []
    return [
        lease
        for lease in leases
        if isinstance(lease, dict)
        and isinstance(lease.get("pid"), int)
        and _pid_is_alive(lease["pid"])
        and now_ts - float(lease.get("updated_at", 0)) <= LEASE_STALE_S
    ]


def save_session_leases(leases: list[dict]) -> None:
    _write_0600(
        LEASES_PATH,
        json.dumps({"version": 1, "leases": leases}, indent=2, sort_keys=True) + "\n",
    )


def upsert_session_lease(
    pid: int,
    session_id: str | None,
    label: str,
    model_family: str,
) -> None:
    with locked():
        now_ts = time.time()
        leases = [
            lease
            for lease in load_session_leases(now_ts)
            if lease.get("pid") != pid
        ]
        leases.append(
            {
                "pid": pid,
                "session_id": session_id,
                "label": label,
                "model_family": model_family,
                "updated_at": now_ts,
            }
        )
        save_session_leases(leases)


def remove_session_lease(pid: int) -> None:
    with locked():
        leases = [
            lease
            for lease in load_session_leases()
            if lease.get("pid") != pid
        ]
        save_session_leases(leases)


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
        print("no minted headless-job tokens")
        return
    now = time.time()
    for label in sorted(tokens):
        e = tokens[label]
        days = int((e.get("expires_at", 0) - now) / 86400)
        state = f"{days}d left" if days > 0 else "EXPIRED — re-mint"
        print(f"  {label:<12} minted {datetime.fromtimestamp(e.get('minted_at', 0)).date()}  {state}")


def _fable_first(rows: list[dict]) -> list[dict]:
    def key(r: dict) -> tuple:
        if fable_eligible(r["five_hour"], r["seven_day"], r["fable"]):
            return (0, *_fable_rank(r))
        return (1,)

    return sorted(rows, key=key)


def _route_preferences() -> tuple[dict, str | None, bool]:
    mode = load_mode()
    session_pin = os.environ.get("ACCOUNTS_PIN") or None
    if session_pin is not None:
        return mode, session_pin, False
    if mode.get("mode") == "set":
        return mode, mode.get("label"), True
    return mode, None, False


def select_profile(
    *,
    avoid_labels: set[str] | None = None,
    require_fable: bool = False,
    prefer_fable: bool | None = None,
    lease_pid: int | None = None,
    force_label: str | None = None,
) -> dict | None:
    avoid_labels = avoid_labels or set()
    with locked():
        blobs = load_blobs()
        blocked_labels = sync_profile_credentials(blobs, persist=True)
        mode, pin, force_pin = _route_preferences()
        if force_label is not None:
            pin = force_label
            force_pin = True
        rows = [
            row
            for row in route_rows(blobs, None, time.time())
            if row["label"] not in blocked_labels
        ]
        if prefer_fable is None:
            prefer_fable = require_fable or mode.get("mode") == "fable"
        leases = load_session_leases()
        rows = rank_profile_rows(
            rows,
            require_fable=prefer_fable,
        )
        if prefer_fable and not require_fable:
            rows = _fable_first(rows)
        excludes = excluded_labels() | avoid_labels
        accounts_map = blobs.get("accounts") or {}
        dirty = False
        while True:
            picked = pick_profile_route(
                rows,
                excludes,
                pin,
                require_fable=require_fable,
                force_pin=force_pin,
            )
            if picked is None:
                break
            candidate = accounts_map.get(picked)
            if candidate is None:
                break
            verdict = verify_entry_auth(picked, candidate, time.time())
            if verdict in ("ok", "ok_rotated"):
                if verdict == "ok_rotated":
                    dirty = True
                if dirty:
                    save_blobs(blobs)
                profile = ensure_native_profile(picked, candidate)
                if lease_pid is not None:
                    existing = next(
                        (
                            lease
                            for lease in leases
                            if lease.get("pid") == lease_pid
                        ),
                        {},
                    )
                    upsert_session_lease(
                        lease_pid,
                        existing.get("session_id"),
                        picked,
                        existing.get("model_family")
                        or ("fable" if require_fable else "general"),
                    )
                return {
                    "profile": str(profile),
                    "label": picked,
                    "email": candidate.get("email") or "",
                    "org_uuid": candidate.get("org_uuid") or "",
                }
            if verdict == "dead":
                dirty = True
            rows = [row for row in rows if row["label"] != picked]
        if dirty:
            save_blobs(blobs)
    return None


def _profile_row_eligible(
    row: dict | None,
    label: str,
    excludes: set[str],
    *,
    require_fable: bool,
) -> bool:
    return bool(
        row
        and not row["expired"]
        and not row.get("stale")
        and label not in excludes
        and (
            fable_eligible(
                row["five_hour"],
                row["seven_day"],
                row["fable"],
            )
            if require_fable
            else _rate_eligible(row["five_hour"], row["seven_day"])
        )
    )


def profile_fable_exhausted(label: str) -> bool:
    try:
        rows = route_rows(load_blobs(), label, time.time())
        row = next((candidate for candidate in rows if candidate["label"] == label), None)
        return bool(
            row
            and not row.get("stale")
            and not fable_eligible(
                row["five_hour"],
                row["seven_day"],
                row["fable"],
            )
        )
    except Exception:
        return False


def profile_general_exhausted(label: str) -> bool:
    try:
        rows = route_rows(load_blobs(), label, time.time())
        row = next((candidate for candidate in rows if candidate["label"] == label), None)
        return bool(
            row
            and not row.get("stale")
            and not _rate_eligible(row["five_hour"], row["seven_day"])
        )
    except Exception:
        return False


def handoff_target(
    current_label: str,
    *,
    require_fable: bool,
) -> str | None:
    try:
        blobs = load_blobs()
        mode, pin, force_pin = _route_preferences()
        rows = route_rows(blobs, current_label, time.time())
        rows = rank_profile_rows(
            rows,
            require_fable=require_fable,
        )
        excludes = excluded_labels()
        current = next(
            (row for row in rows if row["label"] == current_label),
            None,
        )
        if (
            require_fable
            and pin is None
            and _profile_row_eligible(
                current,
                current_label,
                excludes,
                require_fable=True,
            )
        ):
            return None
        target = pick_profile_route(
            rows,
            excludes,
            pin,
            require_fable=require_fable,
            force_pin=force_pin,
        )
        return target if target != current_label else None
    except Exception:
        return None


def cmd_pick_env(args) -> None:
    as_json = bool(getattr(args, "json", False))
    if not as_json:
        print("unset CLAUDE_CODE_OAUTH_TOKEN")
        print("unset CLAUDE_CONFIG_DIR")
        print("unset ACCOUNTS_ROUTED_LABEL")
        print("unset ACCOUNTS_ROUTED_EMAIL")
        print("unset ACCOUNTS_ROUTED_ORG_UUID")
    try:
        selected = select_profile(
            avoid_labels=set(getattr(args, "avoid", None) or []),
            require_fable=bool(getattr(args, "require_fable", False)),
            lease_pid=getattr(args, "lease_pid", None),
        )
    except Exception:
        selected = None
    if as_json:
        print(json.dumps(selected or {}, sort_keys=True))
        return
    if selected is None:
        return
    print(f"export CLAUDE_CONFIG_DIR={shlex.quote(selected['profile'])}")
    print(f"export ACCOUNTS_ROUTED_LABEL={shlex.quote(selected['label'])}")
    print(f"export ACCOUNTS_ROUTED_EMAIL={shlex.quote(selected['email'])}")
    print(f"export ACCOUNTS_ROUTED_ORG_UUID={shlex.quote(selected['org_uuid'])}")


# 5h is an imminent wall; 20% of a WEEK is still hours of runway, so the
# weekly axis gets a looser ceiling instead of blocking an otherwise-fresh
# account.
RATE_CAP_PCT = 80.0
SEVEN_DAY_CAP_PCT = 90.0
# Above this on either axis an account is genuinely walled and never a
# last-resort pick.
HARD_WALL_PCT = 97.0
FABLE_CAP_PCT = 100.0


def fable_eligible(
    _five_hour: float | None,
    _seven_day: float | None,
    fable: float | None,
) -> bool:
    return fable is not None and fable < FABLE_CAP_PCT


def _fable_rank(r: dict) -> tuple:
    return (
        float("inf") if r["fable"] is None else r["fable"],
        binding_pct(r["five_hour"], r["seven_day"]),
        float("inf") if r["five_hour"] is None else r["five_hour"],
        r["label"],
    )


def cmd_set(args) -> None:
    """Force supervised sessions onto <label>."""
    with locked():
        blobs = load_blobs()
        blocked_labels = sync_profile_credentials(blobs, persist=True)
        if args.label in blocked_labels:
            die(f"'{args.label}' has an unverified profile login")
        e = (blobs.get("accounts") or {}).get(args.label)
        if not e:
            die(f"'{args.label}' has no stored OAuth login")
        if entry_needs_login(e, time.time()):
            die(f"'{args.label}' login is unusable — /login it first")
        ensure_native_profile(args.label, e)
        save_mode("set", args.label)
    print(f"SET → {args.label}")
    print("supervised sessions use it until the routing mode changes")


def cmd_auto(_args) -> None:
    """Route supervised sessions to the freshest account."""
    save_mode("auto", None)
    blobs = load_blobs()
    blocked_labels = sync_profile_credentials(blobs, persist=False)
    rows = [
        row for row in route_rows(blobs, None, time.time()) if row["label"] not in blocked_labels
    ]
    pick = pick_profile_route(rows, excluded_labels(), None)
    print("AUTO — supervised sessions use the freshest safe account")
    print(f"  next: {pick or '(none free)'}")


def cmd_fable(_args) -> None:
    """Run supervised sessions on Fable while Fable headroom is available."""
    save_mode("fable", None)
    blobs = load_blobs()
    blocked_labels = sync_profile_credentials(blobs, persist=False)
    rows = [
        row for row in route_rows(blobs, None, time.time()) if row["label"] not in blocked_labels
    ]
    excludes = excluded_labels()
    usable = sorted(
        (
            r
            for r in rows
            if r["label"] not in excludes
            and not r["expired"]
            and fable_eligible(r["five_hour"], r["seven_day"], r["fable"])
        ),
        key=_fable_rank,
    )
    print("FABLE — supervised sessions switch to Fable when headroom is available")
    if not usable:
        print(
            "  no Fable headroom anywhere right now — "
            f"routing normally (next {pick_profile_route(rows, excludes, None) or '(none free)'})"
        )
    else:
        best = usable[0]
        b = binding_pct(best["five_hour"], best["seven_day"], best["fable"])
        print(f"  next: {best['label']} (fable {best['fable']:.0f}%, binding {b:.0f}%)")


def cmd_status(_args) -> None:
    with locked():
        blobs = load_blobs()
        capture_live_to_blobs(blobs)
        blocked_labels = sync_profile_credentials(blobs, persist=True)
    mode = load_mode()
    rows = [
        row for row in route_rows(blobs, None, time.time()) if row["label"] not in blocked_labels
    ]
    excludes = excluded_labels()
    if mode["mode"] == "set":
        tag = f"SET → {mode['label']}"
        ordered = rows
        pin = mode["label"]
        force_pin = True
    elif mode["mode"] == "fable":
        tag = "FABLE"
        ordered = _fable_first(rows)
        pin = None
        force_pin = False
    else:
        tag = "AUTO"
        ordered = rows
        pin = None
        force_pin = False
    next_general = pick_profile_route(
        ordered,
        excludes,
        pin,
        force_pin=force_pin,
    )
    next_fable = pick_profile_route(
        ordered,
        excludes,
        pin,
        require_fable=True,
        force_pin=force_pin,
    )
    print(
        f"mode: {tag}   general: {next_general or '(none free)'}"
        f"   fable: {next_fable or '(none free)'}"
    )
    for r in rows:
        pct = "—" if r["five_hour"] is None else f"{r['five_hour']:.0f}%"
        spct = "—" if r["seven_day"] is None else f"{r['seven_day']:.0f}%"
        fpct = "—" if r["fable"] is None else f"{r['fable']:.0f}%"
        if r["expired"]:
            flag = "  ⚠login"
        elif r["label"] in excludes:
            flag = "  [excluded]"
        else:
            flag = ""
        print(f"   {r['label']:<12} 5h {pct:>5}  7d {spct:>5}  fable {fpct:>5}{flag}")


def _fmt_pct(value: float | None, stale: bool) -> str:
    if value is None:
        return "—"
    return f"{value:.0f}%{'~' if stale else ''}"


def cmd_ls(_args) -> None:
    blobs = load_blobs()
    blocked_labels = sync_profile_credentials(blobs, persist=False)
    rows = route_rows(blobs, None, time.time())
    if not rows:
        print("no stored accounts — /login in Claude Code once; the next accounts command captures it")
        return
    # stable sort: pushes dead creds last, keeps route_rows' binding order otherwise
    rows = sorted(rows, key=lambda r: r["expired"])
    excludes = excluded_labels()
    print(f"{'':2}{'label':<12} {'email':<32} {'5h':>6} {'7d':>6} {'fable':>6}")
    any_expired = False
    for r in rows:
        if r["expired"]:
            suffix = "  EXPIRED — /login to refresh"
            any_expired = True
        elif r["label"] in blocked_labels:
            suffix = "  [unverified]"
        elif r["label"] in excludes:
            suffix = "  [excluded]"
        else:
            suffix = ""
        print(
            f"  {r['label']:<12} {r['email'] or '?':<32} "
            f"{_fmt_pct(r['five_hour'], r['stale']):>6} "
            f"{_fmt_pct(r['seven_day'], r['stale']):>6} "
            f"{_fmt_pct(r['fable'], r['stale']):>6}{suffix}"
        )
    print("\n~ = estimate stale (>3h since that account was polled)")
    print("pcts are USED (0% = full headroom), reset-aware; sorted best-first")
    if any_expired:
        print("EXPIRED = stored refresh token dead; switch to it needs a fresh /login")


def main(argv: list[str] | None = None) -> None:
    retire_legacy_route_agent()
    parser = argparse.ArgumentParser(prog="accounts", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_set = sub.add_parser(
        "set",
        help="force every supervised session onto <label>",
    )
    p_set.add_argument("label")
    p_set.set_defaults(fn=cmd_set)

    sub.add_parser(
        "auto",
        help="route supervised sessions to the freshest account",
    ).set_defaults(fn=cmd_auto)
    sub.add_parser(
        "fable", help="run supervised sessions on Fable when headroom is available"
    ).set_defaults(fn=cmd_fable)
    sub.add_parser(
        "status",
        help="mode + per-account 5h/7d/fable headroom + ⚠login flags",
    ).set_defaults(fn=cmd_status)

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

    p_pick_env = sub.add_parser(
        "pick-env", help="emit env exports for the best routable account"
    )
    p_pick_env.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    p_pick_env.add_argument("--avoid", action="append", default=[], help=argparse.SUPPRESS)
    p_pick_env.add_argument("--require-fable", action="store_true", help=argparse.SUPPRESS)
    p_pick_env.add_argument("--lease-pid", type=int, help=argparse.SUPPRESS)
    p_pick_env.set_defaults(fn=cmd_pick_env)

    sub.add_parser("ls", help="list stored accounts with headroom").set_defaults(fn=cmd_ls)

    args = parser.parse_args(argv)
    try:
        args.fn(args)
    except AccountsError as exc:
        die(str(exc))


if __name__ == "__main__":
    main()
