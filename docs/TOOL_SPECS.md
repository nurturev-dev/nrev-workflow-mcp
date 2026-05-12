# Tool specs — v0.1

Reference for every tool the MCP server exposes. Each entry has: signature, behavior, returns, and any hidden footguns it handles.

---

## Auth

### `set_jwt(token: str) -> dict`

Stores the JWT in this MCP server process's memory. Lost on restart.

- Accepts bare token or `"Bearer <token>"` header value
- Strips leading/trailing whitespace and newlines
- Decodes the JWT payload (no signature verification — we don't have the key) to extract `exp`

**Returns**:
```json
{ "status": "set", "last4": "....", "expires_at_unix": 1234567890,
  "expires_in_minutes": 720, "expired": false }
```
or `{"status": "unset"}` if no token loaded yet.

### `get_auth_status() -> dict`

Same shape as `set_jwt` return value. Never returns the full token.

---

## Read

### `get_workflow(workflow_id: str) -> dict`

Slim view — strips `settings_field_values` from each block to keep the response small.

**Returns**:
```json
{ "id": "uuid", "name": "...", "isRunable": true, "workflowConfigError": null,
  "isTestMode": false, "block_count": 8,
  "blocks": [{ "id": "...", "name": "Scheduler", "typeId": "...",
               "position": {"x": 100, "y": 100}, "isTestMode": false,
               "node_config_error": null, "creditCostPerItem": 0,
               "toBlocks": [{"toBlockId": "...", "src_handle": "_default",
                             "tgt_handle": "_default"}] }] }
```

### `get_node(workflow_id: str, node_id: str) -> dict`

Full block JSON. Use when you need to read settings, copy a block, or debug.

Raises `ValueError` if the block isn't in the workflow.

---

## Validate

### `validate_workflow(workflow_id: str) -> dict`

Inspects the workflow for known config-error patterns:
- top-level `workflowConfigError`
- per-block `node_config_error`
- Magic Node references that aren't valid edge IDs (must be `<src>-<src_handle>-<magic>-<tgt_handle>`, not raw node UUIDs)

**Returns**:
```json
{ "valid": true, "isRunable": true, "workflowConfigError": null,
  "node_errors": [], "magic_ref_warnings": [] }
```

### `validate_custom_code(code: str) -> dict`

Standalone Python sandbox lint. Catches:

| Code | Issue |
|---|---|
| E000 | Syntax error |
| E001 | `from datetime import ...` (datetime is the pre-imported module) |
| E002 | `next(...)` (not defined in sandbox) |
| E003 | Module-level constant referenced inside `run()` (assignments outside `run()` don't propagate) |

**Returns**:
```json
{ "ok": true, "issues": [{"line": 5, "col": 0, "code": "E003",
                          "message": "Sandbox quirk: ..."}] }
```

---

## Build

### `attach_python_block(workflow_id, parent_node_id, name, code, output_columns, output_dtypes?, description?, position_x?, position_y?) -> dict`

Adds a Custom Code block downstream of an existing node. Handles:

1. **Lint** — refuses to attach if `code` has E000/E001/E002/E003 issues
2. **UUID generation** for the new block
3. **Edge** from parent to new block (idempotent — won't duplicate)
4. **`outputs.columns_metadata`** populated from `output_columns` + `output_dtypes` so downstream blocks pass validation
5. **PUT workflow** with new block + edge in one transaction
6. **Verify** — reads response back and surfaces any `node_config_error`

**Args**:
- `code`: must define `def run(...)` — first arg is the upstream df
- `output_columns`: list of column names this block produces
- `output_dtypes`: optional, parallel to `output_columns`; defaults to `"string"` for each
- `position_x`, `position_y`: optional; defaults to 400 px right of parent at same y

**Returns**:
```json
{ "ok": true, "node_id": "uuid", "node_config_error": null,
  "workflowConfigError": null, "isRunable": true,
  "lint_warnings": [] }
```

If lint blocks the attach: `{"ok": false, "stage": "lint", "issues": [...], "message": "..."}` and the workflow is **not** modified.

---

## Execute

### `partial_execute(workflow_id, target_node_id, prior_execution_id?) -> dict`

Runs ONE node. With `prior_execution_id`, reuses cached upstream from that execution — the cost-saving alternative to re-running the whole workflow when you've only changed one downstream block.

**Returns**: whatever the API returns — typically `{execution_id, status, ...}`.

> **Note**: the exact endpoint URL may need adjustment after first real run. If it 404s, check the network tab when triggering a partial-execute from the app.nrev.ai web UI and update `client.execute_node()`.

---

## Test mode

### `set_test_mode(workflow_id, on, node_id?) -> dict`

Default scope: workflow. Pass `node_id` for per-node.

**Refuses** to enable test mode on a free node (`creditCostPerItem == 0`) — test mode caps output to 5 rows, which gives no credit savings on a free node and only makes downstream debugging harder.

**Returns**:
```json
{ "ok": true, "scope": "workflow", "isTestMode": true }
```

---

## Footguns this version does NOT yet handle

These ship in v0.2:
- Magic Node fan-in (auto edge-ID generation, auto-chunking when >5 inputs)
- Strong-AI Research arm scaffold (one tool call → 5-block branch)
- Google Jobs arm scaffold (with VirtualVocations/Jobleads/Dice filter pre-baked)
- Cost dry-run before execution
