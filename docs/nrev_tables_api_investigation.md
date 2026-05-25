# nrev-tables-service — staging API investigation

**Date**: 2026-05-25
**Service**: `https://nrev-tables-service.public.staging.nurturev.com`
**OpenAPI**: `https://nrev-tables-service.public.staging.nurturev.com/openapi.json` (also at `/docs`)
**Version**: 0.1.0
**Status**: Live on staging only. Investigated end-to-end against staging with a real JWT to prepare for an MCP wrapper once it ships to prod.

## TL;DR

A FastAPI table-store service — think "Airtable-light per tenant." Tables have typed columns (8 types incl. JSON), rows are sparse key-value maps keyed by `column_id`, filtering and pagination are first-class. **Surface is small and clean (14 endpoints excluding health)** but has 5 footguns worth a wrapper:

1. **Rows are keyed by column UUID, not column name** — every row payload requires `{column_id: value}`. MCP must translate from human-friendly column names.
2. **No DELETE on table, column, or row** — current API is append-only / update-only. A row "delete" likely requires either a future DELETE or a soft-delete column.
3. **`sortBy` on `GET /tables` is documented but broken** — every format I tried returned `Malformed sortBy entry: '...'`. Filed gap.
4. **`column_count` returned by `POST /tables` is buggy** — initial response said `0` despite 7 user-defined columns. Subsequent calls return correct count.
5. **CSV import requires a pre-staged `s3://` URI** — no upload/presign endpoint exposed on this service. Caller must stage the file externally.

The good news: **the contract is otherwise clean**. Filters echo back as `applied_filter`, type validation is strict and clear ("Cell type mismatch for column ...: expected number, got str."), system columns are properly guarded against rename/reorder, and renames are id-stable.

## Auth

- **Required**: `Authorization: Bearer <supabase_jwt>` (same JWT used by `workflow.public.*.nurturev.com`).
- **No auth → 401** `"Not Authenticated."`
- **`x-user-id` header is in the OpenAPI spec but is silently ignored at runtime.** Likely a debug override for staging. Don't depend on it.
- **Tenant scoping**: a JWT's tenant sees ALL tables created by anyone in the tenant. Tested by listing as Sayanta and seeing tables created by Harsh and adithya. Not per-user.

## Endpoint inventory (14 + 2 health)

| Method | Path | Purpose |
|---|---|---|
| POST | `/tables` | Create table (optionally with inline columns) |
| GET | `/tables` | List tables (paginated, filterable by `name` + `creators`) |
| GET | `/tables/creators` | List distinct creators in this tenant |
| GET | `/tables/{table_id}` | Get full table (incl. columns) |
| PATCH | `/tables/{table_id}` | Rename table |
| POST | `/tables/{table_id}/columns` | Add column |
| PATCH | `/tables/{table_id}/columns/{column_id}` | Rename column |
| PUT | `/tables/{table_id}/columns/{column_id}/position` | Reorder column |
| GET | `/tables/{table_id}/rows` | List rows (paginated, filterable, searchable, sortable) |
| POST | `/tables/{table_id}/rows` | Add row (one at a time — no bulk) |
| PATCH | `/tables/{table_id}/rows/{row_id}` | Update row (merge semantics; `null` deletes) |
| POST | `/tables/csv/import` | Create a new table from a CSV staged in S3 |
| POST | `/tables/{table_id}/csv/append` | Append CSV rows to existing table |
| GET | `/healthz` | Liveness — no auth |
| GET | `/readyz` | Readiness — no auth |

**Notable gaps in the surface (not bugs, missing features):**

- No DELETE for table, column, or row.
- No bulk row insert. Need to loop `POST /rows` per row → expensive for large imports unless you use the CSV path.
- No column type CHANGE (only rename). Type is set at column creation and is fixed.

## Column types (8 total)

```
text | long_text | number | boolean | date | datetime | json | row_id
```

- `row_id` is **system-reserved** — attempting to create a column of this type returns 422 "Value error, column type 'row_id' is system-reserved".
- Every new table is auto-seeded with **3 system columns** at positions 0/1/2: `row_id` (type=row_id), `added_at` (datetime), `last_updated_at` (datetime). They cannot be renamed or reordered.
- `json` accepts arbitrary nested objects (verified with `{"tags": ["lead","priority"], "src": "manual"}`).
- `date` accepts `"YYYY-MM-DD"` strings.
- `datetime` accepts ISO 8601 with or without offset; response normalizes to `+00:00` form (loses trailing `Z` if you sent one).

## Rows API — the column-UUID requirement

**This is the single biggest UX gotcha.** Row payloads are keyed by column UUID, not column name:

```json
POST /tables/{table_id}/rows
{
  "values": {
    "c81ec170-b0ae-4109-8af5-22745f8b9569": "Alice",
    "61934268-c8bb-4c2c-9784-b95f21b2a994": 87.5
  }
}
```

If you send a name instead, you get: `"Unknown column 'first_name' for table <table_id>."`

This makes sense at the storage layer (renames are id-stable, won't break stored data) but **the MCP must translate**. Two viable approaches:

- **Auto-translate** — MCP accepts `{column_name: value}` and resolves names → UUIDs via a cached `GET /tables/{table_id}`. Recommended.
- **Pass-through** — MCP accepts UUIDs and surfaces a separate `get_column_ids(table_id)` helper. Less ergonomic.

Recommended hybrid: accept names by default with a `by_column_id=True` escape hatch (mirrors the `find_email` / `update_node_setting` pattern in the workflow MCP).

## Update semantics

`PATCH /rows/{row_id}` is **merge, not replace**:

- Fields not in `values` keep their existing values.
- `null` in `values` **deletes the field** (verified: PATCHing `{email: null}` → response no longer contains the email key).
- Unknown `row_id` → 404 `"Row 9999 not found in table ..."`
- Type mismatch → 400 `"Cell type mismatch for column ...: expected number, got str."`

## List rows — filter, sort, search

Single-filter syntax via bracketed query params:

```
GET /tables/{tid}/rows
  ?filter[column_id]=<uuid>
  &filter[operator]=<op>
  &filter[value]=<v>           (repeatable for in/not_in)
  &sort_column_id=<uuid>
  &sort_direction=asc|desc     (default desc)
  &search=<freetext>
  &skip=0&limit=100
```

**All 11 filter operators verified working**:

| Operator | Verified behavior |
|---|---|
| `eq` | Exact match. `first_name eq Alice` → 1 row |
| `neq` | Negation. `first_name neq Alice` → 4 rows (out of 5) |
| `contains` | **Case-insensitive substring**. `first_name contains a` matched Frank, Dave, Alice |
| `gt` / `gte` / `lt` / `lte` | Numeric comparison verified on number column |
| `in` / `not_in` | Multi-value — repeat `filter[value]=42&filter[value]=75` |
| `is_empty` / `is_not_empty` | Counts null/missing cells correctly |

**Limitations**:
- Only ONE filter at a time. No AND/OR composition surfaced in the contract.
- `sort_column_id` requires a UUID (no sort-by-name shortcut). System cols (`row_id`, `added_at`) are valid sort keys.
- `search` is freetext — applies to text-y columns; not documented which.
- Response echoes `applied_filter`, `applied_sort`, `applied_search` in `meta` (great for debugging).

**Sort default**: descending by `row_id` (newest first).

## List tables — pagination, filters

```
GET /tables?name=<substr>&creators=<user_id>&skip=0&limit=100&sortBy=...
```

- `name` is **substring match** (case-insensitive? — tested with "Untitled" → returned 9 tables containing that substring).
- `creators` is a user_id filter. Get the IDs from `GET /tables/creators`.
- **`sortBy` is broken**. Every format I tried (`name:asc`, `name asc`, `+name`, `-name`, `name,asc`, `name|asc`, `name.asc`, `name=asc`, `name-asc`) returned `Malformed sortBy entry: '<value>'`. The bracketed form `sortBy[name]=asc` is silently ignored. **File this with the platform team.**

## Error contract

Well-formed for the most part:

| Code | Returned when | Sample |
|---|---|---|
| 200/201 | Success | — |
| 400 | Domain rule violation | `"Column <uuid> is a system column and cannot be reordered."` |
| 401 | Missing/invalid JWT | `"Not Authenticated."` |
| 404 | Resource not found | `"Row 9999 not found in table <uuid>."` |
| 409 | Conflict (e.g. duplicate column name) | `"Column name 'first_name' is already in use."` |
| 422 | Pydantic validation | Full FastAPI validation error list with `loc`/`msg`/`type` |
| 500 | Server-side failure | `"Internal Server Error"` — *no details*. Hit this twice on CSV imports with a bogus s3 URI; should be 4xx. |

## CSV import / append

Both paths require the caller to stage the CSV in S3 first. **No public upload/presign endpoint is exposed by this service** (checked `/upload`, `/uploads`, `/signed-url`, `/presigned-url`, `/files`, `/tables/csv/upload`, `/tables/csv/presign` — all 404). Probably handled by a sibling service we haven't catalogued yet.

### Import (create new table)

```
POST /tables/csv/import
{
  "s3_uri": "s3://bucket/path/file.csv",
  "table_name": "My Table",      // optional, default "Untitled table"; 5-64 chars
  "column_mapping": {
    "<CSV header>": {"name": "<target col name>", "type": "<type>", "position": <int?>}
  }
}
```

`column_mapping` is keyed by the CSV header and the value is a `CreateTableColumnInput` (same shape as inline columns on `POST /tables`).

### Append (existing table)

```
POST /tables/{table_id}/csv/append
{
  "s3_uri": "s3://bucket/path/file.csv",
  "column_mapping": {
    "<CSV header>": "<target column_id UUID>"
  }
}
```

`column_mapping` value is **just a column_id string** (different from import). Every CSV header MUST be mapped or the request is rejected. Mapping to a system column id or unknown id returns 400. Cells are coerced to the column type (empty string → null).

## Bugs / gaps flagged for the platform team

| # | Severity | Issue |
|---|---|---|
| 1 | medium | `sortBy` query param on `GET /tables` rejects every documented format. Either docs are wrong or the parser is broken. |
| 2 | low | `POST /tables` response returns `column_count: 0` even when user-defined columns were created inline. Subsequent reads return correct count. |
| 3 | medium | CSV import with bad `s3_uri` returns HTTP 500 `"Internal Server Error"`. Should be 4xx with a usable message. |
| 4 | low | `GET /tables/upload` returns 422 (string `"upload"` fails UUID validation on `table_id`). Cosmetic; could be 404. |
| 5 | low | OpenAPI declares `x-user-id` as a header param but server doesn't require it. Either remove from spec or enforce. |

## Proposed MCP tool surface

Mapping endpoints to MCP tools, mirroring the `nrev-wf` plugin's idiom (auth via `set_jwt`, secrets never persisted, every tool returns plain dicts):

| MCP tool | Wraps | Notes |
|---|---|---|
| `set_jwt(jwt)` | (in-memory) | Same model as `nrev-wf` |
| `get_auth_status()` | (in-memory) | Same |
| `list_tables(name?, creators?, skip=0, limit=100)` | `GET /tables` | Drop `sortBy` until platform fixes |
| `list_table_creators()` | `GET /tables/creators` | |
| `get_table(table_id)` | `GET /tables/{id}` | Returns full schema incl. columns |
| `create_table(name, columns=None)` | `POST /tables` | `columns` accepts `[{name, type, position?}]` |
| `rename_table(table_id, new_name)` | `PATCH /tables/{id}` | |
| `add_column(table_id, name, type, position?)` | `POST /tables/{id}/columns` | Validates type enum client-side |
| `rename_column(table_id, column_id, new_name)` | `PATCH /tables/{id}/columns/{cid}` | |
| `reorder_column(table_id, column_id, position)` | `PUT /tables/{id}/columns/{cid}/position` | |
| `list_rows(table_id, filter?, sort_by?, search?, skip, limit, columns?)` | `GET /tables/{id}/rows` | `filter` accepts `{column: "<name or id>", operator, value}`; helper resolves name→id. `columns` projection client-side. |
| `add_row(table_id, values, by_column_id=False)` | `POST /tables/{id}/rows` | Auto-translates `{column_name: value}` → `{column_id: value}` |
| `update_row(table_id, row_id, values, by_column_id=False)` | `PATCH /tables/{id}/rows/{rid}` | Same translation |
| `import_csv(s3_uri, column_mapping, table_name?)` | `POST /tables/csv/import` | |
| `append_csv(table_id, s3_uri, column_mapping, by_column_name=True)` | `POST /tables/{id}/csv/append` | Resolve names → ids if `by_column_name=True` |
| `bulk_add_rows(table_id, rows, by_column_id=False)` | (loop over `POST /rows`) | Client-side bulk wrapper. Document the per-call cost so callers prefer CSV for >100 rows. |

**Suggested initial tool count**: 16 tools.

### Footgun-fighting wrappers worth building

These have no direct API endpoint but solve real UX problems:

- **`add_row(by_column_name=True)` default** — the column-UUID requirement is the #1 footgun. Wrap it.
- **`bulk_add_rows`** with per-call retry + progress reporting — single-row insert is fine for 1-50 rows but terrible for 1000s.
- **`list_rows(columns=["name","email"])` projection** — even though the API returns all column values, the response is keyed by UUID. The MCP should optionally return a name-keyed projection.

### Open design questions for prod-launch sprint

- **Where's the upload endpoint?** Need to find the S3-staging counterpart so the MCP can offer `upload_csv_file(local_path) -> s3_uri`. Without this, CSV import is unusable from Claude.
- **Cross-tenant column linking?** Workflow MCP has the `connection_app_id` cross-user-share pattern. Does tables have anything similar (e.g. shared tables across tenants)? Not surfaced in OpenAPI; check on prod ship.
- **Webhooks / change feed?** No streaming endpoint surfaced. If the platform plans table-change webhooks, the MCP should expose subscribe/list/delete tools.
- **Versioning header?** Service version is 0.1.0 with no `X-API-Version` header. Prod ship should pin a stable version.

## Probe artifacts

| File | Purpose |
|---|---|
| `/tmp/nrev_tables_openapi.json` | Cached OpenAPI spec used for this investigation |
| Table `76801ff5-3714-46c6-802d-1cbe2fad6690` (`mcp_probe_renamed`) | Probe table on staging — 5 rows, 11 columns (3 system, 8 user). Safe to leave; staging only. |

## Tables-as-Dashboard-Backend — can it replace a BI database?

**Yes, with caveats.** Built a working live dashboard against staging (HTML + Chart.js + zero backend, queries the tables API directly from the browser). 6 KPI cards, 3 charts, top-N table, industry filter, client-side join across two tables — all wired up against `nrev-tables-service.public.staging`. Page renders in **358ms** for the single-table flow, **389ms** for the dual-table join flow. Tab open → first paint → all charts populated.

Reproduction: `dashboard.html` in this repo root + `python3 -m http.server 8767` + paste a JWT. Or use the `tables-dashboard` Preview configuration in `.claude/launch.json` (added during this investigation).

### What works for dashboards (no platform changes needed)

| Capability | Verified | Notes |
|---|---|---|
| **Real-time read** | ✅ | ~250ms for 1000 rows |
| **Parallel fetch** | ✅ | 3×1000-row pages in 520ms total |
| **Live filtering UI** | ✅ | API filter ops are fast; bracket query syntax (`filter[column_id]=...`) is the format |
| **CORS for in-browser fetch** | ✅ | Allows arbitrary origins (incl. `localhost`); no proxy needed |
| **Time-series** | ✅ | Bucket by date column client-side after fetch |
| **KPI cards (sum/avg/rate)** | ✅ | Client-side aggregation after one read |
| **Group-by + top-N** | ✅ | Client-side after read; under 5ms for 120 rows |
| **Multi-table join** | ✅ | Parallel-fetch both tables + schemas → build hash map → inner-join in <1ms. Verified yielding insights only answerable via join (e.g. "spend by team_owner") |
| **Dynamic re-rendering** | ✅ | Filter buttons re-compute aggregations from cached `allRows` array; no re-fetch needed |

### What's painful (the dashboard tax — current API)

1. **Column UUIDs everywhere**. Every read response is keyed by column UUID. Every dashboard needs a "name → id" map up front. **Solution in the wrapper**: cache `GET /tables/{id}` per dashboard session and translate at the boundary.

2. **No server-side aggregation**. No `SUM(spend) GROUP BY channel` endpoint. Means **every dashboard must fetch all relevant rows and aggregate in the client**. Fine for <10k rows (one or two 1000-row pages); painful at 100k+ (would need 100 pages). Workarounds:
   - Pre-aggregate via a workflow and write summary rows to a "metrics_daily" table that the dashboard reads.
   - Use the existing `filter` to scope down before aggregating (e.g. last 30 days of campaign_date).
   - **MCP could expose a `summarize(table_id, group_by, aggregations)` helper that paginates+aggregates in one call** — feasible until rows get really big.

3. **`limit` is restricted to `[100, 500, 1000]`**. Discovered live; not in OpenAPI. For dashboards this means: page in 1000-row chunks (most efficient) or 100-row chunks (lower latency to first paint). No native count-only / aggregate-only query.

4. **`sortBy` on `GET /tables` is broken**. Returns "Malformed sortBy entry" for every format. Not blocking for in-table dashboards but blocks "show recent tables" pickers.

5. **No JOIN or relational link**. Two tables that should be related are independent stores. Joins must happen client-side. For ~5×500 row joins this works (<400ms total); for two 100k tables it's a non-starter. **Solution**: build a `join_tables(left, right, on={left_col: right_col}, type='inner|left')` helper in the MCP.

6. **No DELETE**. Verified — DELETE on tables, rows, columns all return 405 (despite CORS `allow-methods` listing DELETE permissively). For dashboards specifically, this means **no archival / undo / "remove this row" UX**. Either roll our own soft-delete column convention (`is_deleted: boolean`) and filter on read, or wait for the platform to ship DELETE.

7. **Per-row writes are slow**. 120 rows in 15.4s = 129ms/row. For dashboard "update KPI thresholds" type writes (1-2 rows) this is fine; for bulk seeding, force users to the CSV path.

8. **No DISTINCT endpoint**. To populate a dropdown filter you need to fetch and dedupe client-side. `GET /tables/creators` is the only built-in distinct helper.

9. **No webhooks / change feed**. Dashboards have to poll. The wrapper could expose a `subscribe(table_id, callback)` that polls every N seconds and diffs against last snapshot — works but adds overhead.

### Performance numbers (live, against staging)

| Operation | Time |
|---|---|
| `GET /tables/{id}` (schema) | 80-150ms |
| `GET /tables/{id}/rows?limit=100` | ~250ms |
| `GET /tables/{id}/rows?limit=500` | ~330ms |
| `GET /tables/{id}/rows?limit=1000` | ~245ms |
| 3 parallel `?limit=1000` calls | 520ms total |
| Client-side group-by on 120 rows | <1ms |
| Inner-join 120 × 5 rows (parallel fetch + hash join + aggregate) | 389ms end-to-end |
| Dashboard full render (fetch + 3 charts + 6 KPIs + 10-row table) | 358ms |
| Single-row INSERT | ~130ms |
| Single-row PATCH | ~130ms |

**Headroom for the dashboard use case**: comfortably handles tables up to ~10k rows on initial load (one 1000-row page is ~573KB; ten parallel pages would be ~5.7MB but should complete in well under 2s). Beyond 50k rows, the client-side aggregation pattern starts hurting and we need server-side `summarize`.

### Recommended MCP additions

> **2026-05-25 correction** — the original recommendation here was 6 "dashboard tax" wrappers that *reimplemented* aggregation/joins/distinct/time-bucketing client-side in the MCP. After reading the official [dashboard developer playbook](https://github.com/nurturev/documentations/blob/main/nrev_tables/dashboard/dev_guide/dashboard_developer_playbook.md) and HLDs, **the platform already has these planned as M2 server-side endpoints**. The MCP should *wrap those* when they ship, not reimplement them. See "Correction: official dashboard pattern" below for the full picture.

**Original 6 wrappers — superseded:**

| ~~MCP tool~~ | ~~Solves~~ | Status |
|---|---|---|
| ~~`summarize`~~ | ~~"no server aggregation"~~ | Drop — wrap `POST /tables/{id}/aggregate` (M2) instead |
| ~~`join_tables`~~ | ~~"no JOIN"~~ | Drop — wrap `POST /tables/{id}/join` (M2) instead |
| ~~`distinct_values`~~ | ~~"no DISTINCT"~~ | Drop — wrap `POST /tables/{id}/columns/{col}/distinct-values` (M2) instead |
| ~~`time_bucket`~~ | ~~"manual time-series prep"~~ | Drop — covered by `aggregate` with `date_trunc: day\|week\|month` (M2) |
| `fetch_all_rows` | "limit only 100/500/1000" | **Keep** — pagination helper. Caller-side ergonomic, no platform equivalent |
| `latest_change_token` | "no webhooks" | **Hold** — until platform's change-feed plans land |

**New wrappers to add (M2 endpoints):**

| MCP tool | Wraps | Notes |
|---|---|---|
| `aggregate_table(table_id, group_by, aggregations, filter?)` | `POST /tables/{id}/aggregate` | Server-side count/sum/avg/min/max + group_by + date_trunc |
| `distinct_values(table_id, column, filter?)` | `POST /tables/{id}/columns/{col}/distinct-values` | For filter dropdowns |
| `batch_read(reads)` | `POST /tables/batch-read` | Collapses N first-paint round-trips into 1 |
| `join_rows(left_table_id, joins[], filter?, columns?, limit?)` | `POST /tables/{id}/join` | Server-side hash join up to 3 tables in M2 |

Plus, for the agent-building-customer-dashboards path:

| MCP tool | Purpose | Notes |
|---|---|---|
| `publish_dashboard(dashboard_id, assets_dir, version)` | Drop a generated dashboard into the `nrev-ui-2/public/nrev-elite/dashboards/<id>/` pipeline and bump `config/dashboard-versions.ts` | Needs `nrev-ui-2` write access (or a CI handoff). Likely v2 of the MCP. |

### Bonus: bug roster (cumulative, including dashboard-phase findings)

| # | Severity | Issue |
|---|---|---|
| 1 | medium | `sortBy` query param on `GET /tables` rejects every documented format |
| 2 | low | `POST /tables` response returns `column_count: 0` even when user-defined columns were created inline |
| 3 | medium | CSV import with bad `s3_uri` returns HTTP 500 instead of 4xx |
| 4 | low | `GET /tables/upload` returns 422 (UUID validation on string `"upload"`); could be 404 |
| 5 | low | OpenAPI declares `x-user-id` header param but server doesn't enforce it |
| 6 | medium | **`limit` query param on `GET /tables/{id}/rows` is restricted to `[100, 500, 1000]` but OpenAPI says it's an arbitrary integer with default 100.** Real-world coding against the spec → 400 errors. |
| 7 | low | **When `skip` exceeds total rows, response sets `meta.total_entries: 0`** — should reflect total table size matching the filter, not the empty page count. |
| 8 | low | **CORS `access-control-allow-methods` lists `DELETE` but no DELETE endpoint exists.** Overly permissive default; trivially misleading. |

## Status

- ✅ Surface fully mapped (14 endpoints + 2 health)
- ✅ Auth model confirmed (Bearer JWT, tenant-scoped)
- ✅ Full CRUD round-trip verified
- ✅ All 11 filter operators verified
- ✅ Edge-case behavior (system cols, dupe names, type mismatches, partial updates, `null` delete) verified
- ✅ **Dashboard-backend viability proven** — working HTML+Chart.js dashboard renders live in 358ms
- ✅ **Multi-table client-side join validated** — 389ms end-to-end for 2 tables, yields BI-class insights
- ⚠️ 8 bugs/gaps filed (see consolidated roster above)
- ⏳ CSV upload endpoint location TBD — blocking ergonomic CSV import path in the MCP
- ⏳ Awaiting prod ship before building the MCP wrapper

## Artifacts created during this investigation

| Path | Purpose |
|---|---|
| `dashboard.dev.html` (repo root) | **Dev/test only** — working campaign dashboard. Hand-rolled JWT-paste auth; not the production pattern (see correction below). Still useful for API exploration and "build outside the platform" prototypes. |
| `.claude/launch.json` | `tables-dashboard` Preview config (port 8767) — `preview_start("tables-dashboard")` to open |
| Table `671815e5-06c1-40e7-a593-1ad43597c9e4` (`dashboard_campaigns_2026_05_25`) | 120 realistic campaign rows on staging — feed for the dashboard |
| Table `f66410e6-78a9-480a-8a99-74fbb97cef5e` (`dashboard_channel_lookup_2026_05_25`) | 5-row lookup table on staging — right side of the join test |
| Table `76801ff5-3714-46c6-802d-1cbe2fad6690` (`mcp_probe_renamed`) | Original CRUD probe table — 5 rows, 11 columns, all types |

---

## Correction: official dashboard pattern (added 2026-05-25)

After this investigation landed, user pointed me at the official [dashboard developer playbook](https://github.com/nurturev/documentations/blob/main/nrev_tables/dashboard/dev_guide/dashboard_developer_playbook.md) and supporting HLDs. **Several recommendations above need to be re-read in this light.**

### The official auth model — not "paste your JWT"

Production dashboards do **not** ask the user to paste a JWT. The pattern (from the playbook §3 + HLD 01 §5):

```
user logs into app.nrev.ai (Supabase session)
  → Next.js SSR at /nrev-elite/dashboards/<id> calls POST /authorize/dashboard
  → injects window.__NREV_JWT__ + __NREV_ROLE__ + __NREV_TABLES_URL__
       + __NREV_ACCESSIBLE_RESOURCES__ into the page
  → same-origin iframe reads via window.parent.__NREV_JWT__
  → on 401, dashboard calls window.parent.__NREV_REFRESH__() — never re-prompts
```

Playbook §3 explicit ban: no `supabase.auth.getSession()` in the iframe, no magic-link/slug-token URLs, no `app.nrev.ai/api/me` calls, no JWT in localStorage, no `postMessage` for initial values.

**The `dashboard.dev.html` in this repo violates all of these.** It works because the iframe pattern hasn't shipped to staging yet and CORS allows direct browser access during dev. Once the iframe pipeline lands, `dashboard.dev.html` is still useful for **dev / API exploration / "build outside the platform" prototypes** but is NOT the customer-facing path.

### Hosting — same-origin iframes inside `app.nrev.ai`

Dashboards ship as static assets at `nrev-ui-2/public/nrev-elite/dashboards/<dashboard_id>/index.html` + JS/CSS. Same-origin is a hard requirement (HLD 03 F3): cross-origin would break the `window.parent` bridge with `SecurityError`.

Per-dashboard CI publishes versioned tarballs to S3 via GitHub Actions OIDC; `nrev-ui-2` carries `config/dashboard-versions.ts` (`{dashboard_id → version}` manifest); a postinstall hook downloads the pinned tarballs into `public/nrev-elite/dashboards/<id>/` before `next build`. No standalone "publish dashboard" CLI yet.

### The M2 query API — server-side aggregation, joins, distinct, batch

Same `nrev-tables-service`, plus 4 new analytical endpoints (HLD 02):

| Endpoint | What it does |
|---|---|
| `POST /tables/{id}/aggregate` | `count`, `count_distinct`, `sum`, `avg`, `min`, `max` + `group_by` with `date_trunc: hour\|day\|week\|month\|year` + optional inline joins |
| `POST /tables/{id}/columns/{colId}/distinct-values` | Filter-dropdown population. Server-side dedup, with filter scope. |
| `POST /tables/batch-read` | Collapses N first-paint reads into 1; per-entry partial success. |
| `POST /tables/{id}/join` | Server-side inner/left joins. M2: up to 3 joined tables; M3 adds expression indexes + Linked Record column type. |

**Plus a stricter M1 update to existing endpoints:**
- `GET /tables/{id}/rows` will accept **AND-combined `RowFilter[]`** (not just the single bracketed filter we see today)
- DELETE row endpoint **will exist** (currently 405 per probe #8)
- `in` / `not_in` operators are first-class (already work in staging — verified)
- Tenant + ACL injection is enforced platform-side: `tenant_id` is always JWT-derived (first WHERE), `account_id IN (...)` is auto-pushed on every read of a table that carries `account_id`

### The mandatory `nrevTables.js` client

Production dashboards are **forbidden** from calling `nrev-tables-service` directly via raw `fetch()`. Every dashboard must use the in-iframe `nrevTables.js` client (8 methods: `readRows`, `readRowsById`, `appendRow`, `updateRow`, `deleteRow`, `aggregate`, `distinctValues`, `batchRead`, `joinRows`). It auto-handles:

- Token refresh: single retry on 401 via `window.parent.__NREV_REFRESH__()`, deduped across concurrent calls
- Auto-injection of `account_id IN (...)` ACL scope (admin sentinel `null` skips; empty member array short-circuits client-side with "no accounts assigned yet")
- Schema cache via per-dashboard static `nrev-table-ids.js` (display name → `table_id` / `column_id` map)
- Legacy return-shape contract preserved (`null`/`false` on failure — does not throw)

The client is **copied per-dashboard** into each `public/nrev-elite/dashboards/<id>/` subtree — not an npm package, deliberately to keep dashboards versioned together.

### Implications for the MCP

1. **Two distinct MCP surfaces emerge** — one for agents-using-tables (read/write/aggregate; same JWT model as `nrev-wf`), one for agents-building-dashboards (drop assets into the `nrev-ui-2/public/` pipeline + bump manifest). The latter is v2 of the MCP.
2. **The 6 "dashboard tax" wrappers I originally proposed are mostly wrong.** Drop 4, keep `fetch_all_rows`, hold `latest_change_token`. Add 4 M2-endpoint wrappers (`aggregate_table`, `distinct_values`, `batch_read`, `join_rows`).
3. **`dashboard.dev.html` stays valuable** for dev/API exploration but should not be promoted as "the way to build dashboards." Renamed + banner added.
4. **Several "bugs" I filed may already be on the M1 roadmap** — multi-filter AND, DELETE endpoints, the `limit` enum, the `sortBy` parser. Worth cross-referencing the HLDs before re-filing.

### Status update

- ✅ Investigation re-baselined against official playbook + HLDs
- ✅ MCP tool plan corrected
- ✅ `dashboard.dev.html` renamed + warning banner added
- ⏳ Awaiting M2 prod ship before the new MCP wrappers can be built
- ⏳ The "agent-builds-customer-dashboards" v2 MCP needs more design — HLD 05 (schema gen) is the next read
