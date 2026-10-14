"""Single-container stack integration tests (Task 11).

Only the Caddyfile renderer test runs anywhere; everything else is gated on
``IN_DOCKER`` because it needs the real binaries (xray, caddy) that live in
the image. Run the whole file for real with:

    docker compose build test && docker compose run --rm test
"""

import base64
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx2
import pytest

# Guard BEFORE importing panel.xray.stats (stats.py imports the pb stubs):
# a fresh checkout without generated stubs must skip with an actionable
# reason, not die with a raw ImportError at collection.
pytest.importorskip(
    "panel.xray.pb.command_pb2",
    reason="generated stubs missing — run: bash scripts/gen_protos.sh",
)

from panel import bootstrap, db
from panel.caddy import render_caddyfile
from panel.config import Settings
from panel.paths import PORTS
from panel.xray.config_builder import write_config
from panel.xray.stats import StatsClient

REPO_ROOT = Path(__file__).resolve().parent.parent
PANEL_PORT = 8000  # the Caddyfile's panel backend is pinned to this port

requires_container = pytest.mark.skipif(
    not os.environ.get("IN_DOCKER"),
    reason="needs the container stack (caddy + xray); "
    "run: docker compose run --rm test",
)


# --- unit-style: runs anywhere ---------------------------------------------


def test_render_caddyfile_routes_and_tokens():
    rendered = render_caddyfile(8080, "tok")
    assert "auto_https off" in rendered
    assert ":{$CADDY_PORT}" in rendered  # caddy binds this at runtime
    for protocol in ("vless", "vmess", "trojan", "ss"):
        assert f"/ray-{{$VPN_TOKEN}}/{protocol}*" in rendered
        assert f"reverse_proxy 127.0.0.1:{PORTS[protocol]}" in rendered
    # everything without a valid ray token falls through to the panel
    assert "handle {" in rendered
    assert "reverse_proxy 127.0.0.1:8000" in rendered
    # env placeholders are preserved ({$VAR} syntax), never bare-substituted
    assert rendered.count("{$VPN_TOKEN}") == rendered.count("$VPN_TOKEN")
    assert rendered.count("{$CADDY_PORT}") == rendered.count("$CADDY_PORT")


def test_render_caddyfile_auto_tls_serves_domain_with_https_on():
    rendered = render_caddyfile(
        8080, "tok", auto_tls=True, public_host="vpn.example.com"
    )
    # domain site address; Caddy's automatic HTTPS (LE via HTTP-01 on :80)
    # must stay enabled, and the plain-port listener must disappear
    assert "{$PUBLIC_HOST}" in rendered
    assert "auto_https off" not in rendered
    assert ":{$CADDY_PORT}" not in rendered
    # routing is identical to the plain mode
    for protocol in ("vless", "vmess", "trojan", "ss"):
        assert f"/ray-{{$VPN_TOKEN}}/{protocol}*" in rendered
        assert f"reverse_proxy 127.0.0.1:{PORTS[protocol]}" in rendered
    assert "handle {" in rendered
    assert "reverse_proxy 127.0.0.1:8000" in rendered
    assert rendered.count("{$VPN_TOKEN}") == rendered.count("$VPN_TOKEN")


def test_render_caddyfile_auto_tls_requires_a_bare_hostname():
    for bad in (
        "",
        "   ",
        "https://vpn.example.com",
        "http://vpn.example.com",
        "vpn.example.com:8443",
        "vpn.example.com/path",
        "vpn example com",
        "vpn.example.com{}",
    ):
        with pytest.raises(ValueError, match="PUBLIC_HOST"):
            render_caddyfile(8080, "tok", auto_tls=True, public_host=bad)


# --- helpers ---------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(port: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


@dataclass
class Stack:
    """The booted single-container stack, from the test's point of view."""

    data_dir: Path
    caddy_port: int
    token: str
    xray_bin: str


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    """Boot the real stack: uvicorn panel (spawns real xray) + real caddy.

    Caddy serves the Caddyfile that bootstrap.ensure_runtime writes, on its
    own test port; the panel and xray use their production wiring.
    """
    data_dir = tmp_path_factory.mktemp("stack-data")
    # Production wiring: the rendered Caddyfile hardcodes the panel backend
    # on 127.0.0.1:8000 (brief-pinned); only the public caddy port varies.
    panel_port = PANEL_PORT
    caddy_port = _free_port()
    bootstrap_settings_kwargs = dict(
        ADMIN_USERNAME="admin",
        ADMIN_PASSWORD="it-pass-12345",
        PUBLIC_HOST="localhost",
        DATA_DIR=str(data_dir),
        COOKIE_SECURE=False,
        XRAY_BIN=os.environ.get("XRAY_BIN", "xray"),
        ENV="test",
        PORT=panel_port,
    )

    settings = Settings(**bootstrap_settings_kwargs)
    bootstrap.ensure_runtime(settings)
    token = db.get_or_create_meta("install_token", lambda: uuid.uuid4().hex)

    panel_log = open(data_dir / "panel.out", "w")
    panel = subprocess.Popen(
        [sys.executable, "-m", "panel.main"],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "PORT": str(panel_port),
            "DATA_DIR": str(data_dir),
            "ADMIN_USERNAME": settings.ADMIN_USERNAME,
            "ADMIN_PASSWORD": settings.ADMIN_PASSWORD,
            "PUBLIC_HOST": "localhost",
            "COOKIE_SECURE": "0",
            "ENV": "test",
            "XRAY_BIN": settings.XRAY_BIN,
        },
        stdout=panel_log,
        stderr=subprocess.STDOUT,
    )
    if not _wait_for_port(panel_port, timeout_s=20):
        panel.terminate()
        panel_log.close()
        pytest.fail(
            f"panel did not listen on {panel_port} within 20s; log:\n"
            + (data_dir / "panel.out").read_text()
        )

    caddy_log = open(data_dir / "caddy.out", "w")
    caddy = subprocess.Popen(
        ["caddy", "run", "--config", str(data_dir / "Caddyfile"),
         "--adapter", "caddyfile"],
        cwd=REPO_ROOT,
        env={**os.environ, "CADDY_PORT": str(caddy_port), "VPN_TOKEN": token},
        stdout=caddy_log,
        stderr=subprocess.STDOUT,
    )
    if not _wait_for_port(caddy_port, timeout_s=10):
        caddy.terminate()
        panel.terminate()
        caddy_log.close()
        panel_log.close()
        pytest.fail(
            f"caddy did not listen on {caddy_port} within 10s; log:\n"
            + (data_dir / "caddy.out").read_text()
        )

    yield Stack(
        data_dir=data_dir,
        caddy_port=caddy_port,
        token=token,
        xray_bin=settings.XRAY_BIN,
    )

    caddy.terminate()
    try:
        caddy.wait(timeout=5)
    except subprocess.TimeoutExpired:
        caddy.kill()
        caddy.wait()
    panel.terminate()
    try:
        panel.wait(timeout=10)
    except subprocess.TimeoutExpired:
        panel.kill()
        panel.wait()
    caddy_log.close()
    panel_log.close()


def _admin_client(s: Stack) -> httpx2.Client:
    """Client through the caddy listener with an admin session cookie."""
    client = httpx2.Client(
        base_url=f"http://127.0.0.1:{s.caddy_port}", timeout=10.0
    )
    login = client.post(
        "/login",
        data={"username": "admin", "password": "it-pass-12345"},
        follow_redirects=False,
    )
    assert login.status_code == 302, login.text
    return client


def _create_user(client: httpx2.Client, protocol: str) -> dict:
    name = f"{protocol}-{uuid.uuid4().hex[:10]}"
    response = client.post("/api/users", json={"username": name,
                                               "protocol": protocol})
    assert response.status_code == 201, response.text
    return response.json()["user"]


def _ws_upgrade(port: int, path: str) -> str:
    """Raw websocket upgrade request; returns the first response bytes."""
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        f"GET {path} HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.settimeout(5)
        sock.sendall(request.encode())
        return sock.recv(4096).decode("latin-1")


def _upgrade_reaches_xray(port: int, path: str) -> bool:
    """101 within a few tries (user creation just restarted xray)."""
    last = ""
    for _ in range(10):
        try:
            last = _ws_upgrade(port, path)
        except OSError:
            last = "connection failed"
        if last.startswith("HTTP/1.1 101"):
            return True
        time.sleep(0.5)
    pytest.fail(f"no 101 on {path} via port {port}; last response:\n{last}")


def _start_echo_server(port: int) -> threading.Thread:
    """Sends a fixed banner to every accepted connection, then closes."""

    def serve() -> None:
        with socket.socket() as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", port))
            server.listen(4)
            while True:
                try:
                    conn, _ = server.accept()
                except OSError:
                    return
                with conn:
                    conn.sendall(b"HELLO-FROM-ECHO\n" * 32)
                    time.sleep(0.1)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return thread


# --- integration: real caddy + real xray -----------------------------------


@pytest.mark.integration
@requires_container
def test_ws_upgrade_direct_port(stack):
    """A2 topology: xray owns its VPN ports directly. A WS upgrade on the
    vmess seed's own port (path /) gets 101 from real xray — no caddy in
    the path (auth happens after the upgrade)."""
    assert _upgrade_reaches_xray(PORTS["vmess"], "/")


@pytest.mark.integration
@requires_container
def test_panel_via_caddy_and_vpn_on_direct_port(stack):
    """Caddy fronts only the dashboard; VPN ports are xray's own."""
    client = _admin_client(stack)
    page = client.get("/", follow_redirects=False)
    assert page.status_code in (200, 302), page.text
    assert _upgrade_reaches_xray(PORTS["vmess"], "/")


@pytest.mark.integration
@requires_container
def test_real_config_passes_real_xray(stack):
    """A user created through the live API ends up in a config the real
    `xray run -test` accepts (the manager already tested it; re-prove here)."""
    client = _admin_client(stack)
    user = _create_user(client, "trojan")
    config_path = stack.data_dir / "xray" / "config.json"  # XrayManager path
    assert user["uuid"] is None  # trojan user: credential, not uuid
    assert user["credential"] in config_path.read_text()
    result = subprocess.run(
        [stack.xray_bin, "run", "-test", "-c", str(config_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"xray rejected the live config:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.integration
@requires_container
def test_stats_grpc_roundtrip(stack):
    """Real StatsService gRPC roundtrip against the running xray."""
    client = _admin_client(stack)
    _create_user(client, "vless")  # ensure the in-vless inbound exists
    value = StatsClient().get_inbound_bytes("vless")  # 127.0.0.1:10010
    assert isinstance(value, int)
    assert value >= 0


@pytest.mark.integration
@requires_container
def test_user_traffic_counts_real_bytes(stack):
    """Drive REAL traffic through a ws inbound via a second xray instance as
    the client; the per-user counter must exceed 0 — this empirically locks
    the `user>>>{email}>>>traffic>>>up|downlink` literal in stats.py."""
    client = _admin_client(stack)
    user = _create_user(client, "vmess")  # lands on the vmess seed inbound

    echo_port = _free_port()
    _start_echo_server(echo_port)

    # Client xray: dokodemo-door TCP listener -> vmess-ws outbound to the
    # seed inbound's OWN port. Connecting to client_port tunnels to the echo.
    client_port = _free_port()
    client_config = {
        "log": {"loglevel": "error"},
        "inbounds": [{
            "listen": "127.0.0.1",
            "port": client_port,
            "protocol": "dokodemo-door",
            "settings": {"address": "127.0.0.1", "port": echo_port},
        }],
        "outbounds": [{
            "protocol": "vmess",
            "settings": {
                "vnext": [{
                    "address": "127.0.0.1",
                    "port": PORTS["vmess"],
                    "users": [{"id": user["uuid"], "alterId": 0,
                               "security": "auto"}],
                }]
            },
            "streamSettings": {
                "network": "ws",
                "security": "none",
                # The seed inbound sets headers.Host = PUBLIC_HOST; the
                # client must send the same host header.
                "wsSettings": {"path": "/", "host": "localhost"},
            },
        }],
    }
    client_config_path = stack.data_dir / "xray-client.json"
    write_config(client_config_path, client_config)
    client_log_path = stack.data_dir / "xray-client.out"
    client_log = open(client_log_path, "w")
    xray_client = subprocess.Popen(
        [stack.xray_bin, "run", "-c", str(client_config_path)],
        start_new_session=True,
        stdout=client_log,
        stderr=subprocess.STDOUT,
    )
    try:
        assert _wait_for_port(client_port, timeout_s=10), (
            "client xray did not listen"
        )
        stats = StatsClient()
        banner_received = False
        total = 0
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(
                    ("127.0.0.1", client_port), timeout=2
                ) as sock:
                    sock.settimeout(2)
                    received = b""
                    while len(received) < 4096:
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        received += chunk
                if received.startswith(b"HELLO-FROM-ECHO"):
                    banner_received = True
            except OSError:
                pass
            total = stats.get_bytes(user["id"])
            if banner_received and total > 0:
                break
            time.sleep(0.5)
    finally:
        try:  # start_new_session -> pgid == pid
            os.killpg(xray_client.pid, 15)
        except ProcessLookupError:
            pass
        try:
            xray_client.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(xray_client.pid, 9)
            xray_client.wait()
        client_log.close()

    assert banner_received, (
        "tunneled traffic never reached the echo server; client xray log:\n"
        + client_log_path.read_text()
        + "\nserver xray log:\n"
        + (stack.data_dir / "xray" / "xray.log").read_text()
    )
    assert total > 0, (
        "tunneled traffic flowed but the per-user counter stayed 0 — "
        "the stat_name literal in panel/xray/stats.py is wrong"
    )
    # provenance for the report: the real counter value this run observed
    print(f"observed per-user counter: {total} bytes")


@pytest.mark.integration
@requires_container
def test_reality_e2e_client_connect(stack):
    """Spec §10 headline: a real xray client connects to the REALITY inbound
    (seeded vless on :443) and pulls real bytes through it."""
    client = _admin_client(stack)
    user = _create_user(client, "vless")  # auto-assigned to the vless seed
    seed = json.loads(db.get_inbound("vless")["settings"])

    echo_port = _free_port()
    _start_echo_server(echo_port)

    client_port = _free_port()
    client_config = {
        "log": {"loglevel": "error"},
        "inbounds": [{
            "listen": "127.0.0.1",
            "port": client_port,
            "protocol": "dokodemo-door",
            "settings": {"address": "127.0.0.1", "port": echo_port},
        }],
        "outbounds": [{
            "protocol": "vless",
            "settings": {
                "vnext": [{
                    "address": "127.0.0.1",
                    "port": 443,
                    "users": [{"id": user["uuid"], "encryption": "none",
                               "flow": "xtls-rprx-vision"}],
                }]
            },
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "serverName": seed["server_names"][0],
                    "fingerprint": seed.get("fp", "chrome"),
                    "publicKey": seed["public_key"],
                    "shortId": seed["short_ids"][0],
                },
            },
        }],
    }
    client_config_path = stack.data_dir / "xray-reality-client.json"
    write_config(client_config_path, client_config)
    client_log_path = stack.data_dir / "xray-reality-client.out"
    client_log = open(client_log_path, "w")
    xray_client = subprocess.Popen(
        [stack.xray_bin, "run", "-c", str(client_config_path)],
        start_new_session=True,
        stdout=client_log,
        stderr=subprocess.STDOUT,
    )
    try:
        assert _wait_for_port(client_port, timeout_s=10), (
            "reality client xray did not listen"
        )
        banner_received = False
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(
                    ("127.0.0.1", client_port), timeout=2
                ) as sock:
                    sock.settimeout(2)
                    if sock.recv(64).startswith(b"HELLO-FROM-ECHO"):
                        banner_received = True
                        break
            except OSError:
                pass
            time.sleep(0.5)
    finally:
        try:  # start_new_session -> pgid == pid
            os.killpg(xray_client.pid, 15)
        except ProcessLookupError:
            pass
        try:
            xray_client.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(xray_client.pid, 9)
            xray_client.wait()
        client_log.close()

    assert banner_received, (
        "REALITY-tunneled traffic never reached the echo server; "
        "client xray log:\n" + client_log_path.read_text()
        + "\nserver xray log:\n"
        + (stack.data_dir / "xray" / "xray.log").read_text()
    )


# --- entrypoint lifecycle: runs anywhere (bash + python, fake caddy) -------


# A stand-in caddy: "die" exits at startup, "serve" answers on CADDY_PORT,
# "serve-then-die" exits 2 s after binding (an OOM-style mid-run death).
_FAKE_CADDY = """\
import http.server
import os
import socketserver
import sys
import threading

mode = os.environ.get("FAKE_CADDY_MODE", "serve")
if mode == "die":
    sys.stderr.write("fake caddy: dying immediately\\n")
    sys.exit(1)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


server = socketserver.TCPServer(
    ("127.0.0.1", int(os.environ["CADDY_PORT"])), Handler
)
if mode == "serve-then-die":
    threading.Timer(2.0, lambda: os._exit(1)).start()
server.serve_forever()
"""


def _entrypoint_env(tmp_path, panel_port: int, caddy_port: int,
                    caddy_mode: str) -> dict:
    """Env for a real entrypoint.sh run with a fake caddy first on PATH."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    # A sh wrapper, not a shebang: the repo path (and venv inside it) may
    # contain spaces, which shebang lines cannot express.
    (bin_dir / "fake_caddy.py").write_text(_FAKE_CADDY)
    shim = bin_dir / "caddy"
    shim.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir}/fake_caddy.py" "$@"\n'
    )
    shim.chmod(0o755)
    return {
        **os.environ,
        "PATH": (f"{bin_dir}:{Path(sys.executable).parent}:"
                 f"{os.environ['PATH']}"),
        "DATA_DIR": str(tmp_path / "data"),
        "PANEL_PORT": str(panel_port),
        "CADDY_PORT": str(caddy_port),
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD": "entrypoint-test-123",
        "PUBLIC_HOST": "localhost",
        "COOKIE_SECURE": "0",
        "ENV": "test",
        "XRAY_BIN": os.environ.get("XRAY_BIN", "xray"),
        "FAKE_CADDY_MODE": caddy_mode,
    }


def _wait_for_port_dead(port: int, timeout_s: float = 5) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                pass
        except OSError:
            return True
        time.sleep(0.1)
    return False


def test_entrypoint_fails_fast_when_caddy_dies_at_startup(tmp_path):
    """A caddy that dies at startup (bad config, port in use) must fail the
    container — never leave a panel-only container that looks healthy."""
    env = _entrypoint_env(tmp_path, _free_port(), _free_port(), "die")
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "entrypoint.sh")],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=25,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "caddy not listening" in result.stderr
    assert _wait_for_port_dead(int(env["PANEL_PORT"])), (
        "orphan panel still listening after entrypoint exit"
    )


def test_entrypoint_tears_down_when_caddy_dies_at_runtime(tmp_path):
    """Mid-run caddy death (OOM-style) must also tear the container down:
    whichever child dies first ends the container with a non-zero code."""
    env = _entrypoint_env(
        tmp_path, _free_port(), _free_port(), "serve-then-die"
    )
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "entrypoint.sh")],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert _wait_for_port_dead(int(env["PANEL_PORT"])), (
        "orphan panel still listening after entrypoint exit"
    )


def test_entrypoint_sigterm_stops_cleanly_with_exit_143(tmp_path):
    """SIGTERM (the Railway stop) still stops everything promptly: the
    entrypoint exits 143 and neither child is left listening."""
    panel_port, caddy_port = _free_port(), _free_port()
    env = _entrypoint_env(tmp_path, panel_port, caddy_port, "serve")
    proc = subprocess.Popen(
        ["bash", str(REPO_ROOT / "entrypoint.sh")],
        cwd=REPO_ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        assert _wait_for_port(caddy_port, timeout_s=20), "caddy never up"
        proc.terminate()  # SIGTERM, as Railway sends to PID 1
        stdout, _ = proc.communicate(timeout=10)
        assert proc.returncode == 143, stdout
        assert _wait_for_port_dead(panel_port), "orphan panel still listening"
        assert _wait_for_port_dead(caddy_port), "orphan caddy still listening"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
