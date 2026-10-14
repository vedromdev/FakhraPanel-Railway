# Deploying Fakhra Panel

> **Honesty note:** the automated suite (local pytest + the full Docker gate)
> and the local-container smoke checks are verified in this repository — see
> the bottom of this file. The **VPS installer (`install.sh`) is
> syntax-validated and its Caddyfile rendering is unit-tested, but the live
> Let's Encrypt flow has not been run from this machine** — section Option A
> ends with a checklist for your first deployment. The Railway walkthrough is
> a reference for any PaaS; this project's Railway account was banned under
> their AUP, so treat Railway as "may not accept VPN projects".

## Option A — your own VPS (recommended)

Ubuntu/Debian with a **domain whose A record points at the server** (ports
80/443 reachable — they're needed for the Let's Encrypt HTTP-01 challenge).

### A1. One-command install

```bash
git clone https://github.com/vedromdev/FakhraPanel-Railway
cd FakhraPanel-Railway
sudo bash install.sh
```

The installer:

1. checks root + Debian/Ubuntu, installs Docker if missing;
2. prompts for **admin username**, **admin password** (empty = generates a
   strong one), and your **domain** — or accept them as env vars
   (`ADMIN_USERNAME=... ADMIN_PASSWORD=... PUBLIC_HOST=... sudo -E bash
   install.sh`) for non-interactive runs. Writes `.env` with `AUTO_TLS=true`
   (an existing `.env` is never overwritten);
3. opens 80/443 in ufw when it's active;
4. enables **BBR + tuned TCP buffers** via `/etc/sysctl.d/99-fakhra-panel.conf`
   (skipped with a warning if the kernel lacks BBR);
5. `docker compose up -d --build vps` — Caddy obtains a Let's Encrypt cert
   for your domain and serves HTTPS;
6. polls `https://<domain>/api/health` for up to 2 minutes, then prints the
   login URL (LE issuance usually takes 10–30 s).

### A2. Manual install (same result, step by step)

```bash
cp .env.example .env      # set ADMIN_USERNAME, ADMIN_PASSWORD, PUBLIC_HOST
                          # (bare hostname — no https://, no port), AUTO_TLS=true
docker compose up -d --build vps
```

Equivalent firewall/sysctl setup as in A1 if you're not using the script.

### A3. Verify (first-deploy checklist)

- [ ] `curl -s https://<domain>/api/health` →
      `{"status":"ok","xray":{"running":true,...}}`
- [ ] `https://<domain>/` shows the login page with a **valid cert**
      (no browser warning);
- [ ] log in → create a user → open **View** → the link starts
      `vless://...@<domain>:443?...&security=tls&sni=<domain>`;
- [ ] import that link in a client (v2rayNG, Clash/mihomo, Happ…) and browse;
- [ ] `sysctl net.ipv4.tcp_congestion_control` → `bbr`.

**Do not run the `panel` and `vps` services at the same time** — they share
`./data` (one SQLite DB, one Xray config). Local dev uses `panel`; production
uses `vps`.

### Why AUTO_TLS matters

With `AUTO_TLS=true` Caddy owns :80 (ACME + redirect) and :443 (site), so
end-to-end TLS exists **on your machine** — the VPN's WebSocket traffic is
encrypted all the way to you, and probes to the domain see a normal HTTPS
website. On PaaS (Option B) TLS terminates at the provider's edge instead.

## Option B — Railway-style PaaS (reference)

> **Warning:** Railway banned this project's account under their AUP; other
> PaaS providers may too. This section is kept as a walkthrough for providers
> that accept it (or your own edge setup) — it is **not** a record of a
> currently-working Railway deploy.

### B1. Create the service

1. Sign in to [railway.app](https://railway.app) → **New Project** →
   **Deploy from GitHub repo** → select
   [`vedromdev/FakhraPanel-Railway`](https://github.com/vedromdev/FakhraPanel-Railway).
2. Railway detects the `Dockerfile` and builds the image (Xray + Caddy + panel
   are baked in; no build arguments needed). Deploy the `panel` service
   (default profile) — `AUTO_TLS` stays `false` here.

### B2. Set the environment variables

Service → **Variables** → add:

| Variable | Value |
|---|---|
| `ADMIN_USERNAME` | your admin login name |
| `ADMIN_PASSWORD` | long random value — `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `SECRET_KEY` | *optional* — session-signing key: when set, session tokens are signed with it; rotating it logs all users out. Leave blank to auto-generate (persisted in the DB at `DATA_DIR`) — works fine out of the box |
| `ENV` | *optional* — reserved for spec compatibility, currently unused by the code |
| `PUBLIC_HOST` | `<your-app>.railway.app` — see step B4 |

Notes:

- `ADMIN_USERNAME` / `ADMIN_PASSWORD` default to `admin`/`admin` when unset
  (the panel logs a boot warning) — **set a long random password** before the
  service is public: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
- `PUBLIC_HOST` is **required for links**: if blank the panel logs an
  **ERROR at boot** (see `docker logs` / the deployment log) and
  share-link/QR generation returns 500 until it's set.
- Leave the rest alone: Railway injects `PORT`; the panel defaults to
  `DATA_DIR=/data` (your volume), `XRAY_BIN=xray` (baked into the image),
  `COOKIE_SECURE=true` (correct for the HTTPS edge).

### B3. Attach the volume (persistence)

Service → **Settings → Volumes → Add Volume**, mount path exactly:

```
/data
```

The free plan includes a **0.5 GB** volume — enough for the SQLite DB, the
Xray config, and the install token. Without a volume, every redeploy starts
from an empty database (all users gone).

### B4. Get the public hostname

Service → **Settings → Networking → Generate Domain** → you get a free
`*.railway.app` hostname. Copy it into the `PUBLIC_HOST` variable (step B2)
and restart the service. Share links always point at `<PUBLIC_HOST>:443` with
`sni=<PUBLIC_HOST>` — Railway's edge terminates TLS there.

### B5. Healthcheck & restart policy

**Primary path — works for every service, including new ones:** set these
three values in the service **Settings**:

- Healthcheck path: `/api/health` (must return 2xx before traffic shifts)
- Healthcheck timeout: `30` seconds
- Restart policy: **on failure**

**Legacy alternative — Config as Code:** [`railway.toml`](railway.toml)
carries the same three values:

```toml
[deploy]
healthcheckPath = "/api/health"     # must return 2xx before traffic shifts
healthcheckTimeout = 30
restartPolicyType = "ON_FAILURE"
```

Railway is deprecating Config as Code with a **hard cutoff of 2026-12-01**:
existing files keep working until then, and **new services cannot opt in at
all** — they never read `railway.toml`. Prefer the dashboard settings above
unless your service already has Config as Code enabled.

### B6. Deploy and verify

1. Push/trigger a deployment and watch the build log.
2. Once active:

   ```bash
   curl -s https://<your-app>.railway.app/api/health
   # {"status":"ok","xray":{"running":true,"last_error":"","uptime_s":…}}
   ```

3. Open `https://<your-app>.railway.app/` and log in with
   `ADMIN_USERNAME` / `ADMIN_PASSWORD`.

---

## Manual E2E checklist (spec §10.3)

Run against the deployed service. (Most dashboard/API items also work against
the local Docker stack from `docker compose up panel`; for the client-connect
step, set `PUBLIC_HOST` to an address your client device can actually reach —
`localhost` links only work on the same machine.)

- [ ] **Dashboard loads** → login with the admin credentials works.
- [ ] **Create a user** (single-create form: protocol, expiry, quota, note) →
      open the detail modal → **copy the link and/or QR**.
- [ ] **Connect a client**: import the link (or scan the QR) in **v2rayNG** or
      **V2Box** → browse a few sites.
- [ ] **Usage counter increases**: refresh the dashboard → the user's usage
      bar/number has grown (stats are polled every 30 s).
- [ ] **1-minute expiry**: edit the user → set the expiry to ~1 minute from
      now → wait for the next enforcement tick (30 s; allow 1–2 min) → the
      user is auto-disabled with reason **`expired`**, and drops out of the
      Xray config.
- [ ] **Quota test**: create a user with quota **`0.001` GB** (1 MB — this is
      the API minimum; the dashboard field accepts it) → connect → transfer
      more than 1 MB (any image-heavy page) → next tick disables them with
      reason **`quota`**.
- [ ] **Bulk create**: bulk form (prefix + count 1–100 + shared defaults) →
      get the copyable list of links back.
- [ ] **Account page login**: open `/account` and sign in as the created user
      with the plaintext password shown once at create/reset.
      ⚠️ **Bulk-created users have no plaintext password in the response** —
      before their first login, use **Reset password** in the user modal (or
      `POST /api/users/{id}/reset-password`) and use the password it shows
      once. The account page must show only that user's own link, usage, and
      expiry — no admin data.

## Failure drills (spec §10.4)

- [ ] **Kill Xray → red banner + backoff restart**: open a shell into the
      container — locally `docker compose exec panel sh`, on Railway the
      service **Console** — and kill xray (the image has no `pkill`; use the
      `/proc` scan — the `xr[ay]` bracket stops the shell matching itself):

      ```sh
      for p in /proc/[0-9]*; do grep -aq "xr[ay]" "$p/cmdline" 2>/dev/null && kill -9 "${p##*/}"; done
      ```

      Expected: `GET /api/health` flips to
      `{"status":"degraded","xray":{"running":false,…}}` and the dashboard
      shows the red health banner; the panel restarts Xray with exponential
      backoff (1 s → 2 s → 4 s … capped at 30 s), health returns to `ok`, and
      the banner clears.
      *(Locally verified against `docker compose up panel`: degraded
      immediately after the kill, `ok` again ~5 s later.)*
- [ ] **Bad config can't brick the VPN**: a deliberately invalid config
      change is rejected by `xray run -test`, the previous config stays live,
      and the error is surfaced in the dashboard.

---

## What was actually verified in this repo

- `python3 -m pytest tests/ -v` → **134 passed, 5 skipped** (5 skips are the
  Docker-only integration tests) — includes the AUTO_TLS Caddyfile rendering
  and default-credential tests.
- `docker compose build test && docker compose run --rm test` →
  **139 passed** (real Caddy + panel + real Xray: WS `101` upgrade on
  `/ray-<token>/<proto>`, one port serving both dashboard and VPN, real
  `xray run -test`, gRPC stats roundtrip with real traffic bytes).
- Local container smoke checks (`docker compose up panel`):
  `GET /login` → 200, `GET /` → 302, `GET /ray-<wrong>/vless` → 404,
  `GET /api/health` → 200 `{"status":"ok","xray":{"running":true,…}}`.
- Failure drill (failure drills section) run locally: after the `/proc` kill,
  `/api/health` → `{"status":"degraded","xray":{"running":false,…}}`, then
  `ok` again ~5 s later (backoff restart worked).
- `install.sh` / `entrypoint.sh` → `bash -n` clean; `docker compose config`
  parses with the profile-gated `vps` service (default profile starts only
  `panel` + `test`).

Not verified here: the live Let's Encrypt issuance and the VPS checklist in
Option A — run those on your server — and anything on Railway itself (see the
warning at the top of Option B).
