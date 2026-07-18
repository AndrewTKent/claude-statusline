"""Unit tests for bin/ccx.py — pure logic only (no keychain, no network)."""

import json
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

import ccx  # noqa: E402

NOW = datetime(2026, 7, 16, 20, 0, 0, tzinfo=timezone.utc)

PAIRS = [
    ("acme-max", "andrew.kent@acme.ai", "e1c8"),
    ("acme-work", "andrew.kent@acme.ai", "52ae"),
    ("work", "*@acme.ai", None),
    ("gmail", "user@example.com", None),
]


class TestResolveLabel:
    def test_uuid_qualified_beats_bare(self):
        assert ccx.resolve_label("andrew.kent@acme.ai", "52ae", PAIRS) == "acme-work"

    def test_uuid_qualified_exact(self):
        assert ccx.resolve_label("andrew.kent@acme.ai", "e1c8", PAIRS) == "acme-max"

    def test_bare_fallback_when_uuid_unknown(self):
        assert ccx.resolve_label("andrew.kent@acme.ai", "zzzz", PAIRS) == "work"

    def test_bare_glob(self):
        assert ccx.resolve_label("other@acme.ai", None, PAIRS) == "work"

    def test_no_match_falls_back_to_localpart(self):
        assert ccx.resolve_label("x@nowhere.io", None, PAIRS) == "x"

    def test_no_email(self):
        assert ccx.resolve_label(None, None, PAIRS) == "?"


class TestEffectivePcts:
    def test_past_reset_zeroes(self):
        row = {
            "five_hour_pct": 97.0,
            "five_hour_reset": (NOW - timedelta(minutes=1)).isoformat(),
        }
        assert ccx.effective_pcts(row, NOW)["five_hour"] == 0.0

    def test_future_reset_keeps_pct(self):
        row = {
            "five_hour_pct": 55.0,
            "five_hour_reset": (NOW + timedelta(hours=2)).isoformat(),
        }
        assert ccx.effective_pcts(row, NOW)["five_hour"] == 55.0

    def test_missing_pct_is_none(self):
        assert ccx.effective_pcts({}, NOW)["five_hour"] is None

    def test_missing_reset_keeps_pct(self):
        row = {"seven_day_pct": 31.0}
        assert ccx.effective_pcts(row, NOW)["seven_day"] == 31.0


class TestHeadroomRank:
    def test_orders_by_five_hour_then_seven_day(self):
        a = {"five_hour": 10.0, "seven_day": 90.0, "fable": None}
        b = {"five_hour": 10.0, "seven_day": 5.0, "fable": None}
        c = {"five_hour": 0.0, "seven_day": 99.0, "fable": None}
        ranked = sorted([a, b, c], key=ccx.headroom_rank)
        assert ranked == [c, b, a]

    def test_unknown_ranks_last(self):
        known = {"five_hour": 99.0, "seven_day": 99.0, "fable": 99.0}
        unknown = {"five_hour": None, "seven_day": None, "fable": None}
        assert sorted([unknown, known], key=ccx.headroom_rank) == [known, unknown]


class TestBlobAccessToken:
    def test_nested(self):
        blob = json.dumps({"claudeAiOauth": {"accessToken": "tok-1"}})
        assert ccx.blob_access_token(blob) == "tok-1"

    def test_flat(self):
        assert ccx.blob_access_token(json.dumps({"accessToken": "tok-2"})) == "tok-2"

    def test_invalid(self):
        assert ccx.blob_access_token("not json") is None
        assert ccx.blob_access_token(json.dumps(["nope"])) is None


class TestSynthesizeOauthAccount:
    def test_identifiers_land(self):
        ident = {"uuid": "u1", "email": "e@x.y", "org_uuid": "o1", "org_type": "claude_max",
                 "rate_limit_tier": "default_claude_max_20x"}
        oa = ccx.synthesize_oauth_account(ident, None)
        assert oa["accountUuid"] == "u1"
        assert oa["emailAddress"] == "e@x.y"
        assert oa["organizationUuid"] == "o1"
        assert oa["organizationRateLimitTier"] == "default_claude_max_20x"


class TestCredExpiry:
    def test_blob_refresh_expiry_ms_to_seconds(self):
        blob = json.dumps({"claudeAiOauth": {"refreshTokenExpiresAt": 1784337371728}})
        assert ccx.blob_refresh_expiry(blob) == 1784337371  # ms floored to s

    def test_blob_refresh_expiry_missing(self):
        assert ccx.blob_refresh_expiry(json.dumps({"claudeAiOauth": {}})) is None
        assert ccx.blob_refresh_expiry("not json") is None

    def test_blob_refresh_expiry_flat(self):
        assert ccx.blob_refresh_expiry(json.dumps({"refreshTokenExpiresAt": 2000000000000})) == 2000000000

    def test_cred_expired_past(self):
        assert ccx.cred_expired(1) is True  # epoch 1 is long past

    def test_cred_expired_future(self):
        assert ccx.cred_expired(4000000000) is False  # year 2096

    def test_cred_expired_none_is_not_expired(self):
        assert ccx.cred_expired(None) is False


def _route_row(label, eff5, active=False, expired=False):
    return {
        "uuid": label,
        "label": label,
        "active": active,
        "expired": expired,
        "effs": {"five_hour": eff5, "seven_day": 0.0, "fable": 0.0},
    }


class TestPickRoute:
    NOW = 1_784_000_000.0
    VAULT = {"tokens": {
        "gmail": {"token": "sk-ant-gmail-tok", "expires_at": NOW + 1000},
        "ymail": {"token": "sk-ant-ymail-tok", "expires_at": NOW - 1},  # expired token
    }}

    def test_best_with_token_wins(self):
        rows = [_route_row("alumni", 5.0), _route_row("gmail", 20.0), _route_row("ymail", 1.0)]
        # alumni has no token, ymail's token is expired -> gmail
        assert ccx.pick_route(rows, self.VAULT, set(), self.NOW, None) == ("gmail", "sk-ant-gmail-tok")

    def test_pin_overrides_headroom(self):
        rows = [_route_row("alumni", 5.0), _route_row("gmail", 90.0)]
        assert ccx.pick_route(rows, self.VAULT, set(), self.NOW, "gmail")[0] == "gmail"

    def test_excluded_skipped(self):
        rows = [_route_row("gmail", 5.0)]
        assert ccx.pick_route(rows, self.VAULT, {"gmail"}, self.NOW, None) is None

    def test_expired_cred_row_skipped(self):
        rows = [_route_row("gmail", 5.0, expired=True)]
        assert ccx.pick_route(rows, self.VAULT, set(), self.NOW, None) is None

    def test_no_tokens_none(self):
        rows = [_route_row("alumni", 5.0)]
        assert ccx.pick_route(rows, self.VAULT, set(), self.NOW, None) is None


class TestMergeTokenVaults:
    def test_union(self):
        a = {"tokens": {"gmail": {"token": "g", "minted_at": 1}}}
        b = {"tokens": {"ymail": {"token": "y", "minted_at": 2}}}
        assert set(ccx.merge_token_vaults(a, b)["tokens"]) == {"gmail", "ymail"}

    def test_newer_mint_wins(self):
        a = {"tokens": {"gmail": {"token": "old", "minted_at": 1}}}
        b = {"tokens": {"gmail": {"token": "new", "minted_at": 9}}}
        assert ccx.merge_token_vaults(a, b)["tokens"]["gmail"]["token"] == "new"
        assert ccx.merge_token_vaults(b, a)["tokens"]["gmail"]["token"] == "new"

    def test_empty_sides(self):
        assert ccx.merge_token_vaults({}, {})["tokens"] == {}


class TestTokenVault:
    def test_round_trip_and_perms(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccx, "TOKEN_VAULT_PATH", tmp_path / "sub" / "vault.json")
        vault = {"version": 1, "tokens": {"x": {"token": "sk-ant-t", "expires_at": 2}}}
        ccx.save_token_vault(vault)
        assert ccx.load_token_vault() == vault
        assert (ccx.TOKEN_VAULT_PATH.stat().st_mode & 0o777) == 0o600
        assert (ccx.TOKEN_VAULT_PATH.parent.stat().st_mode & 0o777) == 0o700

    def test_missing_file_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccx, "TOKEN_VAULT_PATH", tmp_path / "none.json")
        assert ccx.load_token_vault() == {"version": 1, "tokens": {}}


class TestRouterCore:
    NOW = 2_000_000_000.0

    def _blob(self, atok, rt_exp_ms):
        return json.dumps({"claudeAiOauth": {"accessToken": atok, "refreshToken": "r",
                                             "refreshTokenExpiresAt": rt_exp_ms}})

    def test_blob_expired_by_refresh_expiry(self):
        assert ccx.blob_expired(self._blob("a", 1_000_000_000_000), self.NOW) is True   # 2001, past
        assert ccx.blob_expired(self._blob("a", 3_000_000_000_000), self.NOW) is False  # 2065, future
        assert ccx.blob_expired('{"claudeAiOauth":{}}', self.NOW) is True    # no refresh token at all
        assert ccx.blob_expired("", self.NOW) is True                        # unparseable
        # regression: cc's rotation rewrites omit refreshTokenExpiresAt — alive, not dead
        rotation = json.dumps({"claudeAiOauth": {"accessToken": "a", "refreshToken": "r"}})
        assert ccx.blob_expired(rotation, self.NOW) is False

    def _rows(self):
        return [
            ccx_row("gmail", 12.0, expired=False, active=False),
            ccx_row("alumni", 4.0, expired=True, active=False),   # freshest but cred dead
            ccx_row("ymail", 30.0, expired=False, active=False),
            ccx_row("acme-max", 88.0, expired=False, active=True),
        ]

    def test_route_pick_skips_expired_and_active(self):
        # alumni is lowest but expired -> skip; active acme-max -> skip -> gmail
        assert ccx.route_pick(self._rows(), set()) == "gmail"

    def test_route_pick_respects_excludes(self):
        assert ccx.route_pick(self._rows(), {"gmail"}) == "ymail"

    def test_route_pick_none_when_all_blocked(self):
        rows = [ccx_row("a", 5.0, expired=True), ccx_row("b", 6.0, active=True)]
        assert ccx.route_pick(rows, set()) is None


def ccx_row(label, five_hour, expired=False, active=False):
    return {"label": label, "email": f"{label}@x", "five_hour": five_hour,
            "expired": expired, "active": active}


class TestUsageMapping:
    USAGE = {
        "five_hour": {"utilization": 42, "resets_at": "2026-07-17T22:00:00Z"},
        "seven_day": {"utilization": 18, "resets_at": "2026-07-23T10:00:00Z"},
        "limits": [
            {"kind": "weekly_scoped", "percent": 7, "resets_at": "2026-07-23T10:00:00Z",
             "scope": {"model": {"display_name": "Fable"}}},
            {"kind": "five_hour", "percent": 99},
        ],
    }

    def test_maps_all_fields(self):
        row = ccx.usage_to_reset_row("e@x.io", "org1", self.USAGE, 1784300000)
        assert row["email"] == "e@x.io" and row["org_uuid"] == "org1"
        assert row["five_hour_pct"] == 42
        assert row["five_hour_reset"] == "2026-07-17T22:00:00Z"
        assert row["seven_day_pct"] == 18
        assert row["fable_pct"] == 7
        assert row["fable_label"] == "Fable"
        assert row["last_seen"] == 1784300000

    def test_missing_weekly_is_none(self):
        row = ccx.usage_to_reset_row("e@x.io", "org1", {"five_hour": {}, "seven_day": {}}, 1)
        assert row["fable_pct"] is None and row["fable_label"] is None
        assert row["five_hour_pct"] == 0  # None utilization -> 0

    def test_access_expiry_ms_to_s(self):
        blob = json.dumps({"claudeAiOauth": {"expiresAt": 1784337371728}})
        assert ccx.blob_access_expiry(blob) == 1784337371

    def test_access_expiry_missing(self):
        assert ccx.blob_access_expiry(json.dumps({"claudeAiOauth": {}})) is None


class TestVaultKey:
    def test_composite_of_account_and_org(self):
        ident = {"uuid": "acct1", "org_uuid": "orgA"}
        assert ccx.vault_key(ident) == "acct1|orgA"

    def test_shared_account_two_orgs_distinct_keys(self):
        # acme-max and acme-work: same account, different org -> must not collide
        maxk = ccx.vault_key({"uuid": "d9eb", "org_uuid": "e1c8"})
        workk = ccx.vault_key({"uuid": "d9eb", "org_uuid": "52ae"})
        assert maxk != workk

    def test_missing_org_is_none(self):
        assert ccx.vault_key({"uuid": "acct1", "org_uuid": None}) is None
        assert ccx.vault_key({"uuid": None, "org_uuid": "orgA"}) is None

    def test_short_key_shows_both_parts(self):
        assert ccx.short_key("d9eb92c0-xxxx|52ae57ff-yyyy") == "d9eb92c0|52ae57ff"


class TestResolveTargetSharedEmail:
    META = {
        "accounts": {
            "d9eb|e1c8": {"email": "andrew.kent@acme.ai", "org_uuid": "e1c8"},
            "d9eb|52ae": {"email": "andrew.kent@acme.ai", "org_uuid": "52ae"},
        }
    }
    PAIRS = [
        ("acme-max", "andrew.kent@acme.ai", "e1c8"),
        ("acme-work", "andrew.kent@acme.ai", "52ae"),
    ]

    def test_label_disambiguates_shared_email(self):
        k, _ = ccx.resolve_target(self.META, "acme-work", self.PAIRS)
        assert k == "d9eb|52ae"

    def test_shared_email_is_ambiguous(self):
        with pytest.raises(SystemExit):
            ccx.resolve_target(self.META, "andrew.kent@acme.ai", self.PAIRS)


class TestResolveTarget:
    META = {
        "accounts": {
            "uuid-a": {"email": "andrew.kent@acme.ai", "org_uuid": "e1c8"},
            "uuid-b": {"email": "user@example.com", "org_uuid": "g1"},
        }
    }

    def test_by_label(self):
        uuid, _ = ccx.resolve_target(self.META, "acme-max", PAIRS)
        assert uuid == "uuid-a"

    def test_by_email(self):
        uuid, _ = ccx.resolve_target(self.META, "user@example.com", PAIRS)
        assert uuid == "uuid-b"

    def test_by_uuid(self):
        uuid, _ = ccx.resolve_target(self.META, "uuid-b", PAIRS)
        assert uuid == "uuid-b"

    def test_missing_dies(self):
        with pytest.raises(SystemExit):
            ccx.resolve_target(self.META, "nope", PAIRS)


def _live_blob(atok, future_ms=3_000_000_000_000):
    # access + refresh both far-future: poll won't skip it, cred isn't expired
    return json.dumps({"claudeAiOauth": {"accessToken": atok, "refreshToken": "r",
                                         "expiresAt": future_ms, "refreshTokenExpiresAt": future_ms}})


class TestLockedReentrant:
    """The BLOCK deadlock: route_once holds locked(), poll's merge_reset_rows
    re-takes it on a second fd — non-reentrant flock self-blocks in-process."""

    def test_nested_locked_does_not_deadlock(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccx, "LOCK_PATH", tmp_path / "ccx.lock")
        monkeypatch.setattr(ccx, "_lock_depth", 0)
        done = threading.Event()

        def run():
            with ccx.locked():
                with ccx.locked():  # nested acquire must return, not block
                    pass
            done.set()

        threading.Thread(target=run, daemon=True).start()
        assert done.wait(timeout=5), "nested locked() deadlocked"


class TestRouteOnceNoDeadlock:
    def _paths(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccx, "LOCK_PATH", tmp_path / "ccx.lock")
        monkeypatch.setattr(ccx, "BLOBS_PATH", tmp_path / "blobs.json")
        monkeypatch.setattr(ccx, "RESETS_PATH", tmp_path / "resets.json")
        monkeypatch.setattr(ccx, "MODE_PATH", tmp_path / "mode.json")
        monkeypatch.setattr(ccx, "CRED_FILE", tmp_path / ".credentials.json")
        monkeypatch.setattr(ccx, "_lock_depth", 0)
        monkeypatch.setattr(ccx, "_last_switch_ts", None)
        # hermetic: never read/delete the REAL keychain from tests
        monkeypatch.setattr(ccx, "kc_read", lambda service, account=None: None)
        monkeypatch.setattr(ccx, "kc_slot_status", lambda service: "absent")
        monkeypatch.setattr(ccx, "kc_delete", lambda service, account=None: None)

    def test_route_once_completes_when_poll_yields_rows(self, tmp_path, monkeypatch):
        # Real poll -> merge_reset_rows re-enters the lock. Pre-fix: hangs here.
        self._paths(tmp_path, monkeypatch)
        blobs = {"accounts": {"gmail": {"email": "g@x", "org_uuid": "o1",
                                        "blob": _live_blob("tok-g")}}}
        (tmp_path / "blobs.json").write_text(json.dumps(blobs))
        monkeypatch.setattr(ccx, "fetch_usage", lambda tok: {
            "five_hour": {"utilization": 10, "resets_at": "2026-07-17T22:00:00Z"},
            "seven_day": {"utilization": 5, "resets_at": "2026-07-23T10:00:00Z"}, "limits": []})
        monkeypatch.setattr(ccx, "fetch_profile", lambda tok: None)
        done, errbox = threading.Event(), []

        def run():
            try:
                ccx.route_once(80.0)
            except Exception as exc:  # noqa: BLE001
                errbox.append(exc)
            finally:
                done.set()

        threading.Thread(target=run, daemon=True).start()
        assert done.wait(timeout=5), "route_once deadlocked (nested locked() self-blocks)"
        assert not errbox, f"route_once raised: {errbox}"
        assert (tmp_path / "resets.json").exists()  # proves it reached merge under the lock


class TestRouteDecision:
    """AUTO/SET switch logic in route_once, with poll/attribution stubbed so only
    the decision path runs. apply_account (the real file write) is NOT stubbed."""

    def _wire(self, tmp_path, monkeypatch, rows, active, mode, blobs):
        monkeypatch.setattr(ccx, "LOCK_PATH", tmp_path / "ccx.lock")
        monkeypatch.setattr(ccx, "CRED_FILE", tmp_path / ".credentials.json")
        monkeypatch.setattr(ccx, "_lock_depth", 0)
        monkeypatch.setattr(ccx, "_last_switch_ts", None)  # never switched: dwell open
        monkeypatch.setattr(ccx, "load_blobs", lambda: blobs)
        monkeypatch.setattr(ccx, "poll_blobs_usage", lambda b: 0)
        monkeypatch.setattr(ccx, "capture_live_to_blobs", lambda b: active)
        monkeypatch.setattr(ccx, "route_rows", lambda b, a, t: rows)
        monkeypatch.setattr(ccx, "load_mode", lambda: mode)
        monkeypatch.setattr(ccx, "excluded_labels", lambda: set())
        # hermetic: never read/delete the REAL keychain from tests
        self.kc_deletes = []
        monkeypatch.setattr(ccx, "kc_read", lambda service, account=None: None)
        monkeypatch.setattr(ccx, "kc_slot_status", lambda service: "absent")
        monkeypatch.setattr(ccx, "kc_delete",
                            lambda service, account=None: self.kc_deletes.append(service))

    def test_auto_holds_when_pick_not_meaningfully_fresher(self, tmp_path, monkeypatch):
        # A active @85 (>=80), B @82 — 3 pts fresher. Pre-fix switches A->B then
        # B->A every pass (ping-pong). Fixed: holds (needs >= hysteresis fresher).
        rows = [ccx_row("A", 85.0, active=True), ccx_row("B", 82.0)]
        self._wire(tmp_path, monkeypatch, rows, "A", {"mode": "auto"},
                   {"accounts": {"B": {"blob": "BLOB-B"}}})
        assert ccx.route_once(80.0) is None
        assert not ccx.CRED_FILE.exists()  # no switch written

    def test_auto_switches_when_pick_much_fresher(self, tmp_path, monkeypatch):
        rows = [ccx_row("A", 85.0, active=True), ccx_row("B", 70.0)]
        self._wire(tmp_path, monkeypatch, rows, "A", {"mode": "auto"},
                   {"accounts": {"B": {"blob": "BLOB-B"}}})
        line = ccx.route_once(80.0)
        assert line and "ROUTED A → B" in line
        assert ccx.CRED_FILE.read_text() == "BLOB-B"

    def test_auto_no_switch_below_threshold(self, tmp_path, monkeypatch):
        rows = [ccx_row("A", 50.0, active=True), ccx_row("B", 5.0)]
        self._wire(tmp_path, monkeypatch, rows, "A", {"mode": "auto"},
                   {"accounts": {"B": {"blob": "BLOB-B"}}})
        assert ccx.route_once(80.0) is None
        assert not ccx.CRED_FILE.exists()

    def test_auto_pick_without_board_row_does_not_crash(self, tmp_path, monkeypatch):
        # B has no five_hour (poll skipped it) -> pick_pct None must not TypeError
        rows = [ccx_row("A", 90.0, active=True), ccx_row("B", None)]
        self._wire(tmp_path, monkeypatch, rows, "A", {"mode": "auto"},
                   {"accounts": {"B": {"blob": "BLOB-B"}}})
        assert ccx.route_once(80.0) is None
        assert not ccx.CRED_FILE.exists()

    def test_set_holds_when_live_present_but_unattributed(self, tmp_path, monkeypatch):
        # CRED_FILE present, capture returns None (profile down) -> a cc-rotated
        # token may be live; must NOT overwrite it with the stored blob.
        rows = [ccx_row("B", 5.0)]
        self._wire(tmp_path, monkeypatch, rows, None, {"mode": "set", "label": "B"},
                   {"accounts": {"B": {"blob": "BLOB-B"}}})
        ccx.CRED_FILE.write_text("LIVE-ROTATED")
        assert ccx.route_once(80.0) is None
        assert ccx.CRED_FILE.read_text() == "LIVE-ROTATED"  # untouched

    def test_set_seeds_when_no_cred_file(self, tmp_path, monkeypatch):
        rows = [ccx_row("B", 5.0)]
        self._wire(tmp_path, monkeypatch, rows, None, {"mode": "set", "label": "B"},
                   {"accounts": {"B": {"blob": "BLOB-B"}}})
        assert "SET" in (ccx.route_once(80.0) or "")
        assert ccx.CRED_FILE.read_text() == "BLOB-B"

    def test_auto_capped_escapes_to_a_usable_account(self, tmp_path, monkeypatch):
        # active @100 (>= cap 95), best alt @91 (below cap = real headroom). Below
        # cap the 9-pt gap would hold on hysteresis; capped, we escape to it.
        rows = [ccx_row("A", 100.0, active=True), ccx_row("B", 91.0)]
        self._wire(tmp_path, monkeypatch, rows, "A", {"mode": "auto"},
                   {"accounts": {"B": {"blob": "BLOB-B"}}})
        line = ccx.route_once(80.0)
        assert line and "ROUTED A → B" in line
        assert ccx.CRED_FILE.read_text() == "BLOB-B"

    def test_auto_capped_holds_when_every_alt_is_also_capped(self, tmp_path, monkeypatch):
        # active capped @96, only alt @98 also >= cap -> hold. Two near-capped
        # accounts never swap: this is what stops the feedback ping-pong.
        rows = [ccx_row("A", 96.0, active=True), ccx_row("B", 98.0)]
        self._wire(tmp_path, monkeypatch, rows, "A", {"mode": "auto"},
                   {"accounts": {"B": {"blob": "BLOB-B"}}})
        assert ccx.route_once(80.0) is None
        assert not ccx.CRED_FILE.exists()

    def test_auto_dwell_rate_limits_switching(self, tmp_path, monkeypatch):
        # Even a far-fresher pick is held if we switched within the dwell — each
        # switch cold-starts a prompt cache, so the daemon switches at most once
        # per ROUTE_MIN_DWELL_S.
        rows = [ccx_row("A", 100.0, active=True), ccx_row("B", 20.0)]
        self._wire(tmp_path, monkeypatch, rows, "A", {"mode": "auto"},
                   {"accounts": {"B": {"blob": "BLOB-B"}}})
        monkeypatch.setattr(ccx, "_last_switch_ts", time.monotonic())  # just switched
        assert ccx.route_once(80.0) is None
        assert not ccx.CRED_FILE.exists()
        monkeypatch.setattr(ccx, "_last_switch_ts", time.monotonic() - ccx.ROUTE_MIN_DWELL_S - 1)
        line = ccx.route_once(80.0)
        assert line and "ROUTED A → B" in line
        assert ccx._last_switch_ts is not None and ccx._last_switch_ts > 1  # stamp advanced


class TestCmdSet:
    def _paths(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccx, "LOCK_PATH", tmp_path / "ccx.lock")
        monkeypatch.setattr(ccx, "CRED_FILE", tmp_path / ".credentials.json")
        monkeypatch.setattr(ccx, "MODE_PATH", tmp_path / "mode.json")
        monkeypatch.setattr(ccx, "BLOBS_PATH", tmp_path / "blobs.json")
        monkeypatch.setattr(ccx, "_lock_depth", 0)
        # never reach the network / real store / real keychain from tests
        monkeypatch.setattr(ccx, "fetch_profile", lambda tok: None)
        monkeypatch.setattr(ccx, "kc_read", lambda service, account=None: None)
        monkeypatch.setattr(ccx, "kc_slot_status", lambda service: "absent")
        monkeypatch.setattr(ccx, "kc_delete", lambda service, account=None: None)

    def test_refuses_expired_blob(self, tmp_path, monkeypatch):
        self._paths(tmp_path, monkeypatch)
        expired = _live_blob("a", future_ms=1_000_000_000_000)  # 2001, past
        monkeypatch.setattr(ccx, "load_blobs", lambda: {"accounts": {"B": {"blob": expired}}})
        with pytest.raises(SystemExit):
            ccx.cmd_set(types.SimpleNamespace(label="B"))
        assert not ccx.CRED_FILE.exists()  # dead cred NOT written

    def test_applies_live_blob(self, tmp_path, monkeypatch):
        self._paths(tmp_path, monkeypatch)
        live = _live_blob("a")
        monkeypatch.setattr(ccx, "load_blobs", lambda: {"accounts": {"B": {"blob": live}}})
        ccx.cmd_set(types.SimpleNamespace(label="B"))
        assert ccx.CRED_FILE.read_text() == live

    def test_captures_live_before_applying(self, tmp_path, monkeypatch):
        # regression: cmd_set must fold an outgoing rotation into blobs (capture)
        # BEFORE overwriting the live file (apply), so a switch never drops the
        # departing account's freshly-rotated token.
        self._paths(tmp_path, monkeypatch)
        live = _live_blob("a")
        monkeypatch.setattr(ccx, "load_blobs", lambda: {"accounts": {"B": {"blob": live}}})
        monkeypatch.setattr(ccx, "save_mode", lambda m, label: None)
        calls = []
        monkeypatch.setattr(ccx, "capture_live_to_blobs", lambda b: calls.append("capture"))
        real_apply = ccx.apply_account
        monkeypatch.setattr(ccx, "apply_account",
                            lambda label, b: (calls.append("apply"), real_apply(label, b))[1])
        ccx.cmd_set(types.SimpleNamespace(label="B"))
        assert calls == ["capture", "apply"]
        assert ccx.CRED_FILE.read_text() == live

    def test_refuses_when_live_present_but_unattributable(self, tmp_path, monkeypatch):
        # cc rotated the live token (not yet in the store) and profile fetch fails
        # (stubbed None in _paths) -> capture can't attribute it. Overwriting would
        # lose that account's login, so refuse and leave the live cred untouched.
        self._paths(tmp_path, monkeypatch)
        monkeypatch.setattr(ccx, "load_blobs",
                            lambda: {"accounts": {"B": {"blob": _live_blob("b")}}})
        ccx.CRED_FILE.write_text(_live_blob("ROTATED-UNKNOWN"))
        with pytest.raises(SystemExit):
            ccx.cmd_set(types.SimpleNamespace(label="B"))
        assert "ROTATED-UNKNOWN" in ccx.CRED_FILE.read_text()  # not clobbered


class TestLoadBlobs:
    def test_corrupt_store_is_preserved_not_destroyed(self, tmp_path, monkeypatch):
        blobs_path = tmp_path / "blobs.json"
        blobs_path.write_text("NOT JSON {{{")
        monkeypatch.setattr(ccx, "BLOBS_PATH", blobs_path)
        monkeypatch.setattr(ccx, "MIRROR_LOG", tmp_path / "log")  # keep log_line off real paths
        assert ccx.load_blobs() == {"version": 1, "accounts": {}}
        backups = list(tmp_path.glob("blobs.json.corrupt.*"))
        assert len(backups) == 1 and backups[0].read_text() == "NOT JSON {{{"
        # still-corrupt on the next read: no second identical backup piles up
        assert ccx.load_blobs() == {"version": 1, "accounts": {}}
        assert len(list(tmp_path.glob("blobs.json.corrupt.*"))) == 1

    def test_missing_store_returns_empty_without_backup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccx, "BLOBS_PATH", tmp_path / "nope.json")
        assert ccx.load_blobs() == {"version": 1, "accounts": {}}
        assert not list(tmp_path.glob("*.corrupt.*"))

    def test_unreadable_store_refuses_instead_of_empty(self, tmp_path, monkeypatch):
        # present-but-unreadable (perms) must NOT read as empty — the next capture
        # would overwrite the store with a single account.
        blobs_path = tmp_path / "blobs.json"
        blobs_path.write_text('{"version": 1, "accounts": {"a": {}, "b": {}}}')
        blobs_path.chmod(0o000)
        monkeypatch.setattr(ccx, "BLOBS_PATH", blobs_path)
        try:
            with pytest.raises(ccx.CcxError):
                ccx.load_blobs()
        finally:
            blobs_path.chmod(0o600)

    def test_non_dict_store_is_preserved_and_empty(self, tmp_path, monkeypatch):
        blobs_path = tmp_path / "blobs.json"
        blobs_path.write_text("[1, 2, 3]")
        monkeypatch.setattr(ccx, "BLOBS_PATH", blobs_path)
        monkeypatch.setattr(ccx, "MIRROR_LOG", tmp_path / "log")
        assert ccx.load_blobs() == {"version": 1, "accounts": {}}
        assert len(list(tmp_path.glob("blobs.json.corrupt.*"))) == 1


class TestLiveCredAndSwitch:
    """The macOS switch primitive: cc reads keychain-first ('keychain-with-
    plaintext-fallback', ~30s TTL), so a switch = write file + DELETE slot."""

    def test_live_cred_prefers_keychain_over_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccx, "CRED_FILE", tmp_path / ".credentials.json")
        ccx.CRED_FILE.write_text("FILE-BLOB")
        monkeypatch.setattr(ccx, "kc_read", lambda service, account=None: "KC-BLOB")
        assert ccx.live_cred() == ("KC-BLOB", "keychain")

    def test_live_cred_falls_back_to_file_then_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccx, "CRED_FILE", tmp_path / ".credentials.json")
        monkeypatch.setattr(ccx, "kc_read", lambda service, account=None: None)
        assert ccx.live_cred() == (None, None)
        ccx.CRED_FILE.write_text("FILE-BLOB")
        assert ccx.live_cred() == ("FILE-BLOB", "file")

    def test_capture_reads_the_keychain_source(self, tmp_path, monkeypatch):
        # cc refreshed into the keychain (file deleted): capture must still
        # attribute the live account without any profile fetch when unchanged.
        monkeypatch.setattr(ccx, "CRED_FILE", tmp_path / ".credentials.json")
        kc_blob = _live_blob("tok-kc")
        monkeypatch.setattr(ccx, "kc_read", lambda service, account=None: kc_blob)
        monkeypatch.setattr(ccx, "fetch_profile",
                            lambda tok: (_ for _ in ()).throw(AssertionError("no network")))
        blobs = {"accounts": {"gmail": {"blob": kc_blob}}}
        assert ccx.capture_live_to_blobs(blobs) == "gmail"

    def test_apply_account_deletes_keychain_slot(self, tmp_path, monkeypatch):
        # While the slot exists the file is invisible to cc — apply must write
        # the file AND delete the slot so the ~30s re-read lands on the file.
        monkeypatch.setattr(ccx, "CRED_FILE", tmp_path / ".credentials.json")
        monkeypatch.setattr(ccx, "MIRROR_LOG", tmp_path / "log")
        kc_state = {"present": True}
        deletes = []
        monkeypatch.setattr(ccx, "kc_slot_status",
                            lambda service: "present" if kc_state["present"] else "absent")

        def fake_delete(service, account=None):
            deletes.append(service)
            kc_state["present"] = False
        monkeypatch.setattr(ccx, "kc_delete", fake_delete)
        assert ccx.apply_account("B", {"accounts": {"B": {"blob": "BLOB-B"}}}) is True
        assert ccx.CRED_FILE.read_text() == "BLOB-B"
        assert deletes == [ccx.LIVE_SERVICE]
        assert not (tmp_path / "log").exists()  # clean switch: no warn logged

    def test_apply_account_skips_delete_when_no_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccx, "CRED_FILE", tmp_path / ".credentials.json")
        deletes = []
        monkeypatch.setattr(ccx, "kc_slot_status", lambda service: "absent")
        monkeypatch.setattr(ccx, "kc_delete", lambda service, account=None: deletes.append(service))
        assert ccx.apply_account("B", {"accounts": {"B": {"blob": "BLOB-B"}}}) is True
        assert deletes == []  # nothing to delete; no gratuitous keychain ops

    def test_apply_account_warns_when_slot_state_unknown(self, tmp_path, monkeypatch):
        # Locked keychain / wedged securityd: the probe can't tell — apply must
        # NOT claim a clean switch (cc may keep reading the intact slot) and
        # must not fire a blind delete.
        monkeypatch.setattr(ccx, "CRED_FILE", tmp_path / ".credentials.json")
        monkeypatch.setattr(ccx, "MIRROR_LOG", tmp_path / "log")
        deletes = []
        monkeypatch.setattr(ccx, "kc_slot_status", lambda service: "unknown")
        monkeypatch.setattr(ccx, "kc_delete", lambda service, account=None: deletes.append(service))
        assert ccx.apply_account("B", {"accounts": {"B": {"blob": "BLOB-B"}}}) is True
        assert deletes == []
        assert "unknown" in (tmp_path / "log").read_text()
