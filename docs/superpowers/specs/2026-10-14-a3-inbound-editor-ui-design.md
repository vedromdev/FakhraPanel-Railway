# A3 — Inbound Editor UI — Design Spec

- **Date:** 2026-10-14
- **Status:** awaiting human review
- **Path:** architectural (spec → plan → implement)
- **Roadmap:** A1 ✅ → A2 ✅ → **A3 (this spec)** → B UI/QOL → C performance → D protocol spike

## 1. Goal

The admin manages Xray inbounds from the dashboard — **full CRUD**
(create / edit / enable-disable / delete) with a structured settings form —
no curl needed. A3 consumes A2's CRUD API as-is, plus one micro-tweak
(§4.2). Visual language stays today's; the full usability overhaul is B.

**In scope:** new `/inbounds` page (route, template, inline JS), nav links on
both pages, the `PUT` `firewall_hint` tweak, tests, docs.

**Out of scope:** B's overhaul (visual redesign, mobile, the user→inbound
picker, shared `app.js` dedup, screenshot re-capture); subscription UI;
traffic stats; duplicate-inbound; test-connect buttons; any other backend
change.

## 2. Fixed decisions (human partner, this session)

1. **Full CRUD** — parity with the existing API.
2. **Separate page** (`/inbounds`), not a dashboard section or modal-launcher.
3. **Structured form + raw-JSON escape** — schema-driven fields, plus a
   collapsible advanced textarea for full API parity (`stream_settings_extra`
   etc.).
4. **No user↔inbound picker in A3** — auto-assignment (A2/T7) keeps working;
   explicit assignment lands in B.
5. **"3X-UI style but different"** — card-per-inbound list with quick toggles
   and a big sectioned add/edit modal, skinned in Fakhra's existing light
   design language. No dark-Bootstrap clone, no 3X-UI feature sprawl.
6. A3 is not a polish pass; **B makes everything usable and user-friendly**.

## 3. Page & UX design

### 3.1 Route & template

- `GET /inbounds` in `routes_pages.py`, `dependencies=[Depends(auth.require_admin)]`
  (same as the dashboard routes), rendering `inbounds.html` with context:
  parsed inbound rows (`settings` JSON-decoded to dicts), `user_counts`,
  `title`.
- `inbounds.html` extends `base.html`, links `style.css`, and is a
  **self-contained page** mirroring `dashboard.html`'s pattern: inline JS
  IIFE, its own copies of the `api()` fetch helper and `serializeForm`
  (deliberate duplication until B dedups into shared JS).
- **Nav:** dashboard header gains an "Inbounds" link; the inbounds header
  shows the title, a "Dashboard" link, and Log out. No health widget on the
  inbounds page (dashboard keeps it; gate failures surface as errors here).

### 3.2 Card list (3X-UI-style, light skin)

Toolbar: **"Add Inbound"** button (opens the empty modal). Below: one card
per inbound —

- protocol chip (color-coded: vless / vmess / trojan / ss),
- bold port, remark (fallback to inbound id),
- transport + security chips, listen address,
- assigned-user count, **enabled switch**, edit + delete icon actions.

Initial cards are **server-rendered via a Jinja loop** (mirrors the dashboard
users grid); after every mutation JS re-renders the container by re-fetching
`GET /api/inbounds` through a JS card-builder — the same dual-render pattern
the dashboard already uses.

### 3.3 Modal editor (create + edit)

One large sectioned dialog:

- **Basics:** protocol select, port, listen (default `0.0.0.0`), remark,
  enabled checkbox.
- **Transport:** select `tcp|ws|grpc|xhttp`, filtered by protocol —
  vless/vmess → all four; trojan/ss → `tcp` only (client-side mirror of
  `validate_settings`; server 400 is the backstop). Conditional fields:
  ws → `path`, `host`; grpc → `serviceName`; xhttp → `path`, `mode`.
- **Security:** select filtered by protocol —
  vless → `none|tls|reality`; vmess/trojan → `none|tls`; ss → `none`
  (client-side mirror of `validate_settings`; the server's 400 is the
  backstop). tls → `cert_file`, `key_file`. reality (vless only) → `dest`,
  `server_names` (comma-separated), `short_ids` (comma-separated), `fp`
  (default `chrome`), `flow` (default `xtls-rprx-vision`), and a **read-only
  public-key row with a copy button**.
- **Protocol select is read-only in edit mode when users are assigned** —
  the API allows changing it, but it would silently break assigned users'
  configs; the UI prevents the footgun (a server-side guard is backlog/B).

Create → `POST /api/inbounds` (201). Edit → `PUT /api/inbounds/{id}` (200),
prefilled from the row. `private_key` is **never rendered as an input**: on
create it's omitted (server auto-gens keypair); on edit it passes through
untouched inside the settings object; changing it is advanced-JSON-only with
a warning ("invalidates every existing client config").

### 3.4 Advanced raw-JSON section

Collapsible textarea bound to the same settings object (single source of
truth):

- structured inputs write recognized keys into the state object on change;
- opening Advanced fills the textarea with `JSON.stringify(settings, null, 2)`;
- "Apply JSON": parse errors → inline message, no state change; recognized
  keys also refresh the structured inputs (transport/security re-filter);
  unknown keys (`stream_settings_extra`, `spiderX`, …) stay in the state
  object and are **preserved on save**;
- before save, keys with empty-string values are dropped (server defaults
  win; no `cert_file: ""` surprises).

Comma-separated list inputs (`server_names`, `short_ids`) are sent as JSON
arrays.

### 3.5 Card actions

- **Edit:** opens the modal prefilled.
- **Enabled switch:** waits for the API 200 before flipping (no optimistic
  UI). Inbounds have no toggle endpoint — the switch sends the **full-row
  `PUT` with `enabled` flipped** (the row data is already in the UI state).
  Disabling with `user_count > 0` → confirm: "Disabling drops this
  inbound from the running config — its N users cannot connect until
  re-enabled."
- **Delete:** confirm (remark + port); success → list refresh.

## 4. Interfaces

### 4.1 Consumed as-is (A2/T5)

| Endpoint | Use in UI |
|---|---|
| `GET /api/inbounds` | list refresh; rows carry `settings` (JSON string → parse) incl. `public_key` for REALITY display |
| `POST /api/inbounds` | create; 201 `{inbound, firewall_hint}` |
| `PUT /api/inbounds/{id}` | edit; full replace (see 4.2) |
| `DELETE /api/inbounds/{id}` | delete; 409 when users assigned |

### 4.2 The one backend tweak

`PUT /api/inbounds/{id}` response gains **`firewall_hint`** (same
`inbounds.firewall_hint(port)` rule as POST): a port change through the
editor must warn about `ufw allow` exactly like create does, or the inbound
is silently unreachable. Covered by new tests (§6).

## 5. Error handling (five-gate surfacing)

Using the existing `api()`-helper detail extraction (copied from the
dashboard). Nothing is ever assumed saved:

| Failure | UI behavior |
|---|---|
| 400 validation / 409 reserved / duplicate / bind | error banner **inside the modal**, form values preserved, modal stays open |
| 422 `{"detail", "saved": false}` (xray rejected the config) | banner: "Nothing was saved — xray rejected the config: …" |
| 409 delete-with-users | inline error with the count: "N users still assigned — reassign or delete them first." |
| Disable with users | confirm dialog with the losing-access warning (§3.5) |
| `firewall_hint` (create 201 + edit 200 when port ∉ pre-opened) | success banner includes the exact `ufw allow <port>/tcp` command |

## 6. Testing strategy (TDD; RED/GREEN per task in the plan)

- **`tests/test_pages.py`:** `/inbounds` redirects when logged out; renders
  all four seeded inbounds (ids, ports, protocol labels); "Add Inbound"
  button + modal + conditional field groups (ws/grpc/xhttp/tls/reality)
  present; the `/inbounds` nav link exists on the dashboard.
- **`tests/test_api.py`:** `PUT` changing a port to a non-pre-opened one
  (e.g. 443 → 2053) returns `firewall_hint == "ufw allow 2053/tcp"`; a PUT
  keeping a pre-opened port returns `firewall_hint is None`.
- **No browser-level JS tests** — parity with the dashboard; API behavior is
  already covered by A2's suite. Manual local click-through per task:
  create ws inbound on a new port (hint shown), edit REALITY dest, disable
  with users, delete blocked with users, invalid-JSON Apply error, and one
  forced 422 drill (tls inbound with a nonexistent cert path → "Nothing was
  saved" banner).
- **Docker gate stays green** (206 + new tests) before any push.

## 7. Documentation

- **README.md:** feature bullet — manage inbounds at `/inbounds`, no curl
  needed. Screenshots note unchanged (B re-captures; they predate A3 too).
- **DEPLOY.md:** Option A3 checklist gains the UI path —
  `https://VPS:8080/inbounds` as the primary way to add inbounds; curl
  remains the API reference.
- Ledger + plan under the established `.superpowers/sdd/` and
  `docs/superpowers/plans/` conventions.

## 8. Assumptions (presented with the design; vetoed by silence = accepted)

1. REALITY private key is never displayed in the UI (auto-generated;
   public key shown read-only).
2. No health widget on the inbounds page.
3. The disable-confirm appears only when users are assigned.
4. `server_names`/`short_ids` are comma-separated text inputs sent as
   arrays.
5. Protocol select locks on edit when users are assigned (§3.3).
