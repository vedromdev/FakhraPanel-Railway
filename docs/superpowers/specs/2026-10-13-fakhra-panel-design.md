# Fakhra Panel — Design Spec

**Date:** 2026-10-13
**Status:** Approved (conversational design review)
**Goal:** A VPN panel that *actually works* on Railway: create V2Ray (Xray) VPN accounts, share them as standard links/QR, manage expiry/quotas/stats — deployed as a single free-tier Railway service.

## 1. Requirements

From the user:

- New project named **Fakhrayi Panel → Fakhra Panel** (fresh build; the old `Fakhrayi Panel` folder is left untouched).
- **Multi-user accounts (B) + service features (C):** expiry dates, traffic quotas, live traffic stats, bulk create, and a **user self-service page** — all five.
- **All four protocols:** VLESS, VMess, Trojan, Shadowsocks.
- **Mixed clients:** emit standard `vless://`, `vmess://`, `trojan://`, `ss://` links + QR codes that any major client understands.
- **Free `*.railway.app` domain**, hostname supplied via `PUBLIC_HOST` env var.
- **Free Railway plan** (must fit $1/mo credit, 0.5 GB RAM, 0.5 GB volume) with a **volume mounted at `/data`** for persistence.
- Priority stated by the user: it must **actually work** — verified, not just written.

**Out of scope (YAGNI):** user signup/self-registration, payments, multi-admin accounts, per-user bandwidth shaping, panels of other proxy softwares, custom domains (later, via `PUBLIC_HOST` only).

## 2. Architecture (Approach A — single service, single container)

```
Internet
   │
   ▼
Railway edge — terminates TLS for *.railway.app (port 443/80)
   │
   ▼
$PORT (Railway-exposed) ──► Caddy  [front, in-container]
                              ├─ handle /ray-<token>/<proto>/* ─► Xray (127.0.0.1:10001-10004)
                              └─ handle * ──────────────────────► FastAPI/uvicorn (127.0.0.1:8000)
```

- **One Railway service**, one exposed port, one container running three processes.
- **Entrypoint script** starts the panel (which owns the Xray process lifecycle) and runs Caddy in the foreground. If the panel or Xray dies, the entrypoint/supervisor restarts them with backoff.
- **Caddy** multiplexes by path: the VPN namespace `/ray-<token>/<protocol>` proxies WebSocket upgrades to the matching Xray inbound port; everything else goes to the panel. Caddy handles WS upgrade proxying natively.
- **Xray inbounds** listen on distinct localhost ports (one per protocol; Xray cannot share a port across protocols):
  - VLESS → 10001, VMess → 10002, Trojan → 10003, Shadowsocks → 10004
  - All use **WebSocket transport, `security: none` on the inbound** (TLS exists only at the Railway edge — the client speaks `wss://` to the edge, edge forwards plain WS internally).
- **Install token:** a random 16-byte hex token generated once, persisted in `/data/install.json`. The full client path is `/ray-<token>/<protocol>`. Not the primary auth (UUIDs/passwords are), but it keeps scanners off the inbounds.
- **Share links** always use `PUBLIC_HOST`, **port 443**, `security=tls`, `sni=<PUBLIC_HOST>`, `host=<PUBLIC_HOST>` — because Railway's edge only serves TLS on 443 for the generated domain.

### Config change lifecycle (why the panel can't brick the VPN)

1. Panel writes a candidate `config.json` to a temp file.
2. Validates with the real validator: `xray run -test -c candidate.json`.
3. Only if valid: atomically replaces `/data/xray/config.json` and restarts Xray (~100 ms blip; acceptable for a small group).
4. If invalid or Xray fails to come up: previous config is restored, error surfaced in the dashboard. **A bad change can never take the VPN down.**

### Process ownership

- Panel manages Xray directly (start/stop/restart/test) — no external supervisor needed for Xray.
- Panel monitors Xray with exponential-backoff restart if it crashes (fixes the old project's broken monitor: compare *actual config content hash*, not port sets; never busy-restart).
- Local dev: `uvicorn` mode with `XRAY_BIN` pointing at a system Xray; full-fidelity check via `docker compose up`.

## 3. Data model (SQLite, WAL mode, `/data/panel.db`)

```sql
users (
  id TEXT PRIMARY KEY,            -- uuid4
  username TEXT UNIQUE NOT NULL,  -- for self-service login
  password TEXT NOT NULL,         -- bcrypt hash, generated random, regenerable by admin
  protocol TEXT NOT NULL CHECK (protocol IN ('vless','vmess','trojan','ss')),
  uuid TEXT,                      -- vless/vmess
  credential TEXT,                -- trojan password / ss password
  ss_method TEXT DEFAULT 'aes-256-gcm',
  enabled INTEGER DEFAULT 1,
  disable_reason TEXT,            -- 'manual' | 'expired' | 'quota'
  expires_at TEXT,                -- ISO8601, NULL = never
  quota_gb REAL,                  -- NULL = unlimited
  used_bytes INTEGER DEFAULT 0,   -- accumulated across xray restarts
  last_raw_bytes INTEGER DEFAULT 0,
  note TEXT DEFAULT '',
  created_at TEXT NOT NULL
)
admin_sessions ( token TEXT PRIMARY KEY, created_at, expires_at )
user_sessions  ( token TEXT PRIMARY KEY, user_id REFERENCES users(id), created_at, expires_at )
meta ( key TEXT PRIMARY KEY, value TEXT )   -- install_token, schema_version, secret_key
```

## 4. Traffic stats & quota enforcement

- Xray config enables `stats` + per-level `statsUserUplink/Downlink`; every client entry carries an `email` = user `id` so counters are addressable as `user<<id>>@<inboundtag>`.
- Panel polls Xray's **StatsService over gRPC** (localhost API inbound, tag `api`) every 30 s using **Python stubs generated at image build** from `.proto` files vendored from the pinned Xray-core release.
- Counter math: `used_bytes += max(0, raw - last_raw)`; if `raw < last_raw` (Xray restarted, counters reset) → `used_bytes += raw` (small undercount for traffic lost across restart; accepted).
- **Quota job** (same 30 s loop): `quota_gb != NULL AND used_bytes >= quota*1e9` → disable with reason `quota`. Expiry job: `expires_at < now` → disable with reason `expired`. Disabled users are removed from the Xray config on the next regeneration (config restart follows).
- Stats unavailable (gRPC down) → non-fatal: dashboard shows last-known values + a warning; never blocks the panel.

## 5. Pages & API

### Admin (session cookie `fakhra_admin`, routes `/`, `/login`, `/api/*`)

- **Login:** username/password from `ADMIN_USERNAME`/`ADMIN_PASSWORD` env. `ADMIN_PASSWORD` is **required at startup (fail fast with a clear error)** — no default password ever runs.
- **Dashboard:** user table — status (enabled/disabled + reason), expiry countdown, usage bar (used vs quota), protocol, created; actions: view link/QR, edit (expiry, quota, note, protocol fixed after creation), enable/disable, regenerate credentials, delete.
- **Create form:** single user — protocol, username prefix auto-filled, expiry (days/never), quota (GB/unlimited), optional note.
- **Bulk create:** username prefix + count (1–100) + shared defaults → returns all links (copyable list).
- **User detail modal:** `vless://…`-style URI (per protocol), QR (PNG data-URI), raw JSON client config for VMess, download buttons.
- **Xray health banner:** red banner with last error text when Xray is down or a config change failed.

### Self-service (`/account`, session cookie `fakhra_user` — separate namespace)

- Login with **their own** username/password (created by admin, shown once / regenerable by admin).
- Sees only: their own link + QR, usage vs quota, expiry, enabled state. Cannot edit anything. No admin data leaks (all queries scoped by `user_id`).

### API (`/api/*`, admin session; JSON)

`GET/POST /api/users`, `GET/PATCH/DELETE /api/users/{id}`, `POST /api/users/{id}/toggle`, `POST /api/users/{id}/regenerate`, `POST /api/users/bulk`, `GET /api/stats` (global), `GET /api/health`. Unauthenticated → `401` for JSON, `302` to `/login` for HTML.

## 6. Link formats

All links: host = `PUBLIC_HOST`, port = `443`, TLS to edge, WS inside.

- `vless://<uuid>@host:443?encryption=none&security=tls&sni=host&type=ws&host=host&path=%2Fray-<token>%2Fvless#<name>`
- `vmess://<base64(json)>` with `{"v":"2","ps","add","port":443,"id","aid":"0","net":"ws","type","host","path","tls":"tls","sni"}`
- `trojan://<password>@host:443?security=tls&sni=host&type=ws&host=host&path=%2Fray-<token>%2Ftrojan#<name>`
- `ss://<base64(method:password)>@host:443?...&type=ws&path=...&security=tls&sni=host#<name>` — SS-over-WS is "Limited" in Xray (TCP only, no UDP); acceptable, and E2E-tested before claiming it works.

QR codes rendered client-side-server as PNG data URIs (the `qrcode` library).

## 7. Security

- `SECRET_KEY`: from env; if absent, generated once and persisted in `/data` (survives restarts).
- Cookies: `HttpOnly`, `SameSite=Lax`, `Secure` in production (`COOKIE_SECURE`, auto-true on Railway), separate admin/user cookie names.
- Login rate-limited (5/min/IP) via slowapi; all state-changing endpoints require the admin session.
- Xray API (gRPC) and all inbounds bind `127.0.0.1` only.
- Parameterized SQL everywhere; CSP-friendly templates; no user-supplied HTML.
- The old project's bugs are explicitly non-regressions: `xray_configs` file/dir collision, missing imports, unset `start_time`, broken login handler, missing `jinja2`/`python-multipart` deps.

## 8. Configuration (env)

| Var | Required | Default | Purpose |
|---|---|---|---|
| `ADMIN_USERNAME` | yes | — | admin login |
| `ADMIN_PASSWORD` | yes | — | admin login (fail-fast if missing) |
| `SECRET_KEY` | no | persisted random | session signing |
| `PUBLIC_HOST` | yes | — | hostname in share links (e.g. `abc.railway.app`) |
| `PORT` | no | 8080 | Railway-provided; Caddy listens here |
| `DATA_DIR` | no | `/data` if exists else `./data` | DB, Xray config, install token, secret key |
| `XRAY_BIN` | no | `xray` | binary path |
| `COOKIE_SECURE` | no | auto (true on Railway) | cookie flag |
| `ENV` | no | `development` | `production` on Railway |

## 9. Build, deploy, repo

- **Dockerfile** (single stage): `python:3.12-slim` + pinned Xray release (`XRAY_VERSION` build arg — set to the latest stable verified at implementation time) + Caddy binary + `pip install` + `grpcio-tools` stub generation from vendored protos + `entrypoint.sh`.
- **`railway.toml`**: healthcheck `GET /api/health`, restart policy.
- **`DEPLOY.md`**: exact copy-paste steps — create project → set env vars → attach volume at `/data` → deploy → generate `ADMIN_PASSWORD`/`SECRET_KEY` → get `PUBLIC_HOST`.
- **`docker-compose.yml`** for local full-stack testing; `.env.example`; `README.md`.
- **Git:** repo initialized in `~/Fakhra Panel`, spec committed first, conventional commits thereafter.

## 10. Testing strategy (definition of "actually works")

1. **Unit (pytest):**
   - All 4 URI formats generated → parsed back → fields round-trip.
   - Generated Xray config passes **`xray run -test -c`** for every protocol × ws.
   - Config lifecycle: invalid candidate rejected, previous config intact.
   - Quota/expiry/disable logic; counter-reset math.
   - Auth: wrong password rejected; login sets cookie; unauthenticated `/api` → 401; `/` → 302.
   - DB fallback (`DATA_DIR` without `/data`), install token stability across restarts.
2. **Integration:** boot the real container (Caddy + panel + Xray):
   - dashboard loads, login works, create user → config passes `xray -test`,
   - **WebSocket upgrade on `/ray-<token>/<proto>` reaches Xray** (proves multiplexing),
   - gRPC stats poll returns counters after simulated traffic.
3. **Manual E2E checklist** (in `DEPLOY.md`): deploy to Railway → open dashboard → create user → scan QR with v2rayNG/V2Box → browse → watch usage counter move → set a 1-minute expiry → confirm auto-disable.
4. **Failure-path checks:** break a config deliberately → panel restores and reports; kill Xray → backoff restart + red banner.

## 11. Project structure

```
Fakhra Panel/
├── Dockerfile  railway.toml  docker-compose.yml  entrypoint.sh  Caddyfile.template
├── requirements.txt  requirements-dev.txt  .env.example  DEPLOY.md  README.md
├── docs/superpowers/specs/…          # this spec
├── proto/                            # vendored Xray .proto files (pinned release)
├── panel/
│   ├── main.py  app.py  config.py  auth.py  db.py  links.py  jobs.py
│   ├── xray/ (config_builder.py, manager.py, stats.py)
│   ├── templates/ (login, dashboard, account)  static/
└── tests/ (test_links, test_config, test_auth, test_api, test_integration)
```
