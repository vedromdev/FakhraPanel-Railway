# Fakhra Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Railway-deployable VPN panel that creates working V2Ray (Xray) VPN accounts across VLESS/VMess/Trojan/Shadowsocks over a single exposed port, with admin + self-service dashboards, expiry, quotas, and live traffic stats.

**Architecture:** One Docker container on one Railway port: Caddy front-multiplexes the VPN namespace (`/ray-<token>/<proto>`) to four localhost Xray inbounds and everything else to a FastAPI panel. The panel owns the Xray process lifecycle (validate with `xray run -test` → atomic swap → restart → rollback on failure), polls Xray's gRPC StatsService for usage, and enforces expiry/quota. Data lives in WAL-mode SQLite under `DATA_DIR` (`/data` on Railway).

**Tech Stack:** Python 3.12, FastAPI, uvicorn, Jinja2, pydantic-settings, SQLite (stdlib), bcrypt, grpcio (generated stubs from vendored Xray protos), Xray-core (pinned release), Caddy, Docker, pytest + fastapi TestClient.

**Spec:** `docs/superpowers/specs/2026-10-13-fakhra-panel-design.md` — the plan argues from the spec; executors read both.

## Global Constraints

- Deploy target: Railway **free plan** — must run as ONE service, ONE exposed port (`$PORT`, default 8080), within 1 vCPU / 0.5 GB RAM; volume mounted at `/data` (0.5 GB cap); data must also work when `/data` is absent (fallback `./data`).
- Share links ALWAYS: host=`PUBLIC_HOST`, port=`443`, `security=tls`, `sni=PUBLIC_HOST`, `type=ws`, path=`/ray-<install_token>/<protocol>`.
- `ADMIN_USERNAME` and `ADMIN_PASSWORD` are required settings — constructing `Settings()` without them must raise; no default credentials ever run.
- Internal ports (loopback only): panel `127.0.0.1:8000`; Xray inbounds vless `10001`, vmess `10002`, trojan `10003`, ss `10004`; Xray stats API `127.0.0.1:10010`.
- All four protocols over WebSocket transport; Shadowsocks inbound `network: "tcp"` (Xray SS-over-ws limitation).
- Config lifecycle: candidate written → `xray run -test -c` passes → atomic `os.replace` → restart; on any failure restore previous config and record `last_error`. A bad change must never take the VPN down.
- Stats: poll every 30 s; `used_bytes += raw - last_raw` normally, `used_bytes += raw` when `raw < last_raw` (counter reset) — spec §4.
- Sessions: opaque random tokens stored only in hashed form (HMAC-SHA256 keyed by the persisted secret key); cookies `fakhra_admin` and `fakhra_user` (separate); unauthenticated → `401` JSON for `/api/*`, `302` redirect for HTML.
- SQLite in WAL mode. All SQL parameterized.
- Docker runs `python:3.12-slim`; `XRAY_VERSION` pinned in Dockerfile and vendored `proto/` files come from that same release.
- Dependencies: `bcrypt` used directly (no passlib); custom in-memory rate limiter (no slowapi). `jinja2` and `python-multipart` MUST be in requirements (old project's bug).
- Old project (`Fakhrayi Panel` folder) is never imported, imported-from, or modified.

## Review Focus

Five inputs/failures most likely to bite a user of this software, and the task test that pins each:

1. **Share link carries wrong host/port/security** (e.g. internal port instead of 443) → client silently can't connect while the dashboard looks green. → Task 4 `test_link_fields_pinned_all_protocols` asserts 443/tls/sni/path for all four formats.
2. **Caddy routing** swallows a panel path or fails the WS upgrade → VPN dead while UI works (or vice versa). → Task 11 `test_ws_upgrade_reaches_xray` (expects HTTP 101 on the ray path) + `test_dashboard_and_vpn_share_one_port`.
3. **Stats counter reset or wrong stat name** → user wrongly disabled for "quota" or usage never moves. → Task 6 `test_compute_delta_*` (normal/reset/first-seen) + Task 7 `test_evaluate_quota_boundary`; live counter movement is DEPLOY.md's manual E2E step.
4. **Invalid config swapped in** → running VPN bricked. → Task 5 `test_apply_config_rejects_invalid_candidate` (fake xray fails `-test` → old file byte-identical, old process still running, `last_error` set).
5. **Self-service privilege leak** (user cookie reaches admin API, or user A sees user B's link). → Task 10 `test_user_cookie_cannot_reach_admin_api` + `test_account_page_excludes_other_users`.

---

### Task 1: Settings, data directory & SQLite foundation

**Files:**
- Create: `panel/config.py`, `panel/db.py`, `panel/bootstrap.py`, `panel/__init__.py`
- Test: `tests/test_settings.py`, `tests/test_db.py`, `tests/conftest.py`

**Interfaces:**
- Produces (consumed by ALL later tasks):
  - `Settings` (pydantic-settings BaseSettings) — fields per spec §8: `PORT: int = 8080`, `ADMIN_USERNAME: str` (no default), `ADMIN_PASSWORD: str` (no default), `SECRET_KEY: str = ""`, `PUBLIC_HOST: str = ""`, `DATA_DIR: str = ""`, `XRAY_BIN: str = "xray"`, `COOKIE_SECURE: bool = True`, `ENV: str = "development"`, `DASHBOARD_TITLE: str = "Fakhra Panel"`; plus method `data_dir() -> Path`: `Path(DATA_DIR)` if set, else `Path("/data")` if `os.path.isdir("/data")`, else `Path("./data")`.
  - `get_settings() -> Settings` (lru_cache; tests do NOT use it — they construct `Settings(**overrides)` directly; pydantic init kwargs override env).
  - `db.init_db(db_path: Path) -> None` — sets module `_db_path`, creates tables `users`, `admin_sessions`, `user_sessions`, `meta` per spec §3 (columns incl. `disable_reason TEXT`, `password TEXT` holding bcrypt hash, `last_raw_bytes INTEGER DEFAULT 0`), sets `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`.
  - `db.create_user(data: dict) -> dict`, `db.create_users(rows: list[dict]) -> list[dict]` (single transaction; `sqlite3.IntegrityError` on username conflict → rollback, DB unchanged), `db.list_users() -> list[dict]`, `db.get_user(user_id: str) -> dict | None`, `db.update_user(user_id: str, fields: dict) -> dict | None` (no-op guard for empty `fields`), `db.delete_user(user_id: str) -> bool`.
  - `db.create_session(kind: Literal["admin","user"], user_id: str | None, ttl_days: int) -> str` — returns raw token `secrets.token_urlsafe(32)`; stores `hmac-sha256(token, secret_key_from_meta)` hex digest + expiry. `db.get_session(kind, raw_token: str) -> dict | None` (hashes the raw token before lookup; returns None if expired). `db.delete_session(kind, raw_token: str) -> None`.
  - `db.get_or_create_meta(key: str, factory: Callable[[], str]) -> str`.
  - `bootstrap.ensure_runtime(settings: Settings) -> None` — mkdir data dir + `xray/` subdir, `init_db(settings.data_dir() / "panel.db")`, `get_or_create_meta("install_token", lambda: secrets.token_hex(16))`, `get_or_create_meta("secret_key", lambda: secrets.token_urlsafe(32))` (used for session hashing).

- [ ] **Step 1: Write failing tests**

`tests/conftest.py`: fixture `settings(tmp_path)` → `Settings(ADMIN_USERNAME="admin", ADMIN_PASSWORD="test-pass-123", PUBLIC_HOST="fakhra.railway.app", DATA_DIR=str(tmp_path), COOKIE_SECURE=False, XRAY_BIN="xray", ENV="test")`.

`tests/test_settings.py`:
- `test_data_dir_prefers_explicit_env` — `Settings(DATA_DIR="/tmp/x", ...)` → `data_dir() == Path("/tmp/x")`.
- `test_data_dir_falls_back_when_missing` — `DATA_DIR=""` and `/data` not a dir → `data_dir() == Path("./data")` (monkeypatch `os.path.isdir` to force branches).
- `test_missing_admin_password_fails_fast` — `pytest.raises(ValidationError)` when constructing `Settings()` with `ADMIN_PASSWORD` removed from env (`monkeypatch.delenv`).

`tests/test_db.py` (setup: `init_db` via `ensure_runtime(settings)`):
- `test_create_and_get_user_roundtrip` — create vless user dict → `get_user` returns all spec §3 fields incl. `enabled == 1`, `used_bytes == 0`.
- `test_username_unique_violation_creates_nothing` — `create_users([good, duplicate])` raises `sqlite3.IntegrityError`, `len(list_users())` unchanged.
- `test_update_user_sets_expiry_and_quota` — `update_user(id, {"expires_at": ..., "quota_gb": 50.0})` round-trips.
- `test_install_token_stable_across_restarts` — `ensure_runtime(settings)` twice → same `install_token`; `panel.db` exists under data dir.
- `test_session_roundtrip_and_expiry` — `create_session("admin", None, ttl_days=1)` → `get_session` with raw token returns row; wrong token → None; session with past expiry (insert directly) → None.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "Fakhra Panel" && python3 -m pytest tests/ -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'panel'` / missing files.

- [ ] **Step 3: Implement `panel/config.py`, `panel/db.py`, `panel/bootstrap.py`, `panel/__init__.py`**

Signatures above. Notes: `model_config = SettingsConfigDict(env_file=".env", extra="ignore")`; sessions store hashed token only; `create_users` wraps inserts in one transaction with rollback on `IntegrityError`; session hashing uses `get_or_create_meta("secret_key", ...)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/ -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add panel/ tests/ requirements.txt requirements-dev.txt
git commit -m "feat: settings, data dir, and SQLite user/session foundation"
```

(Create `requirements.txt` pinned: `fastapi`, `uvicorn[standard]`, `pydantic`, `pydantic-settings`, `jinja2`, `python-multipart`, `bcrypt`, `qrcode[pil]`, `grpcio` — exact pins chosen at install time; `requirements-dev.txt`: `pytest`, `httpx`, `grpcio-tools`. Also create `pytest.ini` or `pyproject` test config so `python3 -m pytest` finds the `panel` package.)

---

### Task 2: App factory, auth & login

**Files:**
- Create: `panel/auth.py`, `panel/ratelimit.py`, `panel/app.py`, `panel/routes_auth.py`, `panel/main.py`, `panel/templates/base.html`, `panel/templates/login.html`
- Test: `tests/test_auth.py`, `tests/test_ratelimit.py`

**Interfaces:**
- Consumes: `Settings`, `db.create_session/get_session/delete_session`, `bootstrap.ensure_runtime` (Task 1).
- Produces:
  - `auth.authenticate_admin(username: str, password: str, settings: Settings) -> bool` — `secrets.compare_digest` on both values (constant-time, env-sourced secret).
  - `auth.hash_password(plain: str) -> str` and `auth.verify_password(plain: str, hashed: str) -> bool` (bcrypt; used from Task 8 on).
  - `auth.require_admin(request) -> dict` and `auth.require_user(request) -> dict` — FastAPI dependencies; raise `HTTPException(401)` when the corresponding cookie session is missing/invalid.
  - `ratelimit.RateLimiter(window_s: int = 60, max_calls: int = 5)` with `allow(key: str) -> bool` (in-memory sliding window; injectable `clock: Callable[[], float] = time.monotonic`).
  - `app.create_app(settings: Settings) -> FastAPI` — lifespan: `ensure_runtime(settings)`, set `app.state.settings`, `app.state.rate = RateLimiter()`, `app.state.manager` (placeholder until Task 5 wires it); registers a `401` exception handler: request path starts with `/api` → `JSONResponse({"detail": "Not authenticated"}, 401)`; else `302` to `/login` (or `/account` when path starts with `/account`).
  - Module-level `app = create_app(get_settings())` lives ONLY in `panel/main.py` (importing `panel.app` in tests must not read env or spawn processes).
  - Routes: `GET /login`, `POST /login` (form fields `username`,`password`; rate-limit key = client IP; failure → re-render with `error`, no cookie; success → set `fakhra_admin` cookie (HttpOnly, SameSite=Lax, `secure=settings.COOKIE_SECURE`, path `/`, max_age 604800) and `302` → `/`), `GET /logout` (clear admin cookie, `302` → `/login`). Placeholder `GET /api/users` behind `require_admin` returning `{"users": []}` (replaced by Task 8).
  - `panel/main.py`: `python -m panel.main` runs uvicorn on `settings.PORT`, host `0.0.0.0`.

- [ ] **Step 1: Write failing tests**

`tests/test_ratelimit.py`:
- `test_sixth_call_in_window_denied` — 5× `allow("ip")` → True, 6th → False.
- `test_window_expiry_allows_again` — fake clock advances 61 s → True.

`tests/test_auth.py` (fixture `client = TestClient(create_app(settings))`):
- `test_wrong_password_rejected` — POST `/login` wrong password → 200, body contains `Invalid`, no `fakhra_admin` cookie in response.
- `test_correct_password_sets_admin_cookie_and_redirects` — 302 `Location: /`, cookie `fakhra_admin` present, HttpOnly flag set.
- `test_root_redirects_to_login_when_unauthenticated` — GET `/` → 302 `/login`.
- `test_api_returns_401_json_when_unauthenticated` — GET `/api/users` → 401, JSON content-type.
- `test_login_rate_limited_on_sixth_attempt` — 6 POSTs wrong password → 6th returns 429.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ratelimit.py tests/test_auth.py -v`
Expected: FAIL — missing modules/routes.

- [ ] **Step 3: Implement auth, ratelimit, app factory, login routes, templates**

Follow Interfaces exactly. `login.html` extends `base.html`; minimal styling (style.css arrives in Task 9). 401 handler via `app.add_exception_handler(401, ...)` inspecting `request.url.path`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/ -v`
Expected: PASS (Task 1 + Task 2 suites).

- [ ] **Step 5: Commit**

```bash
git add panel/ tests/ requirements*.txt
git commit -m "feat: app factory, admin auth, login flow, rate limiting"
```

---

### Task 3: Paths & Xray config builder (4 protocols × ws)

**Files:**
- Create: `panel/paths.py`, `panel/xray/__init__.py`, `panel/xray/config_builder.py`, `scripts/fetch_xray.sh`
- Test: `tests/test_config_builder.py`

**Interfaces:**
- Consumes: `db.list_users()` shape (Task 1) — builder takes `users` as a plain parameter, no DB coupling.
- Produces:
  - `paths.PORTS = {"vless": 10001, "vmess": 10002, "trojan": 10003, "ss": 10004}`
  - `paths.vpn_path(install_token: str, protocol: str) -> str` → `f"/ray-{install_token}/{protocol}"`
  - `config_builder.build_config(users: list[dict], install_token: str, public_host: str) -> dict` — full Xray config: `log` (level `error`), `stats`, `policy` with `levels["0"] = {"statsUserUplink": true, "statsUserDownlink": true}` and `system` stats; `api.services = ["StatsService"]`; API access inbound `{"listen":"127.0.0.1","port":10010,"protocol":"dokodemo-door","settings":{"address":"127.0.0.1"},"tag":"api"}` + outbound `{"protocol":"api","tag":"api"}` + routing rule `{"type":"field","inboundTag":["api"],"outboundTag":"api"}` (AMENDED 2026-10-14: original plan had `"protocol":["api"]` — dropped after a real-binary probe showed Xray 26 has no "api" protocol sniffer, so the condition never matched, api traffic looped through freedom to DEADLINE_EXCEEDED, and StatsService was unreachable; Xray's own command_test.go wires the api inboundTag-only; GetStats verified working after the drop); ONE inbound per protocol having ≥1 **enabled** user (tag `in-<proto>`, port `paths.PORTS[proto]`), protocol omitted entirely when no enabled users; every client entry has `"level": 0` and `"email": user_id`; inbound `streamSettings = {"network":"ws","security":"none","wsSettings":{"path":vpn_path(...),"headers":{"Host": public_host}}}` + sniffing enabled; protocol settings: vless (`"decryption":"none"`; AMENDED 2026-10-14: client `"encryption":"none"` dropped — real xray run -test rejected it in server config), vmess (AMENDED 2026-10-14: no alterId — Xray is AEAD-only; test asserts absence), trojan (client `"password"`), ss (`"method"`, `"password"`, `"network":"tcp"`); outbounds = freedom only.
  - `config_builder.write_config(path: Path, cfg: dict) -> None` (pretty JSON).
  - `scripts/fetch_xray.sh` — downloads Xray release zip (`XRAY_VERSION` env, default `v26.3.27` — AMENDED 2026-10-14 from `latest`: pinning ruling; idempotency guard is version-aware, `latest` stays an explicit opt-in) for current OS/arch into `.bin/xray`, `chmod +x`, verifies with `.bin/xray version`.

- [ ] **Step 1: Write failing test**

`tests/test_config_builder.py`:
- `test_vpn_path_format` — `vpn_path("abc123", "vless") == "/ray-abc123/vless"`.
- `test_one_inbound_per_enabled_protocol` — 2 users (vless enabled, vmess disabled) + 1 ss enabled → inbound tags `{"in-vless","in-ss"}` (no `in-vmess`), ports match `paths.PORTS`.
- `test_clients_have_email_and_level` — every client across inbounds has `email == user_id`, `level == 0`.
- `test_disabled_users_excluded_from_clients` — disabled user's uuid absent from serialized config.
- `test_api_and_stats_configured` — config has `stats`, `api.services == ["StatsService"]`, dokodemo inbound on 10010, routing rule present (inboundTag→outboundTag only; AMENDED 2026-10-14: no `"protocol"` condition — see Task 3 interface amendment).
- `test_config_passes_real_xray_test` — `skipif` when no Xray binary found (`XRAY_BIN` env / `.bin/xray` / `shutil.which("xray")`): parametrize `["vless","vmess","trojan","ss"]` → build with one enabled user → write tmp → `subprocess.run([xray, "run", "-test", "-c", path])` → `returncode == 0`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_config_builder.py -v`
Expected: FAIL — `FileNotFoundError: panel/paths.py`.

- [ ] **Step 3: Implement `panel/paths.py` + `panel/xray/config_builder.py` + `scripts/fetch_xray.sh`**

Follow Interfaces exactly.

- [ ] **Step 4: Fetch real Xray and run tests (config proof)**

Run: `bash scripts/fetch_xray.sh && XRAY_BIN=.bin/xray python3 -m pytest tests/test_config_builder.py -v`
Expected: PASS including `test_config_passes_real_xray_test` — 4/4 protocols validated by the real binary (spec §10 requirement, not optional).

- [ ] **Step 5: Commit**

```bash
git add panel/ scripts/ tests/
git commit -m "feat: xray config builder for 4 protocols over websocket"
```

---

### Task 4: Share-link & QR builders

**Files:**
- Create: `panel/links.py`
- Test: `tests/test_links.py`

**Interfaces:**
- Consumes: `paths.vpn_path` (Task 3).
- Produces:
  - `links.build_link(user: dict, public_host: str, install_token: str) -> str` — switch on `user["protocol"]`:
    - vless: `vless://{uuid}@{public_host}:443?encryption=none&security=tls&sni={public_host}&type=ws&host={public_host}&path={quote(vpn_path, safe="")}#{quote(username)}`
    - trojan: `trojan://{credential}@{public_host}:443?security=tls&sni={public_host}&type=ws&host={public_host}&path={quote(vpn_path, safe="")}#{quote(username)}`
    - ss: `ss://{b64url(method + ":" + credential)}@{public_host}:443?security=tls&sni={public_host}&type=ws&host={public_host}&path={quote(vpn_path, safe="")}#{quote(username)}`
    - vmess: `"vmess://" + base64.b64encode(json.dumps(build_vmess_obj(...)))`
  - `links.build_vmess_obj(user, public_host, install_token) -> dict` — `{"v":"2","ps":username,"add":public_host,"port":"443","id":uuid,"aid":"0","net":"ws","type":"none","host":public_host,"path":vpn_path,"tls":"tls","sni":public_host}`
  - `links.build_qr_data_uri(uri: str) -> str` — `"data:image/png;base64," + ...` via `qrcode`.

- [ ] **Step 1: Write failing test**

`tests/test_links.py`:
- `test_link_fields_pinned_all_protocols` — **(Review Focus #1)** parametrize `["vless","vmess","trojan","ss"]`: assert `:443`, `security=tls`, `sni=fakhra.railway.app`, `type=ws`, percent-encoded `/ray-<token>/<proto>`, fragment `#<username>`; vmess: base64-decode → `port == "443"`, `tls == "tls"`, `net == "ws"`, `id == uuid`, `path == vpn_path`.
- `test_vless_uri_parses_with_urllib` — scheme `vless`, hostname `fakhra.railway.app`, username == uuid.
- `test_ss_credentials_roundtrip` — b64-decode userinfo → `aes-256-gcm:<credential>`.
- `test_qr_is_png_data_uri` — starts `data:image/png;base64,`, decodes to bytes starting `\x89PNG`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_links.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `panel/links.py`**

Signatures above.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add panel/links.py tests/test_links.py
git commit -m "feat: share link and QR generation for all four protocols"
```

---

### Task 5: Xray process manager (validate → swap → restart → rollback)

**Files:**
- Create: `panel/xray/manager.py`, `tests/fixtures/fake_xray.sh`
- Test: `tests/test_manager.py`
- Modify: `panel/app.py` (lifespan: construct manager with `install_token` from meta + `public_host` from settings, `start()`, `start_monitor()`; store `app.state.manager`; shutdown: `stop()`)

**Interfaces:**
- Consumes: `Settings` (`XRAY_BIN`, `data_dir()`), `config_builder.build_config/write_config` (Task 3), `db.list_users` (Task 1), `db.get_or_create_meta("install_token")`, `paths.PORTS`.
- Produces: `class XrayManager`:
  - `__init__(settings: Settings, install_token: str, public_host: str)`
  - `config_path -> Path` (`DATA_DIR/xray/config.json`); candidate `config.next.json`; backup `config.json.bak`
  - `test_config_file(path: Path) -> tuple[bool, str]` — run `[XRAY_BIN, "run", "-test", "-c", path]`, capture combined output, 10 s timeout.
  - `apply_config(users: list[dict]) -> tuple[bool, str]` — build → write candidate → `test_config_file` → pass: keep `config.json.bak` = current file, `os.replace` candidate → `config_path`, `restart()`; restart fails → restore `.bak`, set `last_error`, return `(False, msg)`. Test fails → delete candidate, old file untouched, set `last_error`, return `(False, output)`. Success → `(True, "ok")`, `last_error = ""`.
  - `start() -> bool`, `stop() -> None`, `restart() -> bool`, `is_running() -> bool`, `uptime_s() -> float | None`, `last_error: str`, `start_monitor() -> None` (daemon thread every 5 s: if not running → backoff restart 1→2→4→30 s cap, record errors; never restart a healthy process).

- [ ] **Step 1: Write failing tests**

`tests/fixtures/fake_xray.sh`: args contain `-test` → exit 1 if config file contains `"INVALID_CONFIG"` else exit 0; otherwise (`run -c`) → `trap 'exit 0' TERM INT; while true; do sleep 1; done`.

`tests/test_manager.py` (fixture: `settings` with `XRAY_BIN` → absolute path to fake script, `DATA_DIR` → tmp):
- `test_apply_config_accepts_valid_candidate` — apply with 1 user → `(True, ...)`, config file contains user uuid, `is_running()`, `last_error == ""`.
- `test_apply_config_rejects_invalid_candidate` — **(Review Focus #4)** apply valid config first, read bytes; monkeypatch `config_builder.build_config` → `{"INVALID_CONFIG": True}`; apply → `(False, ...)`, config bytes unchanged, process still running, `last_error` non-empty.
- `test_start_spawns_process_and_stop_kills_it` — `start()` → running; `stop()` → not running.
- `test_monitor_restarts_dead_process` — `start()`, kill pid, wait ≤8 s (backoff base monkeypatched to 0.1 s) → `is_running()` True.
- `test_lifespan_starts_manager` — `with TestClient(create_app(settings)) as c:` → `c.app.state.manager.is_running()`; context exit → stopped.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_manager.py -v`
Expected: FAIL — class missing.

- [ ] **Step 3: Implement `panel/xray/manager.py` + wire lifespan**

Launch: `subprocess.Popen([XRAY_BIN, "run", "-c", config], stdout/stderr → `DATA_DIR/xray/xray.log`, `start_new_session=True)`; `stop()` = SIGTERM to group, 5 s wait, SIGKILL fallback.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add panel/ tests/
git commit -m "feat: xray manager with test-before-swap config lifecycle and rollback"
```

---

### Task 6: Traffic stats — vendored protos, generated stubs, gRPC client

**Files:**
- Create: `scripts/gen_protos.sh`, `proto/app/stats/command.proto` (+ imports from the pinned Xray release), `panel/xray/stats.py`, `panel/xray/pb/` (generated, gitignored)
- Test: `tests/test_stats.py`
- Modify: `.gitignore` (add `panel/xray/pb/`, `.bin/`, `data/`, `__pycache__/`, `.env`)

**Interfaces:**
- Consumes: inbound tags `in-<proto>` (Task 3).
- Produces:
  - `scripts/gen_protos.sh` — `python3 -m grpc_tools.protoc -I proto --python_out=panel/xray/pb --grpc_python_out=panel/xray/pb proto/app/stats/command.proto` (+ any imported files), creates `panel/xray/pb/__init__.py`.
  - `panel/xray/stats.py`:
    - `stats.stat_name(email: str, direction: str) -> str` → `f"user>>>{email}>>>traffic>>>{direction}"` (direction ∈ {"uplink","downlink"}). AMENDED 2026-10-14: original plan pinned `f"user<<{user_id}>>@in-{protocol}"` — a counter name Xray never registers, so per-user stats would return NOT_FOUND forever (silent zero traffic). Client `email` = user id (config_builder), Xray's documented per-user counter convention is `user>>>email>>>traffic>>>direction`. Exact literal (incl. whether brackets wrap the email) MUST be confirmed by a real-binary probe in Task 6 fix round and locked by the test.
    - `stats.compute_delta(raw: int, last_raw: int, used_bytes: int) -> tuple[int, int]` — `raw >= last_raw` → `(used + (raw - last_raw), raw)`; else → `(used + raw, raw)`.
    - `stats.StatsError(Exception)`
    - `class StatsClient`: `__init__(self, addr: str = "127.0.0.1:10010", channel_factory=None)` (factory injectable for tests); `get_bytes(user_id: str) -> int` (AMENDED 2026-10-14: queries BOTH `stat_name(user_id, "uplink")` and `stat_name(user_id, "downlink")` and sums — traffic quota counts up+down; protocol arg dropped, the email-keyed counter is protocol-independent; per-direction gRPC NOT_FOUND → that side counts 0, both missing → 0; transport-level RpcError still → StatsError); `get_inbound_bytes(protocol: str) -> int` (name `f"inbound>>>in-{protocol}>>>traffic>>>uplink"`); raises `StatsError` on RPC failure.
  - Generated stubs imported as `panel.xray.pb.command_pb2` / `command_pb2_grpc` — produced by `gen_protos.sh` (Dockerfile runs it; local dev runs it once).

- [ ] **Step 1: Write failing tests**

`tests/test_stats.py`:
- `test_stat_name_format` — `stat_name("u-1", "uplink") == "user>>>u-1>>>traffic>>>uplink"` (AMENDED 2026-10-14 with the interface; literal locked by real-binary probe).
- `test_compute_delta_normal` — `(1000, 400, 5000)` → `(5600, 1000)`.
- `test_compute_delta_reset` — **(Review Focus #3)** `(300, 5_000_000, 10_000)` → `(10_300, 300)`.
- `test_compute_delta_first_seen` — `(0, 0, 0)` → `(0, 0)`.
- `test_get_bytes_sums_both_directions` — fake channel returns uplink=1000, downlink=234 → `get_bytes(...) == 1234`; both direction names requested (AMENDED 2026-10-14: replaces the old single-counter assertion).
- `test_get_bytes_not_found_returns_0` — both directions NOT_FOUND → `0`; transport failure → StatsError (AMENDED 2026-10-14: replaces `test_get_bytes_raises_stats_error_on_rpc_failure` as the NOT_FOUND-vs-transport distinction test; a transport-failure test remains).

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_stats.py -v`
Expected: FAIL — module/proto missing.

- [ ] **Step 3: Vendor protos + implement `scripts/gen_protos.sh` + `panel/xray/stats.py`**

Vendor `app/stats/command.proto` (and any import protoc reports missing) from the same pinned Xray release as Task 3. Run `bash scripts/gen_protos.sh` locally so tests import stubs.

- [ ] **Step 4: Run test to verify it passes**

Run: `bash scripts/gen_protos.sh && python3 -m pytest tests/test_stats.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add proto/ scripts/ panel/xray/ tests/ .gitignore
git commit -m "feat: stats service client with counter-reset-safe delta math"
```

---

### Task 7: Enforcement jobs (expiry, quota, stats polling loop)

**Files:**
- Create: `panel/jobs.py`
- Test: `tests/test_jobs.py`
- Modify: `panel/app.py` (lifespan: spawn asyncio task running `jobs.run_cycle` every `TICK_SECONDS`; cancel on shutdown)

**Interfaces:**
- Consumes: `db.list_users`, `db.update_user` (Task 1), `stats.StatsClient`, `stats.compute_delta`, `stats.StatsError` (Task 6), `XrayManager.apply_config` (Task 5).
- Produces:
  - `jobs.TICK_SECONDS = 30`
  - `jobs.evaluate(users: list[dict], now: datetime) -> dict[str, str]` — pure; `{user_id: reason}` for **enabled** users only; `expires_at` past → `"expired"` (precedence); else `quota_gb` set and `used_bytes >= quota_gb * 1e9` → `"quota"`; disabled users excluded; `now == expires_at` → not expired (strict `<`).
  - `jobs.run_cycle(settings, manager, stats_client, now=None) -> int` — one tick: (1) each enabled user: `get_bytes(user_id)` → `compute_delta` → `update_user({used_bytes, last_raw_bytes})` (AMENDED 2026-10-14: NULL `last_raw_bytes` → default 0 before compute_delta; NOT_FOUND→0 handled inside `get_bytes`); `StatsError` → skip stats this tick (log, non-fatal); (2) `evaluate(...)` → `update_user({enabled: 0, disable_reason: reason})` per hit; (3) if any disabled → `manager.apply_config(db.list_users())`; returns number disabled.

- [ ] **Step 1: Write failing tests**

`tests/test_jobs.py` (tmp DB via settings fixture):
- `test_evaluate_marks_expired` — expired + enabled → `{"id": "expired"}`; `now == expires_at` → not expired.
- `test_evaluate_quota_boundary` — **(Review Focus #3)** `quota_gb=1, used_bytes=1_000_000_000` → `"quota"`; `999_999_999` → excluded.
- `test_evaluate_unlimited_and_disabled_excluded` — `quota_gb=None` enabled → no entry; disabled-but-expired → no entry.
- `test_run_cycle_updates_used_bytes` — fake client returns 1000; after cycle `used_bytes == 1000`, `last_raw_bytes == 1000`.
- `test_run_cycle_disables_and_reapplies_config` — expired user + fake manager recording calls → returns 1, row has `enabled=0, disable_reason="expired"`, last applied config contains no disabled uuid.
- `test_run_cycle_survives_stats_error` — fake client raises `StatsError` → returns 0, no exception, rows untouched.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_jobs.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `panel/jobs.py` + lifespan loop**

`while True: await asyncio.sleep(TICK_SECONDS); run_cycle(...)` as an asyncio task from `create_app` lifespan; broad catch per tick (log, never crash the panel).

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/ -v`
Expected: PASS (full suite).

- [ ] **Step 5: Commit**

```bash
git add panel/jobs.py panel/app.py tests/test_jobs.py
git commit -m "feat: stats polling and expiry/quota auto-disable jobs"
```

---

### Task 8: Admin JSON API

**Files:**
- Create: `panel/routes_admin.py`
- Test: `tests/test_api.py`
- Modify: `panel/app.py` (include router; remove Task 2 placeholder route)

**Interfaces:**
- Consumes: `db.*` (Task 1), `auth.require_admin`, `auth.hash_password/verify_password` (Task 2), `links.build_link/build_qr_data_uri` (Task 4), `manager.apply_config` (Task 5), `paths.vpn_path` (Task 3).
- Produces (all `/api/*`, behind `require_admin`, JSON bodies except where noted):
  - `GET /api/users` → `{"users": [rows]}`.
  - `POST /api/users` body `{"username","protocol","expiry_days": int|null, "quota_gb": float|null, "note": str}` → `201 {"user", "link", "qr"}`. Server generates: `id=uuid4`, `uuid` (vless/vmess) or `credential = secrets.token_urlsafe(24)` (trojan/ss), self-service `password = secrets.token_urlsafe(16)` stored **bcrypt-hashed in column `password`** (plaintext only in this response), `expires_at = now + expiry_days` when given. Validation → `422 {"detail": ...}`: username `^[a-zA-Z0-9_.-]{1,32}$`, protocol in 4-set, `expiry_days` 1–3650 or null, `quota_gb` 0.1–100000 or null. Duplicate username → `409`.
  - `POST /api/users/bulk` body `{"prefix","count", ...defaults}` → `201 {"users":[...], "links":[...]}`, usernames `f"{prefix}-{i:03d}"` (i from 1), `count` 1–100; any conflict → `409 {"conflicts":[...]}` and nothing created.
  - `GET /api/users/{id}` → `{"user","link","qr"}`; `404` unknown.
  - `PATCH /api/users/{id}` body any of `expires_at`, `quota_gb`, `note` → updated row; `404` unknown.
  - `POST /api/users/{id}/toggle` body `{"enabled": bool}` — disable sets `disable_reason="manual"`, enable clears it (`null`).
  - `DELETE /api/users/{id}` → `{"success": true}`.
  - `POST /api/users/{id}/regenerate` → new VPN credential → `{"user","link","qr"}`.
  - `POST /api/users/{id}/reset-password` → `{"password": "<plaintext, shown once>"}`.
  - `GET /api/stats` → `{"users_total","users_enabled","used_bytes_total","quota_bytes_total"}` (quota sum: non-null `quota_gb × 1e9`).
  - `GET /api/health` (**no auth** — Railway healthcheck) → `{"status", "xray": {"running","last_error","uptime_s"}}`.
  - **Mutation rule:** every mutation ends with `manager.apply_config(db.list_users())`; on `(False, msg)` → `500 {"detail": "config rejected: <msg>", "saved": true}` (DB change kept and reported).

- [ ] **Step 1: Write failing tests**

`tests/test_api.py` (fixture `auth_client`: POST `/login` with settings creds, reuse cookies):
- `test_create_vless_user_returns_link_and_qr` — 201; link has `:443` + `security=tls`; qr prefix `data:image/png`; `user["uuid"]` set; response `password` present, DB row stores bcrypt hash (starts `$2b$`), never the plaintext.
- `test_create_rejects_bad_username` — `"bad user!"` → 422.
- `test_duplicate_username_409` — → 409, list unchanged.
- `test_bulk_creates_numbered_users_and_all_links` — prefix `guest`, count 3 → `guest-001..003`, 3 links, exactly one `apply_config` call (fake manager).
- `test_bulk_conflict_creates_nothing` — existing `p-001`, prefix `p` count 1 → 409 + `conflicts`, DB unchanged.
- `test_toggle_sets_and_clears_disable_reason` — disable → `enabled=0, disable_reason="manual"`; enable → `disable_reason` null.
- `test_unauthenticated_and_user_cookie_get_401` — **(Review Focus #5 prep)** no cookie → 401; only `fakhra_user` cookie → 401.
- `test_health_is_public_and_reports_xray` — no cookie → 200 with `status` + `xray`.
- `test_config_rejection_returns_500_with_saved_true` — fake manager returns `(False, "boom")` after create → 500, `detail` contains `boom`, `saved` true, user still in DB.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_api.py -v`
Expected: FAIL — routes missing.

- [ ] **Step 3: Implement `panel/routes_admin.py` + router wiring**

Conventions: `404` for unknown ids on every `/users/{id}` route; Mutation rule on all writes.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add panel/ tests/
git commit -m "feat: admin API for user CRUD, bulk create, toggle, health, stats"
```

---

### Task 9: Admin dashboard UI

**Files:**
- Create: `panel/routes_pages.py`, `panel/templates/dashboard.html`, `panel/templates/user_modal.html`
- Modify: `panel/app.py` (include pages router), `panel/templates/static/style.css` (copy from old project's file as starting point, then extend)
- Test: `tests/test_pages.py`

**Interfaces:**
- Consumes: Task 8 API (dashboard JS calls it), `auth.require_admin`, `db.list_users`, `app.state.manager`.
- Produces:
  - `GET /` and `GET /dashboard` (behind `require_admin`) → `dashboard.html` with `{"request", "users": db.list_users(), "title": settings.DASHBOARD_TITLE, "xray_up": manager.is_running(), "xray_error": manager.last_error}`.
  - Template contract (tests depend on these selectors): rows carry `data-user-id="<id>"`; banner `id="xray-banner"` rendered **only when `not xray_up`**; create form `id="create-form"`; bulk form `id="bulk-form"`; row buttons `data-action="view|toggle|delete|regenerate"`.
  - Inline JS: fetch `/api/users`, `/api/users/{id}` (modal: input `id="link-uri"`, img `id="qr-img"`), POST/PATCH/DELETE per Task 8 contract; forms serialize to JSON; errors in `id="form-error"`.

- [ ] **Step 1: Write failing tests**

`tests/test_pages.py` (authenticated client):
- `test_dashboard_renders_users_and_title` — 200, contains `Fakhra Panel`, created username, `data-user-id`.
- `test_dashboard_requires_auth` — fresh client GET `/` → 302 `/login`.
- `test_banner_hidden_when_xray_up_shown_when_down` — manager up → `"xray-banner" not in html`; monkeypatch `manager.is_running` → False → present.
- `test_dashboard_js_calls_api_paths` — html references `/api/users` and `/api/health`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_pages.py -v`
Expected: FAIL — route/template missing.

- [ ] **Step 3: Implement pages router, dashboard template, copy + extend stylesheet**

Salvage: copy `style.css` from `~/Fakhrayi Panel/panel/templates/static/style.css` (read-only source); add `.usage-bar` and `.banner-error` styles.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add panel/ tests/
git commit -m "feat: admin dashboard with usage bars, modals, and xray banner"
```

---

### Task 10: Self-service account area

**Files:**
- Create: `panel/routes_account.py`, `panel/templates/account.html`, `panel/templates/account_login.html`
- Modify: `panel/app.py` (include router)
- Test: `tests/test_account.py`

**Interfaces:**
- Consumes: `auth.require_user`, `auth.verify_password`, `db.create_session/get_session/delete_session(kind="user")`, `links.*`, `db.get_user`.
- Produces:
  - `GET /account` — valid `fakhra_user` cookie → `account.html` with **only** `{"request","user": their_row, "link", "qr", "title"}`; else `account_login.html`.
  - `POST /account/login` (form `username`,`password`; rate-limit key = IP) → lookup by username + `verify_password` → set `fakhra_user` cookie (same flags as admin) → `302 /account`; failure → re-render with `error`, no cookie.
  - `GET /account/logout` → clear `fakhra_user`, `302 /account`.
  - Context rule: account template never receives a users list — exactly one row, own data only.

- [ ] **Step 1: Write failing tests**

`tests/test_account.py`:
- `test_login_with_user_credentials_sets_separate_cookie` — create user via Task 8 API (capture plaintext password from response) → POST `/account/login` → 302 `/account`, `fakhra_user` set, `fakhra_admin` NOT set.
- `test_account_page_shows_own_link_and_usage` — html contains their uuid, `:443`, a usage element (`data-usage`), their username.
- `test_account_page_excludes_other_users` — **(Review Focus #5)** users A (logged in) and B → B's uuid and username absent from A's html.
- `test_user_cookie_cannot_reach_admin_api` — **(Review Focus #5)** only `fakhra_user` cookie → `GET /api/users` → 401.
- `test_admin_credentials_do_not_open_account` — admin username+password POST `/account/login` → error (no user with those creds), no cookie.
- `test_wrong_user_password_rejected` — error rendered, no cookie.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_account.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement account routes + templates**

Strict context scoping as in Interfaces; never query other users.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add panel/ tests/
git commit -m "feat: self-service account page with isolated session namespace"
```

---

### Task 11: Container: Caddy front, entrypoint, Dockerfile, integration tests

**Files:**
- Create: `panel/caddy.py`, `Caddyfile.template`, `entrypoint.sh`, `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `tests/test_integration.py`
- Modify: `panel/bootstrap.py` (render Caddyfile at startup; add `__main__` support)

**Interfaces:**
- Consumes: `paths.PORTS`, `paths.vpn_path`, `bootstrap.ensure_runtime`, `get_or_create_meta("install_token")`.
- Produces:
  - `caddy.render_caddyfile(port: int, install_token: str) -> str` — from `Caddyfile.template` containing `$CADDY_PORT` and `$VPN_TOKEN` placeholders (substitute ONLY those two; Caddy `{$VAR}` env syntax so caddy can expand `$CADDY_PORT` at runtime — entrypoint exports both): `auto_https off`; site block listens `:{$CADDY_PORT}`; per protocol `handle /ray-$VPN_TOKEN/<proto>*` → `reverse_proxy 127.0.0.1:<PORTS[proto]>`; final `handle` → `reverse_proxy 127.0.0.1:8000`.
  - `bootstrap.ensure_runtime` additionally writes `DATA_DIR/Caddyfile` (re-rendered every startup; token stable, port may change).
  - `panel/bootstrap.py` `if __name__ == "__main__":` → run `ensure_runtime` for CLI use.
  - `entrypoint.sh`: `set -euo pipefail`; export `VPN_TOKEN` (read from meta via `python3 -m panel.bootstrap` printing it); `python3 -m panel.main &` (panel PID); poll ≤10 s until Caddyfile + panel port ready; `caddy run --config "$DATA_DIR/Caddyfile" --adapter caddyfile &`; `wait` on panel PID → on exit: kill caddy, exit with panel's code (Railway restarts container).
  - `Dockerfile`: `FROM python:3.12-slim`; install `unzip curl`; `ARG XRAY_VERSION` (default = latest stable release verified at implementation time; exact value recorded in the ARG default) → `scripts/fetch_xray.sh` → `/usr/local/bin/xray`; `COPY --from=caddy:2 /usr/bin/caddy /usr/local/bin/caddy`; `pip install -r requirements.txt`; `COPY proto/` + run `gen_protos.sh`; copy app; `EXPOSE 8080`; `CMD ["./entrypoint.sh"]`.
  - `docker-compose.yml`: `panel` (build `.`, ports `"8080:8080"`, env_file `.env`, volume `./data:/data`); `test` (same image, `command: python3 -m pytest tests/ -v`, `environment: IN_DOCKER=1`).

- [ ] **Step 1: Write failing tests**

`tests/test_integration.py` (`pytest.mark.integration`; `skipif` `IN_DOCKER` unset — unit-style tests below still run anywhere):
- `test_render_caddyfile_routes_and_tokens` — rendered contains `auto_https`, four `/ray-tok/<proto>` handles → correct loopback ports, final handle → `:8000`, `{$CADDY_PORT}`/`{$VPN_TOKEN}` placeholders preserved.
- `test_ws_upgrade_reaches_xray` — **(Review Focus #2)** (`IN_DOCKER` only) integration fixture boots real stack: `create_app` with real settings + real xray + `caddy run` subprocess on a test port; raw socket sends `GET /ray-<token>/vless HTTP/1.1` with `Upgrade: websocket`, `Connection: Upgrade`, `Sec-WebSocket-Key`, `Sec-WebSocket-Version: 13` → expect `101 Switching Protocols` (Xray accepts the WS handshake before protocol auth).
- `test_dashboard_and_vpn_share_one_port` — same listener: `GET /` → 200/302 (panel) on the port where the 101 succeeded.
- `test_real_config_passes_real_xray` — create user via API → `manager.config_path` → real `xray run -test -c` → rc 0.
- `test_stats_grpc_roundtrip` — `StatsClient().get_inbound_bytes("vless")` returns `int >= 0` (gRPC name format + connection proven).
- `test_user_traffic_counts_real_bytes` (AMENDED 2026-10-14 after the stat_name defect) — boot real xray with one enabled user, drive REAL traffic through a WS inbound (minimal python vless-ws client or a second xray instance as client), then `get_bytes(user_id)` returns `> 0` — empirically proves the per-user counter name literal.

- [ ] **Step 2: Run test to verify it fails**

Run: `IN_DOCKER=1 python3 -m pytest tests/test_integration.py -v`
Expected: FAIL — caddy/xray/stack missing locally.

- [ ] **Step 3: Implement caddy renderer, bootstrap render, entrypoint, Dockerfile, compose**

Docker build runs `gen_protos.sh`; `.bin`/`panel/xray/pb` gitignored but present in image.

- [ ] **Step 4: Build and run the integration suite in the real container**

Run: `docker compose build test && docker compose run --rm test`
Expected: PASS — full unit + integration suite (WS 101, dashboard on same port, real `xray -test`, gRPC roundtrip). Spec §10.2 gate.

- [ ] **Step 5: Commit**

```bash
git add panel/ scripts/ Dockerfile docker-compose.yml Caddyfile.template entrypoint.sh .dockerignore tests/
git commit -m "feat: single-container stack with caddy port multiplexing + integration tests"
```

---

### Task 12: Railway deployment files & docs

**Files:**
- Create: `railway.toml`, `DEPLOY.md`, `README.md`, `.env.example`
- Modify: `requirements.txt` (final pin check)

**Interfaces:**
- Consumes: everything (docs describe the finished system).
- Produces:
  - `railway.toml` — `[deploy] healthcheck-path = "/api/health"`, `healthcheck-timeout = 30`, `restart-policy = "on_failure"`.
  - `.env.example` — exactly spec §8 vars: `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `SECRET_KEY`, `PUBLIC_HOST`, `PORT`, `DATA_DIR`, `XRAY_BIN`, `COOKIE_SECURE`, `ENV` + generation hints (`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`).
  - `DEPLOY.md` — copy-paste Railway flow: create project from repo → attach Volume mounted at `/data` (free plan includes 0.5 GB) → set env vars (`ENV=production`, `PUBLIC_HOST=<service>.railway.app` from Networking tab, admin creds, `SECRET_KEY`) → deploy → verify `/api/health` → **Manual E2E checklist (spec §10.3):** dashboard → create user → copy link/QR → connect v2rayNG/V2Box → browse → usage counter increases on refresh → 1-minute expiry → auto-disable reason `expired` → quota test (`quota_gb=0.001`) → reason `quota` → **failure drill (§10.4):** kill xray locally → red banner + backoff restart.
  - `README.md` — what it is, architecture diagram (spec §2), local dev (`docker compose up panel`), tests (`docker compose run --rm test`).

- [ ] **Step 1: Write `railway.toml`, `.env.example`, `DEPLOY.md`, `README.md`**

Exact contents per Interfaces; env names copied from spec §8 (no new vars).

- [ ] **Step 2: Verify docs against reality**

Run: `grep -oE '^[A-Z_]+' .env.example | sort -u` → must equal spec §8 var list. Run `docker compose run --rm test` → PASS.

- [ ] **Step 3: Commit**

```bash
git add railway.toml DEPLOY.md README.md .env.example
git commit -m "docs: railway deploy guide, env template, and E2E checklist"
```

- [ ] **Step 4: Final whole-branch review**

Invoke `superpowers:requesting-code-review` for a fresh-reviewer pass over the whole branch against the spec before declaring done.
