# A2 — Dynamic Inbound Engine — Design Spec

- **Date:** 2026-10-14
- **Status:** awaiting human review
- **Path:** architectural (spec → plan → implement)
- **Roadmap:** A1 ✅ → **A2 (this spec)** → A3 inbound-editor UI → B UI/QOL → C performance → D protocol spike

## 1. Goal

3x-ui-style inbound management: the admin can create, edit and delete Xray
inbounds with **any port, protocol, transport and security**; every save is
gated by `xray run -test`; user links are derived from the inbound instead of
hardcoded assumptions; a public **`GET /sub/<token>`** endpoint serves
per-user subscription links.

**In scope:** inbound data model + migration, engine core (config build,
save gates, REALITY keygen), admin CRUD API, user↔inbound binding,
inbound-driven link building, `/sub/<token>`, the topology move (Caddy =
panel only), entrypoint/compose/install.sh updates, docs, tests.

**Out of scope:** A3's structured-form UI (it consumes this API); B's
subscription management UI + Clash format; C performance work; D custom
transports; Hysteria2/TUIC (cut — QUIC is blocked in the target profile);
ShadowTLS/Naive (optional, later).

## 2. Fixed decisions (human partner, this session)

1. **Full 3x-ui exposure** — Xray owns all VPN ports directly; no Caddy
   path-multiplexing for VPN traffic.
2. **REALITY gets :443** — the primary transport sits on the strongest
   masquerade port.
3. **Panel = HTTPS on :8080 via Caddy** — real LE cert, HTTP-01 challenge
   answered on :80.
4. Transports: VLESS+REALITY primary; xhttp, gRPC, WS backups. No
   QUIC/Hysteria2/TUIC.
5. Config freedom = structured settings schema + raw JSON passthrough; the
   final rendered config is always validated by `xray run -test`.
6. Region-neutral defaults (any VPS region); Iran DPI = key target profile.

## 3. Runtime topology

```
Internet
 ├── :443        Xray — REALITY inbound (default vless), does its own TLS
 ├── :<port>…    Xray — admin-created inbounds (xhttp/gRPC/WS/trojan/ss/REALITY)
 ├── :8080       Caddy — panel, HTTPS (LE cert for PUBLIC_HOST)
 └── :80         Caddy — ACME HTTP-01 + HTTP→HTTPS redirect → :8080
```

- **Caddy loses all VPN duty.** The Caddyfile becomes fixed: AUTO_TLS mode =
  one site `https://{$PUBLIC_HOST}:8080` → `reverse_proxy 127.0.0.1:8000`
  (Caddy answers ACME on :80 automatically and redirects :80); plain mode =
  `:{$CADDY_PORT}` → panel only. No regeneration on inbound changes, ever.
- **Xray inbounds bind `0.0.0.0:<port>`** (replacing loopback-only
  10001–10004 behind Caddy). The stats/api loopback inbound (10010) stays
  loopback.
- **`vps` compose service → `network_mode: host`** — otherwise every new
  inbound port needs a compose edit. Consequence (stated honestly): each
  additional inbound port needs `ufw allow <port>/tcp`; `install.sh` opens
  80/443/8080/10002–10004 (the seeded defaults; vless vacates 10001 for
  :443) and the create-inbound API response carries a `firewall_hint` for
  anything beyond those.
- **Caddy LE cert persistence:** entrypoint sets
  `XDG_DATA_HOME=/data/caddy` so certs live on the `./data` volume (closes
  an A1 gap where certs were container-ephemeral and renewals could burn the
  LE rate limit).
- **Honest casualty:** Railway-style single-port hosts can no longer carry
  VPN traffic (they'd carry the panel only). DEPLOY.md's Railway section is
  updated accordingly — that platform already banned this project.
- Local dev (`docker compose up panel`) keeps its current flow for the
  dashboard; direct-port inbounds are reachable inside the container (gate
  tests) and can be published ad hoc for phone testing.

## 4. Data model & migration

```sql
CREATE TABLE inbounds (
  id         TEXT PRIMARY KEY,      -- stable tag: xray inboundTag + stats key "in-<id>"
  protocol   TEXT NOT NULL,         -- vless | vmess | trojan | ss
  port       INTEGER NOT NULL UNIQUE,
  listen     TEXT DEFAULT '0.0.0.0',
  settings   TEXT NOT NULL,         -- JSON: transport/security + params (see §6)
  enabled    INTEGER DEFAULT 1,
  remark     TEXT DEFAULT '',
  created_at TEXT NOT NULL
);
-- users: + inbound_id TEXT          (FK → inbounds)
-- users: + sub_token TEXT UNIQUE    (per-user /sub/<token> credential)
-- protocol column is KEPT as a cache (invariant: equals the inbound's protocol)
```

**Migration (existing `schema_version` mechanism):**

1. Create `inbounds`; seed four defaults (ids = protocol names so existing
   `in-vless`… stats counters keep matching):

   | id | protocol | port | transport | security |
   |----|----------|------|-----------|----------|
   | `vless` | vless | **443** | tcp | **reality** (flow `xtls-rprx-vision`, generated x25519 keypair, serverNames/dest default `www.microsoft.com:443`, random shortId, fp `chrome`) |
   | `vmess` | vmess | 10002 | ws (path `/`) | none |
   | `trojan` | trojan | 10003 | tcp | none — **documented warning**: trojan without TLS is plaintext-auth; admins should add a cert (`stream_settings_extra`) or move these users to REALITY |
   | `ss` | ss | 10004 | tcp | none (SS carries its own cipher) |

2. `users`: add `inbound_id`, backfill by protocol → matching default;
   add `sub_token` = `uuid4().hex`, unique index.
3. REALITY keygen is **pure-Python x25519** (RFC 7748 scalar multiplication,
   stdlib only, ~20 lines) — unit-testable against RFC vectors, no
   subprocess/binary dependency during migration.

**Consequences stated honestly:**

- **Old links break** (path-multiplexing is gone). They self-heal on
  re-view: `build_link` becomes inbound-driven (port/security/SNI/path come
  from the inbound row), so any freshly viewed link is already new-format;
  clients re-import once. `/sub/<token>` makes redistribution trivial.
- Per-user quota/expiry counters (`user>>>…`) are untouched; enforcement
  jobs keep their current flow (they trigger `apply_config`, which now
  renders from inbounds too).
- `install_token` disappears from links/Caddyfile (its path-secrecy job is
  over) but stays in `meta` untouched to avoid collateral churn.

## 5. Engine core

### Config build

`config_builder.build_config(inbounds, users, public_host)`:

- One Xray inbound per **enabled** inbound row: `tag: "in-<id>"`,
  `listen`/`port` from the row, protocol settings from the users assigned to
  it (existing `_client`-per-user logic), `streamSettings` derived from
  `settings` JSON:
  - `security: reality` → `realitySettings` (serverNames, privateKey,
    shortIds, spiderX, dest) + transport settings; enforced **vless-only**
    (that's all xray supports).
  - `security: tls` → `tlsSettings.certificates[]` from
    `cert_file`/`key_file` paths in settings; missing/invalid paths surface
    as `xray -test` errors (honest pipeline).
  - `security: none` → no TLS block.
  - Transports: `tcp`, `ws` (path, host), `grpc` (serviceName), `xhttp`
    (path, mode) — the approved matrix.
  - Escape hatch: `settings.stream_settings_extra` is deep-merged into the
    rendered `streamSettings` **verbatim, last** (power-user passthrough;
    `xray -test` is the judge).
- Stats/api inbound (10010 loopback) and routing/outbounds sections stay as
  they are today.

### Save gates (inbound create/update) — exact order

1. Pydantic schema validation (§6 rules).
2. Port checks: reserved (80, 8080 → 409; 443 allowed when free),
   uniqueness in DB (409), OS bind probe on new/changed ports (409,
   "port already in use").
3. Build the config from the **proposed** state (in-memory, not yet
   committed) → `xray run -test`. Failure → 422 with xray's output, **no
   DB change**.
4. Commit the DB row.
5. Atomic file swap + restart (existing manager machinery). If restart
   fails → rollback file, **revert the DB row** (compensating delete/
   restore), old config keeps serving, error returns. This keeps DB and
   running config consistent in every outcome.

Existing `manager.apply_config` (validate → atomic swap → restart →
rollback) is extended to take a proposed snapshot; startup, quota/expiry
jobs and user-CRUD callers keep their semantics (they already read DB state).

### Inbound CRUD rules

- `DELETE`: 409 when users are assigned (move them first — no silent
  breakage). `enabled=0` allowed; response warns its users' links go dead
  until re-enabled.

## 6. API surface

All admin endpoints: existing session auth + conventions.

| Method/Path | Behavior |
|---|---|
| `GET /api/inbounds` | list + assigned-user counts |
| `POST /api/inbounds` | create (§5 gates); response includes `firewall_hint` when port ∉ pre-opened set |
| `GET /api/inbounds/{id}` | detail |
| `PUT /api/inbounds/{id}` | update, same gates (port change allowed) |
| `DELETE /api/inbounds/{id}` | 409 if users assigned |
| `GET /sub/{token}` | **public**: per-user link, v2ray base64, `text/plain`, `Cache-Control: no-store`, per-IP rate limit; 404 unknown token; 200 even for disabled users (the holder is the owner; the link just won't connect) |

**Structured settings schema** (what A3's form renders and "advanced JSON"
exposes):

- Common: `transport` ∈ `tcp|ws|grpc|xhttp`; `security` ∈ `none|tls|reality`.
- Protocol × security rules: vless → `none|tls|reality`; vmess/trojan/ss →
  `none|tls`. (`reality` on non-vless → 400.)
- Transport params: `path` (ws/xhttp), `host` (ws), `serviceName` (grpc),
  `mode` (xhttp), `flow` (vless+reality default `xtls-rprx-vision`).
- REALITY: `private_key`/`public_key` (auto-generated when omitted on
  create), `server_names`, `dest`, `short_ids`, `fp` (default `chrome`).
- TLS: `cert_file`, `key_file`.
- `stream_settings_extra`: free-form object, merged verbatim last.

**User endpoints:** create/update accept optional `inbound_id`; omitted →
auto-assign the enabled inbound of the user's protocol with the lowest
`created_at`; none exists → 400 with an actionable message. Existing
`protocol` Literal stays.

**Link building:** `build_link(user, inbound, public_host)` — port,
security, SNI, pbk/flow/fp (REALITY), path/serviceName (transport) all come
from the inbound row. `https`-style links no longer apply (each transport
renders its native URI scheme; REALITY links carry `sni=<serverNames[0]>`,
`pbk`, `fp`, `sid`, `flow`, `type`).

## 7. Testing strategy (TDD; per-task RED/GREEN lives in the plan)

- **Unit (hermetic):** migration (seeds, backfill, idempotency, RFC 7748
  keygen vector); config-builder matrix (protocol × transport × security →
  expected streamSettings, `stream_settings_extra` merge, tag naming, user
  assignment); save-gate ordering (failed `xray -test` → no DB row — the
  atomicity guarantee); API validation (reserved/duplicate ports, vless-only
  REALITY); `/sub/<token>` (200/404/rate-limit); link matrix per transport;
  Caddy render tests updated (VPN handles asserted **absent**, both TLS
  modes kept).
- **Gate (IN_DOCKER):** Xray boots with the seeded REALITY inbound;
  direct-port WS inbound serves a real `101` upgrade on **its own port**
  (replaces the old path tests — that behavior is intentionally removed, and
  the affected v0.1 tests are rewritten, not hidden); inbound PUT with bad
  config → 422 + rollback keeps serving; `/sub/{token}` over HTTP;
  `GET /ray-…` now 404s (multiplexing gone).
- Baseline counts and any replaced tests are recorded in the implementation
  plan, not here.

## 8. Docs impact

- **DEPLOY.md:** new topology + port table (80 ACME/redirect, 443 REALITY,
  8080 panel, 10002–10004 defaults, per-inbound ufw steps); "old links:
  re-import once" notice; Railway section → "panel only on single-port
  PaaS" honesty update.
- **README:** architecture diagram + feature bullet; screenshots stay v0.1
  until sub-project B recaptures them.
- **install.sh:** ufw opens 80/443/8080/10001–10004; health poll moves to
  `https://<domain>:8080/api/health`.

## 9. Risks & honest mitigations

| Risk | Mitigation |
|---|---|
| Old links break on upgrade | Re-import once via account page or `/sub/<token>`; docs notice; B adds UX |
| Default REALITY target (`www.microsoft.com`) unreachable from some regions | Field is admin-editable; docs say "pick a fast, reachable site your clients can also reach" |
| Trojan default runs without TLS | Documented loudly; admins add certs via `stream_settings_extra` or move users to REALITY |
| Host networking is Linux-only | `vps` service is VPS-only by definition; local Mac keeps the `panel` service |
| Bind probe vs host-network differences | Probe runs inside the same network namespace the process will bind — accurate in both modes |
| LE cert loss on container recreation | Fixed in this spec via `XDG_DATA_HOME=/data/caddy` |

## 10. Success criteria

- Create a REALITY inbound on a fresh port → save gated by `xray -test` →
  connect a real client **to that port** → traffic is counted.
- Invalid settings JSON → 422 with xray's output and **zero state change**.
- `/sub/<token>` returns an importable link for the owning user only.
- An existing v0.1 install migrates with no manual steps; seeded defaults
  keep stats continuity.
- Local suite and Docker gate green (exact counts recorded in the plan).
