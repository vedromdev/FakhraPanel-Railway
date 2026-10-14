# Fakhra Panel

A VPN panel that actually works: create **Xray (V2Ray) VPN accounts** for the
four mainstream protocols — **VLESS, VMess, Trojan, Shadowsocks** (all over
WebSocket) — share them as standard `vless://` `vmess://` `trojan://` `ss://`
links + QR codes that any major client (v2rayNG, V2Box, …) understands, and
manage **expiry dates, traffic quotas, live stats, bulk creation, and a
self-service user page** — deployed as a single container on Railway's free
plan.

- **Admin dashboard** at `/` (login with `ADMIN_USERNAME` / `ADMIN_PASSWORD`).
- **Self-service page** at `/account` — each user sees only their own link,
  usage, and expiry.
- **Enforcement jobs** run every 30 s: expired → disabled (reason `expired`),
  over quota → disabled (reason `quota`), traffic counters polled from Xray's
  StatsService over gRPC.
- **Fail-safe config lifecycle**: a candidate Xray config is validated with the
  real `xray run -test` before it replaces the live one — a bad change can
  never take the VPN down.

## Screenshots

Captured against a local instance with obviously fake demo users.

**Sign in** — the admin login at `/`:

![Fakhra Panel login page](docs/images/login.png)

**Admin dashboard** — single/bulk create on top, every user below with a live
usage bar, expiry, and per-user actions (demo-04 is suspended):

![Admin dashboard with demo users](docs/images/dashboard.png)

**Share link & QR** — *View* on any user opens the connection details, ready
to copy or scan:

![User modal with share link and QR code](docs/images/modal.png)

**Self-service account page** — users sign in at `/account` and see only
their own link, usage, and expiry:

![Self-service account page](docs/images/account.png)

## Architecture (single service, single container)

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

One Railway service, one exposed port, one container running three processes:

- **Caddy** multiplexes by path: the VPN namespace `/ray-<token>/<protocol>`
  proxies WebSocket upgrades to the matching Xray inbound port (VLESS 10001,
  VMess 10002, Trojan 10003, Shadowsocks 10004 — all localhost-only); every
  other path goes to the FastAPI panel.
- The **panel** owns the Xray process lifecycle (start/stop/restart-with-
  backoff) and writes its config into the data volume.
- The **entrypoint** starts the panel and Caddy, and tears the container down
  if either child dies (a half-alive container never looks healthy).
- Share links always use `PUBLIC_HOST:443` with `security=tls` — TLS exists
  only at the Railway edge; everything inside is plain WebSocket.

## Quickstart — Docker (full fidelity, recommended)

```bash
git clone https://github.com/vedromdev/FakhraPanel-Railway
cd FakhraPanel-Railway

cp .env.example .env      # then edit: ADMIN_USERNAME, ADMIN_PASSWORD, PUBLIC_HOST
docker compose up panel
```

Dashboard: <http://localhost:8080>. This boots the exact production stack
(Caddy + panel + Xray) with `./data` mounted at `/data`.

## Local development (venv)

The steps that are **not** obvious on a fresh clone (skip them and tests skip
or fail with confusing reasons):

```bash
# 1. virtualenv + dependencies (requirements-dev.txt = pytest, httpx2, grpcio-tools)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# 2. fetch the pinned Xray-core release into .bin/xray
#    (needed by the config tests that validate with the real `xray run -test`)
bash scripts/fetch_xray.sh

# 3. generate the gRPC stubs into panel/xray/pb/
#    (without them, the stats/integration tests SKIP with an actionable reason)
bash scripts/gen_protos.sh

# 4. configure + run
cp .env.example .env      # set ADMIN_USERNAME, ADMIN_PASSWORD, PUBLIC_HOST
python3 -m panel.main     # dashboard on http://localhost:8080 (XRAY_BIN=xray on PATH,
                          # or XRAY_BIN=.bin/xray to use the fetched binary)
```

Local uvicorn mode runs the panel (and its Xray child) directly — no Caddy in
front, so VPN WebSocket paths are served only by the full Docker stack above.

## Tests

```bash
# local (fast): unit + integration-minus-Docker — integration tests skip
# unless IN_DOCKER=1; stats tests skip if the stubs from gen_protos.sh are absent
python3 -m pytest tests/ -v
```

> **Note:** run the suite **before** creating your `.env` (or move it aside).
> `test_missing_admin_password_fails_fast` asserts that a bare `Settings()`
> fails without `ADMIN_PASSWORD`, and pydantic-settings would read a local
> `.env` if present — on a fresh clone (no `.env`) the suite is green.

```bash
# full gate (real container: Caddy + panel + real xray, WS upgrade, gRPC stats)
docker compose build test && docker compose run --rm test
```

## Deploy on Railway in 5 steps

A condensed follow-along for the free plan — one service, one container, one
port. Like the rest of the Railway docs in this repo this is **a walkthrough,
not a logged deployment**: the test suites and local-container checks are what
has actually been run here. [DEPLOY.md](DEPLOY.md) has the full version,
including the post-deploy E2E checklist.

### 1. Create the service

[railway.app](https://railway.app) → **New Project** → **Deploy from GitHub
repo** → select
[`vedromdev/FakhraPanel-Railway`](https://github.com/vedromdev/FakhraPanel-Railway).
Railway detects the `Dockerfile` and builds the image — Xray, Caddy, and the
panel are baked in, no build arguments needed.

### 2. Set the environment variables

Service → **Variables** → add:

| Variable | Value |
|---|---|
| `ADMIN_USERNAME` | your admin login name |
| `ADMIN_PASSWORD` | **required** — long random value: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` (the panel fails fast at startup without it) |
| `PUBLIC_HOST` | `<your-app>.railway.app` — you'll fill this in during step 4 |
| `SECRET_KEY` | optional — session-signing key; leave blank to auto-generate and persist it in the DB |

Leave everything else alone: Railway injects `PORT`, and the defaults
(`DATA_DIR=/data`, `XRAY_BIN=xray`, `COOKIE_SECURE=true`) are correct for the
HTTPS edge. A blank `PUBLIC_HOST` logs an ERROR at boot and share links/QR
generation fails until it's set.

### 3. Attach the volume

Service → **Settings → Volumes → Add Volume**, mount path exactly:

```
/data
```

The free plan includes a 0.5 GB volume — plenty for the SQLite DB and Xray
config. **Without it, every redeploy starts from an empty database.**

### 4. Generate the public hostname

Service → **Settings → Networking → Generate Domain** → copy the free
`*.railway.app` hostname into `PUBLIC_HOST` (step 2) and restart the service.
Share links always point at `<PUBLIC_HOST>:443` with `sni=<PUBLIC_HOST>` —
Railway's edge terminates TLS there.

### 5. Healthcheck, deploy, verify

In the service **Settings** set healthcheck path `/api/health`, healthcheck
timeout `30`, and restart policy **on failure** (the same three values sit in
[`railway.toml`](railway.toml), but Railway is deprecating Config as Code —
prefer the dashboard settings). Then trigger a deployment and check:

```bash
curl -s https://<your-app>.railway.app/api/health
# {"status":"ok","xray":{"running":true,"last_error":"","uptime_s":…}}
```

Open `https://<your-app>.railway.app/` and log in with your
`ADMIN_USERNAME` / `ADMIN_PASSWORD` — you should see the dashboard from the
screenshots above.

Full walkthrough, env-var reference, and the manual E2E checklist:
**[DEPLOY.md](DEPLOY.md)** · every environment variable:
[`.env.example`](.env.example).

Repository: <https://github.com/vedromdev/FakhraPanel-Railway>
