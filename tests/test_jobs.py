"""Enforcement jobs: expiry/quota evaluation, the stats cycle, and its loop."""

import json
import logging
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

# Guard BEFORE the panel imports (panel.jobs and panel.app pull in
# panel.xray.stats, which imports the pb stubs): a fresh checkout without
# generated stubs must skip with an actionable reason, not die at collection.
pytest.importorskip(
    "panel.xray.pb.command_pb2",
    reason="generated stubs missing — run: bash scripts/gen_protos.sh",
)

from panel import db, jobs
from panel.app import create_app
from panel.config import Settings
from panel.xray import config_builder
from panel.xray.stats import StatsError

UTC = timezone.utc
NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def settings(tmp_path, fake_xray):
    """Settings pointed at the fake xray binary and a per-test data dir."""
    return Settings(
        ADMIN_USERNAME="admin",
        ADMIN_PASSWORD="test-pass-123",
        PUBLIC_HOST="fakhra.railway.app",
        DATA_DIR=str(tmp_path),
        COOKIE_SECURE=False,
        XRAY_BIN=fake_xray,
        ENV="test",
    )


@pytest.fixture
def live_db(settings):
    """Temp DB initialized for direct db-module use (no app lifespan)."""
    db.init_db(settings.data_dir() / "panel.db")
    return settings


def _user(user_id="alice", **overrides):
    row = {
        "id": user_id,
        "username": user_id,
        "password": "pw",
        "protocol": "vless",
        "uuid": f"uuid-{user_id}",
        "credential": None,
        "ss_method": None,
        "enabled": 1,
        "disable_reason": None,
        "expires_at": None,
        "quota_gb": None,
        "used_bytes": 0,
        "last_raw_bytes": 0,
        "note": "",
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    row.update(overrides)
    return row


class FakeStats:
    """Stats-client double: fixed raw value, or a raised StatsError."""

    def __init__(self, value=0, error=None):
        self.value = value
        self.error = error
        self.queried = []

    def get_bytes(self, user_id):
        self.queried.append(user_id)
        if self.error is not None:
            raise self.error
        return self.value


class FakeManager:
    """Records every apply_config call; returns a configurable (ok, msg)."""

    def __init__(self, ok=True, msg="ok"):
        self.ok = ok
        self.msg = msg
        self.applied = []

    def apply_config(self, users):
        self.applied.append([dict(user) for user in users])
        return self.ok, self.msg


# --- evaluate ----------------------------------------------------------------


def test_evaluate_marks_expired():
    expired = _user("old", expires_at="2025-12-31T00:00:00+00:00")
    fresh = _user("fresh", expires_at="2026-02-01T00:00:00+00:00")
    boundary = _user("boundary", expires_at=NOW.isoformat())  # now == expires_at

    # Strict <: the boundary user is NOT expired yet.
    assert jobs.evaluate([expired, fresh, boundary], NOW) == {"old": "expired"}


def test_evaluate_quota_boundary():
    at_limit = _user("at", quota_gb=1, used_bytes=1_000_000_000)
    under_limit = _user("under", quota_gb=1, used_bytes=999_999_999)

    assert jobs.evaluate([at_limit], NOW) == {"at": "quota"}
    assert jobs.evaluate([under_limit], NOW) == {}


def test_evaluate_unlimited_and_disabled_excluded():
    unlimited = _user("free", quota_gb=None, used_bytes=10**15)
    disabled_expired = _user(
        "off",
        enabled=0,
        expires_at="2025-01-01T00:00:00+00:00",
        quota_gb=1,
        used_bytes=10**12,
    )

    assert jobs.evaluate([unlimited, disabled_expired], NOW) == {}


def test_evaluate_expired_takes_precedence_over_quota():
    both = _user(
        "both",
        expires_at="2025-01-01T00:00:00+00:00",
        quota_gb=1,
        used_bytes=2_000_000_000,
    )

    assert jobs.evaluate([both], NOW) == {"both": "expired"}


def test_evaluate_naive_expires_at_matches_aware_now():
    # Storage normally carries an offset, but a naive ISO string must not
    # blow up with TypeError against an aware `now`.
    naive = _user("naive", expires_at="2025-06-01T00:00:00")

    assert jobs.evaluate([naive], NOW) == {"naive": "expired"}


# --- run_cycle ---------------------------------------------------------------


def test_run_cycle_updates_used_bytes(live_db):
    db.create_user(_user("alice"))
    stats = FakeStats(value=1000)

    assert jobs.run_cycle(live_db, FakeManager(), stats) == 0

    row = db.get_user("alice")
    assert row["used_bytes"] == 1000
    assert row["last_raw_bytes"] == 1000


def test_run_cycle_null_last_raw_defaults_to_zero(live_db):
    # AMENDED (plan line 360): NULL last_raw_bytes must be read as 0 before
    # compute_delta, never passed through as None.
    db.create_user(_user("alice", last_raw_bytes=None))
    stats = FakeStats(value=1000)

    assert jobs.run_cycle(live_db, FakeManager(), stats) == 0

    row = db.get_user("alice")
    assert row["used_bytes"] == 1000
    assert row["last_raw_bytes"] == 1000


def test_run_cycle_disables_and_reapplies_config(live_db):
    db.create_user(_user("goner", expires_at="2025-01-01T00:00:00+00:00"))
    db.create_user(_user("keeper", expires_at="2027-01-01T00:00:00+00:00"))
    manager = FakeManager()

    assert jobs.run_cycle(live_db, manager, FakeStats(value=5), now=NOW) == 1

    row = db.get_user("goner")
    assert row["enabled"] == 0
    assert row["disable_reason"] == "expired"

    # Config was reapplied exactly once, and the disabled uuid is not in it.
    assert len(manager.applied) == 1
    rendered = json.dumps(
        config_builder.build_config(manager.applied[-1], "tok", "host")
    )
    assert "uuid-goner" not in rendered
    assert "uuid-keeper" in rendered


def test_run_cycle_survives_stats_error(live_db):
    db.create_user(_user("alice", used_bytes=42))
    stats = FakeStats(error=StatsError("xray unreachable"))
    manager = FakeManager()

    # Non-fatal: no exception, no disables, rows untouched this tick.
    assert jobs.run_cycle(live_db, manager, stats, now=NOW) == 0

    row = db.get_user("alice")
    assert row["used_bytes"] == 42
    assert row["enabled"] == 1
    assert manager.applied == []


def test_run_cycle_enforces_expiry_even_when_stats_fails(live_db):
    # "Stats down, enforcement lives": a dead stats service must not stall
    # expiry enforcement — evaluate still runs and xray gets the update.
    db.create_user(_user("goner", expires_at="2025-01-01T00:00:00+00:00"))
    db.create_user(_user("keeper", expires_at="2027-01-01T00:00:00+00:00"))
    manager = FakeManager()
    stats = FakeStats(error=StatsError("xray unreachable"))

    assert jobs.run_cycle(live_db, manager, stats, now=NOW) == 1

    row = db.get_user("goner")
    assert row["enabled"] == 0
    assert row["disable_reason"] == "expired"
    assert db.get_user("keeper")["enabled"] == 1

    # apply_config was called exactly once, with the post-disable user list.
    assert len(manager.applied) == 1
    assert {u["id"]: u["enabled"] for u in manager.applied[-1]} == {
        "goner": 0,
        "keeper": 1,
    }


def test_run_cycle_logs_failed_apply_config(live_db, caplog):
    # The DB disable happens before the xray swap; a rejected swap means xray
    # keeps serving the old config — that divergence must be logged loudly.
    db.create_user(_user("goner", expires_at="2025-01-01T00:00:00+00:00"))
    manager = FakeManager(ok=False, msg="xray config test failed: INVALID_CONFIG")

    with caplog.at_level(logging.ERROR, logger="panel.jobs"):
        assert jobs.run_cycle(live_db, manager, FakeStats(value=1), now=NOW) == 1

    errors = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.ERROR
    ]
    assert any("xray config test failed" in msg for msg in errors), errors


# --- lifespan loop -----------------------------------------------------------


def test_lifespan_loop_schedules_ticks_and_cancels_cleanly(settings, monkeypatch):
    ticks = []
    calls = []

    def fake_run_cycle(cfg, manager, stats_client, now=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("simulated tick explosion")
        ticks.append((cfg, manager, stats_client))
        return 0

    monkeypatch.setattr(jobs, "TICK_SECONDS", 0.01)
    monkeypatch.setattr(jobs, "run_cycle", fake_run_cycle)

    with TestClient(create_app(settings)) as client:
        deadline = time.monotonic() + 5.0
        while not ticks and time.monotonic() < deadline:
            time.sleep(0.01)
        task = client.app.state.jobs_task
        stats_client = client.app.state.stats
        manager = client.app.state.manager

    assert len(calls) > 1 and ticks, "loop died after a failing first tick"
    assert ticks[0] == (settings, manager, stats_client)
    assert stats_client is not None  # one long-lived client lives on app.state
    assert task.done()  # shutdown cancelled AND awaited the task (no leak)
