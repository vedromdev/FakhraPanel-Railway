# Deploying Fakhra Panel to Railway

> **Honesty note:** the automated suite (local pytest + the full Docker gate)
> and the local-container smoke checks are verified in this repository — see
> the bottom of this file. **An actual Railway deploy has not been run from
> this machine**: everything in the Railway section is a walkthrough for you to
> follow, not a record of a verified deployment.

## 1. Create the service from the repo

1. Sign in to [railway.app](https://railway.app) → **New Project** →
   **Deploy from GitHub repo** → select
   [`vedromdev/FakhraPanel-Railway`](https://github.com/vedromdev/FakhraPanel-Railway).
2. Railway detects the `Dockerfile` and builds the image (Xray + Caddy + panel
   are baked in; no build arguments needed).

## 2. Set the environment variables

Service → **Variables** → add:

| Variable | Value |
|---|---|
| `ADMIN_USERNAME` | your admin login name |
| `ADMIN_PASSWORD` | long random value — `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `SECRET_KEY` | same command, different value (session signing) |
| `ENV` | `production` |
| `PUBLIC_HOST` | `<your-app>.railway.app` — see step 4 |

Notes:

- `ADMIN_PASSWORD` is **required**: the panel fails fast at startup without it
  (no default password ever runs).
- `PUBLIC_HOST` is **required for links**: if blank the panel logs an
  error-level warning at boot and share-link/QR generation returns 500 until
  it's set.
- Leave the rest alone: Railway injects `PORT`; the panel defaults to
  `DATA_DIR=/data` (your volume), `XRAY_BIN=xray` (baked into the image),
  `COOKIE_SECURE=true` (correct for the HTTPS edge).

## 3. Attach the volume (persistence)

Service → **Settings → Volumes → Add Volume**, mount path exactly:

```
/data
```

The free plan includes a **0.5 GB** volume — enough for the SQLite DB, the
Xray config, and the install token. Without a volume, every redeploy starts
from an empty database (all users gone).

## 4. Get the public hostname

Service → **Settings → Networking → Generate Domain** → you get a free
`*.railway.app` hostname. Copy it into the `PUBLIC_HOST` variable (step 2) and
restart the service. Share links always point at `<PUBLIC_HOST>:443` with
`sni=<PUBLIC_HOST>` — Railway's edge terminates TLS there.

## 5. Healthcheck & restart policy

[`railway.toml`](railway.toml) pins:

```toml
[deploy]
healthcheckPath = "/api/health"     # must return 2xx before traffic shifts
healthcheckTimeout = 30
restartPolicyType = "ON_FAILURE"
```

Caveat: Railway is deprecating Config as Code, and **new services cannot opt
into `railway.toml`**. If your service ignores the file, set the same three
values in the service **Settings** (healthcheck path `/api/health`, healthcheck
timeout 30 s, restart policy *on failure*) — same behavior either way.

## 6. Deploy and verify

1. Push/trigger a deployment and watch the build log.
2. Once active:

   ```bash
   curl -s https://<your-app>.railway.app/api/health
   # {"status":"ok","xray":{"running":true,"last_error":"","uptime_s":…}}
   ```

3. Open `https://<your-app>.railway.app/` and log in with
   `ADMIN_USERNAME` / `ADMIN_PASSWORD`.

---

## 7. Manual E2E checklist (spec §10.3)

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

## 8. Failure drills (spec §10.4)

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

- `python3 -m pytest tests/ -v` → **124 passed, 5 skipped** (5 skips are the
  Docker-only integration tests).
- `docker compose build test && docker compose run --rm test` →
  **129 passed** (real Caddy + panel + real Xray: WS `101` upgrade on
  `/ray-<token>/<proto>`, one port serving both dashboard and VPN, real
  `xray run -test`, gRPC stats roundtrip with real traffic bytes).
- Local container smoke checks (`docker compose up panel`):
  `GET /login` → 200, `GET /` → 302, `GET /ray-<wrong>/vless` → 404,
  `GET /api/health` → 200 `{"status":"ok","xray":{"running":true,…}}`.
- Failure drill (section 8) run locally: after the `/proc` kill,
  `/api/health` → `{"status":"degraded","xray":{"running":false,…}}`, then
  `ok` again ~5 s later (backoff restart worked).

Not verified here: anything on Railway itself — follow this walkthrough
(sections 1–6) on your own deployment, then run the checklists in sections
7–8.
