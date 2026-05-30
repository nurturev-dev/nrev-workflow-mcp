# Native node settings cookbook

Canonical settings shapes for the most-used **native** nRev node types.

**Why this exists**: `get_node_dynamic_fields` only works for Pipedream-action
nodes. For native nodes (catalog `value` slug doesn't start with `pipedream.`)
the endpoint returns HTTP 500. Without this cookbook, agents spelunk other
workflows to learn settings shapes — verified to consume ~30 turns on a
2026-05-29 session where the workflow was abandoned mid-build.

**How to use it**: find your typeId below, copy the settings dict, edit values,
pass to `attach_node(workflow_id, parent_node_ids, type_id, name, settings)`.
If your node isn't listed: search `list_workflows` for an existing workflow
that uses the typeId, then `get_node` to read its working settings.

---

## Reference shapes (top-level structural patterns)

Native nodes fall into TWO settings shapes. Picking the wrong one returns
`"Whoops! Missing a field - <X>"`. v0.2.28's `update_node_setting` hint helps
catch this at the error message; the cookbook avoids it entirely.

### Shape A — flat field (simplest)

```python
settings = {
    "<prefix>-<field>": "<value>",   # e.g. linkedin_scraping-get_person_profile-linkedin_url
}
```

### Shape B — reference-group envelope (nested)

```python
settings = {
    "<prefix>-<group_field>": [
        {"field_name": "<inner_field>", "field_value": "<value>"},
        # additional inner fields if the group accepts them
    ],
}
```

**Which one?** Check the typeId entry below. As a rule of thumb:
- "Single-input" actions (one linkedin_url, one domain) → Shape A
- "Reference" actions that disambiguate between input methods (domain OR
  linkedin OR company_name) → Shape B with the group named `<entity>_reference`

---

## Template values

For values that flow from upstream columns, use the bare `{{column_name}}`
syntax — NOT `{{data.column_name}}` (that works for Slack/Pipedream actions
but not native nodes / nrev_tables).

**Column name MUST be a valid Python identifier**: snake_case, no spaces, no
hyphens. If the upstream Input Form uses `Linkedin URL` as the column label,
template references like `{{Linkedin URL}}` will not resolve. Rename the
Input Form column to `linkedin_url` upstream.

**Templates are validated at attach-time, not runtime.** If the parent
node's `outputs.columns_metadata` does not yet declare a column matching
your template, the platform returns `"Fields not found in available data:
<name>"` and the new node is marked unrunable. This bites most often when:

- The parent is a node that declares columns only after first execution
  (Search People, Get Person Profile, most LinkedIn Scraping nodes). Work
  around it by running the parent once via `partial_execute` so it declares
  its outputs, then attach the downstream node.
- You attached the parent without setting `output_columns` on a Custom Code /
  Magic Node — the platform has no idea what columns will exist. Pass
  `output_columns=[...]` to `attach_node` / `attach_magic_node` to declare
  them statically.
- v0.2.28 attach_node Fix #3 re-PUTs the new node once if it sees this
  error, which can paper over input-cache lag — look for
  `input_refresh_recovered:True` in the response. If still failing after
  that, the column truly doesn't exist upstream.

**Templates always resolve to STRINGS.** A target column typed as `number`
or `boolean` will reject with "Cell type mismatch". Cast upstream in a
Magic Node: `df["score"] = df["score"].astype(int)`.

---

## LinkedIn Scraping

### Get Person Profile (live LinkedIn scrape)

| Property | Value |
|---|---|
| typeId | `4e5005c4-b1a5-417b-af59-453b86f489db` |
| Shape | A (flat) |
| Category | Linkedin Scraping *(lowercase k — matches the catalog string exactly; `category="LinkedIn Scraping"` returns 0 results from `list_node_definitions`)* |
| is_trigger? | **NO** — action node, MUST have a parent that supplies the `linkedin_url` column |

```python
settings = {
    "linkedin_scraping-get_person_profile-linkedin_url": "{{linkedin_url}}",
}
```

**One-off pattern**: since this can't be a workflow root, the canonical
one-off shape is `Search People` (root) → `Get Person Profile`, or a CSV /
Sheets Read (root) supplying a `linkedin_url` column → `Get Person Profile`.

**Template gotcha (verified live 2026-05-30)**: `{{linkedin_url}}` is
validated at attach-time against the upstream's known columns. If the
parent's `outputs.columns_metadata` doesn't yet include `linkedin_url` —
e.g., a Search People node that hasn't executed and therefore hasn't
declared its output schema yet — the platform returns `"Fields not found in
available data: linkedin_url"` even though the template is syntactically
correct. v0.2.28's `attach_node` defensive retry (Fix #3) re-PUTs the node
to force a refresh; check `input_refresh_recovered:True` in the response.

**Output columns** (declared after first execution; check via `get_node` on a
run workflow for exact set): includes `linkedin_url`, `headline`, `about`,
`current_company`, `current_title`, `location`, `experience` (json list),
`education` (json list), `skills` (json list).

### Get Post by Person

| Property | Value |
|---|---|
| typeId | `c854f6d7-f44d-470f-8e9c-f3c42a24a888` |
| Shape | A (flat) |
| Category | Linkedin Scraping |
| is_trigger? | YES — can be a workflow root |

```python
settings = {
    "linkedin_scraping-get_post_by_person-linkedin_url": "{{linkedin_url}}",
}
```

---

## People Data

### Enrich People (paid DB enrichment)

| Property | Value |
|---|---|
| typeId | `6439527f-abe7-44e5-b462-60e1a45be619` |
| Shape | **B (reference-group envelope)** |
| Category | People Data |
| is_trigger? | NO — needs a parent supplying the reference column |

**Alternative**: `RocketReach: Enrich People` is a separate node with typeId
`43ae6689-b0f2-44bc-b34a-970ec02dedd2` (is_trigger=True, can be root).
Same `person_reference` envelope.

```python
settings = {
    "people_data-enrich_people-person_reference": [
        {"field_name": "linkedin_url", "field_value": "{{linkedin_url}}"},
        # OR (mutually exclusive with linkedin_url):
        # {"field_name": "email", "field_value": "{{email}}"},
        # {"field_name": "name", "field_value": "{{name}}"},
    ],
    "people_data-enrich_people-enrichment_fields": [
        # subset of the allowed list — pick what you need:
        "linkedin_url", "employment_history", "title", "seniority",
        "functions", "name", "org_name", "org_primary_domain",
        "org_short_description", "headline", "org_estimated_num_employees",
        "org_keywords",
    ],
}
```

**Allowed `enrichment_fields` values** (declared in the platform):
`linkedin_url, employment_history, org_short_description, headline, title,
seniority, functions, org_estimated_num_employees, org_keywords,
org_primary_domain, name, org_name`.

**Output columns** = the subset you put in `enrichment_fields`. Order matters
for downstream column-mapping; keep the list short.

### Search People (Apollo)

| Property | Value |
|---|---|
| typeId | `15145759-901a-4a87-8db3-84cd9e734a49` |
| Shape | **B (reference-group envelope)** |
| Category | People Data |
| is_trigger? | YES — can be a workflow root |

```python
settings = {
    "people_data-search_people-person_reference": [
        # Must include at least one search criterion — name alone is too
        # loose; combine with title/company/organization. Validated live
        # 2026-05-30: passing only `name` returns
        # "At least one search criteria field must be provided".
        {"field_name": "name", "field_value": "Alice Example"},
        {"field_name": "organization_name", "field_value": "Acme Corp"},
    ],
    "people_data-search_people-per_page": 1,  # cap to control cost
}
```

**Alternative**: `RocketReach: Search People` is a separate node with typeId
`99631757-7b8a-4fc9-9733-b47d5702d9b2` (different filter fields, same
envelope shape).

---

## Company Data

### Enrich Company

| Property | Value |
|---|---|
| typeId | `1e908fa8-d63b-4a67-bb58-004dc15052e2` |
| Shape | **B (reference-group envelope)** |
| Category | Company Data |
| is_trigger? | NO — needs a parent supplying the reference column |

```python
settings = {
    "company_data-enrich_company-company_reference": [
        {"field_name": "domain", "field_value": "{{company_domain}}"},
        # OR linkedin_url, OR name (mutually exclusive):
        # {"field_name": "linkedin_url", "field_value": "{{company_linkedin}}"},
    ],
}
```

**Alternative**: `RocketReach: Enrich Company` is a separate node with typeId
`119be39f-278e-46fd-a0b9-15bc81eb85cb` (is_trigger=True). Same envelope.

### Fetch Jobs

| Property | Value |
|---|---|
| typeId | `d78f7f27-3759-4590-a6a7-525dbda774b1` |
| Shape | **B (reference-group envelope; uses `company_details` not `company_reference`)** |
| Category | Company Data |
| is_trigger? | NO |

```python
settings = {
    "company_data-fetch_jobs-company_details": [
        {"field_name": "domain", "field_value": "{{company_domain}}"},
    ],
}
```

**Note on the inconsistency**: the group field is named `company_details`,
not `company_reference`. Other Company Data nodes use `company_reference`.
File this with platform team if the inconsistency surfaces in more nodes.

---

## nRev Tables (action nodes)

For tables CRUD via the workflow runtime (NOT via the `tables_*` HTTP
wrapper tools — those talk to nrev-tables-service directly). All four nrev
tables nodes use a similar list-of-lists envelope for column values.

### Query Table (TRIGGER, read)

| Property | Value |
|---|---|
| typeId | `a1b2c3d4-0003-4000-8000-000000000003` |
| Shape | Custom (filter envelope + limit enum) |

```python
settings = {
    "nrev_tables-query_table-table_id": "<table_uuid>",
    "nrev_tables-query_table-limit": "100",   # MUST be string of an allowed enum: "100"/"500"/"1000"/"5000"/"10000"/"50000"/"100000"
    "nrev_tables-query_table-filter_operator": "AND",
    "nrev_tables-query_table-filters": [
        {"column_id": "<col_uuid>", "operator": "gt", "value": "0"},
    ],
    "nrev_tables-query_table-sort_column": "<col_uuid>",      # optional
    "nrev_tables-query_table-sort_direction": "desc",          # asc | desc
}
```

### Add Row (action, write)

| Property | Value |
|---|---|
| typeId | `a1b2c3d4-0001-4000-8000-000000000001` |
| Shape | **list-of-lists envelope** (NOT flat dict) |

```python
settings = {
    "nrev_tables-add_row-table_id": "<table_uuid>",
    "nrev_tables-add_row-column_values": [
        # OUTER list of column entries
        [
            # INNER list per column (always exactly TWO entries: column_id + value)
            {"field_name": "column_id", "field_value": "<col_uuid>", "fieldLabel": "name"},
            {"field_name": "value", "field_value": "{{name}}"},
        ],
        [
            {"field_name": "column_id", "field_value": "<col_uuid>", "fieldLabel": "score"},
            {"field_name": "value", "field_value": "{{score}}"},
        ],
    ],
}
```

**Type-coercion gotcha**: `{{column}}` templates always resolve to STRINGS.
If the target column is `number` or `boolean`, the platform rejects with
"Cell type mismatch: expected number, got str." Workaround: cast in an
upstream Magic Node BEFORE the Add Row (`df["score"] = df["score"].astype(int)`).

**Column UUID discovery**: call `get_node_dynamic_fields(workflow_id, node_id)`
after attaching with just `table_id` — the response includes
`available_options[{fieldName: "column_id", options: [{label, value}]}]` with
all column UUIDs labeled by name. No need for a separate `tables_get` call.

### Update Row (action, upsert)

| Property | Value |
|---|---|
| typeId | `a1b2c3d4-0002-4000-8000-000000000002` |
| Shape | Same envelope as Add Row, with `match_conditions` AND `fields_to_update` |

```python
settings = {
    "nrev_tables-update_row-table_id": "<table_uuid>",
    "nrev_tables-update_row-match_conditions": [
        [
            {"field_name": "column_id", "field_value": "<col_uuid>", "fieldLabel": "email"},
            {"field_name": "operator", "field_value": "eq"},
            {"field_name": "value", "field_value": "{{email}}"},
        ],
    ],
    "nrev_tables-update_row-fields_to_update": [
        [
            {"field_name": "column_id", "field_value": "<col_uuid>", "fieldLabel": "status"},
            {"field_name": "value", "field_value": "replied"},
        ],
    ],
    "nrev_tables-update_row-add_row_if_not_found": False,   # bool: upsert vs strict update
}
```

### Get Row (TRIGGER, single)

| Property | Value |
|---|---|
| typeId | `a1b2c3d4-0004-4000-8000-000000000004` |
| Shape | `match_conditions` envelope; returns single row |

```python
settings = {
    "nrev_tables-get_row-table_id": "<table_uuid>",
    "nrev_tables-get_row-match_conditions": [
        [
            {"field_name": "column_id", "field_value": "<col_uuid>", "fieldLabel": "email"},
            {"field_name": "operator", "field_value": "eq"},
            {"field_name": "value", "field_value": "alice@example.com"},
        ],
    ],
}
```

---

## Extending the cookbook

This is the v1 cut covering nodes verified in real prod workflows during the
2026-05-25 to 2026-05-29 sessions. If you encounter a native node not listed:

1. Find an existing workflow on prod that uses the typeId:
   `find_workflows_using_resource()` works for resource-bound nodes (Sheets,
   tables, Slack). For others, `list_workflows()` + manual graph scan.
2. `get_node(workflow_id, node_id)` to read its working `settings_field_values`.
3. Copy the shape into this cookbook + open a PR.

**Catalog typeId lookup**: `list_node_definitions(search="...")` returns all
matching typeIds. The catalog `value` slug tells you which family the node
belongs to (e.g. `linkedin_scraping.get_person_profile`,
`company_data.enrich_company`). That family name is the **prefix** before the
first hyphen in every settings field for that node.

---

---

## Pipedream Google Sheets — the sheetId/worksheetId trap

`pipedream.google_sheets.*` actions are Pipedream nodes, not native, but they
have a UX trap worth documenting alongside the native cookbook because agents
hit it constantly.

### `sheetId` is the SPREADSHEET URL ID — not the sheet name

The field name is misleading. `sheetId` stores the spreadsheet's URL ID
(the long string after `/d/` in a sheets.google.com URL), NOT the human-
friendly name of the workbook.

```python
# ❌ WRONG — agent passed the sheet NAME, not the ID
settings = {
    "pipedream-google_sheets-google_sheets_get_values_in_range-sheetId": "MCP Testing",
}
# Platform error: cannot find spreadsheet

# ✅ CORRECT — pass the URL ID
settings = {
    "pipedream-google_sheets-google_sheets_get_values_in_range-sheetId":
        "1_k71sm0X8Cb5mo_5M7nuPvn6vv24qYxH_1UxU4TfrIQ",
}
```

**Finding the ID**: open the sheet in a browser. URL looks like
`https://docs.google.com/spreadsheets/d/<ID>/edit#gid=<TAB>`. Copy the
`<ID>`. Or use the MCP — `list_field_options(field_name="...-sheetId")` 
returns all spreadsheets accessible to the connection as
`[{label: "Workbook Name", value: "<id>"}, ...]`.

v0.2.28: `attach_node` emits a `pipedream_field_warnings` entry when a
`*-sheetId` value doesn't look like a Google ID (e.g. has spaces, < 30 chars).
The warning has the canonical example + the `list_field_options` pointer.

### `worksheetId` is the TAB's NUMERIC ID — not the tab name

Same trap, different field. `worksheetId` is the tab's numeric `gid` (visible
in the URL after `#gid=`), NOT the display name like "Sheet1" or "Leads".

```python
# ❌ WRONG — tab name
settings = {
    "pipedream-google_sheets-google_sheets_get_values_in_range-worksheetId": "Leads",
}

# ✅ CORRECT — numeric tab ID
settings = {
    "pipedream-google_sheets-google_sheets_get_values_in_range-worksheetId": "101353668",
}
```

Same v0.2.28 warning emits for non-numeric `*-worksheetId` values.

### Canonical Get Values in Range example

```python
settings = {
    "pipedream-google_sheets-google_sheets_get_values_in_range-googleSheets_connection_id":
        "<connection_id>",                   # use list_connections to find
    "pipedream-google_sheets-google_sheets_get_values_in_range-drive":
        "My Drive",                          # or a shared drive name
    "pipedream-google_sheets-google_sheets_get_values_in_range-sheetId":
        "<spreadsheet URL ID>",
    "pipedream-google_sheets-google_sheets_get_values_in_range-worksheetId":
        "<numeric tab gid>",
    "pipedream-google_sheets-google_sheets_get_values_in_range-range":
        "A1:E100",                           # A1 notation; required
}
```

### Add Single Row / Add Multiple Rows / Upsert Row / Update/Upsert Row

Same `sheetId` + `worksheetId` trap. Plus row values come from the upstream
block's columns (the platform reads upstream column NAMES + matches to sheet
headers when `hasHeaders=true`). Don't try to specify row values directly in
`settings` — they get silently ignored.

---

## See also

- `attach_node` docstring — reference-group pattern callout
- `docs/CC_BUG_REPRO_2026_05_25.md` — Custom Code is broken; use Magic Node
- `docs/nrev_tables_api_investigation.md` — full nrev-tables service API
- `update_node_setting` hint on "Missing a field" errors (v0.2.28) — guides
  to this cookbook automatically
