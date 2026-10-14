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

## Deployment

See **[DEPLOY.md](DEPLOY.md)** for the Railway walkthrough (env vars, the
`/data` volume, healthcheck, and the manual E2E checklist), and
[`.env.example`](.env.example) for every environment variable you need to
configure.

Repository: <https://github.com/vedromdev/FakhraPanel-Railway>
