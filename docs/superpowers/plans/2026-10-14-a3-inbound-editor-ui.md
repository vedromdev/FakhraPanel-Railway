# A3 — Inbound Editor UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the `/inbounds` admin page — a 3X-UI-style card list plus a
sectioned add/edit modal over A2's inbound CRUD API, with every five-gate
failure surfaced honestly.

**Architecture:** Self-contained `inbounds.html` mirroring
`dashboard.html`'s proven pattern (server-rendered initial cards via Jinja,
inline vanilla-JS IIFE for all interaction, deliberate copy of the `api()`
helper until B dedups). The modal lives in its own include file
(`inbound_modal.html`, same as `user_modal.html`). Exactly one backend
change: `PUT /api/inbounds/{id}` gains `firewall_hint`.

**Tech Stack:** FastAPI + Jinja2, SQLite (`panel.db`), vanilla JS, extended
`style.css`; pytest + TestClient; Docker compose gate.

**Spec:** `docs/superpowers/specs/2026-10-14-a3-inbound-editor-ui-design.md`
(executors read both; the spec argues the *why*, this plan pins the *what*)

## Global Constraints

- **No new dependencies** — stdlib-only backend, no CDN/JS framework (VPS
  install must not need a wheel; admin UI must work offline).
- **Exactly one backend change** (spec §4.2): `PUT /api/inbounds/{id}`
  response gains `firewall_hint`. Any other backend need = stop and flag.
- Client-side filtering mirrors `panel/inbounds.py::validate_settings`;
  the server's 400/409/422 remain the authority and are always surfaced.
  **Never assume a save worked**; 422 means `saved: false` (opposite of the
  users-API 500 contract the dashboard handles).
- `firewall_hint` is only ever displayed from API responses — never
  computed in JS (`RESERVED_PORTS`/`PRE_OPENED` stay server-side).
- UI copy: honest and actionable ("Run: ufw allow 2053/tcp"); no magic
  claims (region-neutral product profile).
- Visual language: existing light theme; extend `style.css` only.
- Tests: local `.venv/bin/python -m pytest tests/ -q` (move `.env` aside
  first for hermetic runs, restore after); gate:
  `docker compose build test && docker compose run --rm test` — exit 0
  required, 0 skipped.
- **JS is never executed by tests** (no browser harness; repo precedent):
  each JS task's verification is a served-HTML contract test **plus** a
  manual drill on the dev stack (`docker compose up panel`, browse
  `https://localhost:8080/inbounds`, browser console must be clean).
- Commit per task on `main`. Push only after Task 7's full gate, with
  explicit user confirmation; any credential stays inline-only, never
  persisted.

## Review Focus

Spec-implied inputs/failures no single test exercises — each line gets its
drill in the owning task:

1. **JS runtime errors** — unexecuted JS breaks silently; a typo'd ID makes
   the whole page dead. → Every JS task drills in a real browser with the
   console open; Task 7 repeats the full click-through console-clean.
2. **Modal state leakage** — editing inbound A (carrying
   `stream_settings_extra`) then B must not leak A's unknown keys into B's
   payload. → Task 3 drill: open A, close, open B, save, `GET /api/inbounds`
   shows B without A's keys.
3. **REALITY private-key round-trip** — prefill loads `settings` containing
   `private_key`; save must return it byte-identical or REALITY breaks for
   every client. → Task 4 drill: edit only the vless seed's remark, save,
   `GET /api/inbounds` — `private_key` unchanged.
4. **Settings string vs object** — `GET /api/inbounds` returns `settings`
   as a JSON *string*; the page route parses it, `refresh()` re-parses.
   Missed parse → `[object Object]` payloads or double-parse crashes.
   → Task 3 drill: edit the ws seed and see its `path` populated in the
   form; Task 4 create-then-re-edit round-trip.
5. **Stale-row races** — another tab deletes an inbound mid-edit → PUT 404.
   Expected: modal shows "inbound '…' not found", list refreshes. →
   Task 4 drill.

---

### Task 1: `PUT /api/inbounds/{id}` returns `firewall_hint`

**Files:**
- Modify: `panel/routes_inbounds.py:129` (the success response)
- Test: `tests/test_api.py` (Task 5/6 CRUD section, after the existing
  `test_create_inbound_*` tests)

**Interfaces:**
- Produces: `PUT /api/inbounds/{id}` → `200 {"inbound": {...},
  "firewall_hint": str | None}` — same `inbounds.firewall_hint(port)` rule
  as POST (None for pre-opened ports). Task 4's JS consumes
  `body.firewall_hint` on edit saves.

- [ ] **Step 1: Write the failing tests**

```python
def test_update_inbound_returns_firewall_hint(auth_client):
    resp = auth_client.put(
        "/api/inbounds/vless",
        json={"protocol": "vless", "port": 2053, "listen": "0.0.0.0",
              "remark": "", "enabled": True, "settings": {}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["firewall_hint"] == "ufw allow 2053/tcp"


def test_update_inbound_preopened_port_no_hint(auth_client):
    resp = auth_client.put(
        "/api/inbounds/vless",
        json={"protocol": "vless", "port": 443, "listen": "0.0.0.0",
              "remark": "", "enabled": True,
              "settings": {"security": "reality",
                           "dest": "www.apple.com:443",
                           "server_names": ["www.apple.com"]}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["firewall_hint"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_api.py -k firewall_hint -v`
(moved `.env` aside first)
Expected: FAIL — `KeyError: 'firewall_hint'`

- [ ] **Step 3: Implement**

In `update_inbound`'s success path (currently
`JSONResponse(content={"inbound": db.get_inbound(inbound_id)})`), add
`"firewall_hint": inbounds.firewall_hint(row["port"])` — `inbounds` and
`row` are already in scope; POST's response is the exact precedent.

- [ ] **Step 4: Run tests to verify they pass**

Run: same as Step 2. Expected: 2 passed.

- [ ] **Step 5: Run the neighboring CRUD tests**

Run: `.venv/bin/python -m pytest tests/test_api.py -q`
Expected: all pass (no regressions in the Task 5/6 block).

- [ ] **Step 6: Commit**

```bash
git add panel/routes_inbounds.py tests/test_api.py
git commit -m "feat: PUT /api/inbounds returns firewall_hint (honest ufw warns on port change)"
```

---

### Task 2: `/inbounds` page — route, server-rendered cards, nav links

**Files:**
- Modify: `panel/routes_pages.py` (add `_inbounds` + route; add `import json`)
- Create: `panel/templates/inbounds.html`
- Modify: `panel/templates/dashboard.html:18-20` (nav link in `.user-info`)
- Modify: `panel/templates/static/style.css` (chip/mono/toolbar classes)
- Test: `tests/test_pages.py`

**Interfaces:**
- Produces: `GET /inbounds` (admin-only, 302→`/login` when logged out).
  Template context: `inbounds` — `db.list_inbounds()` rows with `settings`
  JSON-decoded to dicts; `user_counts` — `db.count_users_per_inbound()`
  (`dict[str, int]`); `title` — `settings.DASHBOARD_TITLE`.
- Template contract IDs (later tasks + tests depend on these, exactly):
  - Page: `#inbounds-list`, `#empty-hint` (hidden when list non-empty),
    `#add-inbound` ("Add Inbound" button), back link `href="/"`.
  - Card (`article.instance-card`): `data-inbound-id="<id>"`,
    `data-action="toggle"|"edit"|"delete"` buttons, protocol chip
    `.chip.chip-<protocol>`, port, remark (fallback to id),
    transport/security chips, listen, user count.
  - Modal include `inbound_modal.html` (`{% include %}`, dashboard
    precedent): shell `#inbound-modal`; form `#inbound-form`; basics
    `#inbound-protocol`, `#inbound-port`, `#inbound-listen`,
    `#inbound-remark`, `#inbound-enabled`; transport `#inbound-transport`
    with groups `data-group="ws"` (`#inbound-path`, `#inbound-host`),
    `data-group="grpc"` (`#inbound-service`), `data-group="xhttp"`
    (`#inbound-xpath`, `#inbound-mode`); security `#inbound-security` with
    `data-group="tls"` (`#inbound-cert`, `#inbound-key`) and
    `data-group="reality"` (`#inbound-dest`, `#inbound-server-names`,
    `#inbound-short-ids`, `#inbound-fp`, `#inbound-flow`, read-only
    `#inbound-public-key`, `#copy-public-key`); advanced
    `#advanced-details` > `#inbound-json` + `#apply-json` + `#json-error`;
    feedback `#inbound-error`, `#inbound-notice`; actions `#inbound-save`,
    `#inbound-cancel`, `#inbound-modal-close`.

- [ ] **Step 1: Write the failing tests**

```python
def test_inbounds_page_requires_auth(settings):
    with TestClient(create_app(settings)) as client:
        response = client.get("/inbounds", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == "/login"


def test_inbounds_page_renders_seeds_and_editor_contract(settings):
    with TestClient(create_app(settings)) as client:
        client.app.state.manager = FakeManager()
        _login(client, settings)
        response = client.get("/inbounds")
        assert response.status_code == 200
        html = response.text
        for inbound_id, port in (("vless", "443"), ("vmess", "10002"),
                                 ("trojan", "10003"), ("ss", "10004")):
            assert f'data-inbound-id="{inbound_id}"' in html
            assert port in html
        assert 'id="add-inbound"' in html
        assert 'id="inbound-modal"' in html
        assert 'id="inbounds-list"' in html
        for group in ("ws", "grpc", "xhttp", "tls", "reality"):
            assert f'data-group="{group}"' in html


def test_nav_links_between_dashboard_and_inbounds(settings):
    with TestClient(create_app(settings)) as client:
        client.app.state.manager = FakeManager()
        _login(client, settings)
        assert 'href="/inbounds"' in client.get("/").text
        page = client.get("/inbounds")
        assert page.status_code == 200
        assert 'href="/"' in page.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_pages.py -k inbounds -v`
Expected: FAIL — 404 for `/inbounds`.

- [ ] **Step 3: Implement the route**

In `routes_pages.py`, mirror `_dashboard`: `_inbounds(request)` parses each
row's `settings` via `json.loads(row["settings"] or "{}")`, builds context
as in the Interfaces block; route `@router.get("/inbounds",
response_class=HTMLResponse, dependencies=[Depends(auth.require_admin)])`
→ `async def inbounds_page(request)`. Add `import json`.

- [ ] **Step 4: Implement the templates + CSS**

Build `inbounds.html` (extends `base.html`, links `style.css`) with the
header (title, Dashboard link, Log out), toolbar, server-rendered cards
per the contract IDs (Jinja loop like dashboard's users grid;
`{% if row.enabled %}checked{% endif %}` on the toggle), empty hint, and
`{% include "inbound_modal.html" %}`. Build `inbound_modal.html` with every
contract ID above (sections per spec §3.3–3.4; reality fields hidden until
shown by JS in Task 3; `#inbound-json` inside `<details
id="advanced-details">`). Add CSS to `style.css`: `.chip` + per-protocol
variants (4 colors), `.mono`, `.toolbar` — reuse existing
`.instance-card`, `.btn*`, `.form-group`, `.modal*` classes.

- [ ] **Step 5: Run tests to verify they pass**

Run: same as Step 2. Expected: 3 passed.
Also full pages file: `.venv/bin/python -m pytest tests/test_pages.py -q`
Expected: all pass (dashboard tests unaffected by the header link).

- [ ] **Step 6: Commit**

```bash
git add panel/routes_pages.py panel/templates/inbounds.html \
        panel/templates/inbound_modal.html panel/templates/dashboard.html \
        panel/templates/static/style.css tests/test_pages.py
git commit -m "feat: /inbounds page skeleton — server-rendered cards, modal markup, nav"
```

---

### Task 3: JS core — api helper, list refresh, modal open/close, field filtering, prefill

**Files:**
- Modify: `panel/templates/inbounds.html` (inline `<script>` IIFE)
- Test: `tests/test_pages.py`

**Interfaces:**
- Consumes: Task 2 template IDs; `GET /api/inbounds` →
  `{"inbounds": [row…], "user_counts": {id: n}}` (rows' `settings` is a
  JSON **string** → `JSON.parse`).
- Produces (JS identifiers later tasks rely on): `api(path, opts)` (copy of
  dashboard's helper incl. `body.conflicts` fallback), `refresh()`,
  `renderCards()`, `buildCard(row, count)`, `openModal(rowOrNull)`,
  `closeModal()`, `fillForm(row)`, `applyVisibility()`, `showError(el,
  message)`, `state = {mode: "create"|"edit", row: object|null, settings:
  object}` (single source of truth for settings), and the filter maps
  `SECURITY_BY_PROTOCOL = {vless: ["none","tls","reality"], vmess:
  ["none","tls"], trojan: ["none","tls"], ss: ["none"]}`,
  `TRANSPORTS_BY_PROTOCOL = {vless/vmess: ["tcp","ws","grpc","xhttp"],
  trojan/ss: ["tcp"]}`.

- [ ] **Step 1: Write the failing contract test**

```python
def test_inbounds_page_js_contract(settings):
    with TestClient(create_app(settings)) as client:
        client.app.state.manager = FakeManager()
        _login(client, settings)
        response = client.get("/inbounds")
        assert response.status_code == 200
        html = response.text
        assert "/api/inbounds" in html
        assert "SECURITY_BY_PROTOCOL" in html
        assert "TRANSPORTS_BY_PROTOCOL" in html
        assert "applyVisibility" in html
        assert "body.conflicts" in html  # api() fallback, dashboard parity
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_pages.py -k js_contract -v`
Expected: FAIL — `AssertionError` on the first missing identifier.

- [ ] **Step 3: Implement the IIFE**

Mirror `dashboard.html`'s script structure: `api()` helper copied verbatim
(incl. AbortError and `body.conflicts` fallback); `state`;
`refresh()` (GET `/api/inbounds`, `JSON.parse` each row's `settings`,
`renderCards`); `buildCard` producing the same markup as the Jinja card
(incl. `data-*` attributes and per-card error slot
`data-error-for="<id>"`); `openModal(null)` (create, defaults:
transport `tcp`, security `none`, fp `chrome`, flow
`xtls-rprx-vision`, listen `0.0.0.0`) / `openModal(row)` (edit: `fillForm`
populates basics + structured fields from `state.settings`, fills read-only
`#inbound-public-key` from `settings.public_key`, `#inbound-protocol`
selectable only when the card's user count is 0); `closeModal` resets
`state` fully (Review Focus 2: no key leakage between sessions);
`applyVisibility()` rebuilds the protocol/transport/security selects from
the two filter maps, shows/hides `data-group` blocks, and prunes
now-invalid keys from `state.settings`; `#add-inbound`, edit/delete/toggle
delegated listeners wired to the Task 5 handlers (stubs calling
`console.warn` until then); `refresh()` on load. Modal open/close: `#add-inbound`,
`#inbound-modal-close`, `#inbound-cancel`, backdrop click (dashboard modal
precedent). `#copy-public-key` copies the read-only public key
(`navigator.clipboard`, dashboard's `#copy-link` precedent).

- [ ] **Step 4: Run tests to verify they pass**

Run: same as Step 2. Expected: pass. Full file:
`.venv/bin/python -m pytest tests/test_pages.py -q` — all pass.

- [ ] **Step 5: Manual drill (dev stack, console clean)**

`docker compose up panel`, browse `https://localhost:8080/inbounds`:
open modal (create) → switch protocol to `ss` → transport options reduce
to `tcp`, security to `none`; back to `vless` + security `reality` →
reality group appears; cancel. Edit the `vless` seed → form prefilled,
ws seed edit shows its `path` populated (Review Focus 4), public key
read-only and non-empty. Open edit for the trojan seed carrying unknown
keys — none visible; close; open vmess seed; nothing leaked (Review
Focus 2, first half). Delete `docker compose down` or leave running.

- [ ] **Step 6: Commit**

```bash
git add panel/templates/inbounds.html tests/test_pages.py
git commit -m "feat: inbounds page JS core — refresh, modal, protocol/transport/security filtering, prefill"
```

---

### Task 4: Save paths — create/edit, error surfacing, firewall-hint notice

**Files:**
- Modify: `panel/templates/inbounds.html` (submit handler)
- Test: `tests/test_pages.py`

**Interfaces:**
- Consumes: Task 3 `state`/`api`; Task 1's `firewall_hint` on both POST
  (201) and PUT (200) responses.
- Produces: `buildPayload()` → `{protocol, port: int, listen, remark,
  enabled: bool, settings}` where `settings` = `state.settings` minus
  empty-string values; `submitInbound(event)`; banner strings pinned by
  the contract test: `"Nothing was saved"` (422 path) and
  `"firewall_hint"` (success notice).

- [ ] **Step 1: Write the failing contract test**

```python
def test_inbounds_js_save_paths_and_error_copy(settings):
    with TestClient(create_app(settings)) as client:
        client.app.state.manager = FakeManager()
        _login(client, settings)
        html = client.get("/inbounds").text
        assert 'method: "POST"' in html
        assert 'method: "PUT"' in html
        assert "Nothing was saved" in html
        assert "firewall_hint" in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_pages.py -k save_paths -v`
Expected: FAIL on the first missing string.

- [ ] **Step 3: Implement the save path**

`#inbound-form` submit → `buildPayload()` → `POST /api/inbounds` (create)
or `PUT /api/inbounds/{id}` (edit). On success: `#inbound-notice` shows
"Inbound saved." plus, when `body.firewall_hint`, a second line "This port
isn't pre-opened — run: `ufw allow <port>/tcp`" (verbatim hint from the
API, never recomputed); close modal; `refresh()`. On failure (catch from
`api()`): keep modal open with values, show in `#inbound-error`:
- `body.saved === false` (422) → "Nothing was saved — xray rejected the
  config: <detail>" (note: opposite of the users-API 500 semantics);
- otherwise → `detail` verbatim (400 validation, 409 reserved/duplicate/
  bind), pydantic 422 arrays joined as the dashboard helper does.
Structured inputs write into `state.settings` on `input`/`change`
(recognized keys only; lists `#inbound-server-names`/`#inbound-short-ids`
split on commas → arrays; empty → key omitted). `#inbound-protocol`
change re-runs `applyVisibility()`.

- [ ] **Step 4: Run tests to verify they pass**

Run: same as Step 2. Expected: pass.

- [ ] **Step 5: Manual drills (dev stack)**

1. Create: `vless` + `ws`, port `2053`, path `/spider` → success notice
   with "ufw allow 2053/tcp"; card appears. Cleanup: `curl -k -X DELETE
   -b <cookie> https://localhost:8080/api/inbounds/<id>` (Task 5 adds the
   UI delete; the 422 drill below leaves the page dirty otherwise).
2. 422 drill: create `trojan` + `tls`, port `2054`, cert/key
   `/nonexistent/cert.pem` → modal error "Nothing was saved — xray
   rejected the config: …", modal open, values preserved (the real xray
   binary runs in the container gate).
3. Round-trips (Review Focus 3+4): edit the vless seed's remark only →
   save → `GET /api/inbounds` (browser) shows `private_key` byte-identical;
   re-open a saved ws inbound → `path` populated.
4. Race (Review Focus 5): open edit for a `curl`-created inbound, delete
   it via curl, save → modal error "inbound '…' not found".

- [ ] **Step 6: Commit**

```bash
git add panel/templates/inbounds.html tests/test_pages.py
git commit -m "feat: inbounds editor save paths — create/edit with honest five-gate error surfacing"
```

---

### Task 5: Card actions — enable/disable switch and delete

**Files:**
- Modify: `panel/templates/inbounds.html` (toggle/delete handlers)
- Test: `tests/test_pages.py`

**Interfaces:**
- Consumes: Task 3 `state`/`api`/`refresh`; card `data-action` attributes;
  per-card error slot `[data-error-for="<id>"]`.
- Produces: `handleToggle(id, row)` — full-row `PUT` with `enabled`
  flipped (no toggle endpoint exists); disable with assigned users →
  `confirm()` before the request; `handleDelete(id, row)` — `confirm()`
  then `DELETE`; 409 detail (e.g. "2 users still assigned") shown in the
  card's error slot. Pinned strings: `"data-error-for"`,
  `"cannot connect until re-enabled"`.

- [ ] **Step 1: Write the failing contract test**

```python
def test_inbounds_js_card_actions_copy(settings):
    with TestClient(create_app(settings)) as client:
        client.app.state.manager = FakeManager()
        _login(client, settings)
        html = client.get("/inbounds").text
        assert "data-error-for" in html
        assert "cannot connect until re-enabled" in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_pages.py -k card_actions -v`
Expected: FAIL.

- [ ] **Step 3: Implement handlers**

Replace the Task 3 stubs. Toggle: `state` is not involved — send the
row's full mutable fields (`protocol, port, listen, remark, settings`
serialized, `enabled` flipped) via `PUT`; on 200 `refresh()`; on error →
card error slot. When flipping **to disabled** and the card's user count
> 0 → `confirm("Disabling drops this inbound from the running config —
its N users cannot connect until re-enabled.")`; cancel aborts. The
switch's DOM state updates only after the 200 (no optimistic UI). Delete:
`confirm("<remark|id> on port <port> — delete this inbound?")` →
`DELETE /api/inbounds/<id>` → `refresh()`; 409 → card error slot with the
API's detail verbatim.

- [ ] **Step 4: Run tests to verify they pass**

Run: same as Step 2. Expected: pass.

- [ ] **Step 5: Manual drills (dev stack)**

Create a user on the dashboard (auto-assigned to the `vless` seed) →
disable the `vless` seed card → confirm appears naming the user count →
cancel leaves it enabled → confirm again → card flips after the 200.
Delete the `vless` seed → 409 card error "1 users still assigned". Re-enable
the seed. Delete any drill-created inbounds from Task 4 cleanly.

- [ ] **Step 6: Commit**

```bash
git add panel/templates/inbounds.html tests/test_pages.py
git commit -m "feat: inbounds card actions — enable/disable with losing-access warning, delete with 409 surfacing"
```

---

### Task 6: Advanced raw-JSON section

**Files:**
- Modify: `panel/templates/inbounds.html` (advanced section logic)
- Test: `tests/test_pages.py`

**Interfaces:**
- Consumes: Task 3 `state.settings` (single source of truth), Task 2's
  `#advanced-details`/`#inbound-json`/`#apply-json`/`#json-error` IDs.
- Produces: `openAdvanced()` (fills textarea with
  `JSON.stringify(state.settings, null, 2)` when `#advanced-details`
  opens), `applyJson()` (parse → sync recognized keys back into structured
  inputs via `applyVisibility()`; unknown keys stay in `state.settings`
  and survive save; parse failure → `#json-error` "Invalid JSON: <msg>",
  no state change). Pinned strings: `"applyJson"`, `"Invalid JSON"`.

- [ ] **Step 1: Write the failing contract test**

```python
def test_inbounds_js_advanced_json_contract(settings):
    with TestClient(create_app(settings)) as client:
        client.app.state.manager = FakeManager()
        _login(client, settings)
        html = client.get("/inbounds").text
        assert "applyJson" in html
        assert "Invalid JSON" in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_pages.py -k advanced_json -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`toggle` listener on `#advanced-details` → `openAdvanced()`; `#apply-json`
→ `applyJson()` as specified; `#inbound-json` textarea edits nothing until
Apply (state changes only on successful parse — spec §3.4). After Apply,
re-opened structured sections reflect recognized keys; the save path
(Task 4 `buildPayload`) already serializes `state.settings` wholesale, so
unknown keys (`stream_settings_extra`, `spiderX`, …) ride along with no
change there.

- [ ] **Step 4: Run tests to verify they passes**

Run: same as Step 2. Expected: pass.

- [ ] **Step 5: Manual drills (dev stack)**

Edit the vless seed → Advanced → Apply `{"spiderX": "/spider"}` added to
the dumped JSON → structured fields still correct → save → browser
`GET /api/inbounds` shows `spiderX` persisted. Then Apply garbage
(`{oops`) → `#json-error` "Invalid JSON: …", state unchanged, save sends
the old settings.

- [ ] **Step 6: Commit**

```bash
git add panel/templates/inbounds.html tests/test_pages.py
git commit -m "feat: advanced raw-JSON editor — two-way sync, unknown-key preservation, honest parse errors"
```

---

### Task 7: Docs + final gate

**Files:**
- Modify: `README.md` (feature bullets)
- Modify: `DEPLOY.md` (Option A3 first-deploy checklist)

**Interfaces:**
- Consumes: everything above (no code changes in this task).

- [ ] **Step 1: README feature bullet**

Add to the feature list: inbound editor at `/inbounds` — create, edit,
enable/disable and delete inbounds from the browser; REALITY public key
shown read-only for client setup; firewall hint on non-pre-opened ports.
No new screenshots (B re-captures; the existing note already says they
predate the overhaul).

- [ ] **Step 2: DEPLOY.md checklist line**

In "A3. Verify (first-deploy checklist)", after the existing steps, add:
open `https://<VPS_IP>:8080/inbounds` (default admin/admin — change it)
to add or adjust inbounds from the UI; the curl API remains as reference.

- [ ] **Step 3: Full local suite**

Move `.env` aside; run `.venv/bin/python -m pytest tests/ -q`; restore
`.env`. Expected: all prior tests + the 5 new contract/API tests pass,
8 container-gated skips (identical set as A2's final run).

- [ ] **Step 4: Docker gate**

Run: `docker compose build test && docker compose run --rm test`
(≥900s timeout; check exit code explicitly).
Expected: exit 0, 0 skipped (count = A2's 206 + 5 new).

- [ ] **Step 5: Final manual click-through (console clean)**

`docker compose up panel` → browse `https://localhost:8080/inbounds`:
full loop — create ws inbound (hint shown) → edit it → add `spiderX` via
JSON → disable/enable → delete; edit REALITY seed (remark only);
402-adjacent error drills from Tasks 4–6 still behave (nothing saved on
422). Dashboard loads with the nav link; all four seeds intact.

- [ ] **Step 6: Ledger + commit, push on user confirmation**

Log the task in `.superpowers/sdd/2026-10-14-a3-inbound-editor-ui/progress.md`
(task-done checklist + final counts), then:

```bash
git add README.md DEPLOY.md
git commit -m "docs: A3 inbound editor — README bullet, DEPLOY checklist UI path"
```

Push only after explicit user confirmation (inline-only credential; never
persisted; remind about PAT revocation).
