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

---

## LinkedIn Scraping

### Get Person Profile (live LinkedIn scrape)

| Property | Value |
|---|---|
| typeId | `4e5005c4-86fa-46d6-8e76-13f0cbcd5d76` |
| Shape | A (flat) |
| Category | LinkedIn Scraping |

```python
settings = {
    "linkedin_scraping-get_person_profile-linkedin_url": "{{linkedin_url}}",
}
```

**Output columns** (declared after first execution; check via `get_node` on a
run workflow for exact set): includes `linkedin_url`, `headline`, `about`,
`current_company`, `current_title`, `location`, `experience` (json list),
`education` (json list), `skills` (json list).

### Get Post by Person

| Property | Value |
|---|---|
| typeId | `cf30b3d8-3a90-4f70-bca6-2c0c87bd4ada` |
| Shape | A (flat) |
| Category | LinkedIn Scraping |

```python
settings = {
    "linkedin_scraping-get_post_by_person-linkedin_url": "{{linkedin_url}}",
}
```

---

## People Data

### Enrich People (RocketReach / paid DB enrichment)

| Property | Value |
|---|---|
| typeId | `6439527f-9aaf-441a-9c5c-7d9e5c7e3d96` |
| Shape | **B (reference-group envelope)** |
| Category | People Data |

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
| Shape | A (flat fields, many) |
| Category | People Data |

```python
settings = {
    "people_data-search_people-person_reference": [
        {"field_name": "name", "field_value": "Alice Example"},
    ],
    "people_data-search_people-per_page": 1,  # cap to control cost
}
```

The 2026-05-25 session showed Search People accepts the same `person_reference`
envelope as Enrich People — Shape B works here too despite being a "search"
node.

---

## Company Data

### Enrich Company

| Property | Value |
|---|---|
| typeId | `91ec0d74-...` (verify via `list_node_definitions(search="enrich company")`) |
| Shape | **B (reference-group envelope)** |
| Category | Company Data |

```python
settings = {
    "company_data-enrich_company-company_reference": [
        {"field_name": "domain", "field_value": "{{company_domain}}"},
        # OR linkedin_url, OR name (mutually exclusive):
        # {"field_name": "linkedin_url", "field_value": "{{company_linkedin}}"},
    ],
}
```

### Fetch Jobs

| Property | Value |
|---|---|
| typeId | (verify via `list_node_definitions(search="fetch jobs")`) |
| Shape | **B (reference-group envelope; uses `company_details` not `company_reference`)** |
| Category | Company Data |

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

## See also

- `attach_node` docstring — reference-group pattern callout
- `docs/CC_BUG_REPRO_2026_05_25.md` — Custom Code is broken; use Magic Node
- `docs/nrev_tables_api_investigation.md` — full nrev-tables service API
- `update_node_setting` hint on "Missing a field" errors (v0.2.28) — guides
  to this cookbook automatically
