"""XrayManager tests: validate-before-swap config lifecycle and process supervision."""

import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from panel.app import create_app
from panel.config import Settings
from panel.xray import config_builder
from panel.xray.manager import XrayManager

UUID = "11111111-2222-3333-4444-555555555555"
UUID2 = "22222222-3333-4444-5555-666666666666"


def _user(username: str = "alice", uuid: str = UUID) -> dict:
    return {
        "id": username,
        "username": username,
        "password": "pw",
        "protocol": "vless",
        "uuid": uuid,
        "credential": None,
        "ss_method": None,
        "enabled": 1,
        "created_at": "2026-01-01T00:00:00+00:00",
        "inbound_id": "vless-test",
    }


def _inbounds() -> list[dict]:
    """One inbound row the test users point at (fake xray never binds it)."""
    return [{
        "id": "vless-test",
        "protocol": "vless",
        "listen": "127.0.0.1",
        "port": 12345,
        "settings": {"transport": "tcp", "security": "none"},
        "enabled": 1,
    }]


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
def manager(settings):
    mgr = XrayManager(settings, public_host=settings.PUBLIC_HOST)
    yield mgr
    mgr.stop()


def _wait(predicate, timeout: float, interval: float = 0.1) -> bool:
    """Poll until predicate holds or the (bounded) timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_apply_config_accepts_valid_candidate(manager):
    ok, msg = manager.apply_config([_user()], _inbounds())
    assert ok, msg
    assert UUID in manager.config_path.read_text(encoding="utf-8")
    assert manager.is_running()
    assert manager.last_error == ""


def test_apply_config_rejects_invalid_candidate(manager, monkeypatch):
    # Apply a good config first, then try to sneak a broken one past the test.
    ok, msg = manager.apply_config([_user()], _inbounds())
    assert ok, msg
    before = manager.config_path.read_bytes()
    pid_before = manager.pid

    monkeypatch.setattr(
        config_builder,
        "build_config",
        lambda inbounds, users, public_host: {"INVALID_CONFIG": True},
    )

    ok, msg = manager.apply_config([_user()], _inbounds())
    assert not ok
    assert msg
    assert manager.config_path.read_bytes() == before  # old file untouched
    assert not manager.candidate_path.exists()  # candidate cleaned up
    assert not list(manager.config_path.parent.glob("config-*.next.json"))  # temps too
    assert manager.is_running()  # old process still up
    assert manager.pid == pid_before
    assert manager.last_error  # non-empty


def test_apply_config_safe_under_concurrency(manager, monkeypatch):
    # Two overlapping calls (as concurrent Tasks 7/8 request handlers would make),
    # each building the full user list. Both must succeed on their own candidate —
    # never test/swap a candidate the other call wrote, never double-consume one.
    users = [
        _user(username="alice", uuid=UUID),
        _user(username="bob", uuid=UUID2),
    ]
    real_test = manager.test_config_file
    both_written = threading.Barrier(2)

    def synced_test(path):
        try:
            both_written.wait(timeout=1)
        except threading.BrokenBarrierError:
            pass  # peers serialized behind the manager lock won't arrive
        return real_test(path)

    monkeypatch.setattr(manager, "test_config_file", synced_test)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(manager.apply_config, users, _inbounds()) for _ in range(2)]
        results = [f.result(timeout=15) for f in futures]

    assert all(ok for ok, _ in results), results
    live = manager.config_path.read_text(encoding="utf-8")
    assert UUID in live and UUID2 in live
    assert not list(manager.config_path.parent.glob("config-*.next.json"))


def test_apply_config_refused_after_stop(manager):
    ok, msg = manager.apply_config([_user()], _inbounds())
    assert ok, msg
    assert manager.pid is not None

    manager.stop()
    assert manager.pid is None
    config_before = manager.config_path.read_bytes()

    ok, msg = manager.apply_config([_user(username="bob", uuid=UUID2)], _inbounds())

    # A stopped manager must refuse, spawn nothing, and mutate nothing.
    assert (ok, msg) == (False, "manager stopped")
    assert manager.pid is None
    assert not manager.is_running()
    assert manager.config_path.read_bytes() == config_before


def test_apply_config_refused_from_other_thread_after_stop(manager):
    # The detached to_thread worker scenario: stop() first, then apply from
    # another thread — the ordering guard must refuse the late apply.
    ok, msg = manager.apply_config([_user()], _inbounds())
    assert ok, msg
    manager.stop()
    config_before = manager.config_path.read_bytes()

    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(
            manager.apply_config,
            [_user(username="bob", uuid=UUID2)],
            _inbounds(),
        ).result(timeout=15)

    assert result == (False, "manager stopped")
    assert manager.pid is None
    assert not manager.is_running()
    assert manager.config_path.read_bytes() == config_before


def test_start_after_stop_rearms_apply_config(manager):
    ok, _ = manager.apply_config([_user()], _inbounds())
    assert ok
    manager.stop()
    assert manager.apply_config([_user(username="bob", uuid=UUID2)], _inbounds()) == (
        False,
        "manager stopped",
    )

    # start() re-arms: first-boot and second-lifespan re-entry paths work.
    assert manager.start()
    assert manager.is_running()

    ok, msg = manager.apply_config([_user(username="bob", uuid=UUID2)], _inbounds())
    assert ok, msg
    assert UUID2 in manager.config_path.read_text(encoding="utf-8")
    assert manager.is_running()


def test_restart_failure_restores_backup_and_keeps_serving(manager, monkeypatch):
    ok, msg = manager.apply_config([_user()], _inbounds())
    assert ok, msg
    old_pid = manager.pid
    assert old_pid is not None
    pre_bytes = manager.config_path.read_bytes()

    # Candidate passes `-test` but the fake dies on `run` → restart fails →
    # the rollback branch must restore the backup and re-serve the old config.
    real_build = config_builder.build_config

    def build_dying(inbounds, users, public_host):
        cfg = real_build(inbounds, users, public_host)
        cfg["DIE_ON_RUN"] = True
        return cfg

    monkeypatch.setattr(config_builder, "build_config", build_dying)

    ok, msg = manager.apply_config([_user()], _inbounds())
    assert not ok, f"expected restart failure, got (True, {msg!r})"
    assert msg
    assert manager.config_path.read_bytes() == pre_bytes  # backup restored
    assert manager.is_running()  # a live process re-serves the old config
    assert manager.pid is not None and manager.pid != old_pid
    assert manager.last_error  # persists for the Task 9 banner


def test_start_spawns_process_and_stop_kills_it(manager):
    config_builder.write_config(
        manager.config_path,
        config_builder.build_config(_inbounds(), [_user()], manager.public_host),
    )
    assert manager.start()
    pid = manager.pid
    assert pid is not None
    assert manager.is_running()

    assert manager.start()  # second start must not double-spawn
    assert manager.pid == pid

    manager.stop()
    assert not manager.is_running()
    assert manager.pid is None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # reaped, not a zombie


def test_monitor_restarts_dead_process(manager, monkeypatch):
    monkeypatch.setattr(manager, "backoff_base", 0.1)
    config_builder.write_config(
        manager.config_path,
        config_builder.build_config(_inbounds(), [_user()], manager.public_host),
    )
    assert manager.start()
    manager.start_monitor()
    old_pid = manager.pid
    assert old_pid is not None

    os.kill(old_pid, signal.SIGKILL)

    restarted = _wait(
        lambda: manager.is_running() and manager.pid != old_pid, timeout=8.0
    )
    assert restarted, "monitor did not restart the dead process within 8 s"
    assert manager.pid != old_pid


def test_lifespan_starts_manager(settings):
    with TestClient(create_app(settings)) as client:
        mgr = client.app.state.manager
        assert mgr is not None
        assert mgr.is_running()
    assert not mgr.is_running()  # shutdown stopped the process
