#!/usr/bin/env python3
"""ccx — lossless Claude Code account vault, switcher, and headroom router.

Claude Code keeps ONE credential slot (Keychain item "Claude Code-credentials"),
so /login and account switches clobber whichever account was active. ccx wraps
that slot with a per-account vault (one Keychain item per account): switching
PARKS the outgoing account instead of destroying it, and the router picks the
next account by remaining rate-limit headroom (reset-aware, from
~/.claude/account-resets.json).

Claude Code's own auth flow is untouched — it keeps reading/refreshing the live
slot as always; ccx only snapshots the slot into the vault and restores vaulted
creds back into it. Cred blobs move keychain->keychain in-process via the
Security framework — never argv, never a temp file. Only identity metadata
lands in ~/.claude/ccx-vault.json.

Commands:
  enroll          snapshot the active account into the vault (one-shot)
  mirror          loop: re-snapshot the slot whenever it changes (run as daemon)
  ls              vaulted accounts + reset-aware headroom estimates
  best            print the vaulted account with the most headroom
  switch TARGET   park current, restore TARGET (label/email/uuid) into the slot
  selftest        keychain round-trip on a scratch item (never touches live/vault)
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import fnmatch
import hashlib
import json
import os
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
BACKUP_DIR = HOME / ".claude" / "backups"
MIRROR_LOG = HOME / ".claude" / "ccx-mirror.log"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
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


def kc_write(service: str, account: str, secret: str) -> None:
    """Write via the Security framework in-process: no argv (ps-safe) and no
    length limit (real blobs carry MCP OAuth entries and overflow `security -i`)."""
    sec = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    svc, acct, pw = service.encode(), account.encode(), secret.encode()

    add = sec.SecKeychainAddGenericPassword
    add.restype = ctypes.c_int32
    add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_void_p,
    ]
    status = add(None, len(svc), svc, len(acct), acct, len(pw), pw, None)
    if status == 0:
        return
    if status != -25299:  # errSecDuplicateItem -> update existing item below
        raise CcxError(f"keychain add failed (OSStatus {status})")

    find = sec.SecKeychainFindGenericPassword
    find.restype = ctypes.c_int32
    find.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    item = ctypes.c_void_p()
    status = find(None, len(svc), svc, len(acct), acct, None, None, ctypes.byref(item))
    if status != 0:
        raise CcxError(f"keychain find-for-update failed (OSStatus {status})")
    try:
        mod = sec.SecKeychainItemModifyAttributesAndData
        mod.restype = ctypes.c_int32
        mod.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p]
        status = mod(item, None, len(pw), pw)
        if status != 0:
            raise CcxError(f"keychain update failed (OSStatus {status})")
    finally:
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cf.CFRelease(item)


def kc_delete(service: str, account: str) -> None:
    subprocess.run(
        ["security", "delete-generic-password", "-s", service, "-a", account],
        capture_output=True,
        text=True,
    )


def kc_live_account_attr() -> str:
    r = subprocess.run(
        ["security", "find-generic-password", "-s", LIVE_SERVICE],
        capture_output=True,
        text=True,
    )
    for raw in (r.stdout + r.stderr).splitlines():
        line = raw.strip()
        if line.startswith('"acct"'):
            val = line.split("=", 1)[1].strip()
            if val.startswith('"') and val.endswith('"'):
                return val[1:-1]
    return os.environ.get("USER", "")


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


def write_claude_json_oauth(oauth_account: dict) -> None:
    data = json.loads(CLAUDE_JSON.read_text())  # hard-fail on corrupt: never blind-write
    mode = CLAUDE_JSON.stat().st_mode & 0o777
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(CLAUDE_JSON, BACKUP_DIR / f".claude.json.backup.ccx.{int(time.time() * 1000)}")
    data["oauthAccount"] = oauth_account
    tmp = CLAUDE_JSON.with_name(".claude.json.ccx-tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    os.chmod(tmp, mode)
    os.replace(tmp, CLAUDE_JSON)


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
    uuid = ident.get("uuid")
    if not uuid:
        raise CcxError("profile response missing account uuid; not vaulting")

    kc_write(VAULT_SERVICE, uuid, blob)
    if kc_read(VAULT_SERVICE, uuid) != blob:
        raise CcxError("vault write verification failed (read-back mismatch)")

    entry = meta["accounts"].get(uuid, {})
    oa = read_claude_json().get("oauthAccount") or {}
    if oa.get("accountUuid") == uuid:
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
        }
    )
    meta["accounts"][uuid] = entry
    meta["last_live_sha"] = blob_sha
    save_meta(meta)
    return uuid, entry


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
        die(f"'{needle}' is ambiguous across {len(matches)} vaulted accounts; use the uuid")
    return matches[0]


def vault_labels(meta: dict, pairs) -> list[str]:
    return [
        resolve_label(e.get("email"), e.get("org_uuid"), pairs)
        for e in meta["accounts"].values()
    ]


def claude_running() -> bool:
    r = subprocess.run(["pgrep", "-x", "claude"], capture_output=True, text=True)
    return r.returncode == 0


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
    uuid, entry = result
    label = resolve_label(entry.get("email"), entry.get("org_uuid"), pairs)
    print(f"vaulted {label} ({entry.get('email')}) [{uuid[:8]}]")


def cmd_mirror(args) -> None:
    log_line(f"ccx mirror started (interval {args.interval}s)")
    while True:
        try:
            with locked(blocking=False):
                meta = load_meta()
                result = snapshot_live(meta)
            if result is not None:
                uuid, entry = result
                pairs = load_label_pairs()
                label = resolve_label(entry.get("email"), entry.get("org_uuid"), pairs)
                log_line(f"vaulted {label} ({entry.get('email')}) [{uuid[:8]}]")
        except BlockingIOError:
            pass  # a switch holds the lock; catch the change on the next tick
        except CcxError as exc:
            log_line(f"warn: {exc}")
        except Exception as exc:  # daemon must survive anything
            log_line(f"error: {type(exc).__name__}: {exc}")
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
            }
        )
    rows.sort(key=lambda r: r["rank"])
    return rows


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
    for r in rows:
        mark = "* " if r["active"] else "  "
        suffix = "  [excluded]" if r["label"] in excludes else ""
        print(
            f"{mark}{r['label']:<12} {r['email'] or '?':<32} "
            f"{_fmt_pct(r['effs']['five_hour'], r['stale']):>6} "
            f"{_fmt_pct(r['effs']['seven_day'], r['stale']):>6} "
            f"{_fmt_pct(r['effs']['fable'], r['stale']):>6}{suffix}"
        )
    print("\n* = matches live slot   ~ = estimate stale (>3h since that account was live)")
    print("pcts are USED (0% = full headroom), reset-aware; sorted best-first")


def cmd_best(_args) -> None:
    meta = load_meta()
    rows = [r for r in _account_rows(meta) if r["label"] not in excluded_labels()]
    if not rows:
        die("vault is empty (or everything is excluded)")
    top = rows[0]
    effs = top["effs"]
    print(
        f"{top['label']} ({top['email']}) — 5h {_fmt_pct(effs['five_hour'], top['stale'])}, "
        f"7d {_fmt_pct(effs['seven_day'], top['stale'])}, fable {_fmt_pct(effs['fable'], top['stale'])}"
    )


def cmd_switch(args) -> None:
    if args.best == bool(args.target):
        die("switch needs exactly one of TARGET or --best")
    pairs = load_label_pairs()
    with locked():
        meta = load_meta()
        parked = snapshot_live(meta, force=True)
        cur_uuid = parked[0] if parked else None

        if args.best:
            candidates = [
                r
                for r in _account_rows(meta)
                if r["label"] not in excluded_labels() and r["uuid"] != cur_uuid
            ]
            if not candidates:
                die("no other vaulted account to switch to")
            target_uuid = candidates[0]["uuid"]
            target_entry = meta["accounts"][target_uuid]
        else:
            target_uuid, target_entry = resolve_target(meta, args.target, pairs)
        target_label = resolve_label(
            target_entry.get("email"), target_entry.get("org_uuid"), pairs
        )
        if target_uuid == cur_uuid:
            print(f"already on {target_label}")
            return
        if claude_running() and not args.force:
            die(
                "claude session(s) are running. A switch only affects NEW sessions, and a "
                "running session re-writes the slot on its next token refresh (the mirror "
                "keeps every account vaulted, so nothing is lost — but the slot can flip "
                "back). Re-run with --force to switch anyway."
            )

        target_blob = kc_read(VAULT_SERVICE, target_uuid)
        if target_blob is None:
            die(f"vault entry for {target_label} is missing its credential blob")

        old_blob = kc_read(LIVE_SERVICE)
        acct_attr = kc_live_account_attr()
        kc_write(LIVE_SERVICE, acct_attr, target_blob)
        if kc_read(LIVE_SERVICE) != target_blob:
            if old_blob is not None:
                kc_write(LIVE_SERVICE, acct_attr, old_blob)
            die("live slot write verification failed; rolled back")

        try:
            write_claude_json_oauth(target_entry["oauth_account"])
        except Exception as exc:
            if old_blob is not None:
                kc_write(LIVE_SERVICE, acct_attr, old_blob)
            die(f"~/.claude.json update failed ({exc}); live slot rolled back")

        meta["last_live_sha"] = sha256(target_blob)
        save_meta(meta)

    cur_entry = meta["accounts"].get(cur_uuid, {}) if cur_uuid else {}
    cur_label = resolve_label(cur_entry.get("email"), cur_entry.get("org_uuid"), pairs)
    print(
        f"switched {cur_label} → {target_label} "
        f"({cur_label} parked in vault; new `claude` sessions use {target_label})"
    )


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
        kc_write(SELFTEST_SERVICE, "selftest", fake)
        first = kc_read(SELFTEST_SERVICE, "selftest")
        kc_write(SELFTEST_SERVICE, "selftest", fake + "2")  # exercises -U update
        second = kc_read(SELFTEST_SERVICE, "selftest")
    finally:
        kc_delete(SELFTEST_SERVICE, "selftest")
    if first != fake or second != fake + "2":
        die("selftest FAILED: keychain round-trip mismatch")
    print("selftest OK: keychain write/read/update/delete verified (scratch item only)")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ccx", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("enroll", help="vault the active account now").set_defaults(fn=cmd_enroll)

    p_mirror = sub.add_parser("mirror", help="watch the live slot and vault every change")
    p_mirror.add_argument("--interval", type=float, default=20.0)
    p_mirror.add_argument("--once", action="store_true", help="single pass (for cron/tests)")
    p_mirror.set_defaults(fn=cmd_mirror)

    sub.add_parser("ls", help="list vaulted accounts with headroom").set_defaults(fn=cmd_ls)
    sub.add_parser("best", help="print the highest-headroom account").set_defaults(fn=cmd_best)

    p_switch = sub.add_parser("switch", help="park current account, activate TARGET")
    p_switch.add_argument(
        "target", nargs="?", help="label (from ACCOUNT_LABELS), email, or account uuid"
    )
    p_switch.add_argument("--best", action="store_true", help="pick highest-headroom account")
    p_switch.add_argument("--force", action="store_true", help="switch even with sessions running")
    p_switch.set_defaults(fn=cmd_switch)

    sub.add_parser("selftest", help="scratch keychain round-trip").set_defaults(fn=cmd_selftest)

    args = parser.parse_args(argv)
    try:
        args.fn(args)
    except CcxError as exc:
        die(str(exc))


if __name__ == "__main__":
    main()
