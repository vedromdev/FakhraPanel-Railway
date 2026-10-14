# A2 — Dynamic Inbound Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the admin create/edit/delete Xray inbounds with any port, protocol, transport and security (full 3x-ui model), with every save gated by `xray run -test`, links derived from inbound rows, and a public `/sub/{token}` endpoint.

**Architecture:** Xray owns every VPN port directly (Caddy fronts only the panel: ACME on :80, HTTPS dashboard on :8080). New `inbounds` table + migration seeds four defaults (vless-REALITY on :443 keeps stats continuity via protocol-named ids). Inbound mutations run a five-gate flow (schema → port checks → `xray -test` → commit → swap/rollback-with-DB-revert); user mutations keep the existing persist-first flow.

**Tech Stack:** Python 3.14 / FastAPI / pydantic-settings, SQLite (WAL), Xray + Caddy in Docker, pytest (hermetic local suite + IN_DOCKER gate).

**Spec:** `docs/superpowers/specs/2026-10-14-a2-dynamic-inbound-engine-design.md` (the plan argues from the spec; executors read both)

## Global Constraints

- Transport matrix (spec §6): `transport ∈ {tcp, ws, grpc, xhttp}`; `security ∈ {none, tls, reality}`; **reality is vless-only**; vmess/trojan/ss allow `none|tls` only. No QUIC/Hysteria2/TUIC anywhere.
- Reserved ports: **80, 8080 → 409** on inbound save. Seed ports: vless 443, vmess 10002, trojan 10003, ss 10004. `PRE_OPENED = {80, 443, 8080, 10002, 10003, 10004}` (outside it, API returns `firewall_hint`).
- Every inbound row renders as xray inbound `tag: "in-<id>"`; stats/api dokodemo inbound stays loopback `10010`, tag `api`; panel loopback stays `127.0.0.1:8000`.
- `settings.stream_settings_extra` (dict) deep-merges into rendered `streamSettings`, **last** (wins over structured keys).
- Inbound mutations must never leave DB and running config diverged: `-test` failure → no DB row; restart failure → file rollback **and** DB revert (spec §5 order 1–7).
- Links: inbound-driven; no `install_token`, no hardcoded `:443` (removed from `links.py` entirely).
- `/sub/{token}`: per-user, v2ray base64 of that user's link lines, `text/plain`, `Cache-Control: no-store`, per-IP rate limit, 404 unknown token.
- REALITY defaults (region-neutral): serverNames/dest `www.microsoft.com:443`, fp `chrome`, flow `xtls-rprx-vision`, short_id = random 8-hex when absent.
- No `.env` during tests (hermetic suite). Baseline before A2: local **134 passed / 5 skipped**, gate **139 passed**. Final counts recorded in Task 10.
- Panel env conventions stay: `AUTO_TLS=true` means Caddy site `{$PUBLIC_HOST}:8080` with automatic HTTPS; plain mode `:{$CADDY_PORT}`.

## Review Focus

(each line's pinning test is named; all live in the owning task)

1. **REALITY link field correctness** — a wrong pbk/sni/sid/flow makes the client fail silently. Pinned by `test_link_matrix_reality_exact_fields` (Task 6), asserting the exact query string.
2. **Missed `apply_config` call site** — a quota tick or API mutation calling the old signature crashes or drops inbounds. Pinned by updated `tests/test_jobs.py` green + `test_apply_config_accepts_inbounds` (Task 3).
3. **Auto-assign with no enabled inbound** — creating a vless user when every vless inbound is disabled/deleted must 400, not write a dangling row. Pinned by `test_create_user_no_enabled_inbound_400` (Task 7).
4. **DB/running-config divergence on restart failure** — after a bind crash, the DB row must be reverted while the old config keeps serving. Pinned by `test_apply_inbound_change_reverts_db_on_restart_failure` (Task 4).
5. **Panel-killer ports** — an inbound on 8080 (panel) or 80 (ACME) must be rejected before any config work. Pinned by `test_reserved_ports_rejected` (Task 5).

---

### Task 1: x25519 keygen (REALITY keypair)

**Files:**
- Create: `panel/xray/keys.py`
- Test: `tests/test_keys.py`

**Interfaces:**
- Produces: `generate_keypair() -> tuple[str, str]` — `(private_key, public_key)` as xray REALITY strings: urlsafe base64 **without** padding of 32 raw bytes; private = `secrets.token_bytes(32)`; public = X25519(private clamped per RFC 7748 §5, basepoint 9).
- Internal: `public_from_private_hex(private_hex: str) -> str` (hex in/out) — the testable core.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_keys.py
def test_rfc7748_vector():
    # RFC 7748 §6.1 DH test vector (X25519)
    priv = "77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a"
    pub = "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"
    assert keys.public_from_private_hex(priv) == pub

def test_generate_keypair_shape_and_roundtrip():
    priv, pub = keys.generate_keypair()
    for value in (priv, pub):
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        assert len(raw) == 32
    # public recomputes from private (hex round-trip through the encoder)
    priv_hex = base64.urlsafe_b64decode(priv + "=" * (-len(priv) % 4)).hex()
    assert keys.public_from_private_hex(priv_hex) == decode(pub).hex()
```

- [ ] **Step 2: Run to verify FAIL** — ` .venv/bin/python -m pytest tests/test_keys.py -q` → ImportError (`panel.xray.keys` missing).

- [ ] **Step 3: Implement in `panel/xray/keys.py`**

RFC 7748 X25519: clamp private (`k[0] &= 248; k[31] &= 127; k[31] |= 64`), Montgomery ladder over the Curve25519 field (p = 2²⁵⁵−19), output = k·9. Encode with `base64.urlsafe_b64encode(...).rstrip(b"=")`. Pure stdlib (`base64`, `secrets`). ~30 lines — the algorithm is RFC-defined; no design freedom beyond structure.

- [ ] **Step 4: Run to verify PASS** — ` .venv/bin/python -m pytest tests/test_keys.py -q` → 2 passed.

- [ ] **Step 5: Commit** — `git add panel/xray/keys.py tests/test_keys.py && git commit -m "A2 T1: pure-python x25519 keygen for REALITY"`

---

### Task 2: `inbounds` table, migration, seeds, reads

**Files:**
- Modify: `panel/db.py` (schema + `migrate()` + read helpers), `panel/bootstrap.py:41-62` (call `db.migrate()` after `init_db`)
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: `keys.generate_keypair()` (Task 1).
- Produces:
  - `db.migrate() -> None` — idempotent; bumps `meta.schema_version` `"1"` → `"2"`; creates `inbounds`, alters `users` (adds `inbound_id TEXT`, `sub_token TEXT`), seeds 4 defaults, backfills, then `CREATE UNIQUE INDEX IF NOT EXISTS idx_users_sub_token ON users(sub_token)`.
  - `db.list_inbounds() -> list[dict]` (ordered `created_at, id`), `db.get_inbound(inbound_id: str) -> dict | None`.
  - Seed rows (exact): ids `vless|vmess|trojan|ss`; ports `443|10002|10003|10004`; listen `0.0.0.0`; enabled 1; settings JSON:
    - vless: `{"transport":"tcp","security":"reality","flow":"xtls-rprx-vision","private_key":…,"public_key":…,"server_names":["www.microsoft.com"],"dest":"www.microsoft.com:443","short_ids":["<8 hex>"],"fp":"chrome"}`
    - vmess: `{"transport":"ws","security":"none","path":"/"}`; trojan: `{"transport":"tcp","security":"none"}`; ss: `{"transport":"tcp","security":"none"}`

- [ ] **Step 1: Write failing tests** (in `tests/test_db.py`)

```python
def test_migrate_seeds_default_inbounds(settings):
    bootstrap.ensure_runtime(settings)          # conftest _runtime already ran it once
    rows = {r["id"]: r for r in db.list_inbounds()}
    assert set(rows) == {"vless", "vmess", "trojan", "ss"}
    assert rows["vless"]["port"] == 443 and rows["vmess"]["port"] == 10002
    s = json.loads(rows["vless"]["settings"])
    assert s["security"] == "reality" and len(s["private_key"]) >= 40
    assert db.read_meta("schema_version") == "2"

def test_migrate_backfills_users_and_sub_tokens(settings): ...
def test_migrate_idempotent(settings): ...       # migrate() twice → unchanged rows, no raise
def test_migrate_upgrades_v1_database(tmp_path): ...  # hand-built v1 DB (users-only, schema_version=1, one user) → columns added, user gets inbound_id='vless'… by protocol, 32-hex sub_token
```

Backfill SQL (pinned): `UPDATE users SET inbound_id = protocol WHERE inbound_id IS NULL` (default ids == protocol names); `UPDATE users SET sub_token = lower(hex(randomblob(16))) WHERE sub_token IS NULL`.

- [ ] **Step 2: Run to verify FAIL** — `db.migrate` missing.
- [ ] **Step 3: Implement** — keep `_SCHEMA` at v1 shape (single code path: fresh DBs also go 1→2); `migrate()` gated on version inside one transaction; seeds only inside the version bump (so admin-deleted defaults are never resurrected); `INSERT OR IGNORE`.
- [ ] **Step 4: Run full suite** — `.venv/bin/python -m pytest tests/ -q`; fix any row-shape assertions that now see `inbound_id`/`sub_token` keys (expect failures only about extra columns; local total should exceed 134).
- [ ] **Step 5: Commit** — `git add -A && git commit -m "A2 T2: inbounds table, v1→v2 migration, seeded defaults"`

---

### Task 3: config builder from inbounds + manager/call-site rework

**Files:**
- Modify: `panel/xray/config_builder.py` (rewrite core), `panel/xray/manager.py` (ctor + `apply_config(users, inbounds)`), `panel/app.py:52-62`, `panel/routes_admin.py:_apply`, `panel/jobs.py:86`
- Test: `tests/test_config_builder.py` (rewrite), `tests/test_manager.py` (signature), `tests/test_jobs.py` (update), `tests/test_integration.py` (stack fixture)

**Interfaces:**
- Consumes: `db.list_inbounds()` (Task 2).
- Produces:
  - `build_config(inbounds: list[dict], users: list[dict], public_host: str) -> dict` — one xray inbound per **enabled** row + the unchanged stats `api` inbound. Users grouped by `user["inbound_id"]`, `enabled` users only.
  - Render rules (server-side; link-only keys never rendered): `network` = transport; ws → `wsSettings{path: settings.path or "/", headers:{Host: settings.host or public_host}}`; grpc → `grpcSettings{serviceName}`; xhttp → `xhttpSettings{path, mode (default "auto")}`; security none → `"security":"none"`; tls → `tlsSettings{serverName: server_name or public_host, certificates:[{certificateFile, privateKeyFile}]}`; reality → `realitySettings{dest, serverNames, privateKey, shortIds, spiderX (default "/")}` (vless only). vless client gains `flow` from settings when present; `stream_settings_extra` deep-merged last into `streamSettings`. `tag=f"in-{id}"`, `listen`/`port` from row, sniffing block kept as today.
  - `XrayManager.__init__(settings, public_host)` — **`install_token` dropped**; `apply_config(users: list[dict], inbounds: list[dict]) -> tuple[bool, str]` (build call swapped; test/swap/rollback machinery untouched).
- All three call sites become `manager.apply_config(db.list_users(), db.list_inbounds())`.

- [ ] **Step 1: Write failing tests** — `test_build_config_renders_all_seed_inbounds`, `test_stream_settings_matrix` (parametrize transport × security, assert exact `streamSettings` dicts), `test_stream_settings_extra_merged_last`, `test_disabled_inbound_excluded`, `test_users_grouped_by_inbound`, `test_vless_reality_client_flow`, `test_apply_config_accepts_inbounds` (manager), plus rewrite the existing 13 builder tests to the new signature (drop `test_vpn_path_format` only in Task 6; keep it while `links.py` still uses `vpn_path`).
- [ ] **Step 2: Run to verify FAIL.**
- [ ] **Step 3: Implement** per the render table above; update manager + 3 call sites + ctor call in `app.py` (drop `install_token=db.get_or_create_meta(...)` argument).
- [ ] **Step 4: Rewrite stack fixture WS test** — `tests/test_integration.py::stack` builds via new signature (inbounds come from `db.list_inbounds()` after `ensure_runtime`); replace the path-based caddy WS test with `test_ws_upgrade_direct_port`: raw `GET / HTTP/1.1` + `Upgrade: websocket` to `127.0.0.1:10002` (vmess seed, path `/`) expects `101`. Add gate-only `test_reality_e2e_client_connect` (IN_DOCKER): generate client config (sni `www.microsoft.com`, pbk from seeded settings, addr `127.0.0.1`, port 443), run `xray run -c` with a loopback socks inbound, `curl --socks5-hostname` a local `python -m http.server` → 200.
- [ ] **Step 5: Run FULL local suite** — expect green (stack tests skip locally); no old-signature callers left: `grep -rn "apply_config(db.list_users())\|install_token=" panel/ | grep -v meta` → empty.
- [ ] **Step 6: Commit** — `git commit -am "A2 T3: config builder renders inbound rows; manager takes (users, inbounds)"`

---

### Task 4: save gates — bind probe + proposed-snapshot apply

**Files:**
- Create: `panel/xray/ports.py`
- Modify: `panel/xray/manager.py`
- Test: `tests/test_ports.py`, `tests/test_manager.py`

**Interfaces:**
- Produces:
  - `ports.port_available(port: int, listen: str = "0.0.0.0") -> bool` — bind/close probe (`socket.SOCK_STREAM`, no reuse).
  - `XrayManager.apply_inbound_change(users: list[dict], proposed_inbounds: list[dict], commit: Callable[[], None], revert: Callable[[], None]) -> tuple[bool, str]` — order: build → candidate `-test` (**fail → return, neither callback runs**) → bind-probe every proposed `(listen, port)` whose `(port)` differs from the currently running config's inbound ports (**occupied → `(False, "port <p> already in use")`, no callbacks**) → `commit()` → swap+restart (**fail → file rollback + `revert()` + restart old config → `(False, reason)`**) → success `(True, "ok")`. Shares the existing lock/candidate machinery with `apply_config`.
- The running config's current ports are read from `self.config_path` (json) at probe time.

- [ ] **Step 1: Failing tests** — `test_port_available_free` / `test_port_available_occupied` (bind a listener first); `test_apply_inbound_change_rejects_before_commit` (monkeypatch `test_config_file` → False; commit/revert are Mocks, assert neither called, no row); `test_apply_inbound_change_occupied_port_reports` (hold a socket on the proposed port; assert message contains the port, no callbacks); `test_apply_inbound_change_reverts_db_on_restart_failure` (monkeypatch `restart` → False; assert revert called AND old config re-served via existing `start()` pattern); `test_apply_inbound_change_commits_on_success`.
- [ ] **Step 2: Run to verify FAIL.**
- [ ] **Step 3: Implement** `ports.py` + `apply_inbound_change` (reuse `_apply_config`'s candidate/test/swap code path — extract shared private helper if that keeps it smaller).
- [ ] **Step 4: Run to verify PASS** — `pytest tests/test_ports.py tests/test_manager.py -q`.
- [ ] **Step 5: Commit** — `git commit -am "A2 T4: five-gate inbound apply with DB revert + port bind probe"`

---

### Task 5: inbound settings validation module + CRUD API

**Files:**
- Create: `panel/inbounds.py`, `panel/routes_inbounds.py`
- Modify: `panel/db.py` (mutations + counts), `panel/app.py` (include router)
- Test: `tests/test_inbounds.py`, `tests/test_api.py`

**Interfaces:**
- Consumes: `keys.generate_keypair()` (create-time keygen), `manager.apply_inbound_change` (Task 4), `db.list_inbounds/get_inbound` (Task 2).
- Produces:
  - `panel/inbounds.py`: `validate_settings(protocol: str, settings: dict) -> dict` (raises `ValueError` with actionable text: enum/matrix rules — reality→vless only, `flow` only with vless+reality, tls requires `cert_file`+`key_file`, reality requires `private_key`+`server_names`+`dest`; **normalize**: missing `transport`→`"tcp"`, `security`→`"none"`, reality missing `short_ids`→`[secrets.token_hex(4)]`, missing `fp`→`"chrome"`, missing `flow`→`"xtls-rprx-vision"`, reality missing keys → generate via `generate_keypair()`); `RESERVED_PORTS = frozenset({80, 8080})`; `PRE_OPENED = frozenset({80, 443, 8080, 10002, 10003, 10004})`; `firewall_hint(port) -> str | None` → `f"ufw allow {port}/tcp"`.
  - `panel/db.py`: `create_inbound(data: dict) -> dict`, `update_inbound(inbound_id: str, fields: dict) -> dict | None`, `delete_inbound(inbound_id: str) -> bool`, `count_users_per_inbound() -> dict[str, int]`, `default_inbound_id(protocol: str) -> str | None` (enabled, lowest `created_at`).
  - `routes_inbounds.py`: `router = APIRouter(prefix="/api", dependencies=[Depends(auth.require_admin)])` (mirror routes_admin).
    - `GET /api/inbounds` → `{"inbounds": [...], "user_counts": {...}}`
    - `POST /api/inbounds` body `{protocol, port, listen?, remark?, enabled?, settings}` → gates: pydantic-level `validate_settings` (400), reserved (409), `port_available` (409), then `apply_inbound_change` with `commit=lambda: db.create_inbound(row)` / `revert=lambda: db.delete_inbound(id)` → `-test` fail = **422 `{"detail": msg, "saved": false}`**; success 201 + `firewall_hint` when applicable.
    - `PUT /api/inbounds/{id}` → same gates; commit=`db.update_inbound`, revert=inverse patch (capture prior row).
    - `DELETE /api/inbounds/{id}` → 404 unknown; 409 `{"detail": "N users still assigned"}`; else apply-without-inbound → commit=`db.delete_inbound`, revert=re-insert captured row.
  - `app.py`: `app.include_router(inbounds_router)`.

- [ ] **Step 1: Failing tests** — `tests/test_inbounds.py`: `test_settings_normalize_defaults`, `test_reality_vless_only`, `test_flow_requires_vless_reality`, `test_tls_requires_cert_paths`, `test_reserved_ports_rejected`, `test_firewall_hint`. `tests/test_api.py`: `test_create_inbound_ok_with_hint`, `test_create_inbound_reserved_409`, `test_create_inbound_duplicate_port_409`, `test_create_inbound_occupied_port_409`, `test_xray_reject_422_no_persist` (settings with nonexistent `cert_file` → real `xray -test` fails → assert 422 AND `db.get_inbound(id) is None`), `test_delete_inbound_with_users_409`, `test_delete_inbound_ok`, `test_list_includes_user_counts`. Gate-only (IN_DOCKER, same file): `test_fresh_reality_inbound_serves_traffic` — POST a vless+reality inbound on a free port, build a client config from its returned/stored settings, connect and fetch through it (spec §10 headline criterion).
- [ ] **Step 2: Run to verify FAIL.**
- [ ] **Step 3: Implement** module → db mutations → routes → router include.
- [ ] **Step 4: Run to verify PASS** — `pytest tests/test_inbounds.py tests/test_api.py -q`.
- [ ] **Step 5: Commit** — `git commit -am "A2 T5: inbound CRUD API with five-gate save flow"`

---

### Task 6: inbound-driven link building

**Files:**
- Modify: `panel/links.py` (rewrite), `panel/routes_admin.py:147-150,232`, `panel/routes_account.py:74`, `panel/paths.py` (delete `vpn_path`)
- Test: `tests/test_links.py` (rewrite), `tests/test_config_builder.py` (delete `test_vpn_path_format`), caller tests

**Interfaces:**
- Consumes: inbound row shape (Task 2), `db.get_inbound`.
- Produces: `build_link(user: dict, inbound: dict, public_host: str) -> str` and `build_vmess_obj(user, inbound, public_host) -> dict` — `install_token` param **gone** everywhere; blank `public_host` still raises `ValueError`.
- Query construction (exact, per matrix): reality → `security=reality&sni={server_names[0]}&fp={fp}&pbk={public_key}&sid={short_ids[0]}&flow={flow}&type={transport}` (+`serviceName`/`path`/`mode` per transport); tls → `security=tls&sni={server_name or public_host}&type=…`; none + non-tcp → `security=none&type=…` (+`path`/`host`/`serviceName`); tcp+none → **no query**. Port = `inbound["port"]`. vmess JSON mirrors the same values (`add`, `port`, `net`, `tls`, `sni`, `path`/`serviceName`, `aid="0"`).
- Callers fetch `inbound = db.get_inbound(user["inbound_id"])`; `None` → `HTTPException(409, "user has no inbound")` (helper `_inbound_or_404(user)` in routes_admin; routes_account raises the same 409).

- [ ] **Step 1: Failing tests** — rewrite `tests/test_links.py`: `test_link_matrix_reality_exact_fields` (full pinned string incl. pbk/sni/sid/flow), `test_link_matrix_ws_tls`, `test_link_matrix_grpc_service_name`, `test_link_matrix_xhttp_mode`, `test_link_matrix_trojan_plain_no_query`, `test_link_matrix_ss_tcp` + ss/ws variant, `test_vmess_obj_exact_fields` (rewritten to inbound args), keep `test_blank_public_host_raises_valueerror` + QR roundtrip (new signature). Delete `test_vpn_path_format`.
- [ ] **Step 2: Run to verify FAIL.**
- [ ] **Step 3: Implement** `links.py` per matrix; update the 3 caller sites (fetch inbound; drop `install_token` reads); delete `vpn_path` from `panel/paths.py`.
- [ ] **Step 4: Run FULL suite**, repair caller tests (`test_account`, `test_api`, `test_pages` link assertions).
- [ ] **Step 5: Commit** — `git commit -am "A2 T6: links derive port/security from the inbound row; drop install_token"`

---

### Task 7: user endpoints — `inbound_id` auto-assign

**Files:**
- Modify: `panel/db.py:_USER_COLUMNS` (+`inbound_id`), `panel/routes_admin.py` (UserCreate/UserUpdate/bulk/user builder), `panel/inbounds.py` (`resolve_inbound_id`)
- Test: `tests/test_api.py`, `tests/test_inbounds.py`

**Interfaces:**
- Consumes: `db.default_inbound_id` (Task 5).
- Produces: `inbounds.resolve_inbound_id(protocol: str, requested: str | None, inbounds: list[dict]) -> str` — requested must exist, be enabled, and match `protocol` (else `ValueError`); else default (lowest `created_at` enabled of that protocol); none → `ValueError("no enabled <p> inbound — create one first")`. Routes map ValueError → **400 `{"detail": …}`**. User rows now carry `inbound_id` (never NULL from API paths); bulk create resolves once for all rows.

- [ ] **Step 1: Failing tests** — `test_create_user_auto_assigns_inbound`, `test_create_user_explicit_inbound_id`, `test_create_user_wrong_protocol_inbound_400`, `test_create_user_no_enabled_inbound_400` (disable all vless inbounds first), `test_bulk_create_assigns`, `resolve_inbound_id` unit tests in `tests/test_inbounds.py`.
- [ ] **Step 2: Run to verify FAIL.**
- [ ] **Step 3: Implement** — `_USER_COLUMNS` += `inbound_id` (NOT `sub_token`, that's Task 8); route resolution for single + bulk; update path: if `protocol` changes, re-resolve (same rules).
- [ ] **Step 4: Run FULL suite** (row-shape assertions gain `inbound_id`).
- [ ] **Step 5: Commit** — `git commit -am "A2 T7: users auto-assign to their protocol's default inbound"`

---

### Task 8: `/sub/{token}` subscription endpoint

**Files:**
- Create: `panel/routes_sub.py`
- Modify: `panel/db.py:_USER_COLUMNS` (+`sub_token`), `panel/routes_admin.py` (set `sub_token=uuid.uuid4().hex` on single + bulk create), `panel/app.py` (include router, `app.state.sub_rate = RateLimiter(window_s=300, max_calls=30)`)
- Test: `tests/test_sub.py`, `tests/test_api.py`

**Interfaces:**
- Consumes: `links.build_link(user, inbound, public_host)` (Task 6).
- Produces:
  - `db.get_user_by_sub_token(token: str) -> dict | None`.
  - `router = APIRouter()` (NO auth dependency): `GET /sub/{token}` → lookup → `base64.b64encode(link.encode())`, `Response(content=…, media_type="text/plain", headers={"Cache-Control": "no-store"})`; unknown → 404; rate-limited via `request.app.state.sub_rate.allow(f"sub:{rate_limit_key(request)}")` else **429**.
  - New API-created users get `sub_token = uuid.uuid4().hex`.

- [ ] **Step 1: Failing tests** — `tests/test_sub.py`: `test_sub_returns_base64_link` (decode → equals `build_link` of that user; headers: `text/plain`, `no-store`), `test_sub_unknown_token_404`, `test_sub_rate_limited_429` (fake clock, 31st call), `test_sub_needs_no_session` (no auth cookie → 200); `tests/test_api.py::test_new_users_get_sub_token` (32-hex, unique across two creates).
- [ ] **Step 2: Run to verify FAIL.**
- [ ] **Step 3: Implement** module + `_USER_COLUMNS` += `sub_token` + create-path generation + app wiring.
- [ ] **Step 4: Run FULL suite.**
- [ ] **Step 5: Commit** — `git commit -am "A2 T8: public /sub/{token} endpoint with per-user tokens"`

---

### Task 9: topology move — Caddy = panel only, host networking, installer ports

**Files:**
- Modify: `Caddyfile.template`, `panel/caddy.py`, `panel/bootstrap.py:54-61`, `entrypoint.sh`, `docker-compose.yml` (vps), `install.sh`
- Test: `tests/test_integration.py` (render tests + ray-404), entrypoint tests if they reference `VPN_TOKEN`

**Interfaces:**
- Consumes: nothing from earlier tasks (topology only).
- Produces:
  - Template: fixed two-mode file — placeholders stay (`$CADDY_GLOBAL_OPTIONS`, `$CADDY_SITE_ADDRESS`), **all `/ray-` handles removed**, single `handle { reverse_proxy 127.0.0.1:8000 }`; header comment rewritten (no VPN mention).
  - `render_caddyfile(port: int, *, auto_tls: bool, public_host: str) -> str` — **`install_token` param dropped**; TLS mode site address becomes `{$PUBLIC_HOST}:8080`; plain `:{$CADDY_PORT}` unchanged; bootstrap call updated.
  - `entrypoint.sh`: drop the `VPN_TOKEN=…` capture (bootstrap runs for side effects; its token print stays harmless in logs); `AUTO_TLS=true` → `export CADDY_PORT=8080` (probe target — panel HTTPS); add `export XDG_DATA_HOME="${DATA_DIR}/caddy"` before `caddy run` (LE certs persist on the volume).
  - `docker-compose.yml` vps: `network_mode: host` replacing `ports:` (keep env_file/volumes/restart/profile).
  - `install.sh`: ufw opens `80 8080 10002 10003 10004` (443 already); health poll → `https://${PUBLIC_HOST}:8080/api/health`; update comments.
- Expected render outcomes (pin in tests): plain → `:{$CADDY_PORT}` + panel proxy, **no** `auto_https off` change needed beyond existing mode, **zero** `/ray-`; TLS → `{$PUBLIC_HOST}:8080 {` with automatic HTTPS on.

- [ ] **Step 1: Failing tests** — replace `test_render_caddyfile_routes_and_tokens` with `test_render_caddyfile_plain_is_panel_only` and keep/adapt the two AUTO_TLS tests to `:8080` site (assert `":{$CADDY_PORT}"`, `reverse_proxy 127.0.0.1:8000`, **`"/ray-" not in rendered`**, no `$VPN_TOKEN` residue); gate-only `test_ray_paths_404_through_caddy` (IN_DOCKER: `GET /ray-anything/vless` → 404).
- [ ] **Step 2: Run to verify FAIL.**
- [ ] **Step 3: Implement** template → caddy.py signature → bootstrap → entrypoint → compose → install.sh.
- [ ] **Step 4: Verify** — `bash -n install.sh entrypoint.sh`; `docker compose config` parse (default = panel+test only; `--profile vps` includes vps with `network_mode: host`); **real caddy validate both modes**: render both to `$HOME/` files and `docker run --rm -v … --entrypoint caddy fakhrapanel-test validate --config /cfg/Caddyfile --adapter caddyfile` with `PUBLIC_HOST`/`CADDY_PORT` env (TLS render must NOT contain `auto_https off`).
- [ ] **Step 5: Run FULL local suite** (fix entrypoint-test env references to `VPN_TOKEN` if any).
- [ ] **Step 6: Commit** — `git commit -am "A2 T9: Caddy fronts only the panel; vps service on host networking"`

---

### Task 10: docs, final gate, recorded counts

**Files:**
- Modify: `DEPLOY.md`, `README.md`

**Interfaces:** none (documentation of Tasks 1–9).

- [ ] **Step 1: DEPLOY.md** — new topology + port table (80 ACME/redirect, 443 REALITY, 8080 panel, 10002–10004 defaults, per-inbound `ufw allow`); Option A3 checklist: add "old links re-import once" notice + `install.sh` health URL `:8080`; honest-caveat notes from spec §9: the REALITY default target (`www.microsoft.com:443`) is admin-editable and must be reachable by both VPS and clients, the seeded trojan inbound runs **without TLS** (add a cert via `stream_settings_extra` or move those users to REALITY), and `network_mode: host` means the `vps` service is Linux-VPS-only (local Mac keeps the `panel` service); Railway section → "panel only on single-port PaaS" honesty note; verified-list counts → final numbers.
- [ ] **Step 2: README.md** — architecture diagram (three listeners), feature bullet for the inbound engine + `/sub/{token}`, quickstart unchanged, screenshots note (still v0.1 until sub-project B).
- [ ] **Step 3: Full local suite** — ` .venv/bin/python -m pytest tests/ -q` → record exact counts (baseline 134/5; expect higher).
- [ ] **Step 4: Full Docker gate** — `docker compose build test && docker compose run --rm test` → record exact counts (baseline 139; includes REALITY e2e + direct-port WS + ray-404 + no leftover old-signature skips).
- [ ] **Step 5: Write counts into DEPLOY.md verified list** (honesty rule: only numbers actually observed).
- [ ] **Step 6: Commit + push** — `git add -A && git commit -m "A2 docs: topology, ports, honest casualties; record verified counts"` then the one-off authenticated push to `origin main`.
