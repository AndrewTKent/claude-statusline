"""Unit tests for bin/accounts.py — pure logic only (no keychain, no network)."""

import json
import sys
import threading
import time
import types
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

import accounts  # noqa: E402

NOW = datetime(2026, 7, 16, 20, 0, 0, tzinfo=timezone.utc)

PAIRS = [
    ("coram-max", "andrew.kent@coram.ai", "e1c8"),
    ("coram-work", "andrew.kent@coram.ai", "52ae"),
    ("work", "*@coram.ai", None),
    ("gmail", "andrewkent10@gmail.com", None),
]


class TestResolveLabel:
    def test_uuid_qualified_beats_bare(self):
        assert accounts.resolve_label("andrew.kent@coram.ai", "52ae", PAIRS) == "coram-work"

    def test_uuid_qualified_exact(self):
        assert accounts.resolve_label("andrew.kent@coram.ai", "e1c8", PAIRS) == "coram-max"

    def test_bare_fallback_when_uuid_unknown(self):
        assert accounts.resolve_label("andrew.kent@coram.ai", "zzzz", PAIRS) == "work"

    def test_bare_glob(self):
        assert accounts.resolve_label("other@coram.ai", None, PAIRS) == "work"

    def test_no_match_falls_back_to_localpart(self):
        assert accounts.resolve_label("x@nowhere.io", None, PAIRS) == "x"

    def test_no_email(self):
        assert accounts.resolve_label(None, None, PAIRS) == "?"


class TestEffectivePcts:
    def test_past_reset_zeroes(self):
        # A past reset zeroes only when confirmed by a poll after it
        # (last_seen newer than the reset).
        row = {
            "five_hour_pct": 97.0,
            "five_hour_reset": (NOW - timedelta(minutes=1)).isoformat(),
            "last_seen": NOW.timestamp(),
        }
        assert accounts.effective_pcts(row, NOW)["five_hour"] == 0.0

    def test_future_reset_keeps_pct(self):
        row = {
            "five_hour_pct": 55.0,
            "five_hour_reset": (NOW + timedelta(hours=2)).isoformat(),
        }
        assert accounts.effective_pcts(row, NOW)["five_hour"] == 55.0

    def test_missing_pct_is_none(self):
        assert accounts.effective_pcts({}, NOW)["five_hour"] is None

    def test_missing_reset_keeps_pct(self):
        row = {"seven_day_pct": 31.0}
        assert accounts.effective_pcts(row, NOW)["seven_day"] == 31.0


    def test_stale_passed_reset_does_not_show_false_headroom(self):
        # Access token lapsed → poller skipped this row → its reset slid into
        # the past WITHOUT a re-poll (last_seen older than the reset). The
        # window is NOT confirmed empty; effective must return the last-known
        # pct, not 0, or it strands a switch onto a spent account.
        now = datetime(2026, 7, 20, 19, 0, tzinfo=timezone.utc)
        reset_past = datetime(2026, 7, 20, 18, 0, tzinfo=timezone.utc)
        polled_before_reset = datetime(2026, 7, 20, 17, 30, tzinfo=timezone.utc).timestamp()
        row = {
            "fable_pct": 100.0,
            "fable_reset": reset_past.isoformat(),
            "last_seen": polled_before_reset,
        }
        assert accounts.effective_pcts(row, now)["fable"] == 100.0

    def test_confirmed_reset_shows_empty(self):
        # Polled AFTER the reset (last_seen newer) → the window really rolled
        # over → 0 is correct.
        now = datetime(2026, 7, 20, 19, 0, tzinfo=timezone.utc)
        reset_past = datetime(2026, 7, 20, 18, 0, tzinfo=timezone.utc)
        polled_after_reset = datetime(2026, 7, 20, 18, 30, tzinfo=timezone.utc).timestamp()
        row = {
            "fable_pct": 100.0,
            "fable_reset": reset_past.isoformat(),
            "last_seen": polled_after_reset,
        }
        assert accounts.effective_pcts(row, now)["fable"] == 0.0


class TestBlobAccessToken:
    def test_nested(self):
        blob = json.dumps({"claudeAiOauth": {"accessToken": "tok-1"}})
        assert accounts.blob_access_token(blob) == "tok-1"

    def test_flat(self):
        assert accounts.blob_access_token(json.dumps({"accessToken": "tok-2"})) == "tok-2"

    def test_invalid(self):
        assert accounts.blob_access_token("not json") is None
        assert accounts.blob_access_token(json.dumps(["nope"])) is None


class TestCredExpiry:
    def test_blob_refresh_expiry_ms_to_seconds(self):
        blob = json.dumps({"claudeAiOauth": {"refreshTokenExpiresAt": 1784337371728}})
        assert accounts.blob_refresh_expiry(blob) == 1784337371  # ms floored to s

    def test_blob_refresh_expiry_missing(self):
        assert accounts.blob_refresh_expiry(json.dumps({"claudeAiOauth": {}})) is None
        assert accounts.blob_refresh_expiry("not json") is None

    def test_blob_refresh_expiry_flat(self):
        assert accounts.blob_refresh_expiry(json.dumps({"refreshTokenExpiresAt": 2000000000000})) == 2000000000


class TestPickRoute:
    NOW = 1_784_000_000.0
    VAULT = {"tokens": {
        "gmail": {"token": "sk-ant-gmail-tok", "expires_at": NOW + 1000},
        "ymail": {"token": "sk-ant-ymail-tok", "expires_at": NOW - 1},  # expired token
    }}

    def test_best_with_token_wins(self):
        rows = [accounts_row("alumni", 5.0), accounts_row("gmail", 20.0), accounts_row("ymail", 1.0)]
        # alumni has no token, ymail's token is expired -> gmail
        assert accounts.pick_route(rows, self.VAULT, set(), self.NOW, None) == ("gmail", "sk-ant-gmail-tok")

    def test_pin_overrides_headroom(self):
        rows = [accounts_row("alumni", 5.0), accounts_row("gmail", 90.0)]
        assert accounts.pick_route(rows, self.VAULT, set(), self.NOW, "gmail")[0] == "gmail"

    def test_excluded_skipped(self):
        rows = [accounts_row("gmail", 5.0)]
        assert accounts.pick_route(rows, self.VAULT, {"gmail"}, self.NOW, None) is None

    def test_expired_cred_row_skipped(self):
        rows = [accounts_row("gmail", 5.0, expired=True)]
        assert accounts.pick_route(rows, self.VAULT, set(), self.NOW, None) is None

    def test_no_tokens_none(self):
        rows = [accounts_row("alumni", 5.0)]
        assert accounts.pick_route(rows, self.VAULT, set(), self.NOW, None) is None


class TestMergeTokenVaults:
    def test_union(self):
        a = {"tokens": {"gmail": {"token": "g", "minted_at": 1}}}
        b = {"tokens": {"ymail": {"token": "y", "minted_at": 2}}}
        assert set(accounts.merge_token_vaults(a, b)["tokens"]) == {"gmail", "ymail"}

    def test_newer_mint_wins(self):
        a = {"tokens": {"gmail": {"token": "old", "minted_at": 1}}}
        b = {"tokens": {"gmail": {"token": "new", "minted_at": 9}}}
        assert accounts.merge_token_vaults(a, b)["tokens"]["gmail"]["token"] == "new"
        assert accounts.merge_token_vaults(b, a)["tokens"]["gmail"]["token"] == "new"

    def test_empty_sides(self):
        assert accounts.merge_token_vaults({}, {})["tokens"] == {}


class TestTokenVault:
    def test_round_trip_and_perms(self, tmp_path, monkeypatch):
        monkeypatch.setattr(accounts, "TOKEN_VAULT_PATH", tmp_path / "sub" / "vault.json")
        vault = {"version": 1, "tokens": {"x": {"token": "sk-ant-t", "expires_at": 2}}}
        accounts.save_token_vault(vault)
        assert accounts.load_token_vault() == vault
        assert (accounts.TOKEN_VAULT_PATH.stat().st_mode & 0o777) == 0o600
        assert (accounts.TOKEN_VAULT_PATH.parent.stat().st_mode & 0o777) == 0o700

    def test_missing_file_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(accounts, "TOKEN_VAULT_PATH", tmp_path / "none.json")
        assert accounts.load_token_vault() == {"version": 1, "tokens": {}}


class TestRouterCore:
    NOW = 2_000_000_000.0

    def _blob(self, atok, rt_exp_ms):
        return json.dumps({"claudeAiOauth": {"accessToken": atok, "refreshToken": "r",
                                             "refreshTokenExpiresAt": rt_exp_ms}})

    def test_blob_expired_by_refresh_expiry(self):
        assert accounts.blob_expired(self._blob("a", 1_000_000_000_000), self.NOW) is True   # 2001, past
        assert accounts.blob_expired(self._blob("a", 3_000_000_000_000), self.NOW) is False  # 2065, future
        assert accounts.blob_expired('{"claudeAiOauth":{}}', self.NOW) is True    # no refresh token at all
        assert accounts.blob_expired("", self.NOW) is True                        # unparseable
        # regression: cc's rotation rewrites omit refreshTokenExpiresAt — alive, not dead
        rotation = json.dumps({"claudeAiOauth": {"accessToken": "a", "refreshToken": "r"}})
        assert accounts.blob_expired(rotation, self.NOW) is False

    def _rows(self):
        return [
            accounts_row("gmail", 12.0, expired=False, active=False),
            accounts_row("alumni", 4.0, expired=True, active=False),   # freshest but cred dead
            accounts_row("ymail", 30.0, expired=False, active=False),
            accounts_row("coram-max", 88.0, expired=False, active=True),
        ]

    def test_route_pick_skips_expired_and_active(self):
        # alumni is lowest but expired -> skip; active coram-max -> skip -> gmail
        assert accounts.route_pick(self._rows(), set()) == "gmail"

    def test_route_pick_respects_excludes(self):
        assert accounts.route_pick(self._rows(), {"gmail"}) == "ymail"

    def test_route_pick_none_when_all_blocked(self):
        rows = [accounts_row("a", 5.0, expired=True), accounts_row("b", 6.0, active=True)]
        assert accounts.route_pick(rows, set()) is None


def accounts_row(label, five_hour, expired=False, active=False, fable=0.0, seven_day=0.0):
    return {"label": label, "email": f"{label}@x", "five_hour": five_hour,
            "seven_day": seven_day, "fable": fable, "expired": expired, "active": active}


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
        row = accounts.usage_to_reset_row("e@x.io", "org1", self.USAGE, 1784300000)
        assert row["email"] == "e@x.io" and row["org_uuid"] == "org1"
        assert row["five_hour_pct"] == 42
        assert row["five_hour_reset"] == "2026-07-17T22:00:00Z"
        assert row["seven_day_pct"] == 18
        assert row["fable_pct"] == 7
        assert row["fable_label"] == "Fable"
        assert row["last_seen"] == 1784300000

    def test_missing_weekly_is_none(self):
        row = accounts.usage_to_reset_row("e@x.io", "org1", {"five_hour": {}, "seven_day": {}}, 1)
        assert row["fable_pct"] is None and row["fable_label"] is None
        assert row["five_hour_pct"] == 0  # None utilization -> 0

    def test_access_expiry_ms_to_s(self):
        blob = json.dumps({"claudeAiOauth": {"expiresAt": 1784337371728}})
        assert accounts.blob_access_expiry(blob) == 1784337371

    def test_access_expiry_missing(self):
        assert accounts.blob_access_expiry(json.dumps({"claudeAiOauth": {}})) is None


def _live_blob(atok, future_ms=3_000_000_000_000):
    # access + refresh both far-future: poll won't skip it, cred isn't expired
    return json.dumps({"claudeAiOauth": {"accessToken": atok, "refreshToken": "r",
                                         "expiresAt": future_ms, "refreshTokenExpiresAt": future_ms}})


class TestLockedReentrant:
    """The BLOCK deadlock: route_once holds locked(), poll's merge_reset_rows
    re-takes it on a second fd — non-reentrant flock self-blocks in-process."""

    def test_nested_locked_does_not_deadlock(self, tmp_path, monkeypatch):
        monkeypatch.setattr(accounts, "LOCK_PATH", tmp_path / "accounts.lock")
        monkeypatch.setattr(accounts, "_lock_depth", 0)
        done = threading.Event()

        def run():
            with accounts.locked():
                with accounts.locked():  # nested acquire must return, not block
                    pass
            done.set()

        threading.Thread(target=run, daemon=True).start()
        assert done.wait(timeout=5), "nested locked() deadlocked"


class TestRouteOnceNoDeadlock:
    def _paths(self, tmp_path, monkeypatch):
        monkeypatch.setattr(accounts, "LOCK_PATH", tmp_path / "accounts.lock")
        monkeypatch.setattr(accounts, "BLOBS_PATH", tmp_path / "blobs.json")
        monkeypatch.setattr(accounts, "RESETS_PATH", tmp_path / "resets.json")
        monkeypatch.setattr(accounts, "MODE_PATH", tmp_path / "mode.json")
        monkeypatch.setattr(accounts, "CRED_FILE", tmp_path / ".credentials.json")
        monkeypatch.setattr(accounts, "_lock_depth", 0)
        monkeypatch.setattr(accounts, "_last_switch_ts", None)
        # hermetic: never read/delete the REAL keychain from tests
        monkeypatch.setattr(accounts, "kc_read", lambda service, account=None: None)
        monkeypatch.setattr(accounts, "kc_slot_status", lambda service: "absent")
        monkeypatch.setattr(accounts, "kc_delete", lambda service, account=None: None)

    def test_route_once_completes_when_poll_yields_rows(self, tmp_path, monkeypatch):
        # Real poll -> merge_reset_rows re-enters the lock. Pre-fix: hangs here.
        self._paths(tmp_path, monkeypatch)
        blobs = {"accounts": {"gmail": {"email": "g@x", "org_uuid": "o1",
                                        "blob": _live_blob("tok-g")}}}
        (tmp_path / "blobs.json").write_text(json.dumps(blobs))
        monkeypatch.setattr(accounts, "fetch_usage", lambda tok: {
            "five_hour": {"utilization": 10, "resets_at": "2026-07-17T22:00:00Z"},
            "seven_day": {"utilization": 5, "resets_at": "2026-07-23T10:00:00Z"}, "limits": []})
        monkeypatch.setattr(accounts, "fetch_profile", lambda tok: None)
        done, errbox = threading.Event(), []

        def run():
            try:
                accounts.route_once(80.0)
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
        monkeypatch.setattr(accounts, "LOCK_PATH", tmp_path / "accounts.lock")
        monkeypatch.setattr(accounts, "CRED_FILE", tmp_path / ".credentials.json")
        monkeypatch.setattr(accounts, "_lock_depth", 0)
        monkeypatch.setattr(accounts, "_last_switch_ts", None)  # never switched: dwell open
        monkeypatch.setattr(accounts, "load_blobs", lambda: blobs)
        monkeypatch.setattr(accounts, "poll_blobs_usage", lambda b: 0)
        monkeypatch.setattr(accounts, "capture_live_to_blobs", lambda b: active)
        monkeypatch.setattr(accounts, "route_rows", lambda b, a, t: rows)
        monkeypatch.setattr(accounts, "load_mode", lambda: mode)
        monkeypatch.setattr(accounts, "excluded_labels", lambda: set())
        # hermetic: never read/delete the REAL keychain from tests
        self.kc_deletes = []
        monkeypatch.setattr(accounts, "kc_read", lambda service, account=None: None)
        monkeypatch.setattr(accounts, "kc_slot_status", lambda service: "absent")
        monkeypatch.setattr(accounts, "kc_delete",
                            lambda service, account=None: self.kc_deletes.append(service))

    def test_auto_holds_when_pick_not_meaningfully_fresher(self, tmp_path, monkeypatch):
        # A active @85 (>=80), B @82 — 3 pts fresher. Pre-fix switches A->B then
        # B->A every pass (ping-pong). Fixed: holds (needs >= hysteresis fresher).
        rows = [accounts_row("A", 85.0, active=True), accounts_row("B", 82.0)]
        self._wire(tmp_path, monkeypatch, rows, "A", {"mode": "auto"},
                   {"accounts": {"B": {"blob": "BLOB-B"}}})
        assert accounts.route_once(80.0) is None
        assert not accounts.CRED_FILE.exists()  # no switch written

    def test_auto_switches_when_pick_much_fresher(self, tmp_path, monkeypatch):
        rows = [accounts_row("A", 85.0, active=True), accounts_row("B", 70.0)]
        self._wire(tmp_path, monkeypatch, rows, "A", {"mode": "auto"},
                   {"accounts": {"B": {"blob": "BLOB-B"}}})
        line = accounts.route_once(80.0)
        assert line and "ROUTED A → B" in line
        assert accounts.CRED_FILE.read_text() == "BLOB-B"

    def test_auto_no_switch_below_threshold(self, tmp_path, monkeypatch):
        rows = [accounts_row("A", 50.0, active=True), accounts_row("B", 5.0)]
        self._wire(tmp_path, monkeypatch, rows, "A", {"mode": "auto"},
                   {"accounts": {"B": {"blob": "BLOB-B"}}})
        assert accounts.route_once(80.0) is None
        assert not accounts.CRED_FILE.exists()

    def test_auto_pick_without_board_row_does_not_crash(self, tmp_path, monkeypatch):
        # B has no five_hour (poll skipped it) -> pick_pct None must not TypeError
        rows = [accounts_row("A", 90.0, active=True), accounts_row("B", None)]
        self._wire(tmp_path, monkeypatch, rows, "A", {"mode": "auto"},
                   {"accounts": {"B": {"blob": "BLOB-B"}}})
        assert accounts.route_once(80.0) is None
        assert not accounts.CRED_FILE.exists()

    def test_set_holds_when_live_present_but_unattributed(self, tmp_path, monkeypatch):
        # CRED_FILE present, capture returns None (profile down) -> a cc-rotated
        # token may be live; must NOT overwrite it with the stored blob.
        rows = [accounts_row("B", 5.0)]
        self._wire(tmp_path, monkeypatch, rows, None, {"mode": "set", "label": "B"},
                   {"accounts": {"B": {"blob": "BLOB-B"}}})
        accounts.CRED_FILE.write_text("LIVE-ROTATED")
        assert accounts.route_once(80.0) is None
        assert accounts.CRED_FILE.read_text() == "LIVE-ROTATED"  # untouched

    def test_set_seeds_when_no_cred_file(self, tmp_path, monkeypatch):
        rows = [accounts_row("B", 5.0)]
        self._wire(tmp_path, monkeypatch, rows, None, {"mode": "set", "label": "B"},
                   {"accounts": {"B": {"blob": "BLOB-B"}}})
        assert "SET" in (accounts.route_once(80.0) or "")
        assert accounts.CRED_FILE.read_text() == "BLOB-B"

    def test_auto_capped_escapes_to_a_usable_account(self, tmp_path, monkeypatch):
        # active @100 (>= cap 95), best alt @91 (below cap = real headroom). Below
        # cap the 9-pt gap would hold on hysteresis; capped, we escape to it.
        rows = [accounts_row("A", 100.0, active=True), accounts_row("B", 91.0)]
        self._wire(tmp_path, monkeypatch, rows, "A", {"mode": "auto"},
                   {"accounts": {"B": {"blob": "BLOB-B"}}})
        line = accounts.route_once(80.0)
        assert line and "ROUTED A → B" in line
        assert accounts.CRED_FILE.read_text() == "BLOB-B"

    def test_auto_capped_holds_when_every_alt_is_also_capped(self, tmp_path, monkeypatch):
        # active capped @96, only alt @98 also >= cap -> hold. Two near-capped
        # accounts never swap: this is what stops the feedback ping-pong.
        rows = [accounts_row("A", 96.0, active=True), accounts_row("B", 98.0)]
        self._wire(tmp_path, monkeypatch, rows, "A", {"mode": "auto"},
                   {"accounts": {"B": {"blob": "BLOB-B"}}})
        assert accounts.route_once(80.0) is None
        assert not accounts.CRED_FILE.exists()

    def test_auto_dwell_rate_limits_switching(self, tmp_path, monkeypatch):
        # Even a far-fresher pick is held if we switched within the dwell — each
        # switch cold-starts a prompt cache, so the daemon switches at most once
        # per ROUTE_MIN_DWELL_S.
        rows = [accounts_row("A", 100.0, active=True), accounts_row("B", 20.0)]
        self._wire(tmp_path, monkeypatch, rows, "A", {"mode": "auto"},
                   {"accounts": {"B": {"blob": "BLOB-B"}}})
        monkeypatch.setattr(accounts, "_last_switch_ts", time.monotonic())  # just switched
        assert accounts.route_once(80.0) is None
        assert not accounts.CRED_FILE.exists()
        monkeypatch.setattr(accounts, "_last_switch_ts", time.monotonic() - accounts.ROUTE_MIN_DWELL_S - 1)
        line = accounts.route_once(80.0)
        assert line and "ROUTED A → B" in line
        assert accounts._last_switch_ts is not None and accounts._last_switch_ts > 1  # stamp advanced

    # ---- fable mode: greedy chase of the freshest Fable-capable account ----

    def test_fable_holds_when_live_is_best_fable(self, tmp_path, monkeypatch):
        # Live A has the most fable headroom; the only other eligible (B@90) is
        # not >= hysteresis fresher, so hold — no cold-start for nothing.
        rows = [accounts_row("A", 20.0, active=True, fable=10.0), accounts_row("B", 20.0, fable=90.0)]
        self._wire(tmp_path, monkeypatch, rows, "A", {"mode": "fable"},
                   {"accounts": {"B": {"blob": "BLOB-B"}}})
        assert accounts.route_once(80.0) is None
        assert not accounts.CRED_FILE.exists()

    def test_fable_holds_on_near_tie(self, tmp_path, monkeypatch):
        # Live @45, best alt @40 — only 5 pts fresher (< hysteresis 10) → hold.
        rows = [accounts_row("A", 20.0, active=True, fable=45.0), accounts_row("B", 20.0, fable=40.0)]
        self._wire(tmp_path, monkeypatch, rows, "A", {"mode": "fable"},
                   {"accounts": {"B": {"blob": "BLOB-B"}}})
        assert accounts.route_once(80.0) is None
        assert not accounts.CRED_FILE.exists()

    def test_fable_switches_to_meaningfully_fresher(self, tmp_path, monkeypatch):
        # Live @80 (eligible), B @10 — 70 pts fresher (>= hysteresis) → switch.
        rows = [accounts_row("A", 30.0, active=True, fable=80.0), accounts_row("B", 10.0, fable=10.0)]
        self._wire(tmp_path, monkeypatch, rows, "A", {"mode": "fable"},
                   {"accounts": {"B": {"blob": "BLOB-B"}}})
        line = accounts.route_once(80.0)
        assert line and "FABLE A → B" in line
        assert accounts.CRED_FILE.read_text() == "BLOB-B"

    def test_fable_switches_when_live_fable_capped(self, tmp_path, monkeypatch):
        # Live fable exhausted (@97) → live ineligible → switch to eligible B@50.
        rows = [accounts_row("A", 20.0, active=True, fable=97.0), accounts_row("B", 10.0, fable=50.0)]
        self._wire(tmp_path, monkeypatch, rows, "A", {"mode": "fable"},
                   {"accounts": {"B": {"blob": "BLOB-B"}}})
        line = accounts.route_once(80.0)
        assert line and "FABLE A → B" in line
        assert accounts.CRED_FILE.read_text() == "BLOB-B"

    def test_fable_switches_when_live_5h_capped(self, tmp_path, monkeypatch):
        # Live fable fine (@20) but 5h capped (@97) → live unusable → switch.
        rows = [accounts_row("A", 97.0, active=True, fable=20.0), accounts_row("B", 10.0, fable=50.0)]
        self._wire(tmp_path, monkeypatch, rows, "A", {"mode": "fable"},
                   {"accounts": {"B": {"blob": "BLOB-B"}}})
        line = accounts.route_once(80.0)
        assert line and "FABLE A → B" in line
        assert accounts.CRED_FILE.read_text() == "BLOB-B"

    def test_fable_dwell_rate_limits(self, tmp_path, monkeypatch):
        # A switch is due (live fable capped, B fresh) but held within the dwell.
        rows = [accounts_row("A", 20.0, active=True, fable=97.0), accounts_row("B", 10.0, fable=10.0)]
        self._wire(tmp_path, monkeypatch, rows, "A", {"mode": "fable"},
                   {"accounts": {"B": {"blob": "BLOB-B"}}})
        monkeypatch.setattr(accounts, "_last_switch_ts", time.monotonic())  # just switched
        assert accounts.route_once(80.0) is None
        assert not accounts.CRED_FILE.exists()
        monkeypatch.setattr(accounts, "_last_switch_ts", time.monotonic() - accounts.ROUTE_MIN_DWELL_S - 1)
        line = accounts.route_once(80.0)
        assert line and "FABLE A → B" in line

    def test_fable_fallback_holds_below_threshold(self, tmp_path, monkeypatch):
        # No account has fable headroom, but live 5h @30 (< threshold) → the 5h
        # router holds; the session still does normal work, so no pointless switch.
        rows = [accounts_row("A", 30.0, active=True, fable=100.0), accounts_row("B", 5.0, fable=100.0)]
        self._wire(tmp_path, monkeypatch, rows, "A", {"mode": "fable"},
                   {"accounts": {"B": {"blob": "BLOB-B"}}})
        assert accounts.route_once(80.0) is None
        assert not accounts.CRED_FILE.exists()

    def test_fable_fallback_escapes_capped_5h(self, tmp_path, monkeypatch):
        # No fable anywhere AND live 5h capped (@97) → fall back to the 5h router,
        # which escapes to B (@40, real headroom). Log is ROUTED (the 5h path).
        rows = [accounts_row("A", 97.0, active=True, fable=100.0), accounts_row("B", 40.0, fable=100.0)]
        self._wire(tmp_path, monkeypatch, rows, "A", {"mode": "fable"},
                   {"accounts": {"B": {"blob": "BLOB-B"}}})
        line = accounts.route_once(80.0)
        assert line and "ROUTED A → B" in line
        assert accounts.CRED_FILE.read_text() == "BLOB-B"

    def test_fable_holds_when_live_unattributed(self, tmp_path, monkeypatch):
        # active None (profile down) + CRED_FILE present → don't clobber a possibly
        # cc-rotated live token (mirrors the AUTO/SET guard).
        rows = [accounts_row("B", 5.0, fable=5.0)]
        self._wire(tmp_path, monkeypatch, rows, None, {"mode": "fable"},
                   {"accounts": {"B": {"blob": "BLOB-B"}}})
        accounts.CRED_FILE.write_text("LIVE-ROTATED")
        assert accounts.route_once(80.0) is None
        assert accounts.CRED_FILE.read_text() == "LIVE-ROTATED"


class TestCmdSet:
    def _paths(self, tmp_path, monkeypatch):
        monkeypatch.setattr(accounts, "LOCK_PATH", tmp_path / "accounts.lock")
        monkeypatch.setattr(accounts, "CRED_FILE", tmp_path / ".credentials.json")
        monkeypatch.setattr(accounts, "MODE_PATH", tmp_path / "mode.json")
        monkeypatch.setattr(accounts, "BLOBS_PATH", tmp_path / "blobs.json")
        monkeypatch.setattr(accounts, "_lock_depth", 0)
        # never reach the network / real store / real keychain from tests
        monkeypatch.setattr(accounts, "fetch_profile", lambda tok: None)
        monkeypatch.setattr(accounts, "kc_read", lambda service, account=None: None)
        monkeypatch.setattr(accounts, "kc_slot_status", lambda service: "absent")
        monkeypatch.setattr(accounts, "kc_delete", lambda service, account=None: None)

    def test_refuses_expired_blob(self, tmp_path, monkeypatch):
        self._paths(tmp_path, monkeypatch)
        expired = _live_blob("a", future_ms=1_000_000_000_000)  # 2001, past
        monkeypatch.setattr(accounts, "load_blobs", lambda: {"accounts": {"B": {"blob": expired}}})
        with pytest.raises(SystemExit):
            accounts.cmd_set(types.SimpleNamespace(label="B"))
        assert not accounts.CRED_FILE.exists()  # dead cred NOT written

    def test_applies_live_blob(self, tmp_path, monkeypatch):
        self._paths(tmp_path, monkeypatch)
        live = _live_blob("a")
        monkeypatch.setattr(accounts, "load_blobs", lambda: {"accounts": {"B": {"blob": live}}})
        accounts.cmd_set(types.SimpleNamespace(label="B"))
        assert accounts.CRED_FILE.read_text() == live

    def test_captures_live_before_applying(self, tmp_path, monkeypatch):
        # regression: cmd_set must fold an outgoing rotation into blobs (capture)
        # BEFORE overwriting the live file (apply), so a switch never drops the
        # departing account's freshly-rotated token.
        self._paths(tmp_path, monkeypatch)
        live = _live_blob("a")
        monkeypatch.setattr(accounts, "load_blobs", lambda: {"accounts": {"B": {"blob": live}}})
        monkeypatch.setattr(accounts, "save_mode", lambda m, label: None)
        calls = []
        monkeypatch.setattr(accounts, "capture_live_to_blobs", lambda b: calls.append("capture"))
        real_apply = accounts.apply_account
        monkeypatch.setattr(accounts, "apply_account",
                            lambda label, b: (calls.append("apply"), real_apply(label, b))[1])
        accounts.cmd_set(types.SimpleNamespace(label="B"))
        assert calls == ["capture", "apply"]
        assert accounts.CRED_FILE.read_text() == live

    def test_refuses_when_live_present_but_unattributable(self, tmp_path, monkeypatch):
        # cc rotated the live token (not yet in the store) and profile fetch fails
        # (stubbed None in _paths) -> capture can't attribute it. Overwriting would
        # lose that account's login, so refuse and leave the live cred untouched.
        self._paths(tmp_path, monkeypatch)
        monkeypatch.setattr(accounts, "load_blobs",
                            lambda: {"accounts": {"B": {"blob": _live_blob("b")}}})
        accounts.CRED_FILE.write_text(_live_blob("ROTATED-UNKNOWN"))
        with pytest.raises(SystemExit):
            accounts.cmd_set(types.SimpleNamespace(label="B"))
        assert "ROTATED-UNKNOWN" in accounts.CRED_FILE.read_text()  # not clobbered


class TestLoadBlobs:
    def test_corrupt_store_is_preserved_not_destroyed(self, tmp_path, monkeypatch):
        blobs_path = tmp_path / "blobs.json"
        blobs_path.write_text("NOT JSON {{{")
        monkeypatch.setattr(accounts, "BLOBS_PATH", blobs_path)
        monkeypatch.setattr(accounts, "MIRROR_LOG", tmp_path / "log")  # keep log_line off real paths
        assert accounts.load_blobs() == {"version": 1, "accounts": {}}
        backups = list(tmp_path.glob("blobs.json.corrupt.*"))
        assert len(backups) == 1 and backups[0].read_text() == "NOT JSON {{{"
        # still-corrupt on the next read: no second identical backup piles up
        assert accounts.load_blobs() == {"version": 1, "accounts": {}}
        assert len(list(tmp_path.glob("blobs.json.corrupt.*"))) == 1

    def test_missing_store_returns_empty_without_backup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(accounts, "BLOBS_PATH", tmp_path / "nope.json")
        assert accounts.load_blobs() == {"version": 1, "accounts": {}}
        assert not list(tmp_path.glob("*.corrupt.*"))

    def test_unreadable_store_refuses_instead_of_empty(self, tmp_path, monkeypatch):
        # present-but-unreadable (perms) must NOT read as empty — the next capture
        # would overwrite the store with a single account.
        blobs_path = tmp_path / "blobs.json"
        blobs_path.write_text('{"version": 1, "accounts": {"a": {}, "b": {}}}')
        blobs_path.chmod(0o000)
        monkeypatch.setattr(accounts, "BLOBS_PATH", blobs_path)
        try:
            with pytest.raises(accounts.AccountsError):
                accounts.load_blobs()
        finally:
            blobs_path.chmod(0o600)

    def test_non_dict_store_is_preserved_and_empty(self, tmp_path, monkeypatch):
        blobs_path = tmp_path / "blobs.json"
        blobs_path.write_text("[1, 2, 3]")
        monkeypatch.setattr(accounts, "BLOBS_PATH", blobs_path)
        monkeypatch.setattr(accounts, "MIRROR_LOG", tmp_path / "log")
        assert accounts.load_blobs() == {"version": 1, "accounts": {}}
        assert len(list(tmp_path.glob("blobs.json.corrupt.*"))) == 1


class TestLiveCredAndSwitch:
    """The macOS switch primitive: cc reads keychain-first ('keychain-with-
    plaintext-fallback', ~30s TTL), so a switch = write file + DELETE slot."""

    def test_live_cred_prefers_keychain_over_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(accounts, "CRED_FILE", tmp_path / ".credentials.json")
        accounts.CRED_FILE.write_text("FILE-BLOB")
        monkeypatch.setattr(accounts, "kc_read", lambda service, account=None: "KC-BLOB")
        assert accounts.live_cred() == ("KC-BLOB", "keychain")

    def test_live_cred_falls_back_to_file_then_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(accounts, "CRED_FILE", tmp_path / ".credentials.json")
        monkeypatch.setattr(accounts, "kc_read", lambda service, account=None: None)
        assert accounts.live_cred() == (None, None)
        accounts.CRED_FILE.write_text("FILE-BLOB")
        assert accounts.live_cred() == ("FILE-BLOB", "file")

    def test_capture_reads_the_keychain_source(self, tmp_path, monkeypatch):
        # cc refreshed into the keychain (file deleted): capture must still
        # attribute the live account without any profile fetch when unchanged.
        monkeypatch.setattr(accounts, "CRED_FILE", tmp_path / ".credentials.json")
        kc_blob = _live_blob("tok-kc")
        monkeypatch.setattr(accounts, "kc_read", lambda service, account=None: kc_blob)
        monkeypatch.setattr(accounts, "fetch_profile",
                            lambda tok: (_ for _ in ()).throw(AssertionError("no network")))
        blobs = {"accounts": {"gmail": {"blob": kc_blob}}}
        assert accounts.capture_live_to_blobs(blobs) == "gmail"

    def test_apply_account_deletes_keychain_slot(self, tmp_path, monkeypatch):
        # While the slot exists the file is invisible to cc — apply must write
        # the file AND delete the slot so the ~30s re-read lands on the file.
        monkeypatch.setattr(accounts, "CRED_FILE", tmp_path / ".credentials.json")
        monkeypatch.setattr(accounts, "MIRROR_LOG", tmp_path / "log")
        kc_state = {"present": True}
        deletes = []
        monkeypatch.setattr(accounts, "kc_slot_status",
                            lambda service: "present" if kc_state["present"] else "absent")

        def fake_delete(service, account=None):
            deletes.append(service)
            kc_state["present"] = False
        monkeypatch.setattr(accounts, "kc_delete", fake_delete)
        assert accounts.apply_account("B", {"accounts": {"B": {"blob": "BLOB-B"}}}) is True
        assert accounts.CRED_FILE.read_text() == "BLOB-B"
        assert deletes == [accounts.LIVE_SERVICE]
        assert not (tmp_path / "log").exists()  # clean switch: no warn logged

    def test_apply_account_skips_delete_when_no_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr(accounts, "CRED_FILE", tmp_path / ".credentials.json")
        deletes = []
        monkeypatch.setattr(accounts, "kc_slot_status", lambda service: "absent")
        monkeypatch.setattr(accounts, "kc_delete", lambda service, account=None: deletes.append(service))
        assert accounts.apply_account("B", {"accounts": {"B": {"blob": "BLOB-B"}}}) is True
        assert deletes == []  # nothing to delete; no gratuitous keychain ops

    def test_apply_account_warns_when_slot_state_unknown(self, tmp_path, monkeypatch):
        # Locked keychain / wedged securityd: the probe can't tell — apply must
        # NOT claim a clean switch (cc may keep reading the intact slot) and
        # must not fire a blind delete.
        monkeypatch.setattr(accounts, "CRED_FILE", tmp_path / ".credentials.json")
        monkeypatch.setattr(accounts, "MIRROR_LOG", tmp_path / "log")
        deletes = []
        monkeypatch.setattr(accounts, "kc_slot_status", lambda service: "unknown")
        monkeypatch.setattr(accounts, "kc_delete", lambda service, account=None: deletes.append(service))
        assert accounts.apply_account("B", {"accounts": {"B": {"blob": "BLOB-B"}}}) is True
        assert deletes == []
        assert "unknown" in (tmp_path / "log").read_text()


class TestRouteOnceAdoptsLogin:
    """SET mode must adopt a fresh /login instead of clobbering it (2026-07-20:
    pin=alumni overwrote every coram /login within one daemon tick)."""

    LOGIN_BLOB = json.dumps(  # fresh /login: carries refreshTokenExpiresAt
        {"claudeAiOauth": {"accessToken": "at-new", "refreshToken": "rt",
                           "refreshTokenExpiresAt": 4102444800000}}
    )
    ROTATION_BLOB = json.dumps(  # cc rotation: no refreshTokenExpiresAt
        {"claudeAiOauth": {"accessToken": "at-rot", "refreshToken": "rt"}}
    )

    def _run(self, monkeypatch, live_pair, mode_label="alumni", active="coram-max"):
        from contextlib import nullcontext

        calls = {"save_mode": [], "apply": []}
        monkeypatch.setattr(accounts, "locked", lambda blocking=True: nullcontext())
        monkeypatch.setattr(accounts, "load_blobs", lambda: {"accounts": {}})
        monkeypatch.setattr(accounts, "capture_live_to_blobs", lambda blobs: active)
        monkeypatch.setattr(accounts, "poll_blobs_usage", lambda blobs: 0)
        monkeypatch.setattr(accounts, "route_rows", lambda blobs, a, now: [])
        monkeypatch.setattr(accounts, "load_mode", lambda: {"mode": "set", "label": mode_label})
        monkeypatch.setattr(accounts, "live_cred", lambda: live_pair)
        monkeypatch.setattr(
            accounts, "save_mode", lambda mode, label: calls["save_mode"].append((mode, label))
        )
        monkeypatch.setattr(
            accounts, "apply_account", lambda label, blobs: calls["apply"].append(label) or True
        )
        return accounts.route_once(80.0), calls

    def test_keychain_login_blob_moves_the_pin(self, monkeypatch):
        line, calls = self._run(monkeypatch, (self.LOGIN_BLOB, "keychain"))
        assert line is not None and line.startswith("ADOPT /login")
        assert calls["save_mode"] == [("set", "coram-max")]
        assert calls["apply"] == []  # the login is never overwritten

    def test_keychain_rotation_blob_still_pins(self, monkeypatch):
        line, calls = self._run(monkeypatch, (self.ROTATION_BLOB, "keychain"))
        assert line == "SET coram-max → alumni"
        assert calls["save_mode"] == []
        assert calls["apply"] == ["alumni"]

    def test_file_sourced_login_blob_still_pins(self, monkeypatch):
        line, calls = self._run(monkeypatch, (self.LOGIN_BLOB, "file"))
        assert line == "SET coram-max → alumni"
        assert calls["save_mode"] == []
        assert calls["apply"] == ["alumni"]


class TestLoadModeFable:
    def test_fable_round_trips(self, tmp_path, monkeypatch):
        monkeypatch.setattr(accounts, "MODE_PATH", tmp_path / "mode.json")
        accounts.save_mode("fable", None)
        assert accounts.load_mode() == {"mode": "fable", "label": None}

    def test_unknown_mode_defaults_to_auto(self, tmp_path, monkeypatch):
        p = tmp_path / "mode.json"
        p.write_text(json.dumps({"mode": "bogus", "label": None}))
        monkeypatch.setattr(accounts, "MODE_PATH", p)
        assert accounts.load_mode() == {"mode": "auto", "label": None}


class TestFableEligible:
    # signature: fable_eligible(five_hour, seven_day, fable)
    def test_all_under_cap(self):
        assert accounts.fable_eligible(30.0, 30.0, 40.0) is True

    def test_boundary_just_under_cap(self):
        assert accounts.fable_eligible(94.0, 94.0, 94.0) is True

    def test_fable_none_ineligible(self):
        assert accounts.fable_eligible(10.0, 10.0, None) is False

    def test_fable_at_cap_ineligible(self):
        assert accounts.fable_eligible(10.0, 10.0, 95.0) is False  # cap is exclusive

    def test_fable_over_cap_ineligible(self):
        assert accounts.fable_eligible(10.0, 10.0, 100.0) is False

    def test_five_hour_none_ineligible(self):
        assert accounts.fable_eligible(None, 10.0, 10.0) is False

    def test_five_hour_at_cap_ineligible(self):
        assert accounts.fable_eligible(95.0, 10.0, 10.0) is False  # 5h floor

    def test_seven_day_none_ineligible(self):
        assert accounts.fable_eligible(10.0, None, 10.0) is False

    def test_seven_day_at_cap_ineligible(self):
        # the trap: fresh fable but the weekly (all-models) window is maxed
        assert accounts.fable_eligible(10.0, 95.0, 10.0) is False

    def test_fable_headroom_useless_when_weekly_capped(self):
        # 0% fable used but weekly maxed → can't make requests → not usable
        assert accounts.fable_eligible(10.0, 100.0, 0.0) is False

    def test_fable_headroom_useless_when_5h_capped(self):
        assert accounts.fable_eligible(100.0, 10.0, 0.0) is False


class TestRoutePickFable:
    def test_lowest_fable_eligible_wins(self):
        rows = [accounts_row("A", 10.0, active=True, fable=5.0),
                accounts_row("gmail", 10.0, fable=80.0),
                accounts_row("ymail", 10.0, fable=20.0)]
        assert accounts.route_pick_fable(rows, set()) == "ymail"

    def test_skips_active_and_expired_even_if_lower_fable(self):
        rows = [accounts_row("A", 10.0, active=True, fable=1.0),        # lowest but active
                accounts_row("dead", 10.0, fable=2.0, expired=True),    # 2nd but expired
                accounts_row("gmail", 10.0, fable=40.0)]
        assert accounts.route_pick_fable(rows, set()) == "gmail"

    def test_five_hour_floor_excludes(self):
        # B has 0% fable but 100% 5h → unusable; only C qualifies
        rows = [accounts_row("A", 10.0, active=True, fable=90.0),
                accounts_row("B", 100.0, fable=0.0),
                accounts_row("C", 50.0, fable=40.0)]
        assert accounts.route_pick_fable(rows, set()) == "C"

    def test_none_when_all_fable_capped(self):
        rows = [accounts_row("A", 10.0, active=True, fable=10.0),
                accounts_row("B", 10.0, fable=100.0),
                accounts_row("C", 10.0, fable=96.0)]
        assert accounts.route_pick_fable(rows, set()) is None

    def test_none_when_all_5h_capped(self):
        rows = [accounts_row("A", 10.0, active=True, fable=10.0),
                accounts_row("B", 99.0, fable=10.0)]
        assert accounts.route_pick_fable(rows, set()) is None

    def test_respects_excludes(self):
        rows = [accounts_row("A", 10.0, active=True, fable=90.0),
                accounts_row("gmail", 10.0, fable=20.0),
                accounts_row("ymail", 10.0, fable=30.0)]
        assert accounts.route_pick_fable(rows, {"gmail"}) == "ymail"

    def test_fable_none_rows_skipped(self):
        rows = [accounts_row("A", 10.0, active=True, fable=10.0),
                accounts_row("B", 10.0, fable=None)]
        assert accounts.route_pick_fable(rows, set()) is None

    def test_seven_day_floor_excludes_the_weekly_trap(self):
        # gmail-shaped: freshest fable but weekly (7d) maxed → skip; brown wins.
        # This is the exact live scenario: a fresh-fable account you can't run on.
        rows = [accounts_row("A", 10.0, active=True, fable=90.0),
                accounts_row("gmail", 0.0, fable=8.0, seven_day=100.0),   # trap
                accounts_row("brown", 34.0, fable=41.0, seven_day=52.0)]
        assert accounts.route_pick_fable(rows, set()) == "brown"


class TestPickEnvFable:
    NOW = 1_784_000_000.0
    VAULT = {"tokens": {
        "gmail": {"token": "sk-gmail", "expires_at": NOW + 1000},
        "brown": {"token": "sk-brown", "expires_at": NOW + 1000},
        "ymail": {"token": "sk-ymail", "expires_at": NOW + 1000},
    }}

    def test_fable_first_orders_eligible_by_fable(self):
        rows = [accounts_row("gmail", 10.0, fable=80.0),
                accounts_row("brown", 50.0, fable=10.0),
                accounts_row("ymail", 20.0, fable=40.0)]
        assert [r["label"] for r in accounts._fable_first(rows)] == ["brown", "ymail", "gmail"]

    def test_prefers_fable_over_headroom(self):
        # gmail is best 5h (5) but fable-capped; brown worse 5h (50) but fable @10.
        rows = [accounts_row("gmail", 5.0, fable=100.0), accounts_row("brown", 50.0, fable=10.0)]
        picked = accounts.pick_route(accounts._fable_first(rows), self.VAULT, set(), self.NOW, None)
        assert picked == ("brown", "sk-brown")

    def test_falls_back_to_headroom_when_none_eligible(self):
        # all fable-capped → _fable_first keeps headroom order → best 5h (gmail) wins
        rows = [accounts_row("gmail", 5.0, fable=100.0), accounts_row("brown", 50.0, fable=100.0)]
        picked = accounts.pick_route(accounts._fable_first(rows), self.VAULT, set(), self.NOW, None)
        assert picked == ("gmail", "sk-gmail")

    def test_skips_eligible_without_token(self):
        # brown is the freshest fable but has NO token → fall through to the next
        # eligible tokened account (ymail).
        vault = {"tokens": {"ymail": {"token": "sk-ymail", "expires_at": self.NOW + 1000}}}
        rows = [accounts_row("brown", 20.0, fable=10.0), accounts_row("ymail", 30.0, fable=40.0)]
        picked = accounts.pick_route(accounts._fable_first(rows), vault, set(), self.NOW, None)
        assert picked == ("ymail", "sk-ymail")

    def test_skips_weekly_maxed_fable_account(self):
        # gmail: freshest fable but weekly (7d) maxed → _fable_first drops it below
        # brown (headroom on fable AND weekly), so pick_route lands on brown.
        rows = [accounts_row("gmail", 0.0, fable=8.0, seven_day=100.0),
                accounts_row("brown", 34.0, fable=41.0, seven_day=52.0)]
        picked = accounts.pick_route(accounts._fable_first(rows), self.VAULT, set(), self.NOW, None)
        assert picked == ("brown", "sk-brown")


class TestRouteRowsStale:
    def test_stale_flag_sourced_from_last_seen(self, monkeypatch):
        now = 2_000_000_000.0
        monkeypatch.setattr(accounts, "load_resets", lambda: {
            "fresh@x|o1": {"five_hour_pct": 10.0, "last_seen": now - 60},
            "old@x|o2": {"five_hour_pct": 20.0, "last_seen": now - accounts.STALE_AFTER_S - 1},
        })
        blobs = {"accounts": {
            "fresh": {"blob": _live_blob("f"), "email": "fresh@x", "org_uuid": "o1"},
            "old": {"blob": _live_blob("o"), "email": "old@x", "org_uuid": "o2"},
        }}
        rows = accounts.route_rows(blobs, None, now)
        by_label = {r["label"]: r for r in rows}
        assert by_label["fresh"]["stale"] is False
        assert by_label["old"]["stale"] is True

    def test_stale_false_when_never_seen(self, monkeypatch):
        monkeypatch.setattr(accounts, "load_resets", lambda: {})
        blobs = {"accounts": {"gmail": {"blob": _live_blob("g"), "email": "g@x", "org_uuid": "o1"}}}
        rows = accounts.route_rows(blobs, None, time.time())
        assert rows[0]["stale"] is False


class TestCmdPickEnv:
    """The live shell-launch hook: enumerate accounts from the blobs store,
    match labels against the minted-token vault, emit export lines. Must never
    raise — routing can't block launching claude."""

    def _wire(self, monkeypatch, blobs, tokens, *, resets=None, excludes=frozenset(), mode="auto"):
        monkeypatch.setattr(accounts, "load_blobs", lambda: {"accounts": blobs})
        monkeypatch.setattr(accounts, "load_token_vault", lambda: {"tokens": tokens})
        monkeypatch.setattr(accounts, "load_resets", lambda: resets or {})
        monkeypatch.setattr(accounts, "excluded_labels", lambda: set(excludes))
        monkeypatch.setattr(accounts, "load_mode", lambda: {"mode": mode, "label": None})
        monkeypatch.delenv("ACCOUNTS_PIN", raising=False)

    def _tok(self, name):
        return {"token": f"sk-ant-{name}", "expires_at": 9_999_999_999}  # year 2286

    def test_roster_from_blobs_store(self, monkeypatch, capsys):
        # freshest-with-token wins. raising=False: this assertion must keep working
        # once load_meta no longer exists post-legacy-delete.
        monkeypatch.setattr(
            accounts, "load_meta",
            lambda: (_ for _ in ()).throw(AssertionError("meta store touched")),
            raising=False,
        )
        blobs = {
            "gmail": {"blob": _live_blob("g"), "email": "g@x", "org_uuid": "o1"},
            "work": {"blob": _live_blob("w"), "email": "w@x", "org_uuid": "o2"},
        }
        resets = {"g@x|o1": {"five_hour_pct": 60.0}, "w@x|o2": {"five_hour_pct": 10.0}}
        self._wire(monkeypatch, blobs, {"gmail": self._tok("gmail"), "work": self._tok("work")},
                   resets=resets)
        accounts.cmd_pick_env(types.SimpleNamespace())
        out = capsys.readouterr().out
        assert "export CLAUDE_CODE_OAUTH_TOKEN='sk-ant-work'" in out
        assert "export ACCOUNTS_ROUTED_LABEL='work'" in out  # 10% < 60% -> best-first

    def test_excluded_label_respected(self, monkeypatch, capsys):
        blobs = {"gmail": {"blob": _live_blob("g"), "email": "g@x", "org_uuid": "o1"}}
        self._wire(monkeypatch, blobs, {"gmail": self._tok("gmail")}, excludes={"gmail"})
        accounts.cmd_pick_env(types.SimpleNamespace())
        assert capsys.readouterr().out == ""

    def test_pin_overrides_headroom(self, monkeypatch, capsys):
        # pinned account wins even with worse headroom than the free pick
        blobs = {
            "gmail": {"blob": _live_blob("g"), "email": "g@x", "org_uuid": "o1"},
            "work": {"blob": _live_blob("w"), "email": "w@x", "org_uuid": "o2"},
        }
        resets = {"g@x|o1": {"five_hour_pct": 5.0}, "w@x|o2": {"five_hour_pct": 90.0}}
        self._wire(monkeypatch, blobs, {"gmail": self._tok("gmail"), "work": self._tok("work")},
                   resets=resets)
        monkeypatch.setenv("ACCOUNTS_PIN", "work")
        accounts.cmd_pick_env(types.SimpleNamespace())
        out = capsys.readouterr().out
        assert "export CLAUDE_CODE_OAUTH_TOKEN='sk-ant-work'" in out
        assert "export ACCOUNTS_ROUTED_LABEL='work'" in out

    def test_expired_blob_filtered(self, monkeypatch, capsys):
        # refresh-expired blob is skipped despite a live minted token
        blobs = {"gmail": {"blob": _live_blob("g", future_ms=1_000_000_000_000),  # 2001, past
                           "email": "g@x", "org_uuid": "o1"}}
        self._wire(monkeypatch, blobs, {"gmail": self._tok("gmail")})
        accounts.cmd_pick_env(types.SimpleNamespace())
        assert capsys.readouterr().out == ""

    def test_no_minted_token_prints_nothing(self, monkeypatch, capsys):
        blobs = {"gmail": {"blob": _live_blob("g"), "email": "g@x", "org_uuid": "o1"}}
        self._wire(monkeypatch, blobs, {})  # empty token vault
        accounts.cmd_pick_env(types.SimpleNamespace())
        assert capsys.readouterr().out == ""

    def test_internal_exception_prints_nothing(self, monkeypatch, capsys):
        # never-raise contract: any internal failure yields no output, no traceback
        monkeypatch.setattr(accounts, "load_blobs",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        accounts.cmd_pick_env(types.SimpleNamespace())
        assert capsys.readouterr().out == ""

    def test_fable_mode_uses_flat_row_shape(self, monkeypatch, capsys):
        # regression: _fable_first must read route_rows' flat five_hour/seven_day/fable
        # fields; a legacy-shape mismatch here raises inside the try/except and
        # pick-env silently emits nothing.
        blobs = {
            "gmail": {"blob": _live_blob("g"), "email": "g@x", "org_uuid": "o1"},
            "brown": {"blob": _live_blob("b"), "email": "b@x", "org_uuid": "o2"},
        }
        resets = {
            "g@x|o1": {"five_hour_pct": 5.0, "seven_day_pct": 5.0, "fable_pct": 90.0},
            "b@x|o2": {"five_hour_pct": 50.0, "seven_day_pct": 50.0, "fable_pct": 10.0},
        }
        self._wire(monkeypatch, blobs, {"gmail": self._tok("gmail"), "brown": self._tok("brown")},
                   resets=resets, mode="fable")
        accounts.cmd_pick_env(types.SimpleNamespace())
        out = capsys.readouterr().out
        # gmail is best 5h but fable-heavy; brown has real fable headroom -> wins
        assert "export CLAUDE_CODE_OAUTH_TOKEN='sk-ant-brown'" in out
        assert "export ACCOUNTS_ROUTED_LABEL='brown'" in out


class TestCmdLs:
    """The blobs-based board: same visual contract as the old vault board
    (marker, staleness, EXPIRED flag, footnotes), rebuilt from route_rows."""

    def _wire(self, monkeypatch, blobs, *, resets=None, active=None, excludes=frozenset()):
        monkeypatch.setattr(accounts, "locked", lambda blocking=True: nullcontext())
        monkeypatch.setattr(accounts, "load_blobs", lambda: {"accounts": blobs})
        monkeypatch.setattr(accounts, "capture_live_to_blobs", lambda b: active)
        monkeypatch.setattr(accounts, "load_resets", lambda: resets or {})
        monkeypatch.setattr(accounts, "excluded_labels", lambda: set(excludes))

    def test_empty_store_prints_login_onboarding_not_enroll(self, monkeypatch, capsys):
        self._wire(monkeypatch, {})
        accounts.cmd_ls(types.SimpleNamespace())
        out = capsys.readouterr().out
        assert "enroll" not in out
        assert "vault" not in out.lower()
        assert "/login" in out

    def test_renders_header_and_active_marker(self, monkeypatch, capsys):
        blobs = {"gmail": {"blob": _live_blob("g"), "email": "g@x", "org_uuid": "o1"}}
        resets = {"g@x|o1": {"five_hour_pct": 12.0, "seven_day_pct": 3.0, "fable_pct": 7.0}}
        self._wire(monkeypatch, blobs, resets=resets, active="gmail")
        accounts.cmd_ls(types.SimpleNamespace())
        out = capsys.readouterr().out
        assert "label" in out and "email" in out and "5h" in out and "7d" in out and "fable" in out
        assert "* gmail" in out
        assert "g@x" in out
        assert "12%" in out and "3%" in out and "7%" in out

    def test_expired_row_flagged_and_sorted_last(self, monkeypatch, capsys):
        blobs = {
            "dead": {"blob": _live_blob("d", future_ms=1_000_000_000_000), "email": "d@x", "org_uuid": "o1"},
            "gmail": {"blob": _live_blob("g"), "email": "g@x", "org_uuid": "o2"},
        }
        resets = {"d@x|o1": {"five_hour_pct": 1.0}, "g@x|o2": {"five_hour_pct": 50.0}}
        self._wire(monkeypatch, blobs, resets=resets)
        accounts.cmd_ls(types.SimpleNamespace())
        out = capsys.readouterr().out
        assert "EXPIRED" in out
        # dead has the lower 5h% (1 < 50) but must sort AFTER gmail (expired-last)
        assert out.index("gmail") < out.index("dead")

    def test_excluded_label_flagged(self, monkeypatch, capsys):
        blobs = {"gmail": {"blob": _live_blob("g"), "email": "g@x", "org_uuid": "o1"}}
        self._wire(monkeypatch, blobs, resets={"g@x|o1": {"five_hour_pct": 1.0}}, excludes={"gmail"})
        accounts.cmd_ls(types.SimpleNamespace())
        assert "[excluded]" in capsys.readouterr().out

    def test_stale_row_marked(self, monkeypatch, capsys):
        stale_seen = time.time() - accounts.STALE_AFTER_S - 60
        blobs = {"gmail": {"blob": _live_blob("g"), "email": "g@x", "org_uuid": "o1"}}
        resets = {"g@x|o1": {"five_hour_pct": 20.0, "last_seen": stale_seen}}
        self._wire(monkeypatch, blobs, resets=resets)
        accounts.cmd_ls(types.SimpleNamespace())
        assert "20%~" in capsys.readouterr().out

    def test_footnotes_present(self, monkeypatch, capsys):
        blobs = {"gmail": {"blob": _live_blob("g"), "email": "g@x", "org_uuid": "o1"}}
        self._wire(monkeypatch, blobs, resets={"g@x|o1": {"five_hour_pct": 1.0}})
        accounts.cmd_ls(types.SimpleNamespace())
        out = capsys.readouterr().out
        assert "matches live slot" in out
        assert "reset-aware; sorted best-first" in out

    def test_expired_footnote_wording_is_not_legacy_vault(self, monkeypatch, capsys):
        blobs = {"gmail": {"blob": _live_blob("g", future_ms=1_000_000_000_000),
                           "email": "g@x", "org_uuid": "o1"}}
        self._wire(monkeypatch, blobs, resets={"g@x|o1": {"five_hour_pct": 1.0}})
        accounts.cmd_ls(types.SimpleNamespace())
        out = capsys.readouterr().out
        assert "EXPIRED = stored refresh token dead" in out
        assert "vaulted" not in out
