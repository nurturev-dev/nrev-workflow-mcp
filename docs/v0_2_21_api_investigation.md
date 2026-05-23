# v0.2.21 — Platform API Investigation

API-first probe of the nRev workflow platform's actual behavior before designing v0.2.21
MCP fixes. All probes run against `dcb67e4d-e796-42a4-8e6b-173e546a589d` (workflow
"MCP API capture — v0.2.21 reference") with `common.user@nurturev.com` JWT (last4 `HSNc`)
acting on sayanta's Google Sheets connection (`00faa45a-…`) for cross-tenant test.

Each probe is reproducible from `/tmp/probes/<n>_*.json` (request) and the response is
captured under the same prefix.

## Summary of findings

| # | Probe | Key finding | v0.2.21 impact |
|---|---|---|---|
| 1 | GET full workflow | 23 top-level keys; we surface most. Missing in our slim view: `workflow_version_id`, `simulatedWorkflowId`, `isSimulatedWorkflowInSync`, `canRunFullWorkflow`, `playVersion`, `activeAIRequest`. | Minor — add 1-2 useful ones to `get_workflow`. |
| 2 | `POST /nodes/updated-config-and-status` (settingFieldValues body) | Only returns 5 static fields. NEVER issues `dynamic_props_id` or `col_NNNN` fields. | v0.2.19's `get_node_dynamic_fields` is **incomplete** — it can't see Pipedream dynamic schema. |
| 2c | `POST /nodes/reload-props` (settings body) | Returns 13 fields including the 5 static + `col_0000..col_0004` (with `label` = sheet header) + auto-issued `dynamic_props_id` (default value `dyp_*`). **THIS is the dynamic-props endpoint.** | New tool `reload_pipedream_props` is the Rosetta Stone. Sheet header → `col_NNNN` mapping comes for free via `field.label`. |
| 3 | `reload-props` called twice with `dynamic_props_id` already set | NEW token issued each time. **Not idempotent.** | Call reload-props ONCE per settings change, store the issued token, don't re-call. |
| 4 | PUT-node-then-GET to "fix" CC `outputs.columns_metadata` after-the-fact | PUT returns 200 but platform **silently re-clobbers** outputs back to upstream's schema. **`put-node` cannot override CC outputs when block has a parent.** | The "two-step attach + PUT-fixup" v0.2.20 design is dead. Need a different workaround. |
| 5 | `paste-nodes` with explicit `outputs.columns_metadata` AND `toBlocks` (block as a ROOT with children) | **Outputs preserved correctly.** `origin_node_id` = new block's id, type = `data_manipulation.custom_code`. | Confirms the canonical "two-step" pattern: paste-nodes-as-root → add_edge-upstream. |
| 6 | `reload-props` on Update Row | 14 fields incl. two array-typed fields: `updation_criteria` and `fields_to_update`, each with `array_item_schema` describing `{column_to_match/value_to_match}` and `{column_to_update/value_to_update}`. Also `add_if_not_present` (upsert mode!) and `allColumns`. | Update Row works — we just need to send array values as **native list-of-dicts**. |
| 6c | PUT Update Row with `updation_criteria` as native list-of-dicts | Stored as `list` type, no `**` unpack error. `node_config_error: "Column 'timestamp' in updation_criteria not found in sheet"` (validation against the dest sheet — confirms the platform parsed the array correctly). | The earlier "argument after ** must be a mapping, not str" failure was **caller error** (JSON-encoded the value), not a platform bug. |
| 7 | `/execution-logs/.../{exec_id}?only_latest=true` vs no flag | Identical payload (24643 bytes both ways, 6 blockRuns either way). | Flag is a no-op for this execution shape; may matter for retries. Low priority. |

---

## Detailed findings

### Probe 1 — `GET /workflows/{wf}`

**Request**: `GET https://workflow.public.prod.nurturev.com/workflows/dcb67e4d-…`

**Response** (top-level keys, sample values):
```
id                          'dcb67e4d-e796-42a4-8e6b-173e546a589d'
workflow_version_id         'a1b27417-b1c9-47f2-8daf-053f022c3d1f'   ← used by update-workflow-and-execute body
name, description, version  …
blocks                      list[5]
status                      'draft'                                  ← draft | live
isTestMode                  False                                    ← workflow-level test toggle (separate from per-block)
canRunFullWorkflow          True                                     ← readiness signal we can surface
isRunable                   True                                     ← already in our slim view
isDirty                     True                                     ← change-tracking; useful for "needs save"
liveVersion, playVersion    None                                     ← publish history
simulatedWorkflowId         None
isSimulatedWorkflowInSync   True
activeAIRequest             None
stickyNotes                 list[0]
workflowConfigError         None
tenantId                    1
step_function_arn           None                                     ← AWS Step Functions arn for live workflows
createdAt/By, lastUpdatedAt/By
```

**v0.2.21 fix**: `get_workflow` should surface `workflow_version_id` (needed by callers of any save-and-execute flow) and optionally `isDirty` / `canRunFullWorkflow`.

---

### Probe 2 — `POST /nodes/updated-config-and-status` (existing endpoint)

**Request body** (5 static fields, `fieldNameChanged = hasHeaders`):
```json
{
  "nodeId": "<existing Add Single Row id>",
  "nodeDefinitionId": "<Add Single Row typeId>",
  "fieldNameChanged": "pipedream-...-hasHeaders",
  "settingFieldValues": [
    {connection, drive, sheetId, worksheetId, hasHeaders}  // 5 entries
  ],
  "settingsSchema": []
}
```

**Response** (HTTP 200):
```json
{
  "nodeId": "...",
  "nodeDefinition": { "fields": [...5 fields...] },   // ← only 5
  "availableOptions": {},
  "settingFieldValues": [...echo of request...]
}
```

**Finding**: This endpoint **does NOT issue `dynamic_props_id`**. Returns only the 5 static fields. v0.2.19's `get_node_dynamic_fields` (which wraps this endpoint) is incomplete — it cannot see Pipedream's dynamic per-column fields.

---

### Probe 2c — `POST /nodes/reload-props` (the dynamic-props endpoint)

**Request body** (SAME 5 fields, but body key is `settings` not `settingFieldValues`):
```json
{
  "nodeId": "<existing Add Single Row id>",
  "nodeDefinitionId": "<Add Single Row typeId>",
  "fieldNameChanged": "pipedream-...-hasHeaders",
  "settings": [
    {connection, drive, sheetId, worksheetId, hasHeaders}
  ]
}
```

**Response** (HTTP 200):
```json
{
  "componentId": "google_sheets-add-single-row",  // ← Pipedream's component slug
  "errors": [],
  "nodeId": "...",
  "fields": [...13 fields...]
}
```

The 13 fields are:
- 5 static (connection / drive / sheetId / worksheetId / hasHeaders)
- `rowIndex` (string, optional, legacy)
- `myColumnData` (string, optional, legacy — alternative to col_NNNN)
- **`col_0000` through `col_0004`** (string, optional) with **`label` = sheet header name** (`"timestamp"`, `"who"`, `"what"`, `"status"`, `"notes"`). One field per detected sheet column.
- **`dynamic_props_id`** (string, **required**, `conditionalVisibility: always_hidden`, `defaultValue: "dyp_VwUD0ppk"`) — auto-issued.

**Findings**:
1. The platform's Pipedream-action dynamic schema lives at `/nodes/reload-props`, not at `/nodes/updated-config-and-status`.
2. Body shape uses `settings` (not `settingFieldValues`).
3. Response top-level differs (`fields` is flat, not under `nodeDefinition`).
4. `dynamic_props_id` defaultValue is the platform-assigned token to persist into the node's `settings_field_values`.
5. `col_NNNN.label` IS the sheet header name — **column-to-template mapping is already done for us** at the API level.

**v0.2.21 fix**: New tool `reload_pipedream_props(workflow_id, node_id)`. Maybe deprecate `get_node_dynamic_fields` or have it merge both endpoints' responses.

---

### Probe 3 — `reload-props` called twice (idempotency)

**Request**: Same as 2c but include `dynamic_props_id = "dyp_VwUD0ppk"` (the token from probe 2c) in `settings`.

**Response**: NEW token issued (`dyp_ZjU4D77K`). **Not idempotent.**

**v0.2.21 fix**: Cache the dynamic_props_id; do NOT call reload-props on every settings update. Re-call only when one of `[connection, drive, sheetId, worksheetId, hasHeaders]` changes.

---

### Probe 4 — PUT-node-after-paste to fix CC outputs schema

**Setup**: CC `d1e5a8ab-…` has `outputs.columns = ["data"]` (clobbered from Scheduler parent). PUT it with `outputs.columns = [timestamp, who, what, status, notes]` and `outputs.columns_metadata[0].origin_node_id = d1e5a8ab-…`.

**Request**: `PUT /workflows/{wf}/nodes/{node_id}` with full block envelope including corrected outputs.

**Response**: HTTP 200. Looks like the PUT succeeded.

**But on re-fetch**: `outputs.columns` is BACK to `["data"]` and `origin_node_id` is BACK to Scheduler's id.

**Finding**: The platform **silently re-infers CC outputs from upstream parent on every save**. PUT-node cannot override this. The "two-step attach + post-paste PUT-fixup" v0.2.20 design **does not work**.

**v0.2.21 fix**: Drop the PUT-fixup approach. Instead — Probe 5 reveals the working alternative:

---

### Probe 5 — `paste-nodes` with outputs + toBlocks in one body

**Setup**: Build a fresh CC block dict with `outputs.columns_metadata` correctly set AND `toBlocks` pointing to existing Add Single Row. The new CC is configured as **a root** (no upstream parent), with Add Single Row as a downstream child.

**Request**: `POST /workflows/{wf}/paste-nodes` with body `{nodes: [<block dict>]}`.

**Response**: HTTP 200. Block created with a new id (platform reassigned UUID).

**Re-fetch**: `outputs.columns = [timestamp, who, what, status, notes]`, `origin_node_id` = new block's id, `origin_node_type = "data_manipulation.custom_code"`. **Preserved correctly.**

**Finding**: paste-nodes **respects** declared outputs when the block has NO parent (is a root). The clobber from Probe 4 happens because the CC has a parent — the platform's per-save inference looks at upstream.

**v0.2.21 fix**:
- `attach_python_block` should attach the CC as a temporary ROOT (no parent) via paste-nodes, then call `add_edge(parent → CC)` separately. Earlier in-session experiments confirm `add_edge` PRESERVES the existing outputs (the platform only clobbers on subsequent saves of THIS block, not when wiring is added).
- DON'T attach CC with `inputs` populated in the initial paste-nodes call — that's the clobber trigger.

---

### Probe 6 — Update Row schema via `reload-props`

**Request**: reload-props on a fresh Update Row block with 5 static fields bound.

**Response** (14 fields, two of which are arrays):
- `googleSheets_connection_id`, `drive`, `sheetId`, `worksheetId` (5 static)
- `col_0000`..`col_0004` (one per sheet header)
- `allColumns` (string — unclear what for; possibly "match all columns" mode)
- `dynamic_props_id` (required, default-issued)
- **`updation_criteria`** (type `array`, required) with `array_item_schema`:
  - `column_to_match` (select, required)
  - `value_to_match` (string, required)
- **`fields_to_update`** (type `array`, required) with `array_item_schema`:
  - `column_to_update` (select, required)
  - `value_to_update` (string, required)
- `add_if_not_present` (boolean) — **upsert mode!**

---

### Probe 6c — Update Row PUT with native list-of-dicts

**Request**: PUT Update Row with:
```python
updation_criteria = [
  {"pipedream-...-column_to_match": "timestamp", "pipedream-...-value_to_match": "{{timestamp}}"}
]
fields_to_update = [
  {"pipedream-...-column_to_update": "status", "pipedream-...-value_to_update": "success"}
]
```

**Response**: HTTP 200. Re-fetch shows both stored as `list` type with correct dict structure. `node_config_error: "Column 'timestamp' in updation_criteria not found in sheet"` (semantic validation against the live sheet; doesn't affect serialization).

**No** `"argument after ** must be a mapping, not str"` error — that previous failure was caused by the caller passing `value=json.dumps([...])` instead of a native Python list.

**v0.2.21 fix**: `update_node_setting` already passes the value as-is. Docstring needs to add: "for array-typed Pipedream fields (updation_criteria, fields_to_update, similar), pass `value=[{...}, {...}]` as a native Python list-of-dicts — do NOT pass a JSON-encoded string". Maybe add a runtime guard: if the schema (from reload-props) says the field is type `array` and the caller passes a string, raise with a clear error.

---

### Probe 7 — execution-logs `only_latest=true`

**Request**: GET `/execution-logs/workflow/{wf}/workflow-execution/{exec_id}` with and without `?only_latest=true`.

**Response**: Identical for this execution. Same 24643 bytes, same 6 blockRuns.

**Finding**: Flag is a no-op when an execution has no retries / re-runs. Low priority.

---

## v0.2.21 design (post-investigation)

Based on the above, here's the revised v0.2.21 scope. Each fix is now grounded in proven platform behavior.

| Fix | What | Why |
|---|---|---|
| **A** | New tool `reload_pipedream_props(workflow_id, node_id, settings_to_change)` wrapping `POST /nodes/reload-props`. Returns the 13-field schema (including col_NNNN + `label` = sheet header + auto-issued `dynamic_props_id`). | Probe 2c — unlocks the entire Pipedream dynamic schema. Closes the gap left by v0.2.19's `get_node_dynamic_fields`. |
| **B** | New tool `auto_map_pipedream_columns(workflow_id, node_id)` — calls reload-props, then for each `col_NNNN` field uses `label` to find the matching column in the upstream block's outputs, and writes `{{<upstream_col_name>}}` into the node's settings. Also stores the issued `dynamic_props_id`. | Probe 2c — `col_NNNN.label` literally tells us the sheet header. Auto-wiring becomes trivial. |
| **C** | `attach_python_block`: switch to two-step. Phase 1 — paste-nodes the CC as a root with declared outputs (preserves schema). Phase 2 — `add_edge(parent → CC)` to wire upstream. Verify outputs.columns_metadata preserved after Phase 2; if clobbered, surface a warning ("CC output schema lost; the platform inferred from upstream"). Do NOT attempt PUT-fixup — that's a no-op (Probe 4). | Probes 4 & 5 — the only way to preserve CC outputs is to never pass the block to paste-nodes with `inputs` populated. |
| **D** | `add_edge`: also flip target's `isTrigger=False` when wiring downstream of an existing root. Avoids the two-roots UI mess seen earlier this session. | Earlier in-session finding, confirmed by Probe 5 setup. |
| **E** | New tool `save_and_execute(workflow_id, target_node_id)` wrapping `POST /workflows/{wf}/nodes/{n}/update-workflow-and-execute`. Body = full workflow envelope. Atomic save+run. | User's captured cURL — the platform's own "run" button uses this. Avoids stale-state bugs of separate save + execute. |
| **F** | Document `update_node_setting`'s array-field handling: pass native list-of-dicts, NOT JSON string. Optional runtime guard: if the field schema (looked up via reload-props) is type `array` and the caller passes `str`, raise. | Probe 6c — the v0.2.20 "JSON serialization bug" was caller error. Doc fix only. |
| **G** | `list_executions` + `get_execution`: surface useful fields the slim view drops: `workflow_version_id`, `endedAt`, `error_data`. Add `version_type` param (default `draft_version`) to `list_executions`. | Probe 7 ancillary; Probe 1. |
| **H** | `tail_execution`: detect **skipped blocks** (in workflow graph but missing from `block_runs` after a terminal execution). Surface `skipped_blocks: [{block_id, name, likely_cause: "upstream emitted 0 rows / schema mismatch"}]`. | Earlier session finding — the "completed/error:null" silent skip pattern. |
| **I** | `tail_execution` + `_check_pipedream_row_error`: also detect **empty-row writes**. When a Pipedream action's payload has `updatedCells > 0` but the upstream's column names don't match the `col_NNNN.label` requirements (template substitution would fail), flag it as `pipedream_likely_garbage_write`. | The "Sheets reported success but row contains '{{who}}' literals" pattern from earlier debugging. |
| **J** | Scheduler `is_listener=False` rejection — refuse the override outright (with helpful error pointing at real data source roots OR cron mode). | Caller error in this session; constraint not documented. |
| **K** | `get_workflow` slim view: add `workflow_version_id`, `isDirty`, `canRunFullWorkflow` (and document why). | Probe 1 — these matter for save-and-execute callers. |

11 fixes. Tight scope, all grounded in Probe evidence. No more "we think the platform behaves like X" — every fix references a probe response.

---

## End-to-end validation (round-trip via raw API only)

After the 9 probes, ran a clean Sheets read→write workflow from scratch via raw curl
only — no MCP tools — to verify the documented patterns work end-to-end.

**Test workflow**: `d9807852-a2d1-4879-9e4b-fea2e0e18412` ("v0.2.21 round-trip validation").

**Sequence (each step succeeded)**:

1. `POST /workflows` with `{workflow_details: {name, description, blocks:[]}}` → workflow created.
2. `POST /workflows/{wf}/paste-nodes` with GVR block (typeId `ce01c704-...`) as root.
   GVR's `outputs.columns` initialized to `["error", "summary", "payload"]` (generic Pipedream shape).
3. `POST /nodes/reload-props` for GVR → HTTP 200 but body has `errors: ["additionalProps not a function"]` and 0 fields. **Get Values in Range does NOT have dynamic props.** Its schema is static; the "smart 5-column" output schema lives elsewhere.
4. `POST /workflows/{wf}/nodes/{gvr_id}/update-workflow-and-execute` with body `{workflow: <full wf state>}` → execution kicked off.
   Polled `/execution-logs/.../{exec_id}` → completed in 5.68s.
   **Post-execution: GVR's `outputs.columns` UPDATED to `["timestamp","who","what","status","notes"]`** — the platform inferred the 5-column smart schema from the actual response payload at execute time. ❗ **This corrects the earlier doc claim that GVR auto-detects headers at attach time — it only does so AFTER first successful execution.**
5. `POST /workflows/{wf}/paste-nodes` with ASR block (typeId `191db4a1-...`) as orphan with 5 static fields (no col_NNNN, no dyp_).
6. `POST /nodes/reload-props` for ASR → HTTP 200, 13 fields including `dynamic_props_id` (defaultValue `dyp_qZUxKmxL`) + col_0000..col_0004 (labels = `timestamp`, `who`, `what`, `status`, `notes`). **Auto-mapping confirmed.**
7. `PUT /workflows/{wf}/nodes/{asr_id}` with `node:` containing all 11 settings:
   - 5 static + `dynamic_props_id: "dyp_qZUxKmxL"`
   - col_0000=`{{timestamp}}`, col_0001=`{{who}}`, col_0002=`{{what}}`, col_0003=`{{status}}`, col_0004=`{{notes}}`
   → HTTP 200, all stored correctly.
8. `PUT /workflows/{wf}/nodes/{gvr_id}` with GVR updated to include `toBlocks → ASR` → HTTP 200, edge wired. ASR auto-flipped to `isOrphan: False, isTrigger: False`. Workflow `isRunable: True`.
9. `POST /workflows/{wf}/nodes/{asr_id}/update-workflow-and-execute` with full workflow body → execution started.
   Status after 25s: `completed` (3 min duration — GVR re-read Sheet2 then ASR wrote N rows).
10. `GET /executions/.../{exec_id}/node/{asr_id}/preview?handle_condition=_default` → returned `{data: [...], meta: {total_entries: 4, ...}}`.
    **4 rows successfully appended** to Sheet1 (A6:E6 through A9:E9, `updatedCells: 5` per row, `error: "[]"`).
    Template substitution worked perfectly — each appended row contained the actual source values from Sheet2 (timestamp="ran", who="dom", what="da", status="ta", notes="populated").

**Conclusion**: The documented design (Fixes A through K) is correct and sufficient when applied in the right sequence. The only correction needed is the GVR-schema-detection mechanism (point 4 above) — it's post-execution, not at attach time.

**Raw API artifacts** for reproduction: `/tmp/v2/*.json`.

### Sequencing implications for v0.2.21

The validated sequence reveals an implicit **ordering constraint** that the MCP must handle:

- An ASR/Update Row's `col_NNNN.label` values are the **DESTINATION sheet's headers** (from reload-props on ASR with hasHeaders=true). For template substitution to actually work at runtime, the upstream block must emit rows whose column NAMES match those labels.
- For a CC→Pipedream pattern, the CC's outputs schema is silently clobbered (Probe 4) — so the upstream's column names won't match.
- For a GVR→Pipedream pattern (the canonical / validated one), the GVR's smart-schema only populates AFTER a successful execution. So the auto-map-col_NNNN step (reload-props + template generation) MUST happen AFTER the upstream GVR has run at least once.

This drives a new helper design: `setup_sheets_pipeline(source_sheet_id, dest_sheet_id)` should:
1. Attach GVR + ASR (orphan).
2. Execute GVR once via update-workflow-and-execute → populates GVR's smart schema.
3. Reload-props on ASR → get dyp_ + col_NNNN labels.
4. PUT ASR with templates `{{<label>}}` (which now align with GVR's smart-schema columns).
5. Wire GVR → ASR.
6. Ready for end-to-end runs.

---

## Update Row ✅ RESOLVED — Probe 6c was wrong, correct shape now validated

**Investigator error retraction**: my Probe 6c claim that "native list-of-dicts works for
array fields" was wrong. The PUT round-trip succeeded but the execution always failed
with `"Column '<header>' in updation_criteria not found in sheet"` because I was sending
the wrong shape AND the wrong value type. **The doc Update Row design was based on a
misunderstanding of the platform's array-field envelope.**

The user configured Update Row in the platform UI manually and it succeeded; their
captured cURL revealed the actual shape the platform expects:

```python
# updation_criteria — CORRECT shape (validated end-to-end)
"field_value": [
    [
        {"field_name": "pipedream-google_sheets-google_sheets_update_row-column_to_match",
         "field_value": "col_0001",        # ← col_NNNN slug, NOT the header name "who"
         "fieldLabel": "who",              # ← header name annotated for UI only
         "error": None},
        {"field_name": "pipedream-google_sheets-google_sheets_update_row-value_to_match",
         "field_value": "dom",             # ← literal or {{template}}, no fieldLabel
         "error": None}
    ]
    # add more inner lists for additional criteria items (AND'd together)
]
```

```python
# fields_to_update — same shape
"field_value": [
    [
        {"field_name": "pipedream-google_sheets-google_sheets_update_row-column_to_update",
         "field_value": "col_0003",         # ← col_NNNN slug for the column to write
         "fieldLabel": "status",
         "error": None},
        {"field_name": "pipedream-google_sheets-google_sheets_update_row-value_to_update",
         "field_value": "UPDATED-API-PROBE",
         "error": None}
    ]
]
```

**Three deltas from Probe 6c's wrong claim**:

1. **List of LISTS, not list of dicts.** Outer is array (one entry per criterion/field-update
   item). Inner is a LIST of sub-field envelopes (one envelope per sub-field —
   column + value).
2. **Each sub-field is its own envelope dict** with `{field_name, field_value, fieldLabel?, error?}` —
   mirroring the `settings_field_values` envelope. The dict-of-dict shape Probe 6c showed
   was rejected at execute time.
3. **`column_to_match` / `column_to_update` values are `col_NNNN` slugs**, NOT sheet header
   names. The mapping `col_NNNN ↔ header` comes from reload-props (each col_NNNN field has
   `label` = the sheet header). The `fieldLabel` on the column-envelope is a UI annotation
   only; the platform validates against `field_value` matching a known col_NNNN slug.

Additionally, the user's cURL also confirmed:
- **`add_if_not_present` is a separate boolean field** controlling upsert behavior (when
  no row matches the criteria: skip vs append a new row).

### End-to-end validation (post-correction)

Re-PUT the existing Update Row block `74d48e6c-…` in workflow `d9807852-…` with the
correct list-of-lists + col_NNNN envelope shape (criteria: col_0001=="dom", update
col_0003 → "UPDATED-API-PROBE"). Then:

1. `node_config_error` → `None` (was "Column 'timestamp' in updation_criteria not found in sheet" before)
2. `POST .../update-workflow-and-execute` → HTTP 200, execution kicked off
3. Polled — **completed cleanly**, UR block `status: completed, err: None`
4. UR output preview returned 4 rows (one per upstream invocation), each:
   - `payload: {"updated_rows_indices": [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]}`
   - `summary: {"message": "12 row(s) updated"}`
   - `error: "[]"`

**12 rows in Sheet1 were actually updated** (all rows where who="dom" — the destination
sheet had accumulated those from earlier test runs). Sheets API confirmed.

### Implication for v0.2.21

- **Update Row support stays IN scope** for v0.2.21 — no platform bug, just a shape we got
  wrong.
- New helper for `update_node_setting` (or a dedicated `configure_update_row` tool):
  given `criteria=[(header_or_col, value), ...]` and `updates=[(header_or_col, value), ...]`
  in pythonic form, plus the dyp_/col_NNNN map from reload-props, build the correct
  list-of-lists envelope shape automatically. Auto-resolve header→col_NNNN if the caller
  passes a header name (look it up via the col_NNNN.label mapping).
- Document the shape loudly with a worked example.
- File **a separate task** to investigate why the prior `node_config_error` value
  ("Column 'timestamp' in updation_criteria not found in sheet") wasn't cleared when the
  workflowConfigError stale state persisted after PUT — this is a separate platform-side
  cache-invalidation quirk worth surfacing.

### Sub-agent's earlier finding now reframed

The sub-agent's HTTP 400 verdict was correct given the shape they sent (same as mine —
both followed Probe 6c's wrong recipe). The validator message was misleading ("Column
'timestamp' not found") because it was reporting downstream of the platform's failure to
parse the array envelopes correctly. With the right shape, the validator passes and
execution succeeds.

---

## Update Row blocker — STALE SECTION (kept for history)

After end-to-end validation of READ + WRITE worked cleanly, attempted to extend the same
workflow with an Update Row node downstream of GVR. Followed Probe 6/6c patterns exactly:
attached UR as orphan, reload-props for dyp_/col_NNNN/array schemas, PUT with native
list-of-dicts for `updation_criteria` and `fields_to_update`, wired via GVR's `toBlocks`.

PUT succeeded (HTTP 200), settings stored correctly (verified by GET), but:

- **`node_config_error` immediately after PUT**: `"Column 'timestamp' in updation_criteria not found in sheet"`
- **`POST .../update-workflow-and-execute`** on UR returns **HTTP 400**: `"Request Cannot be completed due to invalid request inputs: {'node_id': UUID('...'), 'message': \"Column 'timestamp' in updation_criteria not found in sheet\"}"`

This is reproducible regardless of:
- `value_to_match` being a template (`"{{timestamp}}"`) or literal (`"ran"`)
- Item-dict keys being prefixed (`pipedream-google_sheets-google_sheets_update_row-column_to_match`) or unprefixed (`column_to_match`)
- Whether `add_if_not_present` is set or not
- Re-issuing `dynamic_props_id` via fresh reload-props before each attempt
- Whether the field referenced in criteria is `timestamp`, `who`, or any other actual header

**The column DOES exist in Sheet1.** Independently verified:
- User added headers `timestamp | who | what | status | notes` to A1:E1 of Sheet1 manually before this session
- Same workflow's GVR + ASR successfully wrote 4 rows to Sheet1!A6:E9 using those same column names as templates — `payload.updatedCells: 5` per row, `error: "[]"`, `summary` confirms write
- A diagnostic GVR pointed at Sheet1's A1:E1 (in this same workflow) successfully populated its smart-schema with the 5 headers after first execution

**The sub-agent independently reproduced this**: separate workflow `4ba4b0d2-...`, fresh JWT session, same patterns, same error. Their report (verbatim):
> Update Row validator cannot see sheet headers — even with `hasHeaders=true`, fresh `dynamic_props_id` from reload-props, and the platform-supplied `allColumns` literal stored, `node_config_error` persists as `"Column 'timestamp' in updation_criteria not found in sheet"`. Same error in any column name (`who`, `timestamp`). The Probe 6c claim "semantic validation" was actually a real-bug — it was just non-fatal at PUT time but **fatal at execute time** (HTTP 400).

**Workaround attempts that failed**:
- `PATCH /workflows/{wf}/no-validation` → wrong endpoint (it's for editing workflow name/stickyNotes, not bypassing validation)

**Implication for v0.2.21**:
- Mark Update Row as **not supported end-to-end via API** until the platform validator bug is fixed
- Our MCP can still attach/configure Update Row blocks (Probe 6c PUT round-trip works), but cannot execute them
- Sub-agent flagged this as the highest-priority finding of the verification — agree
- New v0.2.21 task: file a platform bug report referencing this workflow + the HTTP 400 response

**Probe 6c's earlier conclusion was wrong** — I (this investigator) dismissed the
`node_config_error` as "validation against the live sheet which we don't have" but the
sub-agent (and now my own re-test) showed that's a real validator bug, not a configuration
gap. The doc-original claim that "Update Row works" is RETRACTED here.

---

## Independent verification agent report — full text

A separate `general-purpose` agent ran an independent round-trip following only this doc +
JWT (no MCP tools, no conversation context). Their verdict per fix:

| Fix | Status | Notes |
|---|---|---|
| **A** reload-props endpoint | CONFIRMED | dyp_DAUbXabz issued, 13 fields returned |
| **B** auto-map col_NNNN by label | CONFIRMED | 4 rows written A10:E13, templates substituted correctly |
| **C** attach as root, then add edge | CONFIRMED | outputs preserved through edge wiring |
| **D** flip target isTrigger=False | CONFIRMED | wire via PUT-on-parent's-toBlocks |
| **E** update-workflow-and-execute | PARTIAL — doc omits body wrapper `{"workflow": ...}` |
| **F** array fields as native list-of-dicts | CONFIRMED for SERIALIZATION; **BROKEN at execute** (see above) |
| **K** get_workflow extra fields | CONFIRMED |

**Gaps in doc the agent flagged**:
1. PUT and update-workflow-and-execute both need `{"node": ...}` / `{"workflow": ...}` wrapper — doc shows response shape but not request wrapper
2. No `/add-edge` endpoint — edges written by PUT-ing source block with `toBlocks` populated
3. Update Row reload-props returns 8 fields without hasHeaders, 14 with — useful gotcha
4. Output preview endpoint needs `?handle_condition=_default` query param
5. paste-nodes reassigns submitted IDs — callers must capture server-assigned id

**Cleanup**: agent deleted their test workflow (DELETE /workflows/{id} → HTTP 204). Mine
(`d9807852-…`) left running with all 3 swimlanes (GVR → ASR success, GVR → UR blocked, GVR1
diagnostic). User can inspect or I can clean up on request.

---

## What's still unknown (deferred to a future investigation)

- The `allColumns` field on Update Row — what does it do? May be an "update all columns at once" shortcut.
- `add_if_not_present` — does it work as a true upsert? Worth a follow-up Probe.
- The `versionType=draft_version` query param on `/execution-logs/workflow/{wf}` (probe 7's user-captured cURL) — what happens without it? Is `draft_version` the default?
- The `componentId` returned by reload-props (e.g. `google_sheets-add-single-row`) — does it appear on any block or only in reload-props responses?
- Whether `reload-props` for Get Values in Range ever returns fields, or always errors with `additionalProps not a function`. If always erroring, our `get_node_dynamic_fields` / `reload_pipedream_props` need to gracefully handle the "no dynamic props" case.
