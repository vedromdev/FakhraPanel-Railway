"""Xray process supervision: validate → atomic swap → restart, with rollback.

A config change can never brick the running VPN: the candidate file is tested
with `xray run -test` first, swapped in atomically only on success, and the
previous config is restored (and re-served) if the restart then fails.
"""

import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from panel.config import Settings
from panel.xray import config_builder

TEST_TIMEOUT_S = 10.0  # `xray run -test` must answer within this
STOP_TIMEOUT_S = 5.0  # SIGTERM grace before SIGKILL
SPAWN_GRACE_S = 0.05  # catch processes that die on spawn
MONITOR_INTERVAL_S = 5.0  # monitor tick
BACKOFF_BASE_S = 1.0  # restart delay: 1, 2, 4, ... capped at BACKOFF_MAX_S
BACKOFF_MAX_S = 30.0


class XrayManager:
    """Owns the xray config files (config.json + candidate + backup) and process."""

    def __init__(self, settings: Settings, install_token: str, public_host: str) -> None:
        self.settings = settings
        self.install_token = install_token
        self.public_host = public_host
        self.last_error = ""
        # tunables (tests monkeypatch backoff_base)
        self.backoff_base = BACKOFF_BASE_S
        self.backoff_max = BACKOFF_MAX_S
        self.monitor_interval = MONITOR_INTERVAL_S

        self._xray_dir = settings.data_dir() / "xray"
        self._xray_dir.mkdir(parents=True, exist_ok=True)
        # RLock: apply_config holds it across restart()/start(), which re-enter.
        self._lock = threading.RLock()
        # Running-ready by default: the first-boot path calls apply_config
        # before start(). Set True by stop(), cleared by start() — a stopped
        # manager must not spawn or mutate config (see apply_config/_spawn).
        self._stopped: bool = False
        self._proc: subprocess.Popen | None = None
        self._log_fh = None
        self._started_at: float | None = None
        self._monitor_stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None

    @property
    def config_path(self) -> Path:
        return self._xray_dir / "config.json"

    @property
    def candidate_path(self) -> Path:
        return self._xray_dir / "config.next.json"

    @property
    def backup_path(self) -> Path:
        return self._xray_dir / "config.json.bak"

    @property
    def log_path(self) -> Path:
        return self._xray_dir / "xray.log"

    @property
    def pid(self) -> int | None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return None
        return proc.pid

    def test_config_file(self, path: Path) -> tuple[bool, str]:
        """Run `[XRAY_BIN, "run", "-test", "-c", path]`; combined output, 10 s cap."""
        cmd = [self.settings.XRAY_BIN, "run", "-test", "-c", str(path)]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=TEST_TIMEOUT_S
            )
        except subprocess.TimeoutExpired:
            return False, f"xray config test timed out after {TEST_TIMEOUT_S:g}s"
        except OSError as exc:
            return False, f"cannot run {self.settings.XRAY_BIN}: {exc}"
        output = f"{proc.stdout or ''}{proc.stderr or ''}".strip()
        if proc.returncode == 0:
            return True, output
        return False, output or f"xray config test failed (exit {proc.returncode})"

    def apply_config(self, users: list[dict]) -> tuple[bool, str]:
        """Build → write candidate → test → atomic swap → restart.

        Serialized by the manager lock so concurrent callers can't share (and
        lose) the candidate file. Never raises for expected failures (bad
        config, restart failure): always returns (success, message) and
        records `last_error`.
        """
        try:
            with self._lock:
                if self._stopped:
                    # stop() is terminal for this running period: a detached
                    # jobs worker arriving late must not respawn xray or
                    # touch config files after shutdown.
                    return False, "manager stopped"
                return self._apply_config(users)
        except Exception as exc:  # contract: callers get (False, reason), not a raise
            self.last_error = f"apply_config failed: {exc}"
            return False, self.last_error

    def _apply_config(self, users: list[dict]) -> tuple[bool, str]:
        cfg = config_builder.build_config(
            users, self.install_token, self.public_host
        )
        # Unique candidate per call, in the same dir as config.json so the
        # swap below stays a same-filesystem atomic os.replace.
        fd, name = tempfile.mkstemp(
            dir=self._xray_dir, prefix="config-", suffix=".next.json"
        )
        os.close(fd)
        candidate = Path(name)
        try:
            config_builder.write_config(candidate, cfg)

            ok, output = self.test_config_file(candidate)
            if not ok:
                self.last_error = output  # old config untouched
                return False, output

            # Test passed: keep the current file as backup, then swap atomically.
            had_previous = self.config_path.exists()
            if had_previous:
                shutil.copy2(self.config_path, self.backup_path)
            os.replace(candidate, self.config_path)

            if not self.restart():
                reason = self.last_error or "xray restart failed"
                if had_previous:
                    shutil.copy2(self.backup_path, self.config_path)  # restore
                    # last_error intentionally persists after a successful
                    # rollback: the old config keeps serving while the error
                    # stays visible for the Task 9 banner.
                    self.start()  # keep the old config serving
                return False, reason

            self.last_error = ""
            return True, "ok"
        finally:
            candidate.unlink(missing_ok=True)  # consumed by replace; else stale

    def start(self) -> bool:
        """Spawn xray with the current config; no double-spawn.

        Also re-arms a stopped manager (clears `_stopped`) so second-lifespan
        and test re-entry paths work.
        """
        with self._lock:
            self._stopped = False
            if self.is_running():
                return True
            self._stop_process()  # reap a dead predecessor, close its log handle
            return self._spawn()

    def stop(self) -> None:
        """Stop the monitor thread, then SIGTERM (5 s) and SIGKILL the process."""
        self._monitor_stop.set()
        thread = self._monitor_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._lock:
            self._stopped = True  # before killing: lock waiters see it on entry
            self._stop_process()

    def restart(self) -> bool:
        with self._lock:
            self._stop_process()
            return self._spawn()

    def is_running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def uptime_s(self) -> float | None:
        if not self.is_running() or self._started_at is None:
            return None
        return time.monotonic() - self._started_at

    def start_monitor(self) -> None:
        """Daemon thread: every `monitor_interval` s, restart a dead xray with
        backoff (1→2→4… capped at `backoff_max`); never touches a healthy one."""
        thread = self._monitor_thread
        if thread is not None and thread.is_alive():
            return
        self._monitor_stop.clear()
        thread = threading.Thread(
            target=self._monitor_loop, name="xray-monitor", daemon=True
        )
        self._monitor_thread = thread
        thread.start()

    def _monitor_loop(self) -> None:
        failures = 0
        while not self._monitor_stop.is_set():
            if self._monitor_stop.wait(self.monitor_interval):
                break
            with self._lock:
                if self.is_running():
                    failures = 0
                    continue
                delay = min(self.backoff_base * (2 ** failures), self.backoff_max)
                failures += 1
                self.last_error = (
                    f"xray not running; restart attempt in {delay:g}s "
                    f"(failure {failures})"
                )
            if self._monitor_stop.wait(delay):
                break
            with self._lock:
                if self._monitor_stop.is_set() or self._stopped:
                    break
                if self.is_running():
                    continue
                self._stop_process()  # reap dead proc + close its log handle
                if self._spawn():
                    self.last_error = ""

    def _spawn(self) -> bool:
        if self._stopped:  # belt and braces: never respawn a stopped manager
            self.last_error = "xray manager stopped; refusing to spawn"
            return False
        if not self.config_path.exists():
            self.last_error = f"config not found: {self.config_path}"
            return False
        log_fh = None
        try:
            log_fh = open(self.log_path, "ab")
            proc = subprocess.Popen(
                [self.settings.XRAY_BIN, "run", "-c", str(self.config_path)],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            if log_fh is not None:
                log_fh.close()
            self.last_error = f"failed to start xray: {exc}"
            return False
        self._proc = proc
        self._log_fh = log_fh
        self._started_at = time.monotonic()
        time.sleep(SPAWN_GRACE_S)
        if proc.poll() is not None:  # died on spawn
            proc.wait()
            log_fh.close()
            self._proc = None
            self._log_fh = None
            self._started_at = None
            self.last_error = (
                f"xray exited immediately (code {proc.returncode}); "
                f"see {self.log_path}"
            )
            return False
        return True

    def _stop_process(self) -> None:
        proc, log_fh = self._proc, self._log_fh
        self._proc = None
        self._log_fh = None
        self._started_at = None
        if proc is None:
            if log_fh is not None:
                log_fh.close()
            return
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)  # start_new_session → pgid == pid
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + STOP_TIMEOUT_S
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        proc.wait()  # reap; never leave a zombie
        if log_fh is not None:
            log_fh.close()
