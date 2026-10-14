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

Ubuntu/Debian with a **domain whose A record points at the server** — the
A record drives Let's Encrypt (HTTP-01 on :80), and the panel lives at
`https://<domain>:8080`.

### Topology & ports (v0.2)

```
Internet
 ├── :443        Xray — VLESS + REALITY inbound (seed default; does its own TLS)
 ├── :<port>…    Xray — other inbounds: seeds 10002–10004, plus any you create
 ├── :8080       Caddy — panel, API, /sub (HTTPS, LE cert for PUBLIC_HOST)
 └── :80         Caddy — ACME HTTP-01 + HTTP→HTTPS redirect → :8080
```

| Port | Owner | Purpose |
|---|---|---|
| `80/tcp` | Caddy | Let's Encrypt HTTP-01 + redirect to the panel |
| `443/tcp` | Xray | VLESS + REALITY inbound (seeded default) |
| `8080/tcp` | Caddy | Admin panel, API, `/sub/<token>` (HTTPS) |
| `10002/tcp` | Xray | VMess seed (WebSocket) |
| `10003/tcp` | Xray | Trojan seed — **no TLS, see caveats below** |
| `10004/tcp` | Xray | Shadowsocks seed |
| *any port you pick* | Xray | inbounds you create — **you must allow it by hand** |

- The `vps` service runs with `network_mode: host`: Xray binds these ports
  **directly on the host** (no compose edit needed when you add an inbound).
  Consequence: **`vps` is Linux-VPS-only** — on a Mac use the `panel`
  service for the dashboard; its inbounds are reachable inside the container
  and can be published ad hoc (`ports:` entries) for phone testing.
- `install.sh` opens `80/443/8080/10002–10004` in ufw when it's active.
  **Every inbound port beyond those needs its own rule:**
  `sudo ufw allow <port>/tcp` (the create-inbound API response carries a
  `firewall_hint` with the exact line).
- Xray's stats API stays loopback-only (`10010`); the panel backend listens
  on loopback `8000` behind Caddy.

**Caveats, stated honestly:**

- **REALITY target.** The seeded REALITY inbound forwards its TLS handshake
  to `www.apple.com:443` — editable per inbound (`dest` + `server_names`).
  It was changed from the classic `www.microsoft.com:443` because xray
  ≥ 26.3.27 rejects Microsoft's certificate record (it exceeds 8192 bytes —
  XTLS/Xray-core#6356); `www.apple.com` was verified with a real client
  connect end-to-end. Pick a target that is **fast and reachable from both
  your VPS and your clients** — apple/Google domains have a long history of
  interference in some regions (GFW-style SNI routing), so the shipped
  default may not be the best choice where you are. After changing it,
  **verify with a real client connect**: a passing config save cannot prove
  a target works for your clients.
- **The Trojan seed runs without TLS** (`security: none` — Trojan's
  password over plain TCP is trivially identifiable and blockable). Fix it
  by setting the inbound's `security` to `tls` with `cert_file`/`key_file`
  pointing at a PEM pair (e.g. a cert for your domain), use
  `stream_settings_extra` for a raw stream-settings override — or simply
  **move those users to the REALITY inbound**, the recommended default.

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
3. opens `80/443/8080` plus the seed VPN ports `10002–10004` in ufw when
   it's active (inbounds you create later: `sudo ufw allow <port>/tcp`);
4. enables **BBR + tuned TCP buffers** via `/etc/sysctl.d/99-fakhra-panel.conf`
   (skipped with a warning if the kernel lacks BBR);
5. `docker compose up -d --build vps` — host networking: Xray binds your
   VPN ports directly, and Caddy obtains a Let's Encrypt cert and serves
   the panel on `:8080` (certs persist on the `./data` volume);
6. polls `https://<domain>:8080/api/health` for up to 2 minutes, then prints
   the login URL (LE issuance usually takes 10–30 s).

### A2. Manual install (same result, step by step)

```bash
cp .env.example .env      # set ADMIN_USERNAME, ADMIN_PASSWORD, PUBLIC_HOST
                          # (bare hostname — no https://, no port), AUTO_TLS=true
docker compose up -d --build vps
```

Equivalent firewall/sysctl setup as in A1 if you're not using the script.
Requires Linux — the `vps` service uses host networking (see Topology).

### A3. Verify (first-deploy checklist)

- [ ] `curl -s https://<domain>:8080/api/health` →
      `{"status":"ok","xray":{"running":true,...}}`
- [ ] `https://<domain>:8080/` shows the login page with a **valid cert**
      (no browser warning);
- [ ] log in → create a user → open **View** → the link starts
      `vless://…@<domain>:443?…&security=reality&sni=www.apple.com&pbk=…`
      (address = your domain: the inbound's port, `sni`/`pbk` = the REALITY
      target and public key);
- [ ] import that link in a client (v2rayNG, Clash/mihomo, Happ…) and browse;
- [ ] open `https://<domain>:8080/inbounds` (default admin/admin — change
      it) and add or adjust inbounds from the UI; the panel warns when a
      port needs `ufw allow <port>/tcp`. The curl API remains as the
      scriptable reference;
- [ ] **Upgrading from v0.1?** the old `/ray-<token>` paths no longer exist —
      old links are dead. Re-import **once**: the user's `/account` page, or
      `https://<domain>:8080/sub/<subscription-token>` in any client that
      accepts a subscription URL;
- [ ] `sysctl net.ipv4.tcp_congestion_control` → `bbr`.

**Do not run the `panel` and `vps` services at the same time** — they share
`./data` (one SQLite DB, one Xray config). Local dev uses `panel`; production
uses `vps`.

### Why AUTO_TLS matters

With `AUTO_TLS=true` Caddy owns :80 (ACME + redirect) and serves the panel
at `https://<domain>:8080` with a Let's Encrypt cert; :443 belongs to
Xray's REALITY inbound, which does its own TLS — a probe of :443 receives a
faithful replica of the REALITY target's handshake, not your panel. On PaaS
(Option B) TLS terminates at the provider's edge instead.

## Option B — Railway-style PaaS (reference)

> **Warning:** Railway banned this project's account under their AUP; other
> PaaS providers may too. This section is kept as a walkthrough for providers
> that accept it (or your own edge setup) — it is **not** a record of a
> currently-working Railway deploy.
>
> **Honest casualty (v0.2): single-port PaaS hosts carry the panel only.**
> Xray now owns its VPN ports directly, and a one-port host can't expose
> them — the old `/ray-<token>` multiplexing that let one port serve both
> dashboard and VPN is gone. On PaaS the dashboard, user management, and
> `/sub` all work, but **VPN traffic cannot flow**. For a working VPN use a
> VPS (Option A); use PaaS only to poke at the panel.

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
Xray config, and the Let's Encrypt certs (`XDG_DATA_HOME=/data/caddy`).
Without a volume, every redeploy starts from an empty database (all users
gone) and re-issues the cert (LE duplicate-cert rate limits: 5/week).

### B4. Get the public hostname

Service → **Settings → Networking → Generate Domain** → you get a free
`*.railway.app` hostname. Copy it into the `PUBLIC_HOST` variable (step B2)
and restart the service. `PUBLIC_HOST` becomes the address in generated
links — but on single-port PaaS the inbound ports aren't exposed (warning
above), so links generated here won't carry traffic.

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
`localhost` links only work on the same machine, and the local `panel`
service publishes only `8080`, so add a `ports:` mapping for the inbound
you're testing (e.g. `"443:443"`) first.)

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

- `python3 -m pytest tests/ -q` → **198 passed, 8 skipped** — all 8 skips
  are container-only integration tests (they run in the gate below; when the
  suite runs *inside* the container, none skip). Includes the two-mode
  Caddyfile rendering, the REALITY/x25519/link-matrix units, and
  default-credential tests.
- `docker compose build test && docker compose run --rm test` →
  **206 passed, 0 skipped** (real Caddy + panel + real Xray): WS `101`
  upgrade directly on the vmess seed's own port, a full **REALITY client
  connect through :443** with tunneled traffic reaching an echo server, a
  freshly created REALITY inbound serving traffic, **old `/ray-<token>`
  paths → 404 through Caddy**, entrypoint supervision (caddy dies →
  teardown; SIGTERM → 143, no orphaned children), `xray run -test` gating,
  and a gRPC stats roundtrip with real traffic bytes.
- Local container smoke checks (`docker compose up panel`, re-run after the
  v0.2 topology move): `GET /login` → 200, `GET /` → 302,
  `GET /ray-nope/vless` → 404, `GET /api/health` → 200
  `{"status":"ok","xray":{"running":true,…}}`.
- Failure drill (failure drills section) re-run against the same stack:
  after the `/proc` kill, `/api/health` →
  `{"status":"degraded","xray":{"running":false,…}}`, then `ok` again
  within seconds (backoff restart worked).
- `install.sh` / `entrypoint.sh` → `bash -n` clean; `docker compose config`
  parses (default profile starts only `panel` + `test`; `--profile vps`
  includes `vps` with `network_mode: host`).
- Both rendered Caddyfile modes validated by **real caddy** in the test
  image (`caddy validate … --adapter caddyfile` → `Valid configuration`):
  plain (`:{$CADDY_PORT}`, `auto_https off`) and auto-TLS
  (`{$PUBLIC_HOST}:8080`, automatic HTTPS on).

Not verified here: the live Let's Encrypt issuance and the VPS checklist in
Option A — run those on your server; connect tests from real third-party
client apps (the gate's e2e uses Xray itself as the client); anything on
Railway itself (see the warning at the top of Option B).
