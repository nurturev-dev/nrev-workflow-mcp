"""FastMCP server — registers all v0.1 tools.

Run with:  python -m nrev_wf_mcp.server   (or via the `nrev-wf-mcp` entrypoint)
"""
from __future__ import annotations

import ast
import functools
import json as _json
import uuid
from typing import Optional

from fastmcp import FastMCP

from . import auth
from . import block_types
from . import client as api
from . import tables_client as tables_api
from .sandbox_lint import lint


mcp = FastMCP("nrev-wf-mcp")


# ─── Helper: build the API-shape settings-field entry ────────────────────────

def _sf(name: str, value, label: Optional[str] = None) -> dict:
    """Build a settings-field entry in the API's expected shape.

    The platform requires every settings entry — including nested group entries —
    to carry the full envelope (fieldLabel, error, isUserInputInFormMandatory,
    selectedInputTypeIndex, isStale). Missing keys cause silent acceptance with
    null behavior on the UI side.
    """
    return {
        "field_name": name,
        "field_value": value,
        "fieldLabel": label,
        "error": None,
        "isUserInputInFormMandatory": False,
        "selectedInputTypeIndex": None,
        "isStale": False,
    }


# ─── Helpers: columns_metadata ──────────────────────────────────────────────

# Map typeId UUIDs to the value-slug the platform stores in columns_metadata
# (origin_node_type). Most are stable platform UUIDs; extend as we learn more.
_TYPEID_TO_VALUE_SLUG = {
    block_types.CUSTOM_CODE: "data_manipulation.custom_code",
    block_types.MAGIC_NODE:  "data_manipulation.magic_node",
    block_types.CSV_WRITE:   "file_management.csv_write",
}


def _typeid_to_value_slug(type_id: Optional[str]) -> str:
    """Return the value-slug for a typeId, or the typeId itself as a safe default.

    Used when constructing columns_metadata entries — the platform stores
    `origin_node_type` as a slug like `data_manipulation.custom_code`. If we
    don't recognize the typeId, return it unchanged; the platform will reject
    if it's truly malformed.
    """
    if not type_id:
        return ""
    return _TYPEID_TO_VALUE_SLUG.get(type_id, type_id)


def _build_inputs_from_parents(parent_node_ids: list[str],
                                blocks_by_id: dict) -> list[dict]:
    """v0.2.23 Fix #3 — build the inputs[] skeleton for a downstream block from
    its parents' outputs.columns_metadata.

    Pre-v0.2.23, attach_node always set inputs to an empty skeleton
    `[{columns:[], columns_metadata: None, node_id: None, ...}]`. For
    downstream blocks (parent_node_ids non-empty), this caused workflow
    validation to say "Fields not found in available data" until the user
    ran remove_edge + add_edge to force the v0.2.18 refresh path.

    This helper mirrors that refresh logic at attach time: for each parent,
    pull its outputs[0].columns + columns_metadata and emit an input entry
    pointing at the parent. Result is identical to what add_edge would
    produce on a subsequent wiring, just baked into the initial paste-nodes
    PUT.

    Empty parent_node_ids → return the default empty skeleton (root case).
    """
    if not parent_node_ids:
        # Root block — no upstream. Default empty skeleton.
        return [{
            "columns": [], "columns_metadata": None, "file": "",
            "handle_condition": "_default", "node_id": None,
        }]
    inputs = []
    for pid in parent_node_ids:
        parent = blocks_by_id.get(pid)
        if not parent:
            # Fallback to empty skeleton for unknown parent (shouldn't happen —
            # parent validation upstream should catch this)
            inputs.append({
                "columns": [], "columns_metadata": None, "file": "",
                "handle_condition": "_default", "node_id": pid,
            })
            continue
        parent_outputs = parent.get("outputs") or [{}]
        out0 = parent_outputs[0] if parent_outputs else {}
        inputs.append({
            "node_id": pid,
            "file": out0.get("file", ""),
            "handle_condition": out0.get("handle_condition", "_default"),
            "columns": out0.get("columns") or [],
            "columns_metadata": out0.get("columns_metadata") or None,
        })
    return inputs


def _build_columns_metadata(
    output_columns: list[str],
    output_dtypes: list[str],
    *,
    origin_node_id: str,
    origin_node_name: str = "",
    origin_node_type: str = "",
) -> list[dict]:
    """Build the columns_metadata payload the platform requires.

    Bug fix in v0.2.5: prior versions omitted `origin_node_id`, causing PUT to
    return 422 in some cases (column rename, downstream-validation paths). The
    field is mandatory — the platform uses it to track which node "owns" each
    output column for downstream lineage and validation.
    """
    return [
        {
            "column_name": col,
            "data_type": dtype,
            "is_nullable": True,
            "origin_node_id": origin_node_id,
            "origin_node_name": origin_node_name,
            "origin_node_type": origin_node_type,
            "nested_fields": None,
        }
        for col, dtype in zip(output_columns, output_dtypes)
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Auth
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def set_jwt(token: str) -> dict:
    """Store the nRev workflow API JWT for this MCP server process.

    The token is held in memory only — never written to disk — and is lost on
    server restart. Re-call set_jwt after each Claude Code restart.

    Accepts the bare token or a full "Bearer <token>" header value.
    """
    return auth.set_jwt(token)


@mcp.tool()
def get_auth_status() -> dict:
    """Show whether a JWT is loaded and when it expires.

    Never returns the full token — only the last 4 characters.
    """
    return auth.status()


@mcp.tool()
def get_credit_balance() -> dict:
    """Show the tenant's current nRev credit balance.

    The tenant is resolved from the active JWT — no parameter needed. Useful
    before kicking off a credit-heavy run (or as a sanity check between
    sessions if you're juggling multiple tenants).

    Returns: {credits: <int>, note: "..."}.
    """
    try:
        balance = api.credit_balance()
    except Exception as e:
        return {"credits": None, "error": str(e)}
    return {
        "credits": int(balance),
        "note": "Tenant is resolved server-side from the JWT. To check a "
                "different tenant, set that tenant's JWT in this session.",
    }


# ═══════════════════════════════════════════════════════════════════════════
# Read
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_workflow(workflow_id: str) -> dict:
    """Slim view of a workflow's block graph.

    Per-block fields: id, name, typeId, position, isTestMode, isTrigger,
    isListener, node_config_error, toBlocks, creditCostPerItem.

    Workflow-level fields: id, name, status (draft/live), liveVersion,
    playVersion, isRunable, isTestMode, workflowConfigError, block_count.

    `isTrigger` and `isListener` (v0.2.15) let you audit trigger flags at a
    glance — useful for verifying the v0.2.13/.14 fixes worked (e.g. that a
    downstream Gmail node is NOT marked as a listener) without calling
    `get_node` per block. `status` / `liveVersion` show whether the workflow
    is published or still in draft (relevant for `publish_workflow` and
    `partial_execute` flow).

    Use get_node() if you need a block's full settings.
    """
    wf = api.get_workflow(workflow_id)
    blocks = []
    for b in wf.get("blocks", []):
        blocks.append({
            "id": b["id"],
            "name": b.get("variableName"),
            "typeId": b.get("typeId"),
            "position": b.get("position"),
            "isTestMode": b.get("isTestMode"),
            "isTrigger": b.get("isTrigger"),
            "isListener": b.get("isListener"),
            "node_config_error": b.get("node_config_error"),
            "creditCostPerItem": b.get("creditCostPerItem", 0),
            "toBlocks": [
                {
                    "toBlockId": e.get("toBlockId"),
                    "src_handle": e.get("edge_source_handle_condition"),
                    "tgt_handle": e.get("edge_target_handle_condition"),
                }
                for e in (b.get("toBlocks") or [])
            ],
        })
    return {
        "id": wf.get("id"),
        "name": wf.get("name"),
        "status": wf.get("status"),
        "liveVersion": wf.get("liveVersion"),
        "playVersion": wf.get("playVersion"),
        "isRunable": wf.get("isRunable"),
        "workflowConfigError": wf.get("workflowConfigError"),
        "isTestMode": wf.get("isTestMode"),
        "block_count": len(blocks),
        "blocks": blocks,
    }


@mcp.tool()
def get_node(workflow_id: str, node_id: str) -> dict:
    """Fetch a single block in full — settings, inputs, outputs, edges, everything."""
    wf = api.get_workflow(workflow_id)
    for b in wf.get("blocks", []):
        if b["id"] == node_id:
            return b
    raise ValueError(f"Node {node_id} not found in workflow {workflow_id}")


@mcp.tool()
def get_workflow_graph(workflow_id: str, format: str = "mermaid") -> dict:
    """Render the workflow's wiring as a graph for visual inspection.

    Reading the wiring crisply is the prerequisite for any safe edit. Three formats:

    - `"mermaid"` (default) — a Mermaid `flowchart LR` string. Renders in any
      markdown viewer; the model parses it well. Edge labels show source/target
      handles when they're non-default (Magic Node `df1`, `df2`, etc.).
    - `"text"` — plain adjacency listing, one block per line with its outgoing edges.
    - `"json"` — same slim view as `get_workflow` plus a computed `incoming` field
      on each block (since the API only stores forward edges, incoming is computed
      by inverting `toBlocks` across all blocks).

    Returns: {"format": "...", "graph": "<rendered string or dict>", "block_count": N}.
    """
    wf = get_workflow(workflow_id)  # slim view
    blocks = wf["blocks"]

    if format == "json":
        graph = _with_incoming_edges(blocks)
        return {"format": "json", "block_count": len(blocks), "graph": graph}
    if format == "text":
        return {"format": "text", "block_count": len(blocks),
                "graph": _to_text_graph(blocks)}
    if format == "mermaid":
        return {"format": "mermaid", "block_count": len(blocks),
                "graph": _to_mermaid(blocks, wf.get("name") or workflow_id)}
    raise ValueError(f"format must be one of 'mermaid', 'text', 'json' — got {format!r}")


def _with_incoming_edges(blocks: list[dict]) -> list[dict]:
    """Annotate each block with its computed incoming edges."""
    incoming: dict[str, list[dict]] = {b["id"]: [] for b in blocks}
    for b in blocks:
        for e in (b.get("toBlocks") or []):
            tgt = e.get("toBlockId")
            if tgt in incoming:
                incoming[tgt].append({
                    "fromBlockId": b["id"],
                    "fromName": b.get("name"),
                    "src_handle": e.get("src_handle"),
                    "tgt_handle": e.get("tgt_handle"),
                })
    return [{**b, "incoming": incoming[b["id"]]} for b in blocks]


def _short_id(block_id: str) -> str:
    """Mermaid-safe short identifier — first 10 hex chars of a UUID, no hyphens."""
    return "n_" + block_id.replace("-", "")[:10]


def _mermaid_label(block: dict) -> str:
    name = (block.get("name") or "untitled").replace('"', "'").replace("[", "(").replace("]", ")")
    cost = block.get("creditCostPerItem") or 0
    cost_part = f"<br/>{cost}cr" if cost > 0 else ""
    err_part = "<br/>⚠ ERROR" if block.get("node_config_error") else ""
    test_part = "<br/>🧪 test" if block.get("isTestMode") else ""
    return f"{name}{cost_part}{test_part}{err_part}"


def _to_mermaid(blocks: list[dict], title: str) -> str:
    lines = [f"%% {title}", "flowchart LR"]
    sids = {b["id"]: _short_id(b["id"]) for b in blocks}
    for b in blocks:
        lines.append(f'    {sids[b["id"]]}["{_mermaid_label(b)}"]')
    for b in blocks:
        for e in (b.get("toBlocks") or []):
            tgt_full = e.get("toBlockId")
            if tgt_full not in sids:
                continue  # dangling edge — skip
            sh = e.get("src_handle") or "_default"
            th = e.get("tgt_handle") or "_default"
            if sh == "_default" and th == "_default":
                lines.append(f"    {sids[b['id']]} --> {sids[tgt_full]}")
            else:
                lines.append(f"    {sids[b['id']]} -->|{sh} → {th}| {sids[tgt_full]}")
    return "\n".join(lines)


def _to_text_graph(blocks: list[dict]) -> str:
    by_id = {b["id"]: b for b in blocks}
    lines = []
    for b in blocks:
        cost = b.get("creditCostPerItem") or 0
        cost_str = f" ({cost}cr)" if cost > 0 else ""
        lines.append(f"[{b.get('name')}]{cost_str}  id={b['id'][:8]}…")
        for e in (b.get("toBlocks") or []):
            tgt = by_id.get(e.get("toBlockId"))
            tgt_name = tgt.get("name") if tgt else "??(missing)"
            sh = e.get("src_handle") or "_default"
            th = e.get("tgt_handle") or "_default"
            arrow = "→" if (sh == "_default" and th == "_default") else f"={sh}/{th}=>"
            lines.append(f"    {arrow} [{tgt_name}]")
    return "\n".join(lines)


@mcp.tool()
def list_executions(workflow_id: str, limit: int = 10) -> dict:
    """List recent executions of a workflow (slim view).

    Replaces the manual `curl /execution-logs/workflow/{wf}?limit=N` step.
    Returns: {"executions": [{id, status, creditsUsed, nodeExecutionCount,
    createdAt, ...}], "count": N}.
    """
    raw = api.list_executions(workflow_id, limit=limit)
    items = raw.get("data", []) if isinstance(raw, dict) else raw
    slim = [
        {
            "id": e.get("id"),
            "status": e.get("status"),
            "creditsUsed": e.get("creditsUsed"),
            "nodeExecutionCount": e.get("nodeExecutionCount"),
            "createdAt": e.get("createdAt"),
            "executionTriggeredBy": e.get("executionTriggeredBy"),
            "isTestMode": e.get("isTestMode"),
        }
        for e in (items or [])
    ]
    return {"workflow_id": workflow_id, "count": len(slim), "executions": slim}


@mcp.tool()
def get_node_output(
    workflow_id: str,
    execution_id: str,
    node_id: str,
    handle_condition: str = "_default",
    skip: int = 0,
    limit: int = 50,
    columns: Optional[list[str]] = None,
    drop_json_columns: bool = False,
) -> dict:
    """Fetch the actual rows produced by a node in a specific past execution.

    This is the canonical debugger: "what data flowed through node X in run Y?"
    No need to clone or re-run anything — every past execution is preserved.

    `limit` is clamped to 100 (the API silently returns 0 rows above that).
    Use `skip` for pagination.

    Two ways to shrink response size when rows carry heavy JSON columns
    (e.g. `person_linkedin_profile` carries 40+ nested fields per row, which
    blows up context fast):

    - `columns=["x", "y"]` — project to only these columns. Other columns are
      dropped. Use when you know exactly what you want.
    - `drop_json_columns=True` — auto-drop any column whose value is a dict /
      list (i.e. is JSON-typed). Use when you want everything scalar.

    `columns` takes precedence over `drop_json_columns`. Both are applied
    client-side after the API returns — they don't change what's stored
    upstream, just what comes back over the MCP wire.

    Returns:
        {"total_entries": N, "skip": ..., "limit": ..., "rows": [...],
         "projected_columns": [...] | None}
    """
    raw = api.get_node_preview(
        workflow_id, execution_id, node_id,
        handle_condition=handle_condition, skip=skip, limit=limit,
    )
    rows = raw.get("data", []) if isinstance(raw, dict) else raw
    meta = raw.get("meta") or {}

    projected: Optional[list[str]] = None
    if rows and (columns is not None or drop_json_columns):
        rows, projected = _project_rows(rows, columns, drop_json_columns)

    return {
        "execution_id": execution_id,
        "node_id": node_id,
        "handle_condition": handle_condition,
        "total_entries": meta.get("total_entries", len(rows or [])),
        "skip": meta.get("skip", skip),
        "limit": meta.get("limit", limit),
        "projected_columns": projected,
        "rows": rows,
    }


def _project_rows(
    rows: list[dict],
    columns: Optional[list[str]],
    drop_json_columns: bool,
) -> tuple[list[dict], list[str]]:
    """Apply column projection to a list of row dicts.

    Returns (projected_rows, list_of_columns_kept). Pure function — extracted
    for unit-testability.

    Order of precedence:
      1. If `columns` is set: keep only those keys per row, in that order.
         Missing keys produce None for that row's value.
      2. Else if `drop_json_columns` is True: drop any key whose value in ANY
         row is a dict or list (i.e. likely a JSON column).
      3. Else: return rows unchanged.
    """
    if not rows:
        return rows, []

    if columns is not None:
        kept = list(columns)
        projected = [{c: r.get(c) for c in kept} for r in rows]
        return projected, kept

    if drop_json_columns:
        # Scan all rows to find any column that's ever a dict/list — drop those
        all_keys = set()
        for r in rows:
            all_keys.update(r.keys())
        json_like_keys: set[str] = set()
        for r in rows:
            for k, v in r.items():
                if isinstance(v, (dict, list)):
                    json_like_keys.add(k)
        kept = [k for k in all_keys if k not in json_like_keys]
        # Preserve original first-row ordering where possible
        first_row_keys = [k for k in rows[0].keys() if k in kept]
        rest_keys = [k for k in kept if k not in first_row_keys]
        ordered_kept = first_row_keys + rest_keys
        projected = [{k: r.get(k) for k in ordered_kept} for r in rows]
        return projected, ordered_kept

    return rows, list(rows[0].keys()) if rows else []


# ═══════════════════════════════════════════════════════════════════════════
# Validate
# ═══════════════════════════════════════════════════════════════════════════

def _validate_workflow_impl(workflow_id: str) -> dict:
    """Pure-function implementation of validate_workflow.

    Extracted so other mutating tools can call it without going through the
    @mcp.tool() decorator wrapper (auto-validate-after-edit pattern).
    """
    wf = api.get_workflow(workflow_id)
    node_errors = []
    magic_ref_warnings = []

    for b in wf.get("blocks", []):
        if b.get("node_config_error"):
            node_errors.append({
                "node_id": b["id"],
                "name": b.get("variableName"),
                "error": b["node_config_error"],
            })
        if b.get("typeId") == block_types.MAGIC_NODE:
            for ref in _magic_refs(b):
                if not _looks_like_edge_id(ref):
                    magic_ref_warnings.append({
                        "node_id": b["id"],
                        "name": b.get("variableName"),
                        "ref": ref,
                        "message": (
                            "Magic Node references must be edge IDs of form "
                            "'<src>-<src_handle>-<magic>-<tgt_handle>', not raw node UUIDs."
                        ),
                    })

    valid = (
        not wf.get("workflowConfigError")
        and not node_errors
        and not magic_ref_warnings
        and bool(wf.get("isRunable"))
    )
    return {
        "valid": valid,
        "isRunable": wf.get("isRunable"),
        "workflowConfigError": wf.get("workflowConfigError"),
        "node_errors": node_errors,
        "magic_ref_warnings": magic_ref_warnings,
    }


@mcp.tool()
def validate_workflow(workflow_id: str) -> dict:
    """Inspect a workflow for known config-error patterns.

    Returns:
      valid                  — overall pass/fail
      isRunable              — server's view
      workflowConfigError    — top-level error from the API (if any)
      node_errors            — per-node config errors
      magic_ref_warnings     — Magic Node references that don't look like edge IDs
                               (must be '<src>-<src_handle>-<magic>-<tgt_handle>',
                                NOT raw node UUIDs)

    v0.2.25: be aware that platform-side validation can briefly echo back
    errors for recently-deleted node_ids — the validation cache lags behind
    the mutation by one or two requests. If you see a `node_errors[].node_id`
    that isn't in the current `get_workflow_graph` output, run any small
    mutation (e.g. another `validate_workflow` call, or a no-op
    `update_node_setting`) to force the cache to refresh. Self-heals
    quickly; don't waste time chasing ghost errors.
    """
    return _validate_workflow_impl(workflow_id)


def _maybe_validate(workflow_id: str, validate_after: bool) -> Optional[dict]:
    """Run validate_workflow if validate_after=True; return validation dict or None.

    Used by every mutating tool to surface post-PUT validation errors that the
    raw PUT response often misses. One extra GET per mutating call; well worth
    catching silent breakages.

    Failures inside the validation call itself are caught and surfaced as a
    `validation.error` so the mutating tool's own success doesn't get masked.
    """
    if not validate_after:
        return None
    try:
        return _validate_workflow_impl(workflow_id)
    except Exception as e:
        return {"valid": None, "error": f"validation call failed: {e}"}


def _magic_refs(magic_block: dict) -> list[str]:
    settings = magic_block.get("settings_field_values") or []
    for grp in settings:
        if grp.get("field_name") == "data_manipulation-magic_node-instructions_and_ref":
            for sub in (grp.get("field_value") or []):
                if sub.get("field_name") == "data_manipulation-magic_node-references":
                    return sub.get("field_value") or []
    return []


def _looks_like_edge_id(s: str) -> bool:
    """Edge IDs contain handle markers like '-_default-' or '-df<N>'."""
    if not isinstance(s, str):
        return False
    if "-_default-" in s:
        return True
    return any(f"-df{i}" in s for i in range(1, 10))


# ═══════════════════════════════════════════════════════════════════════════
# Sandbox lint (standalone)
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def validate_custom_code(code: str) -> dict:
    """Lint Python code for sandbox-incompatible patterns BEFORE pasting into a Custom Code block.

    Codes:
      E000  syntax error
      E001  `from datetime import ...`         (datetime is the pre-imported module)
      E002  `next(...)`                        (not defined in the sandbox)
      E003  Module-level constants in run()    (assignments outside run() don't propagate)
      E004  `import X as _Y`                   (underscore alias stripped from run() namespace)
      W005  `try/except: return []/{}/None`    (silent failure pattern — warning, not blocking)

    `ok` is True when there are no BLOCKING issues (E*). W005 warnings surface in
    `issues` but don't fail validation — sometimes you genuinely want a defensive swallow.
    """
    issues = lint(code)
    blocking = [i for i in issues if i.is_blocking]
    warnings = [i for i in issues if not i.is_blocking]
    return {
        "ok": len(blocking) == 0,
        "issues": [
            {"line": i.line, "col": i.col, "code": i.code,
             "severity": i.severity, "message": i.message}
            for i in issues
        ],
        "blocking_count": len(blocking),
        "warning_count": len(warnings),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Build helpers
# ═══════════════════════════════════════════════════════════════════════════

def _rewrite_block_id(block: dict, old_id: str, new_id: str) -> dict:
    """Return a deep copy of `block` with every occurrence of `old_id` replaced
    by `new_id`. Used to repair internal self-references after paste-nodes
    reassigns the platform-side block id.

    Affects every field that may carry a self-reference:
      - block.id
      - block.outputs[].node_id
      - block.outputs[].columns_metadata[].origin_node_id
      - block.settings_field_values (Magic Node references include the new_id
        inside edge-id-shaped strings like `parent-_default-NEW-df1`)

    Implementation is a JSON round-trip with string replacement. UUIDs are
    122 bits of entropy — collisions with unrelated substrings are not a
    real concern in practice.
    """
    import json
    s = json.dumps(block)
    s = s.replace(old_id, new_id)
    return json.loads(s)


def _attach_block_via_paste_and_wire(
    *,
    workflow_id: str,
    new_block: dict,
    parent_edges: list[tuple[str, str, str]],
    fallback_parents: dict[str, dict],
    existing_block_ids: set[str],
) -> tuple[dict, str]:
    """Add `new_block` to a workflow and wire `parent_edges` into it, using
    small-payload endpoints only:

      1. POST /workflows/{id}/paste-nodes with `{"nodes": [new_block]}` —
         typically 1–3 KB regardless of how big the workflow already is.
         **The platform reassigns the block id**, ignoring the one we send.
         We detect the new id by diffing the response against
         `existing_block_ids` (the only id present after that wasn't before
         IS our newly-created block).
      2. If our internal self-references (outputs.node_id, columns_metadata
         origin_node_id, Magic Node refs) need updating to match the
         reassigned id, PUT a corrected copy of the new block via
         `put_node` — still 2–3 KB, irrelevant to workflow size.
      3. For each (parent_id, source_handle, target_handle) in `parent_edges`,
         PUT /workflows/{id}/nodes/{parent_id} with the parent's full block +
         the new edge appended (referencing the reassigned id, not our
         local UUID). Usually 2–5 KB per parent.

    Why this matters: prior versions did a single full `PUT /workflows/{id}`
    that re-sent every existing block. On workflows past ~50 blocks the body
    routinely exceeded the platform's request-size limit (HTTP 413). This
    helper avoids the giant PUT entirely.

    Returns:
      (paste_response, actual_block_id) — the actual_block_id is the
      platform-assigned UUID, NOT the one in `new_block["id"]`. Callers
      should use it for any downstream operations (return value, edge
      lookups, etc.).

    Raises if any parent is missing from `fallback_parents`, or if the
    paste-nodes response doesn't contain exactly one new block.
    """
    paste_resp = api.paste_nodes(workflow_id, {"nodes": [new_block]})
    response_ids = {b.get("id") for b in (paste_resp.get("blocks") or []) if b.get("id")}
    new_ids = response_ids - existing_block_ids
    if len(new_ids) != 1:
        raise RuntimeError(
            f"paste-nodes response had {len(new_ids)} new block ids (expected 1). "
            f"Cannot determine which block to wire edges to. "
            f"Diff: {sorted(new_ids)}"
        )
    actual_id = new_ids.pop()
    original_id = new_block["id"]

    # If the platform reassigned our id, PUT a corrected copy so all
    # self-references (outputs.node_id, columns_metadata.origin_node_id, Magic
    # Node refs) line up with the new id. Skip the PUT if id matches — that
    # would be wasted bandwidth.
    if actual_id != original_id:
        fixed_block = _rewrite_block_id(new_block, original_id, actual_id)
        api.put_node(workflow_id, actual_id, fixed_block)

    for parent_id, source_handle, target_handle in parent_edges:
        parent = fallback_parents.get(parent_id)
        if parent is None:
            raise ValueError(
                f"parent {parent_id} not present in workflow snapshot — "
                f"refusing to wire an edge from an unknown parent."
            )
        edges = parent.get("toBlocks") or []
        already = any(
            e.get("toBlockId") == actual_id
            and e.get("edge_target_handle_condition") == target_handle
            for e in edges
        )
        if not already:
            edges.append({
                "edgeId": f"{parent_id}-{source_handle}-{actual_id}-{target_handle}",
                "edge_source_handle_condition": source_handle,
                "edge_target_handle_condition": target_handle,
                "toBlockId": actual_id,
            })
            parent["toBlocks"] = edges
            api.put_node(workflow_id, parent_id, parent)
    return paste_resp, actual_id


def _new_block_error_from_paste(paste_resp: dict, new_id: str) -> Optional[str]:
    """Read `node_config_error` for our newly-pasted block from the paste-nodes
    response. The response echoes the full workflow; we just need our block."""
    for b in (paste_resp.get("blocks") or []):
        if b.get("id") == new_id:
            return b.get("node_config_error")
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Build
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def create_workflow(name: str, description: str = "", validate_after: bool = True) -> dict:
    """Create a new empty workflow. Returns the new workflow including its
    assigned `id`. No blocks are added; the caller decides the shape.

    KEY VOCABULARY (the platform models these as TWO distinct flags):

      START NODE = `isTrigger=True`. Marks a block as a swimlane entry
        point. EVERY workflow needs at least one start node (the platform
        returns 'Workflow has no start nodes' otherwise). MULTIPLE start
        nodes are allowed — each begins its own swimlane.

      TRIGGER (the user-facing word for "automation") = `isTrigger=True`
        AND `isListener=True`. The single block that polls / subscribes
        to events so the workflow runs on its own (cron, webhook, new
        message arrived). ONLY ONE per workflow (platform-enforced).

    Most one-off workflows need start nodes but NOT a trigger.

    Three common patterns — `attach_node` handles the flag defaults
    correctly for each:

      1. ONE-OFF / ad-hoc (run manually, possibly multiple times). First
         node = a real DATA SOURCE — Google Sheets "Get Values in Range",
         a CSV reader, a Pipedream HTTP fetch, etc. NOT a Custom Code
         that builds a hardcoded DataFrame: the platform refuses to
         execute parent-less Custom Code nodes with "No input data
         provided" because root execution slots expect to receive runtime
         input from a real source (verified live in v0.2.16 stress test).
         Example: `attach_node(parent_node_ids=[], type_id=<Sheets Get Values in Range>, ...)`
         → wrapper auto-sets `isTrigger=True, isListener=True` (Sheets
         read is listener-capable in the catalog). If you only want a
         one-shot read (no continuous polling), override with
         `is_listener=False`. Workflow stays in draft; run via
         `partial_execute`.

      2. SCHEDULED / live (cron). First node = Scheduler. Example:
         `attach_node(parent_node_ids=[], type_id=<Scheduler>, ...)`
         → wrapper auto-sets `isTrigger=True, isListener=True` (Scheduler
         is listener-capable). Publish via `publish_workflow` to start
         the cron firing.

      3. EVENT-DRIVEN. Webhook, Gmail new message, Slack new message.
         Same pattern as Scheduler — first node is a listener-type from
         the catalog, both flags auto-set.

    OVERRIDES: pass `is_trigger=False` explicitly if you genuinely want
    a non-start-node root (uncommon; workflow will be invalid until
    something IS a start node). Pass `is_listener=False` on a
    listener-capable root if you want a one-off run of an otherwise-
    pollable type (e.g. read a sheet once, don't keep polling).

    Trigger-capable nodes attached WITH parents (Sheets "Get Values in
    Range" used as a downstream read, etc.) are correctly NOT marked as
    start nodes — the v0.2.13 `attach_node` fix forces both flags False
    when parents exist, regardless of the catalog.

    PITFALL — CUSTOM CODE AS A ROOT: `attach_node` allows parent-empty
    Custom Code (sets `isTrigger=True`), and validation passes (workflow
    `isRunable=true`). But execution fails with "No input data provided"
    because the platform engine still tries to feed the root from an
    upstream that doesn't exist. **If you need a "pure transform" root
    for testing, use Magic Node 1-input with a dummy upstream Sheets-read
    or attach a single-row CSV reader.** This is a platform constraint,
    not an MCP-side guard we can fix here.
    """
    resp = api.create_workflow(name=name, description=description)
    new_id = resp.get("id")
    return {
        "ok": True,
        "id": new_id,
        "name": resp.get("name"),
        "isRunable": resp.get("isRunable"),
        "workflowConfigError": resp.get("workflowConfigError"),
        "validation": _maybe_validate(new_id, validate_after) if new_id else None,
    }


@mcp.tool()
def attach_magic_node(
    workflow_id: str,
    parent_node_ids: list[str],
    name: str,
    code: str,
    output_columns: list[str],
    output_dtypes: Optional[list[str]] = None,
    instructions_text: str = "",
    description: str = "",
    position_x: Optional[float] = None,
    position_y: Optional[float] = None,
    validate_after: bool = True,
) -> dict:
    """Attach a Magic Node downstream of one or more parent nodes.

    PRIMARY transform tool. Magic Node accepts 1-5 input dataframes (df1..df5)
    so joins, normalizes, and concats can happen in a single step — preferred
    over Custom Code in nearly all cases. Same Python sandbox under the hood.

    What this tool handles for you:
      1. Sandbox-lints `code`.
      2. Verifies your `def run(...)` signature has exactly len(parent_node_ids)
         positional args (so df1..dfN line up).
      3. Generates Magic Node UUID and builds the deeply-nested settings shape.
      4. Generates the references list as edge IDs in the format
         `<src>-_default-<magic>-df{N}` (NOT raw node UUIDs — wrong format
         silently breaks the node).
      5. Adds an edge from each parent with the correct target handle (df1, df2, ...).
      6. Sets `outputs.columns_metadata` from `output_columns` so downstream
         blocks pass schema validation.
      7. PUTs the workflow and verifies no `node_config_error`.

    Args:
        parent_node_ids: 1-5 upstream block IDs. Order matters: index i → df{i+1}.
        code: must define `def run(df1, df2, ...)` AND assign to `result`,
              following the Magic Node convention (see NREV_WORKFLOW_GUIDE §11.5).
              Example template:
                  import pandas as pd
                  def run(df1, df2):
                      out = pd.concat([df1, df2], ignore_index=True)
                      return out.drop_duplicates()
                  result = run(df1, df2)
        output_columns: column names this Magic Node produces.
        output_dtypes: parallel to output_columns; defaults to "string" for each.
        instructions_text: human-readable description that lives on the node
                           (helpful for the AI-assisted UI).
        position_x, position_y: defaults to 400 px right of the rightmost parent
                                at the average y of all parents.
    """
    if not parent_node_ids:
        raise ValueError("parent_node_ids must contain 1 to 5 entries")
    if len(parent_node_ids) > 5:
        raise ValueError(
            f"Magic Node accepts at most 5 inputs (df1..df5); you passed "
            f"{len(parent_node_ids)}. Chain a second Magic Node to fan in more."
        )

    issues = lint(code)
    blocking = [i for i in issues if i.is_blocking]
    warnings = [i for i in issues if not i.is_blocking]
    if blocking:
        return {
            "ok": False,
            "stage": "lint",
            "issues": [i.format() for i in blocking],
            "message": "Sandbox lint blocked the attach. Fix the issues above and retry.",
        }

    arity_issue = _check_run_arity(code, expected=len(parent_node_ids))
    if arity_issue:
        return {
            "ok": False,
            "stage": "arity",
            "message": arity_issue,
        }

    if output_dtypes is None:
        output_dtypes = ["string"] * len(output_columns)
    if len(output_dtypes) != len(output_columns):
        raise ValueError("output_dtypes length must match output_columns length")

    wf = api.get_workflow(workflow_id)
    blocks_by_id = {b["id"]: b for b in wf["blocks"]}
    missing_parents = [p for p in parent_node_ids if p not in blocks_by_id]
    if missing_parents:
        raise ValueError(
            f"parent_node_ids not found in workflow {workflow_id}: {missing_parents}"
        )

    new_id = str(uuid.uuid4())

    # References — these MUST be edge IDs, not raw node UUIDs (verified gotcha).
    references = [
        f"{pid}-_default-{new_id}-df{i + 1}"
        for i, pid in enumerate(parent_node_ids)
    ]

    columns_metadata = _build_columns_metadata(
        output_columns, output_dtypes,
        origin_node_id=new_id,
        origin_node_name=name,
        origin_node_type="data_manipulation.magic_node",
    )

    # Position: 400 px right of rightmost parent, at average y.
    parents = [blocks_by_id[p] for p in parent_node_ids]
    if position_x is None:
        position_x = max(p["position"]["x"] for p in parents) + 400
    if position_y is None:
        position_y = sum(p["position"]["y"] for p in parents) / len(parents)

    magic_settings = [
        {
            "field_name": "data_manipulation-magic_node-instructions_and_ref",
            "field_value": [
                {
                    "field_name": "data_manipulation-magic_node-instructions",
                    "field_value": [
                        _sf("data_manipulation-magic_node-instructions_text",
                            instructions_text or f"Magic Node: {name}",
                            label="Instructions Text"),
                    ],
                    "fieldLabel": None, "error": None,
                    "isUserInputInFormMandatory": False,
                    "selectedInputTypeIndex": None, "isStale": False,
                },
                {
                    "field_name": "data_manipulation-magic_node-references",
                    "field_value": references,
                    "fieldLabel": None, "error": None,
                    "isUserInputInFormMandatory": False,
                    "selectedInputTypeIndex": None, "isStale": False,
                },
            ],
            "fieldLabel": None, "error": None,
            "isUserInputInFormMandatory": False,
            "selectedInputTypeIndex": None, "isStale": False,
        },
        {
            "field_name": "data_manipulation-magic_node-code_section",
            "field_value": [_sf("data_manipulation-magic_node-code", code)],
            "fieldLabel": None, "error": None,
            "isUserInputInFormMandatory": False,
            "selectedInputTypeIndex": None, "isStale": False,
        },
    ]

    new_block = {
        "id": new_id,
        "typeId": block_types.MAGIC_NODE,
        "variableName": name,
        "description": description,
        "settings_field_values": magic_settings,
        "isTrigger": False,
        "isOrphan": False,
        "isPartOfActiveSwimlane": True,
        "isListener": False,
        "isTestMode": False,
        "inputs": [{
            "columns": [], "columns_metadata": None, "file": "",
            "handle_condition": "_default", "node_id": None,
        }],
        "outputs": [{
            "columns": output_columns,
            "columns_metadata": columns_metadata,
            "file": "",
            "handle_condition": "_default",
            "node_id": new_id,
        }],
        "toBlocks": [],
        "position": {"x": float(position_x), "y": float(position_y)},
        "creditCostPerItem": 0,
        "column_operations": None,
        "node_config_error": None,
    }

    # Small-payload path (v0.2.8): see attach_node for rationale. Magic Node
    # uses df1..dfN target handles, one per parent index, source handle is
    # always _default. The Magic Node references field carries strings that
    # embed `new_id` — those get rewritten in the helper's id-fixup PUT after
    # paste-nodes returns the platform-assigned id.
    parent_edges = [
        (pid, "_default", f"df{i + 1}")
        for i, pid in enumerate(parent_node_ids)
    ]
    resp, actual_new_id = _attach_block_via_paste_and_wire(
        workflow_id=workflow_id,
        new_block=new_block,
        parent_edges=parent_edges,
        fallback_parents=blocks_by_id,
        existing_block_ids={b["id"] for b in wf["blocks"]},
    )
    err = _new_block_error_from_paste(resp, actual_new_id)

    return {
        "ok": err is None,
        "node_id": actual_new_id,
        "node_config_error": err,
        "workflowConfigError": resp.get("workflowConfigError"),
        "isRunable": resp.get("isRunable"),
        "edge_target_handles": [f"df{i + 1}" for i in range(len(parent_node_ids))],
        "lint_warnings": [i.format() for i in warnings],
        "validation": _maybe_validate(workflow_id, validate_after),
    }


def _check_run_arity(code: str, expected: int) -> Optional[str]:
    """Returns None if `def run(...)` has exactly `expected` positional args; else a message."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None  # already caught by sandbox lint
    run_func = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run"),
        None,
    )
    if run_func is None:
        return f"`code` must define `def run(...)` accepting {expected} dataframe args."
    actual = len(run_func.args.args) + len(run_func.args.posonlyargs or [])
    if actual != expected:
        return (
            f"`def run(...)` takes {actual} positional args, but parent_node_ids has "
            f"{expected} entries. The signature should be `def run(" +
            ", ".join(f"df{i + 1}" for i in range(expected)) + ")` "
            f"so each parent's data lands in the matching df slot."
        )
    return None


@mcp.tool()
def attach_python_block(
    workflow_id: str,
    parent_node_id: str,
    name: str,
    code: str,
    output_columns: list[str],
    output_dtypes: Optional[list[str]] = None,
    description: str = "",
    position_x: Optional[float] = None,
    position_y: Optional[float] = None,
    validate_after: bool = True,
    i_understand_cc_is_broken: bool = False,
) -> dict:
    """⚠️ DO NOT USE — Custom Code is broken via the MCP (v0.2.24).

    USE `attach_magic_node` INSTEAD with `parent_node_ids=[parent_node_id]`.

    What's broken: live reproduction on 2026-05-25 confirmed that CC nodes
    attached via this tool run "successfully" but the platform silently
    discards the code's return value and passes through the parent's data
    verbatim. Status: completed. Error: none. The user-visible symptom is
    "I wrote code that should produce X but I got the parent's data with no
    new columns/rows."

    Magic Node uses the same Python sandbox, the same `def run(df1): ... return
    df` shape, and works correctly. Only difference: signature is `df1` not
    `df`. See docs/CC_BUG_REPRO_2026_05_25.md for the full repro + root cause.

    If you absolutely need a raw Custom Code node (the only known reason is a
    workflow built outside the MCP that already has a working CC and you want
    to clone it), pass `i_understand_cc_is_broken=True` to override the block.
    Even then expect silent passthrough — verify with `partial_execute` +
    `get_node_output` immediately and convert to MN if the output is wrong.

    -----

    (original docstring, retained for historical context — only relevant if
    you've overridden the block:)

    PREFER `attach_magic_node` IN MOST CASES. Magic Node accepts 1-5 inputs
    (so you can fold joins/merges into the same step) and works identically
    for single-input transforms. Reach for `attach_python_block` only when you
    deliberately want a single-input Custom Code block and have no plans to add
    more inputs later.

    What this tool does for you (the footguns it kills):
      1. Sandbox-lints `code` and refuses to attach if there are blocking issues.
      2. Generates a UUID for the new block and sets all the boilerplate fields.
      3. Adds an edge from `parent_node_id` to the new block.
      4. Sets `outputs.columns_metadata` from `output_columns` so downstream blocks
         can introspect the new schema (avoids the "Fields not found in available
         data" validation error after creating a CC that adds columns).
      5. PUTs the workflow with the new block + edge.
      6. Reads the response back to verify no node_config_error.

    `code` must define `def run(...)` — for single-input Custom Code, the
    convention is `def run(df)` (just `df`, NOT `df1`). Multi-input is
    Magic Node territory and uses `df1, df2, ..., dfN`. Stress testing in
    v0.2.18 found that single-input CC with `def run(df1)` raises
    `NameError: name 'df1' is not defined` at execution.

    `output_columns` is the list of column names this block produces.
    `output_dtypes` is parallel to output_columns; defaults to "string"
    for each. Setting output_columns is what makes downstream blocks see
    your new schema — without it, the platform infers from upstream and
    your transform's column changes look invisible to the next node.

    Note that `attach_python_block` requires a parent. To attach a Custom
    Code AS A ROOT (workflow start), use `attach_node(type_id=<CC typeId>,
    parent_node_ids=[], settings={...})` instead.

    v0.2.20 Fix A: when the parent is a Pipedream-wrapped action (Slack send,
    Sheets read, Gmail send, etc.), its outputs are the fixed triple
    `[error, summary, payload]`. A downstream CC that doesn't explicitly
    declare its own output_columns will have its schema silently overwritten
    by the platform's Pipedream-shape inference, losing whatever new columns
    your transform produces. This tool now refuses to attach with an empty
    output_columns list when the parent is Pipedream-shaped — pass the
    columns you actually want downstream blocks to see.

    Position defaults to 400 px right of parent.
    """
    # v0.2.24 Fix #2: Custom Code is broken — code is silently ignored at
    # runtime, parent data passes through verbatim. Refuse by default and
    # steer to attach_magic_node. Live-reproduced on 2026-05-25; see
    # docs/CC_BUG_REPRO_2026_05_25.md.
    if not i_understand_cc_is_broken:
        return {
            "ok": False,
            "stage": "cc_silent_failure_guard",
            "message": (
                "⚠️ Custom Code attach is broken via the MCP. The platform "
                "silently discards your code's return value and passes the "
                "parent's data through verbatim with status=completed and no "
                "error. Use attach_magic_node instead — same Python sandbox, "
                "same return shape, but it actually runs your code."
            ),
            "use_instead": {
                "tool": "attach_magic_node",
                "args": {
                    "workflow_id": workflow_id,
                    "parent_node_ids": [parent_node_id],
                    "name": name,
                    "code": code.replace("def run(df)", "def run(df1)").replace("run(df)", "run(df1)"),
                    "output_columns": output_columns,
                    "instructions_text": description or f"Magic Node: {name}",
                },
            },
            "note": (
                "Magic Node uses `df1` instead of `df`. The `use_instead.args.code` "
                "above is auto-converted. Verify the conversion looks right "
                "before calling."
            ),
            "override": (
                "If you absolutely must create a raw CC node, pass "
                "i_understand_cc_is_broken=True. Expect silent passthrough; "
                "verify with partial_execute + get_node_output."
            ),
            "docs": "docs/CC_BUG_REPRO_2026_05_25.md",
        }

    issues = lint(code)
    blocking = [i for i in issues if i.is_blocking]
    warnings = [i for i in issues if not i.is_blocking]
    if blocking:
        return {
            "ok": False,
            "stage": "lint",
            "issues": [i.format() for i in blocking],
            "message": "Sandbox lint blocked the attach. Fix the issues above and retry.",
        }

    if output_dtypes is None:
        output_dtypes = ["string"] * len(output_columns)
    if len(output_dtypes) != len(output_columns):
        raise ValueError("output_dtypes length must match output_columns length")

    wf = api.get_workflow(workflow_id)
    parent = next((b for b in wf["blocks"] if b["id"] == parent_node_id), None)
    if parent is None:
        raise ValueError(
            f"parent_node_id {parent_node_id} not found in workflow {workflow_id}"
        )

    # v0.2.20 Fix A: refuse empty output_columns when parent is Pipedream-shaped.
    # The platform's schema inference for Pipedream parents propagates
    # [error, summary, payload] as the child's columns_metadata — burying any
    # new columns the CC produces from downstream introspection.
    if not output_columns and _is_pipedream_block(parent):
        parent_outs = (parent.get("outputs") or [{}])[0]
        return {
            "ok": False,
            "stage": "pipedream_parent_schema_guard",
            "parent_columns": parent_outs.get("columns") or [],
            "message": (
                f"Parent {parent_node_id!r} is a Pipedream-wrapped action; its "
                f"outputs are the fixed [error, summary, payload] triple. "
                f"You MUST pass explicit `output_columns` listing the columns "
                f"this CC produces — otherwise downstream blocks will see only "
                f"those three Pipedream columns. Tip: parse `payload` in your "
                f"CC and emit the row fields you care about."
            ),
        }

    new_id = str(uuid.uuid4())
    columns_metadata = _build_columns_metadata(
        output_columns, output_dtypes,
        origin_node_id=new_id,
        origin_node_name=name,
        origin_node_type="data_manipulation.custom_code",
    )
    px = position_x if position_x is not None else (parent["position"]["x"] + 400)
    py = position_y if position_y is not None else parent["position"]["y"]

    new_block = {
        "id": new_id,
        "typeId": block_types.CUSTOM_CODE,
        "variableName": name,
        "description": description,
        "settings_field_values": [_sf("data_manipulation-custom_code-code", code)],
        "isTrigger": False,
        "isOrphan": False,
        "isPartOfActiveSwimlane": True,
        "isListener": False,
        "isTestMode": False,
        "inputs": [{
            "columns": [], "columns_metadata": None, "file": "",
            "handle_condition": "_default", "node_id": None,
        }],
        "outputs": [{
            "columns": output_columns,
            "columns_metadata": columns_metadata,
            "file": "",
            "handle_condition": "_default",
            "node_id": new_id,
        }],
        "toBlocks": [],
        "position": {"x": float(px), "y": float(py)},
        "creditCostPerItem": 0,
        "column_operations": None,
        "node_config_error": None,
    }

    # Small-payload path (v0.2.8): see attach_node for rationale.
    resp, actual_new_id = _attach_block_via_paste_and_wire(
        workflow_id=workflow_id,
        new_block=new_block,
        parent_edges=[(parent_node_id, "_default", "_default")],
        fallback_parents={parent_node_id: parent},
        existing_block_ids={b["id"] for b in wf["blocks"]},
    )
    err = _new_block_error_from_paste(resp, actual_new_id)

    return {
        "ok": err is None,
        "node_id": actual_new_id,
        "node_config_error": err,
        "workflowConfigError": resp.get("workflowConfigError"),
        "isRunable": resp.get("isRunable"),
        "lint_warnings": [i.format() for i in warnings],
        "validation": _maybe_validate(workflow_id, validate_after),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Execute
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def partial_execute(
    workflow_id: str,
    target_node_id: str,
    prior_execution_id: Optional[str] = None,
    refresh_chain: Optional[list[str]] = None,
    refresh_timeout_seconds: int = 300,
) -> dict:
    """Execute one node, optionally reusing cached upstream from a prior execution.

    This is the cost-saving alternative to running the full workflow when only
    one downstream block has changed.

    `refresh_chain`: when an intermediate node has been changed since prior_execution
    ran, its cached output is stale. The platform's partial_execute does NOT auto-
    detect this — calling it on a downstream target will reuse the stale cache.
    Pass `refresh_chain=[changed_intermediate_node_id, ...]` (in upstream-to-downstream
    order) and this tool will refresh each in sequence (polling until each completes,
    timeout per node = refresh_timeout_seconds) before running the target.

    Without refresh_chain, behaves identically to before: kicks off target_node and
    returns immediately.
    """
    chain = list(refresh_chain or [])
    refresh_results = []

    for refresh_node_id in chain:
        try:
            api.execute_node(workflow_id, refresh_node_id, prior_execution_id)
            ok = _wait_for_node(workflow_id, prior_execution_id, refresh_node_id,
                                timeout=refresh_timeout_seconds) if prior_execution_id else True
            refresh_results.append({
                "node_id": refresh_node_id,
                "status": "completed" if ok else "timeout",
            })
            if not ok:
                return {
                    "ok": False,
                    "stage": "refresh_chain",
                    "stuck_at_node": refresh_node_id,
                    "refresh_results": refresh_results,
                    "message": f"refresh-chain node {refresh_node_id} did not complete within {refresh_timeout_seconds}s",
                }
        except Exception as e:
            refresh_results.append({"node_id": refresh_node_id, "status": "error", "error": str(e)})
            return _execute_error_response(
                stage="refresh_chain",
                stuck_at_node=refresh_node_id,
                refresh_results=refresh_results,
                exc=e,
            )

    # ── Final target-node execute ───────────────────────────────────────────
    # v0.2.13: catch the platform's non-2xx errors so we surface a structured
    # ok=False instead of bubbling a raw exception that the caller has to
    # interpret. Also augment well-known errors with a diagnostic hint
    # (orphan triggers are the most common cause of "Workflow must be
    # executable" / "not in a valid state").
    try:
        resp = api.execute_node(workflow_id, target_node_id, prior_execution_id)
    except Exception as e:
        return _execute_error_response(
            stage="target_execute",
            stuck_at_node=target_node_id,
            refresh_results=refresh_results,
            exc=e,
        )
    result = {"ok": True, "response": resp}
    if refresh_results:
        result["refresh_results"] = refresh_results
    return result


# Substrings the platform commonly returns when execution is gated.
# Treated as advisory pattern matching — the load-bearing signal is the
# non-2xx HTTP status from api.execute_node. If a future platform release
# changes the wording, the hint is just absent; ok=False is still returned.
#
# v0.2.17: added "has no trigger nodes" and "has no start nodes" — the v0.2.16
# stress test found these are the actual most-common messages, and the original
# three phrases (added in v0.2.13) basically never fired in practice.
_EXECUTE_GATE_PHRASES = (
    "workflow must be executable",
    "not in a valid state to execute",
    "must be in a valid state",
    "has no trigger nodes",
    "has no start nodes",
    "has no listener node",
)


def _execute_error_response(
    *,
    stage: str,
    stuck_at_node: str,
    refresh_results: list[dict],
    exc: Exception,
) -> dict:
    """Build a structured failure response for partial_execute. Surfaces the
    platform's raw error and — when it matches a known execution-gate
    phrase — adds a hint pointing the caller at the most common cause."""
    msg = str(exc)
    body = {
        "ok": False,
        "stage": stage,
        "stuck_at_node": stuck_at_node,
        "message": msg,
    }
    if refresh_results:
        body["refresh_results"] = refresh_results
    low = msg.lower()
    if any(p in low for p in _EXECUTE_GATE_PHRASES):
        body["hint"] = (
            "The platform refused to execute. The most common cause is "
            "orphan trigger nodes (multiple blocks with isTrigger=True). "
            "Run validate_workflow + get_workflow_graph to inspect. If a "
            "trigger-capable node (Sheets read, Gmail poll, etc.) was "
            "attached as a downstream block, it may have been auto-flagged "
            "as a trigger by attach_node pre-v0.2.13. Patch the offender "
            "via update_node_setting or rebuild it correctly."
        )
    return body


@mcp.tool()
def tail_execution(
    workflow_id: str,
    execution_id: str,
    wait_until: str = "any_change",
    target_block_id: Optional[str] = None,
    timeout_seconds: int = 60,
    poll_seconds: int = 3,
) -> dict:
    """Block-level polling of an in-flight execution; returns when a condition is met.

    Replaces the sleep-poll loop pattern. The tool polls `get_execution` at
    `poll_seconds` intervals, returning when:

      wait_until="any_change"        — current snapshot every `poll_seconds`;
                                        returns the first time a block_run status
                                        changes (running → completed/failed).
      wait_until="status_terminal"   — returns when the execution's overall status
                                        is `completed`, `failed`, or `stopped`.
      wait_until="block_completed"   — returns when `target_block_id` reaches
                                        terminal status (`completed` / `failed`).
                                        Requires target_block_id.

    Always returns the latest execution snapshot. `timed_out: True` if the
    condition wasn't met within timeout_seconds.

    Default behavior: any_change with a 60s timeout — useful for "wake me up
    when the next block finishes".
    """
    import time

    if wait_until == "block_completed" and not target_block_id:
        raise ValueError("wait_until='block_completed' requires target_block_id")

    last_block_statuses: dict[str, str] = {}
    start = time.time()

    while time.time() - start < timeout_seconds:
        raw = api.get_execution_detail(workflow_id, execution_id)
        overall_status = raw.get("status")
        block_runs = raw.get("blockRuns") or []

        if wait_until == "status_terminal":
            if overall_status in ("completed", "failed", "stopped"):
                pr = _maybe_enrich_pipedream_errors(workflow_id, raw)
                return _slim_execution(raw, timed_out=False, pipedream_row_errors=pr)

        elif wait_until == "block_completed":
            target_br = next((br for br in block_runs if br.get("workflowBlockId") == target_block_id), None)
            if target_br and target_br.get("status") in ("completed", "failed"):
                pr = _maybe_enrich_pipedream_errors(workflow_id, raw)
                return _slim_execution(raw, timed_out=False, pipedream_row_errors=pr)

        elif wait_until == "any_change":
            current = {br.get("workflowBlockId"): br.get("status") for br in block_runs}
            if last_block_statuses and current != last_block_statuses:
                # Only enrich if any block reached terminal — otherwise no rows yet
                any_terminal = any(br.get("status") in ("completed", "failed")
                                   for br in block_runs)
                pr = _maybe_enrich_pipedream_errors(workflow_id, raw) if any_terminal else {}
                return _slim_execution(raw, timed_out=False, pipedream_row_errors=pr)
            last_block_statuses = current

        time.sleep(poll_seconds)

    # Timed out — return current state
    raw = api.get_execution_detail(workflow_id, execution_id)
    pr = _maybe_enrich_pipedream_errors(workflow_id, raw) if (raw.get("blockRuns") or []) else {}
    return _slim_execution(raw, timed_out=True, pipedream_row_errors=pr)


def _is_pipedream_block(block: dict) -> bool:
    """True if the block is a Pipedream-wrapped action (Slack, Sheets, Gmail, Calendar, etc.).

    Detection: a settings field whose name starts with "pipedream-" OR an
    outputs column_metadata entry whose origin_node_type starts with "pipedream.".
    Pipedream-wrapped actions have a quirk: block-level status reports
    `completed / error:null` EVEN WHEN THE ACTION FAILED — the real error is
    embedded in the row output's `error` column. See _check_pipedream_row_error.
    """
    if not isinstance(block, dict):
        return False
    sfv = block.get("settings_field_values") or []
    for entry in sfv:
        fn = entry.get("field_name") or ""
        if fn.startswith("pipedream-"):
            return True
    outs = block.get("outputs") or []
    for out in outs:
        for col in (out.get("columns_metadata") or []):
            ont = col.get("origin_node_type") or ""
            if ont.startswith("pipedream."):
                return True
    return False


def _check_pipedream_row_error(workflow_id: str, execution_id: str,
                                block_id: str) -> Optional[dict]:
    """Fetch the first output row of a Pipedream node and surface its error column.

    v0.2.20: Pipedream-wrapped Add Single Row / Send Message / etc. return
    block-level status:completed/error:null even when the underlying action
    failed at the Pipedream layer. The real error is in row[0].error. This
    helper reads that row and returns a structured dict:
        {"has_row_error": bool, "row_error": <str | None>, "error_attribution": <dict | None>}
    or None if the output couldn't be read.

    v0.2.25: include `_diagnostic` field when the preview fetch fails — pre-fix,
    `_maybe_enrich_pipedream_errors` silently dropped these so callers got
    `has_pipedream_row_errors: undefined` even when the platform refused the
    preview call (e.g., handle_condition mismatch, expired execution).
    """
    try:
        rows_resp = api.get_node_preview(workflow_id, execution_id, block_id,
                                          handle_condition="_default",
                                          skip=0, limit=1)
    except Exception as e:
        return {"has_row_error": False, "row_error": None,
                "error_attribution": None,
                "_diagnostic": f"preview fetch failed: {type(e).__name__}: {str(e)[:200]}"}
    # The API returns the rows under "entries" (full response shape) and the
    # server tool surfaces them as "rows" — accept either.
    rows = None
    if isinstance(rows_resp, dict):
        rows = rows_resp.get("entries") or rows_resp.get("rows") or []
    if not rows:
        return {"has_row_error": False, "row_error": None, "error_attribution": None}
    row0 = rows[0] if isinstance(rows[0], dict) else {}
    err_raw = row0.get("error")
    err_1 = row0.get("error_1")
    # The "error" column can be a JSON string envelope ([{ts, k, err: {name,message,...}}]);
    # error_1 is the un-enveloped form. Prefer error_1 if present.
    candidate = err_1 if err_1 else err_raw
    if not candidate or candidate in (None, "", "[]", "null"):
        return {"has_row_error": False, "row_error": None, "error_attribution": None}
    # Try to parse error_1 JSON for a clean message + attribution
    import json
    parsed = None
    if isinstance(candidate, str):
        try:
            parsed = json.loads(candidate)
        except Exception:
            parsed = None
    if isinstance(parsed, dict):
        msg = parsed.get("message") or parsed.get("name") or str(candidate)[:300]
        attribution = parsed.get("attribution")
        return {"has_row_error": True, "row_error": msg,
                "error_attribution": attribution if isinstance(attribution, dict) else None}
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
        first = parsed[0]
        inner = first.get("err") if isinstance(first.get("err"), dict) else first
        msg = inner.get("message") or inner.get("name") or str(candidate)[:300]
        return {"has_row_error": True, "row_error": msg, "error_attribution": None}
    return {"has_row_error": True, "row_error": str(candidate)[:300],
            "error_attribution": None}


def _maybe_enrich_pipedream_errors(workflow_id: str, raw_execution: dict) -> dict:
    """For each completed block run in the execution, if the block is Pipedream
    and reported status:completed/error:null, fetch row[0] and surface row.error.

    Returns a dict mapping block_id → {has_row_error, row_error, error_attribution}
    ONLY for blocks where row error was detected. Empty dict if nothing to flag.
    Best-effort: any per-block read failure is silently skipped.
    """
    execution_id = raw_execution.get("id")
    if not execution_id:
        return {}
    block_runs = raw_execution.get("blockRuns") or []
    if not block_runs:
        return {}
    # We need the workflow's block definitions to know which are Pipedream
    try:
        wf = api.get_workflow(workflow_id)
        blocks_by_id = {b.get("id"): b for b in (wf.get("blocks") or [])}
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for br in block_runs:
        if br.get("status") != "completed":
            continue
        # Block-level error already present — don't bother with row-level check
        if br.get("error"):
            continue
        block_id = br.get("workflowBlockId")
        if not block_id:
            continue
        block = blocks_by_id.get(block_id)
        if not block or not _is_pipedream_block(block):
            continue
        chk = _check_pipedream_row_error(workflow_id, execution_id, block_id)
        if chk and chk.get("has_row_error"):
            out[block_id] = chk
    return out


def _slim_execution(raw: dict, timed_out: bool,
                    pipedream_row_errors: Optional[dict] = None) -> dict:
    """Compact representation of an execution snapshot.

    v0.2.20: if `pipedream_row_errors` is provided (dict block_id → row_error info),
    block_runs entries for those blocks get an additional `pipedream_row_error`
    field surfaced, AND the slim envelope adds a top-level `has_pipedream_row_errors`
    flag so callers can branch on it without diving into per-block dicts.
    """
    block_runs = raw.get("blockRuns") or []
    pr = pipedream_row_errors or {}
    result = {
        "execution_id": raw.get("id"),
        "status": raw.get("status"),
        "creditsUsed": raw.get("creditsUsed") or raw.get("credits_used"),
        "duration": raw.get("duration"),
        "block_run_count": len(block_runs),
        "block_runs": [
            {
                "block_id": br.get("workflowBlockId"),
                "block_name": br.get("workflowBlockName"),
                "status": br.get("status"),
                "creditsUsed": br.get("creditsUsed"),
                "duration": br.get("duration"),
                "error": br.get("error"),
                **({"pipedream_row_error": pr[br.get("workflowBlockId")]}
                   if br.get("workflowBlockId") in pr else {}),
            }
            for br in block_runs
        ],
        "timed_out": timed_out,
    }
    if pr:
        result["has_pipedream_row_errors"] = True
        result["pipedream_row_error_count"] = len(pr)
    return result


@mcp.tool()
def check_node_errors(workflow_id: str, execution_id: str,
                       node_id: Optional[str] = None) -> dict:
    """Explicitly check for Pipedream row-level errors that block-level
    status doesn't surface.

    v0.2.25 — split out from the auto-detection in `tail_execution` so callers
    have a deterministic way to ask "did this Pipedream node ACTUALLY work?"
    when the block reports `status: completed, error: null`.

    Why this exists: Pipedream-wrapped actions (Slack Send Message, Sheets Add
    Row, Gmail Send, Calendar Create Event, etc.) report `status: completed,
    error: null` even when the underlying action failed at the Pipedream layer
    (the network call to Slack/Sheets/etc. returned HTTP 200 with an error in
    the body). The real error lives in the row's `error` / `error_1` column.
    The 2026-05-25 comprehensive prod test caught this: a Slack node returned
    `invalid_blocks` inside an HTTP 200 response; `tail_execution` honestly
    reported "completed, no error" because that's what the platform said.

    If `node_id` is provided, check only that block. If omitted, scan ALL
    Pipedream-shaped blocks in the execution.

    Returns:
        {
          "execution_id": "...",
          "checked_block_count": N,
          "blocks_with_errors": [
            {"block_id": "...", "block_name": "...", "row_error": "<msg>",
             "error_attribution": {...}}
          ],
          "blocks_without_errors": [<block_id>, ...],
          "skipped_non_pipedream": [<block_id>, ...],
          "diagnostics": [<block_id>: "<reason if check failed>", ...]
        }
    """
    raw = api.get_execution_detail(workflow_id, execution_id)
    block_runs = raw.get("blockRuns") or []
    if not block_runs:
        return {
            "execution_id": execution_id,
            "checked_block_count": 0,
            "blocks_with_errors": [],
            "blocks_without_errors": [],
            "skipped_non_pipedream": [],
            "diagnostics": [],
            "note": "Execution has no block_runs yet — still running?",
        }

    wf = api.get_workflow(workflow_id)
    blocks_by_id = {b.get("id"): b for b in (wf.get("blocks") or [])}

    blocks_with_errors = []
    blocks_without = []
    skipped = []
    diagnostics = []

    for br in block_runs:
        bid = br.get("workflowBlockId")
        if not bid:
            continue
        if node_id and bid != node_id:
            continue  # filter to single block
        block = blocks_by_id.get(bid)
        if not block or not _is_pipedream_block(block):
            skipped.append(bid)
            continue
        chk = _check_pipedream_row_error(workflow_id, execution_id, bid)
        if not chk:
            diagnostics.append({"block_id": bid, "diagnostic": "helper returned None (unexpected)"})
            continue
        if chk.get("_diagnostic"):
            diagnostics.append({"block_id": bid, "diagnostic": chk["_diagnostic"]})
            continue
        if chk.get("has_row_error"):
            blocks_with_errors.append({
                "block_id": bid,
                "block_name": br.get("workflowBlockName"),
                "row_error": chk.get("row_error"),
                "error_attribution": chk.get("error_attribution"),
            })
        else:
            blocks_without.append(bid)

    return {
        "execution_id": execution_id,
        "checked_block_count": len(blocks_with_errors) + len(blocks_without),
        "blocks_with_errors": blocks_with_errors,
        "blocks_without_errors": blocks_without,
        "skipped_non_pipedream": skipped,
        "diagnostics": diagnostics,
    }


@mcp.tool()
def abort_execution(workflow_id: str, execution_id: str) -> dict:
    """Stop an in-flight execution. Use when a partial_execute is misfiring
    or burning credits faster than expected.

    NOTE: the abort endpoint may need adjustment if the platform uses a different
    route. If this 404s, check the network tab when clicking stop in the web UI
    and update `client.abort_execution()` accordingly.
    """
    resp = api.abort_execution(workflow_id, execution_id)
    return {"ok": True, "execution_id": execution_id, "response": resp}


# ═══════════════════════════════════════════════════════════════════════════
# Test mode
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def set_test_mode(
    workflow_id: str,
    on: bool,
    node_id: Optional[str] = None,
    validate_after: bool = True,
) -> dict:
    """Toggle test mode. Default scope is workflow; pass node_id for per-node.

    Test mode caps row-count at 5 on paid nodes. There's no point enabling test
    mode on a free node — the cap doesn't save credits, just truncates output
    and makes downstream debugging harder. This tool refuses that.
    """
    if node_id is not None:
        wf = api.get_workflow(workflow_id)
        target = next((b for b in wf["blocks"] if b["id"] == node_id), None)
        if target is None:
            raise ValueError(f"node {node_id} not found in workflow {workflow_id}")

        if on and (target.get("creditCostPerItem") or 0) == 0:
            return {
                "ok": False,
                "message": (
                    f"Refusing to enable test mode on '{target.get('variableName')}' — it's a "
                    "free node (creditCostPerItem=0). Test mode would cap its output to 5 rows "
                    "for no credit savings, only making downstream debugging harder."
                ),
            }
        # v0.2.17: send the FULL node block with isTestMode mutated, not a
        # partial {"isTestMode": ...} body. The platform's PUT /nodes/{id}
        # endpoint returns HTTP 422 if id / typeId / variableName /
        # settings_field_values / isTrigger are missing — even when those
        # fields aren't being changed. bulk_set_test_mode already does this
        # correctly; set_test_mode lagged.
        target["isTestMode"] = on
        api.put_node(workflow_id, node_id, target)
        return {"ok": True, "scope": "node", "node_id": node_id, "isTestMode": on,
                "validation": _maybe_validate(workflow_id, validate_after)}

    wf = api.get_workflow(workflow_id)
    payload = {"workflow_details": {
        "id": workflow_id,
        "name": wf.get("name"),
        "description": wf.get("description"),
        "isTestMode": on,
        "blocks": wf["blocks"],
    }}
    api.put_workflow(workflow_id, payload)
    return {"ok": True, "scope": "workflow", "workflow_id": workflow_id, "isTestMode": on,
            "validation": _maybe_validate(workflow_id, validate_after)}


def _pick_test_mode_targets(
    blocks: list[dict],
    node_ids: Optional[list[str]],
    downstream_of: Optional[str],
) -> tuple[list[dict], list[str]]:
    """Compute the target set for bulk_set_test_mode.

    Returns (target_block_list, missing_node_ids). Precedence:
      1. explicit node_ids list
      2. downstream_of (DFS via toBlocks, includes the start node itself)
      3. all blocks in the workflow

    Pure function — separated from the tool so it's unit-testable.
    """
    blocks_by_id = {b["id"]: b for b in blocks}

    if node_ids:
        targets = [blocks_by_id[nid] for nid in node_ids if nid in blocks_by_id]
        missing = [nid for nid in node_ids if nid not in blocks_by_id]
        return targets, missing

    if downstream_of:
        if downstream_of not in blocks_by_id:
            return [], [downstream_of]
        seen: set[str] = set()
        stack = [downstream_of]
        while stack:
            curr = stack.pop()
            if curr in seen:
                continue
            seen.add(curr)
            for e in (blocks_by_id[curr].get("toBlocks") or []):
                tgt = e.get("toBlockId")
                if tgt and tgt in blocks_by_id and tgt not in seen:
                    stack.append(tgt)
        return [blocks_by_id[nid] for nid in seen], []

    return list(blocks), []


@mcp.tool()
def bulk_set_test_mode(
    workflow_id: str,
    on: bool,
    node_ids: Optional[list[str]] = None,
    downstream_of: Optional[str] = None,
    only_paid: bool = True,
    validate_after: bool = True,
) -> dict:
    """Toggle test mode on multiple nodes in ONE PUT.

    Three ways to pick the target set (precedence order):
      1. `node_ids=[...]`     — explicit list, used verbatim
      2. `downstream_of=X`    — DAG-walk: every node reachable from X via
                                toBlocks edges (includes X itself)
      3. neither              — every node in the workflow

    `only_paid=True` (default) skips free nodes (creditCostPerItem == 0) when
    enabling — test mode caps output to 5 rows on free nodes for no credit
    savings, just truncated downstream data. When `on=False`, `only_paid` is
    ignored — disabling is universal.

    Idempotent: nodes already in the desired state are skipped (no spurious PUT
    diff).

    Returns a summary:
        enabled          — nodes that were turned ON this call
        disabled         — nodes that were turned OFF this call
        skipped_free     — paid-only filter excluded these (free nodes, when on=True)
        skipped_already  — already in desired state, no change made
        missing_node_ids — node IDs that didn't exist in the workflow
    """
    wf = api.get_workflow(workflow_id)
    targets, missing = _pick_test_mode_targets(wf["blocks"], node_ids, downstream_of)

    enabled: list[dict] = []
    disabled: list[dict] = []
    skipped_free: list[dict] = []
    skipped_already: list[dict] = []

    for b in targets:
        cost = b.get("creditCostPerItem") or 0
        currently_on = bool(b.get("isTestMode"))
        info = {"node_id": b["id"], "name": b.get("variableName"), "cost": cost}

        if on:
            if only_paid and cost == 0:
                skipped_free.append(info)
                continue
            if currently_on:
                skipped_already.append(info)
                continue
            b["isTestMode"] = True
            enabled.append(info)
        else:
            if not currently_on:
                skipped_already.append(info)
                continue
            b["isTestMode"] = False
            disabled.append(info)

    if not enabled and not disabled:
        return {
            "ok": True,
            "message": "no changes — all targeted nodes already in desired state or skipped",
            "operation": "enable" if on else "disable",
            "scope": _describe_test_mode_scope(node_ids, downstream_of),
            "enabled": [], "disabled": [],
            "skipped_free": skipped_free,
            "skipped_already": skipped_already,
            "missing_node_ids": missing,
        }

    resp = _put_workflow_blocks(workflow_id, wf)
    return {
        "ok": not resp.get("workflowConfigError"),
        "operation": "enable" if on else "disable",
        "scope": _describe_test_mode_scope(node_ids, downstream_of),
        "enabled": enabled,
        "disabled": disabled,
        "skipped_free": skipped_free,
        "skipped_already": skipped_already,
        "missing_node_ids": missing,
        "workflowConfigError": resp.get("workflowConfigError"),
        "validation": _maybe_validate(workflow_id, validate_after),
    }


def _describe_test_mode_scope(node_ids: Optional[list[str]], downstream_of: Optional[str]) -> str:
    if node_ids:
        return f"explicit ({len(node_ids)} node IDs)"
    if downstream_of:
        return f"downstream_of:{downstream_of}"
    return "all_in_workflow"


# ═══════════════════════════════════════════════════════════════════════════
# Wiring (add/remove edges, delete blocks, splice branches)
# ═══════════════════════════════════════════════════════════════════════════


# Handle names used by Magic Node for fan-in (df1, df2, ..., df5). Exempt
# from the single-input guard because Magic Node is THE supported way to wire
# multiple upstreams into one block. This is an MCP-wrapper convention — the
# platform's OpenAPI spec doesn't enumerate handle names, so we hardcode it
# here. If a future platform release adds another fan-in node type, this set
# needs to grow.
_MAGIC_NODE_FAN_IN_HANDLES = {"df1", "df2", "df3", "df4", "df5"}


def _find_existing_default_incoming(blocks: list[dict], target_node_id: str) -> Optional[dict]:
    """Search every block's toBlocks for a `_default → _default` edge pointing
    at `target_node_id`. Returns the first such edge dict (with `source_id`
    added for diagnostics) or None.

    Used by the single-input guard in add_edge / splice_branch: if such an
    edge already exists, adding another `_default` edge into the same target
    silently breaks runtime (downstream block only knows how to read one input).
    """
    for src_block in blocks:
        for e in (src_block.get("toBlocks") or []):
            if (e.get("toBlockId") == target_node_id
                    and e.get("edge_target_handle_condition") == "_default"):
                return {**e, "source_id": src_block.get("id")}
    return None


@mcp.tool()
def add_edge(
    workflow_id: str,
    source_node_id: str,
    target_node_id: str,
    source_handle: str = "_default",
    target_handle: str = "_default",
    allow_multi_input: bool = False,
    validate_after: bool = True,
) -> dict:
    """Add an edge from source_node → target_node.

    Idempotent: if an identical edge already exists (same source, target, both
    handles), this is a no-op.

    SINGLE-INPUT GUARD (v0.2.13): if `target_handle == "_default"` and the
    target already has a `_default` incoming edge from a different source,
    this tool refuses with a ValueError. Wiring two `_default` edges into
    a single-input node (HubSpot, Gmail, Sheets, Custom Code, AI — almost
    everything) looks fine in the UI but silently breaks at execution
    because the downstream block only knows how to read one input. Use
    `attach_magic_node` (1–5 inputs with df1..dfN handles) for joins, or
    `remove_edge` to drop the existing edge first. The guard is skipped
    when `target_handle ∈ {df1..df5}` (Magic Node fan-in). For the legacy
    Merge block specifically, pass `allow_multi_input=True`.

    For Magic Node fan-in, set `target_handle` to `df1` … `df5` to pick the
    dataframe slot. NOTE: adding an edge to a Magic Node also requires updating
    the Magic Node's `references` list to include the new edge ID — `add_edge`
    does NOT do this. Use `update_magic_node(...)` separately if you need to
    update references.

    TARGET-SIDE REFRESH (v0.2.18): if the target block is orphan
    (`isOrphan=true`) or has empty `inputs`, this tool also PUTs the target
    with `isOrphan=False` and an inputs skeleton. Pre-v0.2.18 add_edge only
    PUT the source, leaving orphan targets unreachable at execution
    ("Node is orphan"). The platform doesn't auto-recompute these fields
    when a sibling block's `toBlocks` changes — the wrapper has to mutate
    the target explicitly. Surfaced in the response as
    `target_isOrphan_refreshed: true` when this fix-up ran.

    Returns `{ok, edge_existed, edge_added, target_isOrphan_refreshed,
    target_isTrigger_flipped, workflowConfigError, isRunable, validation}`.

    START-NODE-vs-TRIGGER quick reference (see create_workflow docstring for
    the full story):
      `isTrigger=True` marks a block as a workflow START NODE. A workflow needs
      at least one. Multiple are allowed (each begins its own swimlane).
      `isListener=True` marks the SINGLE block that polls/subscribes to events
      (Scheduler, "New X" types). Max one per workflow (platform-enforced).
      When `add_edge` wires INTO an existing start node, this tool auto-flips
      target.isTrigger=False (v0.2.21 fix) so the workflow doesn't end up with
      multiple roots when only one was intended. To CONVERT a one-off workflow
      into a triggered automation, use `prepend_trigger` (v0.2.22) — it's the
      attach-then-add_edge sequence wrapped in one tool with a built-in
      explanation of the runtime behavior gotcha.
    """
    wf = api.get_workflow(workflow_id)
    blocks_by_id = {b["id"]: b for b in wf["blocks"]}
    if source_node_id not in blocks_by_id:
        raise ValueError(f"source_node_id {source_node_id} not in workflow {workflow_id}")
    if target_node_id not in blocks_by_id:
        raise ValueError(f"target_node_id {target_node_id} not in workflow {workflow_id}")

    # ── Single-input guard ──────────────────────────────────────────────────
    # Only applies when target_handle is _default (the everything-else path).
    # Magic Node uses dfN handles and is exempt by design.
    if (target_handle == "_default"
            and target_handle not in _MAGIC_NODE_FAN_IN_HANDLES
            and not allow_multi_input):
        existing = _find_existing_default_incoming(wf["blocks"], target_node_id)
        # An identical edge (same source) is fine — that's the idempotent path
        # below. Only refuse when the existing edge is from a DIFFERENT source.
        if existing and existing.get("source_id") != source_node_id:
            raise ValueError(
                f"add_edge refuses to wire a second `_default` edge into "
                f"{target_node_id}. It already has an incoming `_default` edge "
                f"from {existing.get('source_id')}. Multiple `_default` edges "
                f"into one block looks fine in the UI but silently breaks at "
                f"execution. For joining or merging multiple data streams use "
                f"attach_magic_node (1–5 inputs, df1..dfN handles). To replace "
                f"the existing edge, call remove_edge first. For the legacy "
                f"Merge block specifically, pass allow_multi_input=True."
            )

    src = blocks_by_id[source_node_id]
    edges = src.get("toBlocks") or []
    for e in edges:
        if (e.get("toBlockId") == target_node_id
                and e.get("edge_source_handle_condition") == source_handle
                and e.get("edge_target_handle_condition") == target_handle):
            return {"ok": True, "edge_existed": True, "edge_added": False,
                    "workflowConfigError": wf.get("workflowConfigError"),
                    "validation": _maybe_validate(workflow_id, validate_after)}

    edges.append({
        "edgeId": f"{source_node_id}-{source_handle}-{target_node_id}-{target_handle}",
        "edge_source_handle_condition": source_handle,
        "edge_target_handle_condition": target_handle,
        "toBlockId": target_node_id,
    })
    src["toBlocks"] = edges

    # v0.2.16: per-node PUT instead of full-workflow PUT (avoids 413 on big workflows).
    # Send just the updated source block to put_node (defer validation until
    # after the target-side fix below).
    api.put_node(workflow_id, source_node_id, src)

    # v0.2.18: refresh target's isOrphan + inputs ──────────────────────────
    # Pre-fix, add_edge only PUT the source. The target's isOrphan/inputs
    # were not touched. For the common case of wiring an existing orphan
    # block (e.g. one created via paste_nodes with no parents), the target
    # stayed isOrphan=True and execution failed with "Node is orphan". The
    # platform doesn't auto-recompute isOrphan when a sibling block's
    # toBlocks gains a new edge — we have to mutate the target ourselves.
    #
    # v0.2.21: ALSO flip target's isTrigger=False when wiring downstream of
    # an existing root. Pre-fix, if both source AND target had isTrigger=True,
    # the workflow ended up with two start nodes and the UI got confused
    # (validated by user session: "the scheduler was still a start node not
    # a trigger node"). The platform allows multiple start nodes, but a node
    # that has a parent should NOT also be a start node.
    tgt = blocks_by_id[target_node_id]
    target_needs_refresh = (
        tgt.get("isOrphan")
        or not (tgt.get("inputs") or [])  # missing or empty inputs skeleton
        or tgt.get("isTrigger")  # v0.2.21: extra refresh trigger
    )
    target_isTrigger_flipped = False
    if target_needs_refresh:
        tgt["isOrphan"] = False
        if tgt.get("isTrigger"):
            tgt["isTrigger"] = False
            target_isTrigger_flipped = True
        if not (tgt.get("inputs") or []):
            tgt["inputs"] = [{
                "columns": [], "columns_metadata": None, "file": "",
                "handle_condition": "_default", "node_id": None,
            }]
        api.put_node(workflow_id, target_node_id, tgt)

    # Final validation (single GET) after both PUTs.
    validation = _maybe_validate(workflow_id, validate_after)
    workflow_err = validation.get("workflowConfigError") if validation else None
    is_runable = validation.get("isRunable") if validation else None
    return {
        "ok": not workflow_err,
        "edge_existed": False,
        "edge_added": True,
        "target_isOrphan_refreshed": target_needs_refresh,
        "target_isTrigger_flipped": target_isTrigger_flipped,  # v0.2.21
        "node_config_error": None,
        "workflowConfigError": workflow_err,
        "isRunable": is_runable,
        "validation": validation,
    }


@mcp.tool()
def remove_edge(
    workflow_id: str,
    source_node_id: str,
    target_node_id: str,
    source_handle: Optional[str] = None,
    target_handle: Optional[str] = None,
    validate_after: bool = True,
) -> dict:
    """Remove edge(s) from source → target.

    If `source_handle` and/or `target_handle` are omitted, ALL edges from
    source to target are removed regardless of handle. Pass them explicitly to
    target a specific edge (important when a source feeds the same target via
    multiple handles, e.g. a Filter feeding both `_passed` and `_failed` lanes
    to the same downstream sink).

    Returns `{ok, removed_count}`. removed_count == 0 is not an error — the
    operation is idempotent.
    """
    wf = api.get_workflow(workflow_id)
    src = next((b for b in wf["blocks"] if b["id"] == source_node_id), None)
    if src is None:
        raise ValueError(f"source_node_id {source_node_id} not in workflow {workflow_id}")

    edges = src.get("toBlocks") or []
    kept: list[dict] = []
    removed = 0
    for e in edges:
        match_tgt = e.get("toBlockId") == target_node_id
        match_src_h = source_handle is None or e.get("edge_source_handle_condition") == source_handle
        match_tgt_h = target_handle is None or e.get("edge_target_handle_condition") == target_handle
        if match_tgt and match_src_h and match_tgt_h:
            removed += 1
        else:
            kept.append(e)
    src["toBlocks"] = kept

    if removed == 0:
        return {"ok": True, "removed_count": 0, "message": "no matching edge — no-op",
                "validation": _maybe_validate(workflow_id, validate_after)}

    # v0.2.16: per-node PUT instead of full-workflow PUT (avoids 413 on big workflows).
    # The only mutation is the source's toBlocks; everything else in `wf` is
    # untouched. Send just the updated source block to put_node.
    mutation = _put_node_and_validate(workflow_id, source_node_id, src, validate_after)
    return {
        "removed_count": removed,
        **mutation,
    }


@mcp.tool()
def delete_node(workflow_id: str, node_id: str,
                 validate_after: bool = True,
                 confirm: bool = False) -> dict:
    """Delete a block and cascade-clean all incident edges.

    Removes:
      - the block itself
      - every outgoing edge from the deleted block (gone with it)
      - every incoming edge from any other block to the deleted block

    Does NOT auto-fix Magic Nodes that referenced this block as a `references`
    entry — call `update_magic_node(magic_id, ...)` separately if needed, or
    `validate_workflow` to surface the dangling references.

    v0.2.18: `ok` now reflects DELETE-OPERATION success, not workflow validity
    post-delete. Pre-fix, deleting the only block in a workflow always
    returned `ok:false` because the empty workflow has `workflowConfigError:
    "Workflow has no start nodes"` — which was confusing (3 stress-test
    agents independently misread it as a delete failure). The node IS gone;
    that's what `ok:true` means now. `workflowConfigError` and `isRunable`
    are still surfaced separately for callers who care about post-delete
    workflow state.

    v0.2.25: `confirm` is accepted as a no-op (for symmetry with
    `delete_workflow(confirm=True)`). Agents that learned the destructive-op
    pattern from `delete_workflow` were getting Pydantic validation errors
    here. Either value works; the delete fires either way. NOTE: this differs
    from `delete_workflow` where `confirm=True` is REQUIRED.

    Returns `{ok, deleted_node_id, deleted_node_name, incoming_edges_removed,
    outgoing_edges_removed, workflowConfigError, isRunable, validation}`.
    """
    _ = confirm  # accepted for ergonomic symmetry; no-op
    wf = api.get_workflow(workflow_id)
    target = next((b for b in wf["blocks"] if b["id"] == node_id), None)
    if target is None:
        raise ValueError(f"node {node_id} not in workflow {workflow_id}")

    outgoing_count = len(target.get("toBlocks") or [])
    new_blocks = [b for b in wf["blocks"] if b["id"] != node_id]
    incoming_count = 0
    for b in new_blocks:
        kept = []
        for e in (b.get("toBlocks") or []):
            if e.get("toBlockId") == node_id:
                incoming_count += 1
            else:
                kept.append(e)
        b["toBlocks"] = kept

    wf["blocks"] = new_blocks
    try:
        resp = _put_workflow_blocks(workflow_id, wf)
    except Exception as e:
        # The platform PUT raised — the block was NOT deleted server-side.
        # Surface as ok:false. This is distinct from the "deleted but workflow
        # invalid" case below.
        return {
            "ok": False,
            "deleted_node_id": node_id,
            "deleted_node_name": target.get("variableName"),
            "incoming_edges_removed": 0,
            "outgoing_edges_removed": 0,
            "error": str(e),
        }
    return {
        # v0.2.18: ok = "delete API call succeeded". The node IS gone.
        # workflowConfigError is surfaced separately — a post-delete
        # "no start nodes" error doesn't mean the delete failed.
        "ok": True,
        "deleted_node_id": node_id,
        "deleted_node_name": target.get("variableName"),
        "incoming_edges_removed": incoming_count,
        "outgoing_edges_removed": outgoing_count,
        "workflowConfigError": resp.get("workflowConfigError"),
        "isRunable": resp.get("isRunable"),
        "validation": _maybe_validate(workflow_id, validate_after),
    }


@mcp.tool()
def splice_branch(
    workflow_id: str,
    new_terminal_node_id: str,
    downstream_target_node_id: str,
    downstream_target_handle: str = "_default",
    replace_edge_from_node_id: Optional[str] = None,
    replace_edge_target_handle: str = "_default",
    allow_multi_input: bool = False,
    validate_after: bool = True,
) -> dict:
    """Splice a new branch into the live path. Atomic add + optional replace.

    The "splice in" half of the parallel-branch test pattern (NREV_WORKFLOW_GUIDE
    §11.8). Adds an edge from `new_terminal_node_id` to `downstream_target_node_id`,
    optionally removing the equivalent edge from `replace_edge_from_node_id` to
    the same downstream — all in a single PUT for atomicity.

    SINGLE-INPUT GUARD (v0.2.13): if `replace_edge_from_node_id` is None AND
    `downstream_target_handle == "_default"`, the same guard as add_edge fires:
    we refuse to wire a second `_default` edge into a single-input target.
    The typical splice pattern (with `replace_edge_from_node_id` set) is
    unaffected because the old edge is removed before the new one is added.
    Pass `allow_multi_input=True` for the legacy Merge case.

    After this, you can safely `delete_node(replace_edge_from_node_id)` and any
    upstream blocks that were exclusive to the old chain.
    """
    wf = api.get_workflow(workflow_id)
    blocks_by_id = {b["id"]: b for b in wf["blocks"]}
    if new_terminal_node_id not in blocks_by_id:
        raise ValueError(f"new_terminal_node_id {new_terminal_node_id} not in workflow")
    if downstream_target_node_id not in blocks_by_id:
        raise ValueError(f"downstream_target_node_id {downstream_target_node_id} not in workflow")

    removed_count = 0
    if replace_edge_from_node_id:
        old_src = blocks_by_id.get(replace_edge_from_node_id)
        if old_src is None:
            raise ValueError(
                f"replace_edge_from_node_id {replace_edge_from_node_id} not in workflow"
            )
        kept = []
        for e in (old_src.get("toBlocks") or []):
            if (e.get("toBlockId") == downstream_target_node_id
                    and e.get("edge_target_handle_condition") == replace_edge_target_handle):
                removed_count += 1
            else:
                kept.append(e)
        old_src["toBlocks"] = kept

    # ── Single-input guard (same as add_edge) ───────────────────────────────
    # Only when there's no replace target — otherwise the splice pattern is
    # already removing one edge before adding the new one, so net incoming
    # count stays at 1.
    if (replace_edge_from_node_id is None
            and downstream_target_handle == "_default"
            and downstream_target_handle not in _MAGIC_NODE_FAN_IN_HANDLES
            and not allow_multi_input):
        existing = _find_existing_default_incoming(wf["blocks"], downstream_target_node_id)
        if existing and existing.get("source_id") != new_terminal_node_id:
            raise ValueError(
                f"splice_branch refuses to wire a second `_default` edge into "
                f"{downstream_target_node_id}. It already has an incoming "
                f"`_default` edge from {existing.get('source_id')}. To replace "
                f"that edge, pass replace_edge_from_node_id="
                f"{existing.get('source_id')!r}. For Magic Node fan-in use a "
                f"dfN target handle. For the legacy Merge block specifically, "
                f"pass allow_multi_input=True."
            )

    new_terminal = blocks_by_id[new_terminal_node_id]
    new_edges = new_terminal.get("toBlocks") or []
    edge_existed = any(
        e.get("toBlockId") == downstream_target_node_id
        and e.get("edge_target_handle_condition") == downstream_target_handle
        for e in new_edges
    )
    if not edge_existed:
        new_edges.append({
            "edgeId": f"{new_terminal_node_id}-_default-{downstream_target_node_id}-{downstream_target_handle}",
            "edge_source_handle_condition": "_default",
            "edge_target_handle_condition": downstream_target_handle,
            "toBlockId": downstream_target_node_id,
        })
    new_terminal["toBlocks"] = new_edges

    resp = _put_workflow_blocks(workflow_id, wf)
    return {
        "ok": not resp.get("workflowConfigError"),
        "spliced": True,
        "edge_added": not edge_existed,
        "old_edges_removed": removed_count,
        "workflowConfigError": resp.get("workflowConfigError"),
        "isRunable": resp.get("isRunable"),
        "validation": _maybe_validate(workflow_id, validate_after),
    }


def _put_workflow_blocks(workflow_id: str, wf: dict) -> dict:
    """Internal helper: PUT the workflow with current blocks (used after wiring edits).

    LEGACY (pre-v0.2.16): this is the full-workflow PUT that 413s on workflows
    past ~50 blocks. v0.2.16 converted the 6 single-block mutation tools
    (update_node_setting, update_magic_node, update_ai_prompt,
    set_node_output_schema, add_edge, remove_edge) to use the smaller
    `_put_node_and_validate` helper instead. The remaining callers
    (delete_node, splice_branch, clone_node, set_test_mode workflow-scope,
    bulk_set_test_mode) still use this path — see v0.2.17+ for those.
    """
    payload = {"workflow_details": {
        "id": workflow_id,
        "name": wf.get("name"),
        "description": wf.get("description"),
        "blocks": wf["blocks"],
    }}
    return api.put_workflow(workflow_id, payload)


def _put_node_and_validate(
    workflow_id: str,
    node_id: str,
    node: dict,
    validate_after: bool,
) -> dict:
    """v0.2.16 helper for single-block mutation tools.

    Replaces the pre-v0.2.16 full-workflow PUT (which 413s past ~50 blocks)
    with the platform's per-node PUT. Returns a standard response slice
    `{ok, node_config_error, workflowConfigError, isRunable, validation}`
    that the converted tools merge into their richer return dicts.

    The per-node PUT response carries `node_config_error` for the updated
    block. Workflow-level fields (`workflowConfigError`, `isRunable`) come
    from the optional post-mutation validate GET — set `validate_after=False`
    to skip that GET, in which case workflow-level fields are surfaced as
    None and the caller is responsible for any follow-up validation.
    """
    put_resp = api.put_node(workflow_id, node_id, node)
    node_err = put_resp.get("node_config_error") if isinstance(put_resp, dict) else None
    validation = _maybe_validate(workflow_id, validate_after)
    workflow_err = validation.get("workflowConfigError") if validation else None
    is_runable = validation.get("isRunable") if validation else None
    return {
        "ok": node_err is None and not workflow_err,
        "node_config_error": node_err,
        "workflowConfigError": workflow_err,
        "isRunable": is_runable,
        "validation": validation,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Edit (in-place node mutations)
# ═══════════════════════════════════════════════════════════════════════════

def _walk_settings_set(settings_list: list[dict], segments: list[str], value) -> bool:
    """Walk a /-separated path through nested settings_field_values and set the leaf.

    Returns True if the path was found and the value was set; False otherwise.
    Each path segment matches an entry's `field_name`. Nested groups are
    traversed via the entry's `field_value` (which must be a list).
    """
    if not segments:
        return False
    head, rest = segments[0], segments[1:]
    for entry in settings_list:
        if entry.get("field_name") == head:
            if not rest:
                entry["field_value"] = value
                return True
            if isinstance(entry.get("field_value"), list):
                return _walk_settings_set(entry["field_value"], rest, value)
            return False
    return False


def _walk_settings_set_label(settings_list: list[dict], segments: list[str], label) -> bool:
    """Walk a /-separated path and set the fieldLabel on the leaf entry.

    v0.2.20: companion to _walk_settings_set, used when update_node_setting
    is called with field_label= to bind a human-readable label alongside the
    value (Pipedream connection fields and some dropdowns require this).
    """
    if not segments:
        return False
    head, rest = segments[0], segments[1:]
    for entry in settings_list:
        if entry.get("field_name") == head:
            if not rest:
                entry["fieldLabel"] = label
                return True
            if isinstance(entry.get("field_value"), list):
                return _walk_settings_set_label(entry["field_value"], rest, label)
            return False
    return False


def _list_field_paths(settings_list: list[dict], prefix: str = "") -> list[str]:
    """Enumerate all leaf field paths in nested settings_field_values."""
    paths = []
    for entry in (settings_list or []):
        name = entry.get("field_name")
        if not name:
            continue
        path = f"{prefix}{name}"
        val = entry.get("field_value")
        if isinstance(val, list) and val and all(isinstance(x, dict) and "field_name" in x for x in val):
            # Group entry — recurse
            paths.extend(_list_field_paths(val, prefix=path + "/"))
        else:
            paths.append(path)
    return paths


PROMPT_KEYWORDS = ("prompt", "instruction", "system_message", "user_message", "query")
PROMPT_NEGATIVE_KEYWORDS = ("file", "url", "attachment", "ids", "list", "count", "id", "name",
                            "type", "model", "image", "schema")


def _score_prompt_field(field_name: str) -> int:
    """Score a field name for prompt-ness.

      0   = no match (or trailing-segment is a negative keyword)
      3   = keyword appears as substring (last-resort match)
      10  = trailing segment exactly matches a prompt keyword

    Negative keywords (`file`, `url`, `attachment`, `ids`, `list`, `count`, `id`,
    `name`, `type`, `model`, `image`, `schema`) zero the score ONLY when they
    are the trailing segment (or plural form) — so `model-system_message` still
    scores 10 (trailing segment is "system_message"), but `ai-prompt_file_urls`
    scores 0 (trailing is "urls", a plural negative).
    """
    n = field_name.lower()
    last_after_slash = n.split("/")[-1]
    last_after_dash = last_after_slash.split("-")[-1]
    last_after_under = last_after_slash.split("_")[-1]

    # Strong positive: trailing segment exactly matches a prompt keyword
    if last_after_dash in PROMPT_KEYWORDS or last_after_under in PROMPT_KEYWORDS:
        return 10

    # Negative: trailing segment matches a negative keyword (or its plural)
    for neg in PROMPT_NEGATIVE_KEYWORDS:
        if last_after_under in (neg, neg + "s") or last_after_dash in (neg, neg + "s"):
            return 0

    # Weak positive: keyword appears as substring
    if any(kw in n for kw in PROMPT_KEYWORDS):
        return 3

    return 0


def _find_prompt_fields(settings_list: list[dict], prefix: str = "") -> list[tuple[str, int]]:
    """Find leaf paths whose field_name suggests it's a prompt/instruction.

    Returns a list of `(path, score)` tuples. Empty list if no candidates.
    """
    matches: list[tuple[str, int]] = []
    for entry in (settings_list or []):
        name = entry.get("field_name") or ""
        path = f"{prefix}{name}"
        val = entry.get("field_value")
        is_group = isinstance(val, list) and val and all(isinstance(x, dict) and "field_name" in x for x in val)
        if is_group:
            matches.extend(_find_prompt_fields(val, prefix=path + "/"))
        else:
            score = _score_prompt_field(name)
            if score > 0:
                matches.append((path, score))
    return matches


@mcp.tool()
def update_node_setting(
    workflow_id: str,
    node_id: str,
    field_path: str,
    value,
    validate_after: bool = True,
    verify: bool = False,
    verify_cost_ack: bool = False,
    add_if_missing: bool = True,
    field_label: Optional[str] = None,
) -> dict:
    """Replace (or add) a single setting value on a node, identified by its field path.

    `field_path` is `/`-separated. Each segment matches a `field_name` in the
    settings tree. For top-level settings, just the field name. For nested
    group fields, e.g. for a Magic Node code:

        "data_manipulation-magic_node-code_section/data_manipulation-magic_node-code"

    `add_if_missing=True` (default, v0.2.20): if the field_path is not present
    in the node's current settings, append it as a new entry with the proper
    envelope shape. This is REQUIRED for Pipedream nodes whose schema
    progresses — they start with only the connection bound and additional
    fields (drive, sheetId, hasHeaders, row data, etc.) must be ADDED as you
    configure the node step-by-step. Only supports top-level paths (single
    segment) for safety; for nested group additions, set the parent group
    explicitly. Pass `add_if_missing=False` to restore strict modify-only
    semantics.

    `field_label`: optional human-readable label to bind alongside the value
    (used by Pipedream connection fields and some dropdowns). Stored as
    `fieldLabel` in the settings envelope.

    If the path isn't found AND add_if_missing=False (or the path is nested
    and the parent group doesn't exist), returns the list of all leaf paths so
    you can spot the right one. Use `get_node`, `list_node_settings`, or
    `get_node_dynamic_fields` first to inspect.

    `verify=True` (default False) runs `partial_execute` on this node after the
    update, reusing cached upstream from the most recent completed execution,
    and returns the new row_count in the response. NOTE: for paid nodes
    (creditCostPerItem > 0, typically AI nodes), additional opt-in is required —
    pass `verify_cost_ack=True` to confirm the spend. Without ack, verify is
    skipped on paid nodes and an explanatory note is returned.
    """
    # v0.2.24 Fix #1: MCP transport coerces structured values (lists/dicts) to
    # JSON strings under some callers. If we receive a string that LOOKS like
    # JSON (starts with [ or {), try to parse it back. Without this defensive
    # coerce, a `references` array on a Magic Node arrives as the literal
    # string '["edge1","edge2"]' and the platform validator iterates over it
    # character-by-character (one warning per `[`, `"`, `e`, `d`, `g`, …).
    # Detected via the screenshot in the 2026-05-25 session — Claude burned
    # several attempts trying to edit Magic Node references before giving up
    # and rebuilding the node.
    if isinstance(value, str) and value and value[0] in "[{":
        try:
            parsed = _json.loads(value)
            if isinstance(parsed, (list, dict)):
                value = parsed
        except (_json.JSONDecodeError, ValueError):
            # Not JSON — value is a string that happens to start with [ or {.
            # Leave it as-is (the field probably expects exactly that string).
            pass

    wf = api.get_workflow(workflow_id)
    target = next((b for b in wf["blocks"] if b["id"] == node_id), None)
    if target is None:
        raise ValueError(f"node {node_id} not in workflow {workflow_id}")

    settings = target.get("settings_field_values") or []
    segments = field_path.split("/")
    added = False
    if not _walk_settings_set(settings, segments, value):
        # v0.2.20: optionally append as a new top-level entry
        if add_if_missing and len(segments) == 1:
            if target.get("settings_field_values") is None:
                target["settings_field_values"] = []
            target["settings_field_values"].append(_sf(segments[0], value, label=field_label))
            added = True
        else:
            return {
                "ok": False,
                "message": (
                    f"field_path {field_path!r} not found in node settings"
                    + (" (nested paths cannot be auto-added; set the parent group first)"
                       if len(segments) > 1 else "")
                    + "."
                ),
                "available_paths": _list_field_paths(settings),
            }
    elif field_label is not None:
        # Path found and field_label provided — update the label too
        _walk_settings_set_label(target.get("settings_field_values") or [],
                                 segments, field_label)

    # v0.2.16: per-node PUT instead of full-workflow PUT (avoids 413 on big workflows)
    mutation = _put_node_and_validate(workflow_id, node_id, target, validate_after)
    result = {
        "node_id": node_id,
        "field_path": field_path,
        "added_new_field": added,  # v0.2.20: True if path was created, False if modified
        **mutation,
    }

    if verify and result["ok"]:
        result["verify"] = _verify_node_after_update(workflow_id, node_id, target, verify_cost_ack)

    return result


def _verify_node_after_update(workflow_id: str, node_id: str, target_block: dict,
                              verify_cost_ack: bool) -> dict:
    """Run partial_execute on a node post-update; return the new row_count.

    Cost-guarded: paid nodes require verify_cost_ack=True (the user has explicitly
    acknowledged the spend). Without ack, returns `{"status": "skipped_paid"}`.
    """
    cost = target_block.get("creditCostPerItem") or 0
    if cost > 0 and not verify_cost_ack:
        return {
            "status": "skipped_paid",
            "creditCostPerItem": cost,
            "message": (
                f"Node costs {cost} cr/item; pass verify_cost_ack=True to "
                f"confirm spending credits on the verify run."
            ),
        }

    # Find a recent completed execution to use as cached upstream
    try:
        raw = api.list_executions(workflow_id, limit=5)
        items = raw.get("data") if isinstance(raw, dict) else (raw or [])
        prior = next(
            (e.get("id") for e in (items or [])
             if e.get("status") == "completed" and not e.get("isTestMode")),
            None,
        )
    except Exception as e:
        return {"status": "skipped_error", "message": f"could not list executions: {e}"}

    if not prior:
        return {
            "status": "skipped_no_prior",
            "message": "no prior completed execution found to use as cached upstream",
        }

    try:
        api.execute_node(workflow_id, node_id, prior_execution_id=prior)
        # Poll until this node completes
        if not _wait_for_node(workflow_id, prior, node_id, timeout=300):
            return {"status": "timeout", "prior_execution_id": prior,
                    "message": "node still running after 5 minutes"}
        # Read row count from the now-fresh output
        preview = api.get_node_preview(workflow_id, prior, node_id, limit=1)
        result = {
            "status": "completed",
            "prior_execution_id": prior,
            "total_entries": (preview or {}).get("meta", {}).get("total_entries"),
        }
        # v0.2.20 Fix F: for Pipedream-wrapped nodes, block-level status:completed
        # can mask a row-level error. Probe row[0].error so verify doesn't
        # falsely claim success on a silently-failed Pipedream action.
        if _is_pipedream_block(target_block):
            chk = _check_pipedream_row_error(workflow_id, prior, node_id)
            if chk and chk.get("has_row_error"):
                result["pipedream_row_error"] = chk
                result["status"] = "completed_with_pipedream_row_error"
                result["message"] = (
                    "The block completed but the Pipedream action returned an "
                    "error in the row output. See pipedream_row_error for details."
                )
        return result
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _wait_for_node(workflow_id: str, execution_id: str, node_id: str,
                   timeout: int = 300, poll_seconds: int = 2) -> bool:
    """Poll get_execution until the named node hits a terminal status.

    Returns True if node reached completed / failed within timeout, else False.
    """
    import time
    start = time.time()
    while time.time() - start < timeout:
        try:
            raw = api.get_execution_detail(workflow_id, execution_id)
            for br in raw.get("blockRuns") or []:
                if br.get("workflowBlockId") == node_id:
                    status = br.get("status")
                    if status in ("completed", "failed"):
                        return True
                    break
        except Exception:
            pass
        time.sleep(poll_seconds)
    return False


@mcp.tool()
def update_magic_node(
    workflow_id: str,
    node_id: str,
    code: Optional[str] = None,
    name: Optional[str] = None,
    instructions_text: Optional[str] = None,
    output_columns: Optional[list[str]] = None,
    output_dtypes: Optional[list[str]] = None,
    validate_after: bool = True,
) -> dict:
    """Update a Magic Node's code, name, instructions, or output schema in place.

    Re-lints the code if provided (refuses to PUT if E000-E004 blocking issues found;
    W005 warnings surface but don't block).
    Repatches `outputs.columns_metadata` if `output_columns` is provided.

    Does NOT update the `references` list — wiring changes go through
    `add_edge` / `remove_edge`. To add a new input to a Magic Node, the safer
    pattern is: build a new Magic Node via `attach_magic_node` with the new
    parent set, then `splice_branch` and `delete_node` the old one.

    All updateable fields are optional; pass only what you want changed.
    """
    wf = api.get_workflow(workflow_id)
    target = next((b for b in wf["blocks"] if b["id"] == node_id), None)
    if target is None:
        raise ValueError(f"node {node_id} not in workflow {workflow_id}")
    if target.get("typeId") != block_types.MAGIC_NODE:
        raise ValueError(
            f"node {node_id} is not a Magic Node "
            f"(typeId={target.get('typeId')}). Use update_node_setting for other block types."
        )

    lint_warnings: list[str] = []
    if code is not None:
        issues = lint(code)
        blocking = [i for i in issues if i.is_blocking]
        warns = [i for i in issues if not i.is_blocking]
        if blocking:
            return {
                "ok": False,
                "stage": "lint",
                "issues": [i.format() for i in blocking],
                "message": "Sandbox lint blocked the update. Fix the issues above and retry.",
            }
        lint_warnings = [i.format() for i in warns]
        _walk_settings_set(
            target["settings_field_values"],
            ["data_manipulation-magic_node-code_section", "data_manipulation-magic_node-code"],
            code,
        )

    if instructions_text is not None:
        _walk_settings_set(
            target["settings_field_values"],
            [
                "data_manipulation-magic_node-instructions_and_ref",
                "data_manipulation-magic_node-instructions",
                "data_manipulation-magic_node-instructions_text",
            ],
            instructions_text,
        )

    if name is not None:
        target["variableName"] = name

    if output_columns is not None:
        if output_dtypes is None:
            output_dtypes = ["string"] * len(output_columns)
        if len(output_dtypes) != len(output_columns):
            raise ValueError("output_dtypes length must match output_columns length")
        # v0.2.23 Fix #1 — use the canonical _build_columns_metadata helper so
        # origin_node_id / origin_node_name / origin_node_type are populated
        # for self-produced columns. Pre-v0.2.23 we emitted incomplete entries
        # which the platform 422'd, blocking iterative Magic Node edits.
        target["outputs"] = [{
            "columns": output_columns,
            "columns_metadata": _build_columns_metadata(
                output_columns, output_dtypes,
                origin_node_id=node_id,
                origin_node_name=target.get("variableName") or name or "",
                origin_node_type="data_manipulation.magic_node",
            ),
            "file": "",
            "handle_condition": "_default",
            "node_id": node_id,
        }]

    # v0.2.16: per-node PUT instead of full-workflow PUT (avoids 413 on big workflows)
    mutation = _put_node_and_validate(workflow_id, node_id, target, validate_after)
    return {
        "node_id": node_id,
        "lint_warnings": lint_warnings,
        **mutation,
    }


@mcp.tool()
def update_ai_prompt(
    workflow_id: str,
    node_id: str,
    new_prompt: str,
    validate_after: bool = True,
) -> dict:
    """Find the prompt/instruction field on an AI node and update it.

    Walks the node's settings scoring leaf fields by prompt-ness:
      - Trailing-segment exact match (e.g. `ai-prompt`, `ai-system_message`)  → score 10
      - Keyword appears anywhere in name (substring)                          → score 3
      - Negative keywords (`file`, `url`, `attachment`, `ids`, `list`, etc.)  → score 0

    Picks the highest-scoring candidate if it's a clear winner (score >= 5 AND
    gap >= 5 to the second-best). Otherwise returns ambiguity with the candidate
    list so you can disambiguate via `update_node_setting(field_path=...)`.

    This is the fix for the `prompt` vs `prompt_file_urls` ambiguity bomb —
    `prompt_file_urls` now scores 0 because of the negative `url` / `urls`
    keyword, and `prompt` wins cleanly.
    """
    wf = api.get_workflow(workflow_id)
    target = next((b for b in wf["blocks"] if b["id"] == node_id), None)
    if target is None:
        raise ValueError(f"node {node_id} not in workflow {workflow_id}")

    settings = target.get("settings_field_values") or []
    candidates = _find_prompt_fields(settings)
    candidates.sort(key=lambda x: -x[1])  # highest score first

    if not candidates:
        return {
            "ok": False,
            "message": "No prompt-like field found in node settings. "
                       "Use get_node or list_node_settings to inspect, then "
                       "update_node_setting with the right field path.",
            "available_paths": _list_field_paths(settings),
        }

    top_path, top_score = candidates[0]
    runner_up_score = candidates[1][1] if len(candidates) > 1 else 0
    clear_winner = top_score >= 5 and (len(candidates) == 1 or (top_score - runner_up_score) >= 5)

    if not clear_winner:
        return {
            "ok": False,
            "message": "Multiple prompt-like fields with similar scores; ambiguous. "
                       "Disambiguate via update_node_setting(field_path=<one of these>).",
            "candidates": [{"path": p, "score": s} for p, s in candidates[:5]],
        }

    _walk_settings_set(settings, top_path.split("/"), new_prompt)

    # v0.2.16: per-node PUT instead of full-workflow PUT (avoids 413 on big workflows)
    mutation = _put_node_and_validate(workflow_id, node_id, target, validate_after)
    return {
        "node_id": node_id,
        "field_path": top_path,
        "score": top_score,
        **mutation,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Run inspection (per-execution, per-node neighbors)
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_execution(workflow_id: str, execution_id: str) -> dict:
    """Per-block status, errors, credits, duration for one execution.

    Slim view of `/execution-logs/workflow/{wf}/workflow-execution/{exec_id}`.
    Use this to find which block failed in a past run, then `get_node_output`
    on its upstream to see what data caused the failure.
    """
    raw = api.get_execution_detail(workflow_id, execution_id)
    block_runs = raw.get("blockRuns") or []
    slim_runs = [
        {
            "block_id": br.get("workflowBlockId"),
            "block_name": br.get("workflowBlockName"),
            "status": br.get("status"),
            "creditsUsed": br.get("creditsUsed"),
            "duration": br.get("duration"),
            "rowCount": br.get("rowCount"),
            "error": br.get("error"),
            "isTestMode": br.get("isTestMode"),
        }
        for br in block_runs
    ]
    return {
        "execution_id": execution_id,
        "workflow_id": workflow_id,
        "status": raw.get("status"),
        "creditsUsed": raw.get("creditsUsed"),
        "createdAt": raw.get("createdAt"),
        "completedAt": raw.get("completedAt"),
        "duration": raw.get("duration"),
        "block_run_count": len(slim_runs),
        "block_runs": slim_runs,
    }


@mcp.tool()
def get_node_neighbors(workflow_id: str, node_id: str) -> dict:
    """Inspect a node's incoming and outgoing edges.

    Outgoing comes straight from the node's `toBlocks`. Incoming is computed
    by inverting `toBlocks` across every block in the workflow (the API only
    stores forward edges).

    Always run this BEFORE editing wiring around a node — knowing what feeds in
    and what flows out prevents broken splices.
    """
    wf_data = api.get_workflow(workflow_id)
    target = next((b for b in wf_data["blocks"] if b["id"] == node_id), None)
    if target is None:
        raise ValueError(f"node {node_id} not in workflow {workflow_id}")

    blocks_by_id = {b["id"]: b for b in wf_data["blocks"]}

    outgoing = []
    for e in (target.get("toBlocks") or []):
        peer = blocks_by_id.get(e.get("toBlockId"))
        outgoing.append({
            "node_id": e.get("toBlockId"),
            "node_name": peer.get("variableName") if peer else None,
            "src_handle": e.get("edge_source_handle_condition"),
            "tgt_handle": e.get("edge_target_handle_condition"),
        })

    incoming = []
    for b in wf_data["blocks"]:
        if b["id"] == node_id:
            continue
        for e in (b.get("toBlocks") or []):
            if e.get("toBlockId") == node_id:
                incoming.append({
                    "node_id": b["id"],
                    "node_name": b.get("variableName"),
                    "src_handle": e.get("edge_source_handle_condition"),
                    "tgt_handle": e.get("edge_target_handle_condition"),
                })

    return {
        "node_id": node_id,
        "node_name": target.get("variableName"),
        "type_id": target.get("typeId"),
        "incoming_count": len(incoming),
        "outgoing_count": len(outgoing),
        "incoming": incoming,
        "outgoing": outgoing,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Diagnostics (path tracing, cost estimation)
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def trace_path(
    workflow_id: str,
    from_node_id: str,
    to_node_id: Optional[str] = None,
    max_paths: int = 50,
) -> dict:
    """Enumerate every DAG path from `from_node_id` (to `to_node_id` or all sinks).

    Useful for "what depends on this node?" investigations and for understanding
    multi-branch fan-in / fan-out. Cycle-safe: a node already on the current path
    is skipped.

    Returns up to `max_paths` paths to keep responses bounded on dense graphs.
    """
    wf_data = api.get_workflow(workflow_id)
    blocks_by_id = {b["id"]: b for b in wf_data["blocks"]}
    if from_node_id not in blocks_by_id:
        raise ValueError(f"from_node_id {from_node_id} not in workflow {workflow_id}")
    if to_node_id is not None and to_node_id not in blocks_by_id:
        raise ValueError(f"to_node_id {to_node_id} not in workflow {workflow_id}")

    paths: list[list[str]] = []

    def dfs(current: str, path: list[str]) -> None:
        if len(paths) >= max_paths:
            return
        path = path + [current]
        outgoing = [e["toBlockId"] for e in (blocks_by_id[current].get("toBlocks") or [])]
        if to_node_id is None:
            if not outgoing:
                paths.append(path)
                return
        else:
            if current == to_node_id:
                paths.append(path)
                return
        for n in outgoing:
            if n in path or n not in blocks_by_id:
                continue  # cycle or dangling
            dfs(n, path)

    dfs(from_node_id, [])

    rendered = [
        [{"id": nid, "name": blocks_by_id[nid].get("variableName")} for nid in p]
        for p in paths
    ]
    return {
        "from_node_id": from_node_id,
        "to_node_id": to_node_id,
        "path_count": len(paths),
        "truncated": len(paths) >= max_paths,
        "paths": rendered,
    }


@mcp.tool()
def dry_run_cost(workflow_id: str) -> dict:
    """Estimate full-run cost for a workflow.

    Returns:
      - per-paid-node cost-per-item
      - sum-per-row (lower bound: assumes 1 row through each paid node)
      - if a recent successful execution exists, its actual creditsUsed as a
        more realistic estimate

    Per-row sum is just `sum(creditCostPerItem)`. Real cost = per-row sum × the
    typical row count flowing through each paid node, which varies per run.
    """
    wf_data = api.get_workflow(workflow_id)
    paid: list[dict] = []
    per_row_sum = 0
    for b in wf_data.get("blocks", []):
        cpi = b.get("creditCostPerItem") or 0
        if cpi > 0:
            paid.append({
                "node_id": b["id"],
                "name": b.get("variableName"),
                "creditCostPerItem": cpi,
            })
            per_row_sum += cpi

    estimate = {
        "ok": True,
        "workflow_id": workflow_id,
        "paid_nodes": paid,
        "per_row_credit_sum": per_row_sum,
        "based_on": "static — sum of creditCostPerItem across paid nodes; multiply by your typical row count",
    }

    # Try to enrich with a recent completed execution as a real-world anchor.
    try:
        raw = api.list_executions(workflow_id, limit=10)
        items = raw.get("data") if isinstance(raw, dict) else (raw or [])
        completed = next(
            (e for e in (items or []) if e.get("status") == "completed" and not e.get("isTestMode")),
            None,
        )
        if completed:
            estimate["last_full_run_credits"] = completed.get("creditsUsed")
            estimate["last_full_run_id"] = completed.get("id")
            estimate["last_full_run_at"] = completed.get("createdAt")
            estimate["based_on"] = (
                "Static per-row sum + last completed (non-test) full execution as the anchor."
            )
    except Exception:
        pass

    return estimate


# ═══════════════════════════════════════════════════════════════════════════
# Settings inspection + output-schema patching (v0.2.1 additions)
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def list_node_settings(workflow_id: str, node_id: str) -> dict:
    """List every leaf field path inside a node's settings.

    Use this to discover the right `field_path` for `update_node_setting` without
    having to wade through the full `get_node` blob. For nested group fields, paths
    are `/`-separated (e.g. for a Magic Node code):

        data_manipulation-magic_node-code_section/data_manipulation-magic_node-code

    Free; one GET. Faster than a full node read when all you want is the field map.
    """
    wf = api.get_workflow(workflow_id)
    target = next((b for b in wf["blocks"] if b["id"] == node_id), None)
    if target is None:
        raise ValueError(f"node {node_id} not in workflow {workflow_id}")
    paths = _list_field_paths(target.get("settings_field_values") or [])
    return {
        "node_id": node_id,
        "name": target.get("variableName"),
        "type_id": target.get("typeId"),
        "field_count": len(paths),
        "field_paths": paths,
    }


@mcp.tool()
def set_node_output_schema(
    workflow_id: str,
    node_id: str,
    columns: list[dict],
    validate_after: bool = True,
) -> dict:
    """Patch a node's `outputs.columns` and `outputs.columns_metadata` in place.

    The standard follow-up after editing a Custom Code or Magic Node whose code
    now produces a different set of output columns. Without this patch, downstream
    nodes fail with "Fields not found in available data" because the platform
    relies on `columns_metadata` for cross-block schema validation.

    `columns` is a list of dicts: each entry should have:
        name      — str, required (the column name)
        dtype     — str, optional, defaults to "string"
                    (typical values: string, integer, float, boolean, json, datetime)
        nullable  — bool, optional, defaults to True

    Example:
        set_node_output_schema(wf, node_id, columns=[
            {"name": "company"},
            {"name": "icp_score", "dtype": "integer"},
            {"name": "contacts_json", "dtype": "json"},
            {"name": "contact_count", "dtype": "integer"},
        ])
    """
    if not columns:
        raise ValueError("columns must be a non-empty list")

    wf = api.get_workflow(workflow_id)
    target = next((b for b in wf["blocks"] if b["id"] == node_id), None)
    if target is None:
        raise ValueError(f"node {node_id} not in workflow {workflow_id}")

    type_label = _typeid_to_value_slug(target.get("typeId"))
    target_name = target.get("variableName", "")
    col_names = []
    col_metadata = []
    for c in columns:
        if not isinstance(c, dict) or "name" not in c:
            raise ValueError(f"each column entry must be a dict with at least 'name'; got {c!r}")
        col_names.append(c["name"])
        col_metadata.append({
            "column_name": c["name"],
            "data_type": c.get("dtype", "string"),
            "is_nullable": c.get("nullable", True),
            # The platform requires origin_node_id on every column; without it
            # PUT returns 422. Default to current node (callers can override
            # for pass-through columns from upstream).
            "origin_node_id": c.get("origin_node_id", node_id),
            "origin_node_name": c.get("origin_node_name", target_name),
            "origin_node_type": c.get("origin_node_type", type_label),
            "nested_fields": c.get("nested_fields"),
        })

    target["outputs"] = [{
        "columns": col_names,
        "columns_metadata": col_metadata,
        "file": "",
        "handle_condition": "_default",
        "node_id": node_id,
    }]

    # v0.2.16: per-node PUT instead of full-workflow PUT (avoids 413 on big workflows)
    mutation = _put_node_and_validate(workflow_id, node_id, target, validate_after)
    return {
        "node_id": node_id,
        "column_count": len(col_names),
        "columns": col_names,
        **mutation,
    }


@mcp.tool()
def clone_node(
    workflow_id: str,
    source_node_id: str,
    new_name: Optional[str] = None,
    position_offset_x: float = 0,
    position_offset_y: float = 350,
    set_test_mode: Optional[bool] = None,
    set_settings: Optional[dict] = None,
    validate_after: bool = True,
) -> dict:
    """Clone a block in-place. Generates new UUID, optionally tweaks settings,
    appends to the workflow's block list. Does NOT wire any edges (caller
    decides — call add_edge separately).

    Useful for:
      - parallel-branch test patterns: clone an Ask AI / Magic Node / Filter,
        modify its prompt or code, leave it disconnected from downstream,
        partial_execute on it, compare outputs
      - duplicating a "known good" block as the starting point for a variant
      - any time you want a block's exact configuration as a baseline

    Args:
        source_node_id: the block to clone
        new_name: defaults to "<source name> [copy]"
        position_offset_x / position_offset_y: pixels to offset from source's
            position. Default `y=350` places the clone visibly below the source.
            Pass 0 for both to land exactly on top of the source (not recommended).
        set_test_mode: None inherits source's value; True/False overrides
        set_settings: dict mapping field_path (slash-separated, same syntax as
            update_node_setting) → new value. Each entry is applied to the
            CLONED node's settings tree. Use to swap a prompt, change a model,
            tweak Custom Code, etc. without a follow-up update call.

    Returns:
        {ok, new_node_id, source_node_id, validation: {...}}
    """
    wf = api.get_workflow(workflow_id)
    source = next((b for b in wf["blocks"] if b["id"] == source_node_id), None)
    if source is None:
        raise ValueError(f"source_node_id {source_node_id} not in workflow {workflow_id}")

    import copy as _copy
    cloned = _copy.deepcopy(source)
    new_id = str(uuid.uuid4())

    cloned["id"] = new_id
    cloned["variableName"] = new_name or f"{source.get('variableName', 'untitled')} [copy]"

    # Offset position
    pos = source.get("position") or {"x": 0, "y": 0}
    cloned["position"] = {
        "x": float(pos.get("x", 0)) + position_offset_x,
        "y": float(pos.get("y", 0)) + position_offset_y,
    }

    # Test mode override (None = inherit)
    if set_test_mode is not None:
        cloned["isTestMode"] = bool(set_test_mode)

    # Clear outgoing edges — clone is terminal until caller wires it
    cloned["toBlocks"] = []

    # ── Strip trigger flags (v0.2.13) ───────────────────────────────────────
    # A clone is structurally never a trigger — the workflow already has its
    # trigger (the source we just copied from). Deepcopy carries isTrigger
    # and isListener through, so a naive clone of a Scheduler creates a
    # second trigger and silently corrupts the workflow. Always strip both,
    # and surface a note if the source had them set so the caller knows.
    cloned_trigger_stripped = bool(source.get("isTrigger") or source.get("isListener"))
    cloned["isTrigger"] = False
    cloned["isListener"] = False

    # Self-reference fix: outputs[].node_id pointed at the source; update to new_id
    for out in cloned.get("outputs") or []:
        if out.get("node_id") == source_node_id:
            out["node_id"] = new_id

    # Apply settings overrides
    settings_paths_set: list[str] = []
    settings_paths_missing: list[str] = []
    if set_settings:
        for field_path, value in set_settings.items():
            segments = field_path.split("/")
            if _walk_settings_set(cloned.get("settings_field_values") or [], segments, value):
                settings_paths_set.append(field_path)
            else:
                settings_paths_missing.append(field_path)

    # Append and PUT
    wf["blocks"].append(cloned)
    resp = _put_workflow_blocks(workflow_id, wf)

    err = next(
        (b.get("node_config_error") for b in resp.get("blocks", []) if b["id"] == new_id),
        None,
    )

    out_response = {
        "ok": err is None and not resp.get("workflowConfigError"),
        "new_node_id": new_id,
        "source_node_id": source_node_id,
        "new_node_name": cloned["variableName"],
        "isTestMode": cloned.get("isTestMode"),
        "position": cloned["position"],
        "settings_paths_set": settings_paths_set,
        "settings_paths_missing": settings_paths_missing,
        "node_config_error": err,
        "workflowConfigError": resp.get("workflowConfigError"),
        "validation": _maybe_validate(workflow_id, validate_after),
    }
    if cloned_trigger_stripped:
        out_response["trigger_stripped"] = (
            "Source node was a trigger (isTrigger or isListener was True). "
            "The clone was created with both flags set to False — a clone "
            "is structurally never the workflow's trigger. If you intended "
            "a second trigger, use attach_node with is_trigger=True instead."
        )
    return out_response


# ═══════════════════════════════════════════════════════════════════════════
# Discovery (workflows, node definitions, connections) — v0.2.5
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def list_workflows(
    limit: int = 20,
    offset: int = 0,
    search: Optional[str] = None,
) -> dict:
    """List workflows in the current tenant. Paginated; supports search.

    Returns slim view per workflow: id, name, description, version, isLive,
    updatedAt, lastRunAt. Sorted by recency.

    Use search to filter by name (case-insensitive substring match).
    Use limit + offset for pagination.
    """
    raw = api.list_workflows(limit=limit, offset=offset, search=search)
    items = raw.get("data", []) if isinstance(raw, dict) else (raw or [])
    meta = (raw.get("meta") or {}) if isinstance(raw, dict) else {}
    slim = [
        {
            "id": w.get("id"),
            "name": w.get("name"),
            "description": w.get("description"),
            "version": w.get("version"),
            "live_version": w.get("liveVersion"),
            "is_live": (w.get("liveVersion") or 0) > 0,
            "updated_at": w.get("updatedAt"),
            "last_run_at": w.get("lastRunAt"),
            "updated_by": w.get("updatedBy"),
        }
        for w in items
    ]
    return {
        "count": len(slim),
        "total_entries": meta.get("total_entries"),
        "limit": meta.get("limit", limit),
        "offset": meta.get("skip", offset),
        "search": search,
        "workflows": slim,
    }


@mcp.tool()
def list_node_definitions(
    limit: int = 50,
    offset: int = 0,
    search: Optional[str] = None,
    category: Optional[str] = None,
    only_trigger: bool = False,
    only_action: bool = False,
    only_listener: bool = False,
) -> dict:
    """Catalog of every node type the platform supports.

    Use this to discover typeIds before calling `attach_node`. Each result
    carries `node_definition_id` (the typeId you pass to attach_node), `name`,
    `category`, and `description`.

    Use search to filter by name ("Gmail", "Scheduler", "Magic Node").
    Use category to scope to one section (e.g. "Data Manipulation", "Gmail").

    v0.2.22 filters:
      - `only_trigger=True`: nodes that CAN be workflow start nodes (catalog
        `is_trigger=True`). Includes both true listeners (Scheduler, "New X"
        events) AND one-shot start nodes (Get Values in Range, Search People).
        Use this to discover viable root candidates.
      - `only_action=True`: the inverse — action-only nodes that REQUIRE a
        parent. Includes Custom Code, all transformations, all Send/Add/Update
        operations. Attaching these as a root via attach_node will be refused
        (v0.2.22 guard).
      - `only_listener=True`: client-side filter to the TRUE automation
        triggers (catalog `is_listener=True` — Scheduler, all the "New X" /
        "(Instant)" events). Subset of only_trigger.

    Combined search/category/filter args narrow further.
    """
    raw = api.list_node_definitions(
        limit=limit, offset=offset, search=search, category=category,
        only_trigger=only_trigger, only_action=only_action,
    )
    items = raw.get("data", []) if isinstance(raw, dict) else []
    meta = (raw.get("meta") or {}) if isinstance(raw, dict) else {}
    # Client-side filter for only_listener (platform doesn't expose this)
    if only_listener:
        items = [n for n in items if n.get("isListener")]
    slim = [
        {
            "type_id": n.get("node_definition_id"),
            "value": n.get("value"),
            "name": n.get("name"),
            "category": n.get("category"),
            "description": n.get("description"),
            "is_trigger": n.get("is_trigger"),
            "is_listener": n.get("isListener"),
            "starting_price": n.get("startingPrice"),
        }
        for n in items
    ]
    return {
        "count": len(slim),
        "total_entries": meta.get("total_entries"),
        "limit": meta.get("limit", limit),
        "offset": meta.get("skip", offset),
        "search": search,
        "category": category,
        "only_trigger": only_trigger,
        "only_action": only_action,
        "only_listener": only_listener,
        "node_definitions": slim,
    }


@mcp.tool()
def get_node_definition(type_id: str) -> dict:
    """Fetch a single node definition by its typeId.

    The platform has no get-by-id endpoint, so this paginates through the
    catalog (5 pages × 100 = 500 max — the catalog is ~463 entries, so this
    covers it). Returns the slim shape plus a `raw` field with all platform
    fields (icon, vendorIcons, version).
    """
    PAGE = 100
    for offset in range(0, 500, PAGE):
        raw = api.list_node_definitions(limit=PAGE, offset=offset)
        items = (raw.get("data") if isinstance(raw, dict) else []) or []
        match = next((n for n in items if n.get("node_definition_id") == type_id), None)
        if match:
            return {
                "ok": True,
                "type_id": match.get("node_definition_id"),
                "value": match.get("value"),
                "name": match.get("name"),
                "category": match.get("category"),
                "description": match.get("description"),
                "is_trigger": match.get("is_trigger"),
                "is_listener": match.get("isListener"),
                "starting_price": match.get("startingPrice"),
                "icon": match.get("icon"),
                "raw": match,
            }
        if not items or len(items) < PAGE:
            break  # exhausted

    return {
        "ok": False,
        "type_id": type_id,
        "message": "node definition not found in catalog (scanned first 500 entries). "
                   "Try list_node_definitions(search='<name>') to discover the right typeId.",
    }


@mcp.tool()
def list_connections(connection_app_id: Optional[str] = None) -> dict:
    """List OAuth connections (Gmail, Sheets, Slack, Calendar, etc.).

    Two modes:

      - **Unfiltered** (default — `connection_app_id=None`): returns ONLY
        the JWT user's own connections. For solo / single-user tenants
        this is sufficient. For multi-user tenants, this returns 0 if the
        JWT user hasn't personally OAuth'd anything — even when their
        teammates have.

      - **Filtered by app** (`connection_app_id=<id>`): returns ALL
        connections in the tenant for that app, including teammates'. This
        is what the platform's UI uses when populating the connection
        picker for an action node. v0.2.18 added this filter after
        cross-user discovery was found broken via field_options (which
        returns "No valid connection found in settings" — chicken-and-egg).

    Discovery flow for a Pipedream-action node in a multi-user tenant:
      1. `app_id = list_connection_apps(search="Gmail").apps[0].connection_app_id`
      2. `connections = list_connections(connection_app_id=app_id).connections`
      3. Pick one (any owner — auth is handled server-side via the
         connection_id) and use it in your `attach_node` settings.

    Returns: {count, connections: [{connection_id, app_name,
    connection_name, status, provider, created_at, connection_app_id}]}.
    """
    raw = api.list_connections(connection_app_id=connection_app_id)
    items = raw if isinstance(raw, list) else (raw.get("data") if isinstance(raw, dict) else []) or []
    slim = [
        {
            "connection_id": c.get("connectionId"),
            "connection_app_id": c.get("connectionAppId"),
            "app_name": c.get("appName"),
            "connection_name": c.get("connectionName"),
            "provider": c.get("provider"),
            "status": c.get("status"),
            "created_at": c.get("createdAt"),
            "updated_at": c.get("updatedAt"),
        }
        for c in items
    ]
    return {"count": len(slim), "connections": slim, "filtered_by_app_id": connection_app_id}


@mcp.tool()
def list_connection_apps(
    limit: int = 50,
    offset: int = 0,
    category: Optional[str] = None,
    search: Optional[str] = None,
) -> dict:
    """Catalog of apps the platform CAN connect to (vs `list_connections`
    which shows what the user HAS connected).

    Use search to find an app by name ("HubSpot", "Notion", etc.).
    Use category to scope (e.g. "CRM", "Productivity").
    """
    raw = api.list_connection_apps(limit=limit, offset=offset,
                                    category=category, search=search)
    items = raw.get("data", []) if isinstance(raw, dict) else []
    meta = (raw.get("meta") or {}) if isinstance(raw, dict) else {}
    slim = [
        {
            "connection_app_id": a.get("connectionAppId"),
            "app_id": a.get("appId"),
            "name": a.get("name"),
            "category": a.get("category"),
            "description": a.get("description"),
            "icon_url": a.get("iconUrl"),
            "provider": a.get("provider"),
            "is_active": a.get("isActive"),
        }
        for a in items
    ]
    return {
        "count": len(slim),
        "total_entries": meta.get("total_entries"),
        "limit": meta.get("limit", limit),
        "offset": meta.get("skip", offset),
        "search": search,
        "category": category,
        "apps": slim,
    }


@mcp.tool()
def list_field_options(
    workflow_id: str,
    node_id: str,
    field_name: str,
    search: Optional[str] = None,
) -> dict:
    """Fetch the dropdown options for a single field on a node.

    This is what the platform's UI calls when populating dropdowns. For
    cascading fields (e.g. worksheetId depends on sheetId), the prerequisite
    settings on the node must be set first; the platform reads them to scope
    the options.

    CROSS-TENANT (v0.2.19): WORKS for connections owned by ANY teammate in
    the tenant, IF you pass the action-specific `field_name`. The right
    field name varies per Pipedream action (e.g. for Slack New Message,
    the channel field is `pipedream-slack_v2-slack_v2_new_message_in_channels-conversations`,
    NOT `channel` or `channelId` as you might guess). The reliable way to
    get the right field name is **`get_node_dynamic_fields(workflow_id,
    node_id)`** (v0.2.19) — it returns the full action schema. Try that
    first if you're unsure what fields exist.

    Use cases:
      - Discover sheets / worksheets / channels / folders available for an
        app-backed node before configuring it
      - Resolve a value back to its human-readable label (e.g. UUID → name)
      - Search a large option list (passes `search` through to the API)

    Returns: {field_name, count, options: [{label, value}], errors}
    """
    wf = api.get_workflow(workflow_id)
    target = next((b for b in wf["blocks"] if b["id"] == node_id), None)
    if target is None:
        raise ValueError(f"node {node_id} not in workflow {workflow_id}")

    settings_array = _settings_as_value_array(target.get("settings_field_values") or [])
    resp = api.field_options(
        node_id=node_id,
        node_definition_id=target.get("typeId", ""),
        field_name=field_name,
        settings=settings_array,
        search=search,
    )
    opts = resp.get("options") or []
    return {
        "field_name": field_name,
        "count": len(opts),
        "options": [{"label": o.get("label"), "value": o.get("value")} for o in opts],
        "errors": resp.get("errors") or [],
    }


@mcp.tool()
def get_node_dynamic_fields(
    workflow_id: str,
    node_id: str,
    field_name_changed: Optional[str] = None,
) -> dict:
    """Get the FULL field schema for a Pipedream-action node, including
    dynamic fields that materialize after a connection is bound.

    This is the v0.2.19 breakthrough that unlocks the customer-tenant
    use case. Wraps `POST /nodes/updated-config-and-status` — the same
    endpoint the platform's UI calls when a Pipedream node's settings
    change to recompute the form schema.

    Why you need this: Pipedream actions have field names that vary per
    action and are NOT discoverable from the catalog alone. For example:
      - Gmail Send Email connection field: pipedream-gmail-gmail_send_email-gmail
      - Slack Send Message connection field: pipedream-slack_v2-slack_v2_send_message-slack_v2
      - Slack New Message channel field: pipedream-slack_v2-slack_v2_new_message_in_channels-conversations
      - (All three differ in the trailing segment — there's no formula.)

    Also: dynamic dropdowns (Slack channels, Calendar IDs, Sheets
    worksheets, etc.) are listed in the response as fields with
    `inputTypes[].dataSource.endpoint = "/nodes/field-options"`. Use
    `list_field_options(field_name=<that field's name>)` to fetch the
    actual values.

    CRITICAL — works cross-tenant. Unlike `/nodes/reload-props` (which
    returns "No valid connection found in settings" for connections
    owned by other tenant users), this endpoint accepts cross-tenant
    bindings and returns the full schema. Confirmed live with sayanta's
    Slack connection from common.dev's JWT.

    Recommended discovery flow for a Pipedream node:
      1. attach_node with placeholder settings = {connection_field: connection_id}.
         Guess the connection field name (try common patterns like
         `pipedream-<app>-<action>-<app>`); if attach fails with
         "Connection not found", try `pipedream-<app>-<action>-<app>_connection_id`.
      2. get_node_dynamic_fields(workflow_id, node_id) → returns nodeDefinition.fields.
      3. From the response, find the connection field's actual name (it
         has `type: "app_connection"`) and any dependent dropdowns.
      4. For each dropdown field with a dataSource pointing to
         /nodes/field-options, call list_field_options(field_name=<that name>).
      5. Update settings via update_node_setting using the correct field names.

    `field_name_changed` defaults to the connection field if not specified.

    Returns:
      {
        node_id, node_definition_id,
        fields: [{name, type, label, required, placeholder, conditionalVisibility, dataSource, inputTypes}, ...],
        dropdown_fields: [list of fields with type=multi_select or app_connection or with dataSource],
        available_options, setting_field_values
      }
    """
    wf = api.get_workflow(workflow_id)
    target = next((b for b in wf["blocks"] if b["id"] == node_id), None)
    if target is None:
        raise ValueError(f"node {node_id} not in workflow {workflow_id}")

    sfv = target.get("settings_field_values") or []
    # Default field_name_changed: the first app_connection-shaped field name
    # we can guess from the current settings (looking for connection-id-like
    # field names). If nothing fits, use the first field name available.
    if not field_name_changed:
        for s in sfv:
            fn = s.get("field_name") or ""
            if "connection" in fn.lower() or fn.endswith("-gmail") or fn.endswith("-slack_v2"):
                field_name_changed = fn
                break
        if not field_name_changed and sfv:
            field_name_changed = sfv[0].get("field_name")
        if not field_name_changed:
            raise ValueError(
                "Cannot infer field_name_changed — node has no settings. "
                "Attach the node with at least a placeholder connection_id "
                "setting first, then call this tool."
            )

    resp = api.updated_node_config(
        node_id=node_id,
        node_definition_id=target.get("typeId", ""),
        field_name_changed=field_name_changed,
        setting_field_values=sfv,
        settings_schema=[],
    )

    fields = (resp.get("nodeDefinition") or {}).get("fields") or []
    dropdowns = [
        f for f in fields
        if f.get("type") in ("app_connection", "multi_select", "select")
        or (f.get("dataSource") and f["dataSource"].get("endpoint"))
        or any(
            (it.get("dataSource") or {}).get("endpoint")
            for it in (f.get("inputTypes") or [])
        )
    ]
    return {
        "node_id": resp.get("nodeId"),
        "node_definition_id": target.get("typeId"),
        "field_count": len(fields),
        "fields": [
            {
                "name": f.get("name"),
                "type": f.get("type"),
                "label": f.get("label"),
                "required": f.get("required"),
                "placeholder": f.get("placeholder"),
                "default_value": f.get("defaultValue"),
                "conditional_visibility": f.get("conditionalVisibility"),
                "data_source": f.get("dataSource"),
                "input_types": f.get("inputTypes"),
            }
            for f in fields
        ],
        "dropdown_field_names": [f.get("name") for f in dropdowns],
        "available_options": resp.get("availableOptions"),
        "setting_field_values": resp.get("settingFieldValues"),
        "note": (
            "STATIC fields only (5 for Add Single Row). For Pipedream DYNAMIC "
            "fields (col_NNNN per sheet column, dynamic_props_id, array fields "
            "like updation_criteria), call `reload_pipedream_props` — that's "
            "the endpoint the platform's UI calls to materialize the dynamic "
            "schema. v0.2.21 ships reload_pipedream_props for that purpose."
        ),
    }


# ─── v0.2.21: Pipedream dynamic-props (col_NNNN + dynamic_props_id) ─────────


@mcp.tool()
def reload_pipedream_props(
    workflow_id: str,
    node_id: str,
    field_name_changed: Optional[str] = None,
) -> dict:
    """Get the FULL DYNAMIC field schema for a Pipedream action — including
    `col_NNNN` per sheet column (with `label` = the sheet header), the
    auto-issued `dynamic_props_id` token, and array-typed fields like
    `updation_criteria` / `fields_to_update` for Update Row.

    v0.2.21 — wraps `POST /nodes/reload-props`. **This is what unlocks
    Sheets writes and updates end-to-end** (without it, the platform
    stores values but the action runtime ignores them).

    DIFFERENT FROM `get_node_dynamic_fields`:
      - `get_node_dynamic_fields` calls `/nodes/updated-config-and-status`
        and returns only the STATIC fields (e.g. 5 for Add Single Row:
        connection / drive / sheetId / worksheetId / hasHeaders).
      - `reload_pipedream_props` calls `/nodes/reload-props` and returns
        the FULL dynamic schema (e.g. 13 fields for Add Single Row,
        adding `col_0000`..`col_NNNN` plus `dynamic_props_id`).
      - Use BOTH: `get_node_dynamic_fields` for cross-tenant connection
        validation (works on any Pipedream node); `reload_pipedream_props`
        for unlocking col_NNNN + dynamic_props_id for SheetsWrite-style
        nodes.

    Returns:
        {
          "node_id": str,
          "component_id": "google_sheets-add-single-row" | ...,
          "dynamic_props_id": "dyp_VwUD0ppk",          ← AUTO-ISSUED token
          "fields": [{name, type, label, required, ...}],
          "col_to_label": {"col_0000": "timestamp", ...},  ← mapping for auto-wiring
          "array_fields": [name, ...],  ← updation_criteria etc.
          "has_dynamic_props": bool,  ← False if action has no dyn props
          "errors": list[str],
        }

    GOTCHAS:
      - NOT idempotent: every call issues a fresh `dynamic_props_id`.
        Call ONCE per real settings change (connection / sheet / worksheet
        / hasHeaders). Cache the token. Subsequent unrelated edits should
        NOT re-call this.
      - For Pipedream nodes without dynamic props (e.g. Get Values in
        Range — its schema is static), returns
        `errors: ["additionalProps not a function"]` and 0 fields. The
        `has_dynamic_props` flag is False in that case.
      - The node must already exist in the workflow (the platform
        validates node-existence). Attach the block first, then call.

    Typical use (Add Single Row downstream of a GVR):
      1. attach_node(Add Single Row, settings={5 static fields})
      2. reload_pipedream_props → get dynamic_props_id + col_to_label
      3. update_node_setting to persist dynamic_props_id + col_NNNN
         templates (use auto_map_pipedream_columns helper)
      4. add_edge(GVR -> Add Single Row)
      5. save_and_execute
    """
    wf = api.get_workflow(workflow_id)
    target = next((b for b in wf["blocks"] if b["id"] == node_id), None)
    if target is None:
        raise ValueError(f"node {node_id} not in workflow {workflow_id}")

    sfv = target.get("settings_field_values") or []
    # Default field_name_changed: anything sensible. The endpoint uses it
    # as the trigger field but otherwise doesn't validate.
    if not field_name_changed:
        if sfv:
            field_name_changed = sfv[0].get("field_name") or ""
        else:
            raise ValueError(
                "Cannot infer field_name_changed — node has no settings. "
                "Attach the node with at least placeholder settings first."
            )

    # Flatten settings to the simple {field_name, field_value} pairs the
    # reload-props endpoint wants
    settings_pairs = _settings_as_value_array(sfv)

    resp = api.reload_pipedream_props(
        node_id=node_id,
        node_definition_id=target.get("typeId", ""),
        field_name_changed=field_name_changed,
        settings=settings_pairs,
    )

    fields = resp.get("fields") or []
    errors = resp.get("errors") or []
    # Detect "no dynamic props" — endpoint returns errors with this string
    has_dynamic_props = not any(
        "additionalProps not a function" in str(e) for e in errors
    )

    # Extract dynamic_props_id (auto-issued in defaultValue)
    dyp = None
    for f in fields:
        name = f.get("name") or ""
        if name.endswith("dynamic_props_id"):
            dyp = f.get("defaultValue")
            break

    # Build col_NNNN → label mapping (for auto-mapping upstream columns)
    col_to_label: dict[str, str] = {}
    for f in fields:
        name = f.get("name") or ""
        # Match the trailing col_NNNN segment (col_0000 is 8 chars: c,o,l,_,0,0,0,0)
        seg = name.rsplit("-", 1)[-1] if "-" in name else name
        if seg.startswith("col_") and len(seg) == 8 and seg[4:].isdigit():
            label = f.get("label")
            if label:
                col_to_label[seg] = label

    # Identify array-typed fields (for Update Row / Upsert)
    array_fields = [f.get("name") for f in fields if f.get("type") == "array"]

    return {
        "node_id": node_id,
        "component_id": resp.get("componentId"),
        "dynamic_props_id": dyp,
        "has_dynamic_props": has_dynamic_props,
        "fields": [
            {
                "name": f.get("name"),
                "type": f.get("type"),
                "label": f.get("label"),
                "required": f.get("required"),
                "placeholder": f.get("placeholder"),
                "default_value": f.get("defaultValue"),
                "conditional_visibility": f.get("conditionalVisibility"),
                "array_item_schema": f.get("array_item_schema"),
            }
            for f in fields
        ],
        "col_to_label": col_to_label,
        "array_fields": array_fields,
        "errors": errors,
        "note": (
            "Use the `dynamic_props_id` value to set the node's "
            "dynamic_props_id field. Use `col_to_label` to auto-map "
            "upstream columns to col_NNNN templates (see "
            "auto_map_pipedream_columns helper)."
        ),
    }


@mcp.tool()
def auto_map_pipedream_columns(
    workflow_id: str,
    node_id: str,
    template_format: str = "{{{{{label}}}}}",
) -> dict:
    """For an Add Single Row node downstream of a Sheets read, auto-fill
    the `col_NNNN` fields with `{{<upstream_header_name>}}` templates.

    v0.2.21 — uses `reload_pipedream_props` to discover col_NNNN.label
    (which IS the destination sheet's header name), then wraps each in a
    Jinja-like template that references the upstream column of the same
    name. At runtime, Pipedream substitutes the value from each upstream
    row per template.

    Also persists the auto-issued `dynamic_props_id` to the node's
    settings, so the node is ready to execute.

    Args:
      workflow_id, node_id: target Add Single Row block (already attached)
      template_format:      f-string with `{label}` placeholder. Default
                            `"{{{{{label}}}}}"` produces `{{timestamp}}`,
                            `{{who}}`, etc. Override if your upstream
                            emits prefixed columns (e.g. `"{{{{row.{label}}}}}"`
                            → `{{row.timestamp}}`).

    Returns: {col_to_label, dynamic_props_id, applied: list[{col, template, ok}]}.

    Errors if the node has no dynamic props (e.g. it's Get Values in Range,
    not Add Single Row).
    """
    schema = reload_pipedream_props(workflow_id, node_id)
    if not schema.get("has_dynamic_props"):
        return {
            "ok": False,
            "message": (
                f"Node {node_id} has no dynamic Pipedream props "
                f"(component={schema.get('component_id')!r}). "
                f"Auto-mapping only works for Add Single Row / similar nodes "
                f"that expose col_NNNN fields per sheet column. Errors: "
                f"{schema.get('errors')}"
            ),
        }
    col_to_label = schema.get("col_to_label") or {}
    dyp = schema.get("dynamic_props_id")
    if not col_to_label:
        return {
            "ok": False,
            "message": (
                "reload-props returned no col_NNNN fields. Likely the static "
                "settings (connection / sheet / worksheet / hasHeaders) aren't "
                "fully bound yet — set them first via update_node_setting."
            ),
            "errors": schema.get("errors"),
        }

    # Get the node's component-id prefix for building col_NNNN field names
    component_prefix = None
    for f in schema.get("fields") or []:
        nm = f.get("name") or ""
        if "-col_" in nm:
            component_prefix = nm.rsplit("-col_", 1)[0] + "-"
            break
    if not component_prefix:
        return {"ok": False, "message": "Could not derive component prefix from col_NNNN field names."}

    applied = []
    # Persist dynamic_props_id first
    if dyp:
        dyp_path = f"{component_prefix}dynamic_props_id"
        res = update_node_setting(workflow_id, node_id, dyp_path, dyp,
                                  validate_after=False, add_if_missing=True)
        applied.append({"path": dyp_path, "value": dyp, "ok": res.get("ok", False)})

    # Persist each col_NNNN template
    for col, label in sorted(col_to_label.items()):
        template = template_format.format(label=label)
        path = f"{component_prefix}{col}"
        res = update_node_setting(workflow_id, node_id, path, template,
                                  validate_after=False, add_if_missing=True)
        applied.append({"path": path, "value": template, "ok": res.get("ok", False)})

    return {
        "ok": all(a["ok"] for a in applied),
        "col_to_label": col_to_label,
        "dynamic_props_id": dyp,
        "applied": applied,
        "note": (
            "Templates reference upstream columns by header name. Each "
            "upstream row will be one Pipedream invocation, writing one "
            "row to the destination sheet."
        ),
    }


@mcp.tool()
def configure_update_row(
    workflow_id: str,
    node_id: str,
    criteria: list[dict],
    updates: list[dict],
    add_if_not_present: bool = False,
) -> dict:
    """Configure a Pipedream Update Row block end-to-end.

    v0.2.21 — handles the platform's array-field envelope correctly:
    `updation_criteria` and `fields_to_update` are stored as LISTS OF
    LISTS of sub-field envelopes, with column values referencing
    `col_NNNN` slugs (NOT the human-readable header names).

    Pythonic interface: caller passes `criteria` and `updates` as lists
    of `{"header": "<sheet header name>", "value": "<literal or {{template}}>"}`
    dicts. This helper:
      1. Calls reload_pipedream_props to discover col_NNNN.label → slug mapping
       AND the fresh `dynamic_props_id`
      2. Resolves each `header` to the corresponding `col_NNNN` slug
      3. Builds the correct list-of-lists envelope shape for both array fields
      4. PUTs the full settings (5 static + dyp_ + criteria + updates + add_if_not_present)

    Args:
      workflow_id, node_id: target Update Row block (already attached, 5 static fields bound)
      criteria:    list of {"header": "<name>", "value": "<literal or {{template}}>"}.
                   Multiple criteria are AND'd by the platform.
                   Example: [{"header": "who", "value": "ana@example.com"}]
      updates:     list of {"header": "<name>", "value": "<new value>"} for fields to set
                   Example: [{"header": "status", "value": "replied"}]
      add_if_not_present: if True, Update Row acts as upsert — appends a new row when
                          criteria matches nothing. Default False (skip when no match).

    Returns: {ok, dynamic_props_id, criteria_resolved, updates_resolved, message}.
    """
    schema = reload_pipedream_props(workflow_id, node_id)
    if not schema.get("has_dynamic_props"):
        return {"ok": False, "message": f"Node has no dynamic props (component={schema.get('component_id')})."}
    col_to_label = schema.get("col_to_label") or {}
    if not col_to_label:
        return {"ok": False, "message": "reload-props returned no col_NNNN fields; bind static settings first."}
    # Build header → col_NNNN reverse map
    label_to_col = {label: col for col, label in col_to_label.items()}
    dyp = schema.get("dynamic_props_id")

    # Component prefix (e.g. pipedream-google_sheets-google_sheets_update_row-)
    component_prefix = None
    for f in schema.get("fields") or []:
        nm = f.get("name") or ""
        if nm.endswith("dynamic_props_id"):
            component_prefix = nm[:-len("dynamic_props_id")]
            break
    if not component_prefix:
        return {"ok": False, "message": "Could not derive component prefix from schema."}

    # Resolve header → col_NNNN
    def _resolve(items, *, col_key, val_key):
        out = []
        resolved_log = []
        for item in items:
            header = item.get("header")
            value = item.get("value")
            col = label_to_col.get(header)
            if not col:
                return None, f"Header {header!r} not found in sheet (available: {sorted(label_to_col.keys())})"
            resolved_log.append({"header": header, "col": col, "value": value})
            out.append([
                {"field_name": f"{component_prefix}{col_key}",
                 "field_value": col, "fieldLabel": header, "error": None},
                {"field_name": f"{component_prefix}{val_key}",
                 "field_value": value, "error": None},
            ])
        return out, resolved_log

    criteria_envelope, criteria_log = _resolve(criteria, col_key="column_to_match", val_key="value_to_match")
    if criteria_envelope is None:
        return {"ok": False, "message": f"criteria error: {criteria_log}"}
    updates_envelope, updates_log = _resolve(updates, col_key="column_to_update", val_key="value_to_update")
    if updates_envelope is None:
        return {"ok": False, "message": f"updates error: {updates_log}"}

    # PUT each setting via update_node_setting
    applied = []
    # dynamic_props_id
    if dyp:
        r = update_node_setting(workflow_id, node_id, f"{component_prefix}dynamic_props_id",
                                 dyp, validate_after=False, add_if_missing=True)
        applied.append({"path": "dynamic_props_id", "ok": r.get("ok", False)})
    # updation_criteria
    r = update_node_setting(workflow_id, node_id, f"{component_prefix}updation_criteria",
                             criteria_envelope, validate_after=False, add_if_missing=True)
    applied.append({"path": "updation_criteria", "ok": r.get("ok", False)})
    # fields_to_update
    r = update_node_setting(workflow_id, node_id, f"{component_prefix}fields_to_update",
                             updates_envelope, validate_after=False, add_if_missing=True)
    applied.append({"path": "fields_to_update", "ok": r.get("ok", False)})
    # add_if_not_present
    r = update_node_setting(workflow_id, node_id, f"{component_prefix}add_if_not_present",
                             bool(add_if_not_present), validate_after=True, add_if_missing=True)
    applied.append({"path": "add_if_not_present", "ok": r.get("ok", False)})

    return {
        "ok": all(a["ok"] for a in applied),
        "dynamic_props_id": dyp,
        "criteria_resolved": criteria_log,
        "updates_resolved": updates_log,
        "applied": applied,
        "note": (
            "Update Row is now configured. Execute via save_and_execute. "
            "Per upstream row, Pipedream substitutes {{templates}} and runs "
            "the action once. Each invocation returns "
            "payload.updated_rows_indices listing the sheet rows mutated."
        ),
    }


def _find_in_flight_execution(workflow_id: str) -> Optional[dict]:
    """v0.2.23 helper — return the most recent in-flight execution for a workflow,
    or None if no execution is currently running.

    "In flight" = status in {'running', 'pending', 'queued'} (anything not terminal).
    Used by save_and_execute to detect the platform's silent execution-id-reuse
    behavior before it bites.

    Best-effort: any API failure returns None (we don't want to block save_and_execute
    on a list_executions hiccup).
    """
    try:
        raw = api.list_executions(workflow_id, limit=5)
    except Exception:
        return None
    items = raw.get("data") if isinstance(raw, dict) else (raw or [])
    if not isinstance(items, list):
        return None
    NON_TERMINAL = {"running", "pending", "queued", "in_progress"}
    for ex in items:
        status = (ex.get("status") or "").lower()
        if status in NON_TERMINAL:
            return ex
    return None


@mcp.tool()
def save_and_execute(
    workflow_id: str,
    target_node_id: str,
    if_in_flight: str = "refuse",
) -> dict:
    """Atomic save-then-execute. Persists the current workflow state AND
    kicks off execution of `target_node_id` in one server-side call.

    v0.2.21 — wraps `POST /workflows/{wf}/nodes/{n}/update-workflow-and-execute`,
    the same endpoint the platform's UI "Run Workflow" button uses.

    Why prefer this over `partial_execute`:
      - `partial_execute` only calls the execute endpoint. If you made
        unsaved changes to the workflow first, the execution sees a
        stale snapshot — silent skips, wrong outputs.
      - `save_and_execute` PUTs the latest workflow state THEN runs,
        all in one transaction. Avoids the class of stale-state bugs.

    Use this after `auto_map_pipedream_columns` or `configure_update_row`
    when you want to immediately run the configured block.

    v0.2.23 IN-FLIGHT GUARD: if an execution for this workflow is currently
    running, calling this endpoint AGAIN returns the SAME execution_id (the
    platform reuses the in-flight execution slot). Callers that don't notice
    this end up polling the stale prior run and seeing wrong results. This
    tool now detects the in-flight case via list_executions BEFORE calling
    update-workflow-and-execute, and behaves per `if_in_flight`:
      - `"refuse"` (default) — return ok:False with `in_flight_execution_id`
        so caller knows to wait
      - `"return_existing"` — return the existing in-flight execution_id
        with `was_in_flight: True` so caller can poll the existing run
      - `"wait_and_retry"` — poll until the in-flight run completes (up to
        300s) then start the new one. Use sparingly — blocks for a long time.

    Returns: {ok, execution_id, status, was_in_flight, in_flight_execution_id,
    response, note}.
    """
    if if_in_flight not in ("refuse", "return_existing", "wait_and_retry"):
        raise ValueError(
            f"if_in_flight must be one of: refuse, return_existing, "
            f"wait_and_retry. Got: {if_in_flight!r}"
        )
    wf = api.get_workflow(workflow_id)
    target = next((b for b in wf["blocks"] if b["id"] == target_node_id), None)
    if target is None:
        raise ValueError(f"target_node_id {target_node_id} not in workflow {workflow_id}")

    # v0.2.23 Fix #2 — check for in-flight execution before firing.
    # The platform's update-workflow-and-execute endpoint silently returns
    # the existing execution_id when one is already running for this
    # workflow, leading to stale-snapshot polling. Detect and surface.
    in_flight_exec = _find_in_flight_execution(workflow_id)
    if in_flight_exec:
        if if_in_flight == "refuse":
            return {
                "ok": False,
                "stage": "in_flight_guard",
                "in_flight_execution_id": in_flight_exec.get("id"),
                "in_flight_status": in_flight_exec.get("status"),
                "message": (
                    f"Workflow already has an execution in flight "
                    f"(id={in_flight_exec.get('id')!r}, "
                    f"status={in_flight_exec.get('status')!r}). The platform's "
                    f"update-workflow-and-execute endpoint would silently reuse "
                    f"that execution slot, causing stale-snapshot bugs. Wait "
                    f"for it to complete (poll via tail_execution) OR pass "
                    f"if_in_flight='return_existing' to receive the in-flight "
                    f"id explicitly OR if_in_flight='wait_and_retry' to block "
                    f"until it finishes then start a new run."
                ),
            }
        if if_in_flight == "return_existing":
            return {
                "ok": True,
                "execution_id": in_flight_exec.get("id"),
                "status": in_flight_exec.get("status"),
                "was_in_flight": True,
                "in_flight_execution_id": in_flight_exec.get("id"),
                "note": (
                    "Returned the EXISTING in-flight execution_id (per "
                    "if_in_flight='return_existing'). No new execution was "
                    "started. Settings changes you made since the in-flight "
                    "execution started are NOT reflected in this run."
                ),
            }
        # wait_and_retry: poll until done, then fire fresh
        import time
        deadline = time.time() + 300
        while time.time() < deadline:
            time.sleep(5)
            in_flight_exec = _find_in_flight_execution(workflow_id)
            if not in_flight_exec:
                break
        if in_flight_exec:
            return {
                "ok": False,
                "stage": "in_flight_guard",
                "in_flight_execution_id": in_flight_exec.get("id"),
                "message": (
                    f"Waited 300s for in-flight execution "
                    f"{in_flight_exec.get('id')!r} to complete; it's still "
                    f"running. Caller should poll or abort manually."
                ),
            }

    try:
        resp = api.update_workflow_and_execute(workflow_id, target_node_id, wf)
    except Exception as e:
        return {"ok": False, "stage": "execute", "message": str(e)}

    execution = (resp.get("execution") or {}).get("response") or {}
    return {
        "ok": True,
        "execution_id": execution.get("id"),
        "status": execution.get("status"),
        "started_at": execution.get("startedAt"),
        "was_in_flight": False,  # v0.2.23 — caller can rely on this to know it's a fresh run
        "note": (
            "Poll with tail_execution(execution_id, wait_until='block_completed', "
            f"target_block_id='{target_node_id}'). For Pipedream nodes, also "
            "check the row output via get_node_output to confirm payload."
        ),
    }


def _settings_as_value_array(settings_field_values: list[dict]) -> list[dict]:
    """Flatten settings_field_values (which carry the full envelope) into the
    simple `[{field_name, field_value}, ...]` shape the field-options endpoint
    expects in its `settings` body parameter.

    Recurses into group entries (where field_value is itself a list of dicts).
    Group entries themselves are omitted; only leaf fields are emitted.
    """
    out: list[dict] = []
    for entry in (settings_field_values or []):
        name = entry.get("field_name")
        if not name:
            continue
        val = entry.get("field_value")
        is_group = isinstance(val, list) and val and all(
            isinstance(x, dict) and "field_name" in x for x in val
        )
        if is_group:
            out.extend(_settings_as_value_array(val))
        else:
            out.append({"field_name": name, "field_value": val})
    return out


# Suffixes that indicate a dropdown-like field whose value is an ID/UUID and
# whose label would be helpful to resolve. Used by attach_node when
# auto_resolve_labels is True.
_AUTO_RESOLVE_LABEL_SUFFIXES = (
    "connection_id",
    "connectionid",
    "connectionId",
    "sheetId",
    "worksheetId",
    "folderId",
    "channelId",
    "userId",
    "calendarId",
    "boardId",
    "spaceId",
    "tableId",
    "baseId",
    "groupId",
    "teamId",
    "projectId",
    "pipelineId",
    "campaignId",
)


def _looks_like_dropdown_field(field_name: str) -> bool:
    """Heuristic: does this field look like an ID-valued dropdown whose label
    would be a human-readable name? Match on the trailing segment of the
    dot/dash-separated field name."""
    if not field_name:
        return False
    last = field_name.rsplit("-", 1)[-1]
    return any(last.endswith(suf) for suf in _AUTO_RESOLVE_LABEL_SUFFIXES)


def _is_connection_id_field(field_name: str) -> bool:
    """connection_id is a special case: field-options can't resolve it
    (the endpoint needs a connection to fetch options FOR a connection field,
    which is circular). Resolve via list_connections() instead."""
    if not field_name:
        return False
    last = field_name.rsplit("-", 1)[-1].lower()
    return last.endswith("connection_id") or last.endswith("connectionid")


def _extract_pipedream_app_slug(field_name: str) -> Optional[str]:
    """For a Pipedream connection field like
    `pipedream-google_sheets-google_sheets_add_single_row-googleSheets_connection_id`,
    return the app slug (`google_sheets`). Used by Fix D to look up the
    connection_app_id when the connection is owned by a teammate.
    """
    if not field_name or not field_name.startswith("pipedream-"):
        return None
    parts = field_name.split("-")
    if len(parts) >= 2:
        return parts[1]
    return None


@functools.lru_cache(maxsize=64)
def _connection_app_id_for_slug(slug: str) -> Optional[str]:
    """Look up the connection_app_id (UUID) for a Pipedream app slug
    (e.g. `google_sheets`, `slack_v2`, `gmail`).

    Cached for the server-process lifetime — the app catalog is static.
    Paginates the catalog so we don't miss apps past offset 50.
    """
    if not slug:
        return None
    slug_low = slug.lower()
    # Targeted search first (cheap) — match by app name approximation
    search_term = slug.replace("_", " ")
    try:
        raw = api.list_connection_apps(limit=200, search=search_term) or {}
    except Exception:
        raw = {}
    apps = raw.get("data") if isinstance(raw, dict) else (raw or [])
    if not isinstance(apps, list):
        apps = []
    for a in apps:
        name_slug = (a.get("name") or "").lower().replace(" ", "_")
        if name_slug == slug_low or (a.get("slug") or "").lower() == slug_low:
            return a.get("connectionAppId") or a.get("id")
    # Fallback: full pagination
    try:
        for offset in range(0, 1000, 100):
            raw = api.list_connection_apps(limit=100, offset=offset) or {}
            apps = raw.get("data") if isinstance(raw, dict) else (raw or [])
            if not isinstance(apps, list) or not apps:
                break
            for a in apps:
                name_slug = (a.get("name") or "").lower().replace(" ", "_")
                if name_slug == slug_low or (a.get("slug") or "").lower() == slug_low:
                    return a.get("connectionAppId") or a.get("id")
            if len(apps) < 100:
                break
    except Exception:
        pass
    return None


def _resolve_connection_label(target_value, field_name: Optional[str] = None) -> Optional[str]:
    """Look up a connection's friendly name from its connection_id.

    v0.2.20 Fix D: in multi-user tenants, the JWT user's own connection list
    won't contain teammates' connections. If the first (unfiltered) lookup
    misses AND `field_name` is a Pipedream connection field, derive the app
    slug from the field name and call list_connections(connection_app_id=<id>)
    to fetch ALL tenant connections for that app — that DOES include other
    users' connections.
    """
    if not target_value:
        return None
    target_str = str(target_value)
    try:
        raw = api.list_connections() or []
    except Exception:
        raw = []
    conns = raw if isinstance(raw, list) else (raw.get("data") if isinstance(raw, dict) else [])
    for c in (conns or []):
        if c.get("connectionId") == target_str:
            return c.get("connectionName") or c.get("appName")

    # v0.2.20 Fix D fallback: try the cross-tenant lookup
    slug = _extract_pipedream_app_slug(field_name) if field_name else None
    if not slug:
        return None
    app_id = _connection_app_id_for_slug(slug)
    if not app_id:
        return None
    try:
        raw2 = api.list_connections(connection_app_id=app_id) or []
    except Exception:
        return None
    conns2 = raw2 if isinstance(raw2, list) else (raw2.get("data") if isinstance(raw2, dict) else [])
    for c in (conns2 or []):
        if c.get("connectionId") == target_str:
            return c.get("connectionName") or c.get("appName")
    return None


def _resolve_field_label(
    *,
    node_id: str,
    type_id: str,
    field_name: str,
    target_value,
    settings_array: list[dict],
) -> Optional[str]:
    """Look up the human-readable label for a field value.

    Routing:
      - connection_id fields → resolve via list_connections() (the
        field-options endpoint can't help — circular dependency)
      - everything else → resolve via the field-options endpoint

    Returns the matching label, or None if no match / if the lookup errors.
    Failures are silent so a partial-resolution doesn't block attach_node.
    """
    if target_value is None or target_value == "":
        return None

    if _is_connection_id_field(field_name):
        # v0.2.20 Fix D: pass field_name so cross-tenant fallback can derive
        # the Pipedream app slug and look up connections owned by teammates.
        return _resolve_connection_label(target_value, field_name=field_name)

    try:
        resp = api.field_options(
            node_id=node_id,
            node_definition_id=type_id,
            field_name=field_name,
            settings=settings_array,
        )
    except Exception:
        return None
    target_str = str(target_value)
    for opt in (resp.get("options") or []):
        opt_val = opt.get("value")
        # Match on stringified value. The endpoint sometimes returns floats
        # for numeric IDs (e.g. worksheetId=410711210.0); the block stores
        # strings ("410711210"). Compare flexibly.
        if str(opt_val) == target_str:
            return opt.get("label")
        if isinstance(opt_val, float) and opt_val.is_integer() and str(int(opt_val)) == target_str:
            return opt.get("label")
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Generic build (any node type) + workflow duplication — v0.2.5
# ═══════════════════════════════════════════════════════════════════════════

# Cache node-def flags for the duration of the server process. The catalog is
# static within a server lifetime, so each typeId is looked up at most once.
@functools.lru_cache(maxsize=256)
def _lookup_node_def_flags(type_id: str) -> tuple[Optional[bool], Optional[bool]]:
    """Look up (is_trigger, is_listener) for a given typeId from the node-def
    catalog. Returns (None, None) if the typeId can't be found or the lookup
    errors — the caller treats that as "fall back to safe defaults".

    The platform exposes both flags per definition (`is_trigger` snake_case,
    `isListener` camelCase — yes, mixed). Both must be True on the block for
    a scheduler-like node to actually drive the live toggle in the UI: the
    trigger flag alone is necessary but not sufficient.
    """
    PAGE = 100
    try:
        for offset in range(0, 600, PAGE):
            raw = api.list_node_definitions(limit=PAGE, offset=offset)
            items = (raw.get("data") if isinstance(raw, dict) else []) or []
            match = next((n for n in items if n.get("node_definition_id") == type_id), None)
            if match:
                return (
                    bool(match.get("is_trigger")),
                    bool(match.get("isListener")),
                )
            if not items or len(items) < PAGE:
                break
    except Exception:
        pass
    return (None, None)


@mcp.tool()
def attach_node(
    workflow_id: str,
    parent_node_ids: list[str],
    type_id: str,
    name: str,
    settings: dict,
    description: str = "",
    position_x: Optional[float] = None,
    position_y: Optional[float] = None,
    output_columns: Optional[list[str]] = None,
    output_dtypes: Optional[list[str]] = None,
    is_trigger: Optional[bool] = None,
    is_listener: Optional[bool] = None,
    credit_cost_per_item: int = 0,
    field_labels: Optional[dict] = None,
    auto_resolve_labels: bool = True,
    allow_multi_input: bool = False,
    validate_after: bool = True,
    force_root: bool = False,
    force_demote_listener: bool = False,
) -> dict:
    """Generic block-attach for ANY node type (Scheduler, Gmail, Calendar, AI, etc.).

    Use `list_node_definitions(search="...")` first to find the typeId for the
    node you want, plus understand its expected settings shape. Then construct
    the `settings` dict (mapping field_name → value) and pass it here.

    For app-backed nodes (Gmail send, Sheets read/write, Calendar list, etc.),
    use `list_connections()` to find the connection_id and include it in the
    settings as the platform expects (typically a field like
    `pipedream-<app>-<action>-connectionId`).

    `parent_node_ids`: ZERO entries for a workflow root (start node), or
    ONE entry for everything else. This tool refuses 2+ parents by default
    because almost every node type (Custom Code, HubSpot, Gmail, Sheets,
    Slack, AI, etc.) is single-input — multiple `_default` edges into a
    single-input node leave the workflow silently broken at runtime. For
    joining or merging multiple data streams use `attach_magic_node`
    (1–5 inputs, df1..dfN handles, the correct pattern). The only legitimate
    multi-input non-Magic node is the legacy Merge block; pass
    `allow_multi_input=True` if you genuinely need that.

    FLAG DEFAULTS (see `create_workflow` docstring for the full
    start-node-vs-trigger vocabulary):

      Parents PRESENT → block is downstream. Both `isTrigger` and
        `isListener` default to False. Explicit overrides honored.
        Pre-v0.2.13 this is where the orphan-trigger bug lived.

      Parents EMPTY (root block) → `isTrigger` defaults to True (every
        root is a start node — the platform requires at least one).
        `isListener` is auto-detected from the node-def catalog:
        Scheduler / Gmail New Message / Sheets Read (listener-capable
        types) → True (live polling, becomes the workflow's automation
        trigger); Custom Code / plain transforms → False (a start node
        but not the automation entry point). Explicit overrides honored.

      Common explicit overrides:
        - `is_listener=False` on a Scheduler / Sheets-read root → one-off
          run of an otherwise-pollable type (read once, don't keep polling)
        - `is_trigger=False` on a root → unusual; produces a workflow
          with no start node, invalid until something else is a start.

      The platform allows MULTIPLE start nodes (each begins its own
      swimlane) but ONLY ONE listener per workflow.

    v0.2.22 GUARDS:
      - `force_root` (default False): if catalog `is_trigger=False` for the
        typeId AND parent_node_ids=[], attach is REFUSED (raises ValueError).
        Custom Code, Magic Node, and all action-only nodes have
        `is_trigger=False` in the catalog and require a parent. Pass
        force_root=True to bypass for a catalog edge case (rare).
      - `force_demote_listener` (default False): if attaching a listener-
        capable node as a root AND the workflow already has a block with
        isListener=True, the attach is REFUSED. The platform enforces
        max-one-listener-per-workflow. Pass force_demote_listener=True to
        auto-flip is_listener=False on the new block (response surfaces
        `demoted_from_listener: True`). Or pass is_listener=False
        explicitly with the same effect. Used internally by `prepend_trigger`.

    `output_columns` is optional but IMPORTANT for Custom Code blocks that
    transform / reshape their input. Without it, the wrapper doesn't set
    `outputs.columns_metadata` on the new block, and downstream nodes
    continue to see UPSTREAM columns — your transform's column changes
    are invisible. For Custom Code specifically prefer `attach_python_block`
    which always sets the output schema. For app-backed nodes (Gmail,
    Sheets, etc.) the platform fills in the schema from the catalog;
    leave `output_columns=None`.

    PIPEDREAM CONNECTION-FIELD NAMING IS INCONSISTENT AND NOT INFERABLE —
    the trailing-segment heuristic below works for SOME actions but NOT all.
    The ONLY reliable way to get the right field names is
    `get_node_dynamic_fields(workflow_id, node_id)` — call it once after an
    initial placeholder attach, read `dropdown_field_names`, and use those
    exact strings in subsequent settings. Examples of the inconsistency:
      - Gmail Send Email     → `pipedream-gmail-gmail_send_email-gmail`
      - Slack V2 Send Message → `pipedream-slack_v2-slack_v2_send_message-slack_v2`
      - Slack New Msg channel → `pipedream-slack_v2-slack_v2_new_message_in_channels-conversations`
                                                                                      ^^^^^^^^^^^^^
                                                       NOT `channel` or `channelId`
      - Sheets Add Single Row connection
                              → `pipedream-google_sheets-google_sheets_add_single_row-googleSheets_connection_id`
                                                                                      ^^^^^^^^^^^^^^^^^^^^^^^^
                              The trailing token can be ANY camelCase variant.
    Bottom line: never guess Pipedream field names from a formula. Discover
    them with `get_node_dynamic_fields`.

    CANONICAL SHEETS CRUD PATTERNS (use these, not alternatives):
      - **READ** rows: `Get Values in Range`
        (`pipedream.google_sheets.google_sheets_get_values_in_range`).
        Pass `range` in A1 notation (`Sheet1!A1:E100`). Use this for
        anything that needs columns; the platform reads the header row
        and emits one row per spreadsheet row.
      - **WRITE one row at a time**: ALWAYS use `Add Single Row`
        (`pipedream.google_sheets.google_sheets_add_single_row`) in a
        per-row loop, NEVER `Add Multiple Rows`. Add Single Row has 5
        schema fields (connection, drive, sheetId, worksheetId, hasHeaders);
        the actual row values come from the UPSTREAM block's columns:
          - hasHeaders=true  → upstream column names match sheet headers
          - hasHeaders=false → upstream column order maps to sheet columns A,B,C,...
        DO NOT try to set `myColumnData` or similar in `settings` — it
        will be persisted silently and ignored by the Pipedream runtime.
      - **UPDATE / DELETE rows**: similar pattern — use the single-row
        action and pass row identity via upstream.

    PIPEDREAM ROW-LEVEL ERROR DETECTION (v0.2.20): when a Pipedream node
    "completes" with `error:null` at the block level, that does NOT mean
    the action succeeded. The real error may be in row[0].error of the
    output. `tail_execution` and `update_node_setting(verify=True)` auto-
    surface this as `pipedream_row_error` — always check it.

    CROSS-TENANT CONNECTION RUNTIME ACCEPTANCE (v0.2.18 finding): in
    multi-user nRev tenants, the same connection_id may work at attach
    time for one app and fail at execute time for another. Gmail and
    Sheets accept cross-tenant connection_ids; Google Calendar's
    Pipedream component code throws
    `Cannot read properties of undefined (reading 'oauth_access_token')`
    at runtime. Recommendation: in multi-user tenants, have each user
    OAuth their own connection rather than share.

    `list_node_settings` LIMITATION FOR PIPEDREAM NODES: returns only the
    connection_id field on a freshly-attached node. The action's full
    field list (recipient, subject, body, etc.) materializes only after
    other settings are submitted. Don't trust `list_node_settings` as a
    "what does this need?" preview — consult the catalog `value` slug
    and Pipedream's documented action schema instead.

    `field_labels` (optional): explicit {field_name: human_label} map. Useful
    when you already know the labels (e.g. copied from another node). Values
    here take precedence over auto-resolution.

    `auto_resolve_labels` (default True): for each setting whose field_name
    looks like a dropdown (ends in `_connection_id`, `sheetId`, `worksheetId`,
    `channelId`, `folderId`, etc.) AND has no explicit label, call the
    platform's field-options endpoint to resolve the value to its human
    label. This is what makes the UI dropdowns show "Competitive tracking"
    instead of a raw spreadsheet UUID. Adds ~1 extra API call per dropdown
    field. Failures are silent (label stays None) — doesn't block attach.

    KNOWN AUTO-RESOLVE LIMITATIONS — pass via `field_labels` when these hit:
      - connection_id fields can only be auto-resolved for connections owned
        by the JWT caller. In multi-user tenants, if you're reusing a
        teammate's OAuth connection (e.g. their Google Sheets), the platform
        returns no visibility into other users' connections — pass the label
        explicitly.
      - Some Pipedream dropdowns require prerequisite settings (e.g.
        worksheetId needs sheetId set first). Auto-resolve sends ALL settings
        as context so cascading works, but exotic chains may need manual help.
    """
    if not type_id:
        raise ValueError("type_id is required (use list_node_definitions to find it)")
    if not isinstance(settings, dict):
        raise ValueError("settings must be a dict mapping field_name → value")

    # ── Single-input guard ──────────────────────────────────────────────────
    # Almost every nRev node type is single-input. Multi-input is the
    # exception (Magic Node 1–5, legacy Merge 2). Allowing the caller to wire
    # multiple `_default` edges into a single-input node silently produces a
    # workflow that looks correct in the UI but fails at execution — the
    # downstream block only knows how to read one input. Refuse here so the
    # caller is forced to switch to attach_magic_node when they really want
    # a join, or opt in explicitly via allow_multi_input.
    if len(parent_node_ids) > 1 and not allow_multi_input:
        magic_hint = ""
        if type_id == block_types.MAGIC_NODE:
            magic_hint = (
                " You passed the Magic Node typeId; use attach_magic_node "
                "instead — it sets up the df1..dfN target handles and "
                "references list that Magic Node requires."
            )
        else:
            magic_hint = (
                " For joining or merging multiple data streams use "
                "attach_magic_node (1–5 inputs, df1..dfN handles). For the "
                "legacy Merge block specifically, pass allow_multi_input=True."
            )
        raise ValueError(
            f"attach_node refuses to wire {len(parent_node_ids)} parents into "
            f"a single-input node ({type_id}). Multiple `_default` edges into "
            f"one block looks fine in the UI but silently breaks at execution."
            f"{magic_hint}"
        )

    wf = api.get_workflow(workflow_id)
    blocks_by_id = {b["id"]: b for b in wf["blocks"]}
    missing = [p for p in parent_node_ids if p not in blocks_by_id]
    if missing:
        raise ValueError(f"parent_node_ids not found in workflow {workflow_id}: {missing}")

    new_id = str(uuid.uuid4())
    type_slug = _typeid_to_value_slug(type_id)

    # ── Resolve isTrigger / isListener ──────────────────────────────────────
    # Two distinct concepts the platform models with two flags:
    #
    #   isTrigger = "this block is a START NODE" — a swimlane entry point.
    #   Every workflow needs AT LEAST ONE start node ("no start nodes"
    #   error otherwise). Multiple are allowed (each starts its own
    #   swimlane).
    #
    #   isListener = "this block is the workflow's automation trigger" —
    #   it polls / subscribes to events so the workflow runs on its own.
    #   ONLY ONE listener per workflow (platform enforces).
    #
    # Default resolution:
    #   Parents present → block is downstream → both flags False (unless
    #     caller explicitly overrides). Pre-v0.2.13 this was the bug:
    #     trigger-capable types got auto-flagged as triggers even
    #     downstream, producing orphan triggers.
    #   No parents → block is a root → isTrigger=True ALWAYS (every root
    #     is a start node by platform rule). isListener auto-detected from
    #     the node-def catalog: Scheduler / Gmail New Message / Sheets
    #     Read (listener-capable types) → True; Custom Code / plain
    #     transforms → False. This preserves the v0.2.7 ergonomics
    #     (Scheduler "just works" without remembering is_listener=True)
    #     while also correctly marking plain Custom Code roots as start
    #     nodes so one-off workflows are valid.
    #
    # Explicit overrides always win. Pass is_listener=False on a
    # Scheduler/listener-capable root if you want a one-off run of an
    # otherwise-pollable type.
    auto_trigger: Optional[bool] = None
    auto_listener: Optional[bool] = None
    if not parent_node_ids:
        auto_trigger, auto_listener = _lookup_node_def_flags(type_id)

    # v0.2.22 Fix #1 — refuse non-trigger-capable types as root.
    # Catalog `is_trigger=False` means the node REQUIRES a parent — it's an
    # action / transformation that can't be a workflow start node. Attempting
    # this used to fail silently at runtime with "No input data provided" (e.g.
    # Custom Code as root). Now blocked upfront with a clear pointer.
    if not parent_node_ids and auto_trigger is False and not force_root:
        raise ValueError(
            f"Node typeId {type_id!r} (name={name!r}) cannot be a workflow start "
            f"node — the catalog marks it `is_trigger=False`, meaning it's an "
            f"action that requires a parent. Attach a real data source as the "
            f"root (Get Values in Range, CSV Upload, Search People, etc.) and "
            f"wire this block downstream. If you genuinely need to override "
            f"this for a catalog edge case, pass force_root=True."
        )

    # v0.2.22 Fix #2 — one-listener-per-workflow guard.
    # The platform allows at most ONE block with isListener=True per workflow
    # (the "automation trigger"). Attaching a 2nd listener-capable node as a
    # root would normally auto-set isListener=True via the catalog, creating
    # an invalid workflow. Detect and refuse with a clear path forward.
    # NOTE: only fires when isListener WOULD be True (catalog says listener-
    # capable AND caller didn't explicitly set is_listener=False).
    workflow_already_has_listener = next(
        (b for b in wf["blocks"] if b.get("isListener")), None
    )
    would_be_listener = (
        not parent_node_ids
        and is_listener is not False
        and (is_listener is True or auto_listener)
    )
    demoted_from_listener = False
    if workflow_already_has_listener and would_be_listener and not force_demote_listener:
        existing_name = (workflow_already_has_listener.get("variableName")
                         or workflow_already_has_listener["id"])
        raise ValueError(
            f"Workflow already has a listener: {existing_name!r}. Only ONE "
            f"listener (automation trigger) per workflow is allowed by the "
            f"platform. To attach this as a NON-listener start node instead, "
            f"pass either force_demote_listener=True (auto-flips to "
            f"is_listener=False) or is_listener=False explicitly."
        )
    if workflow_already_has_listener and would_be_listener and force_demote_listener:
        # Auto-demote: silently flip is_listener to False
        is_listener = False
        demoted_from_listener = True

    if parent_node_ids:
        resolved_is_trigger = is_trigger if is_trigger is not None else False
        resolved_is_listener = is_listener if is_listener is not None else False
    else:
        # Root block: always a start node, listener auto from catalog.
        resolved_is_trigger = is_trigger if is_trigger is not None else True
        resolved_is_listener = is_listener if is_listener is not None else (auto_listener or False)

    # v0.2.21 — Scheduler is_listener=False guard. Scheduler-as-non-listener is
    # a footgun: it becomes a "start node that doesn't actually fire", which
    # makes the UI confused and the workflow effectively dead. The caller was
    # almost certainly trying to do a one-off test run — point them at the
    # right pattern (real data source as root, OR Scheduler+leave-listener-on
    # for cron). v0.2.20 session burned ~10 min of debugging on this.
    # v0.2.22: skip this guard if Scheduler was auto-demoted by Fix #2
    # (force_demote_listener=True with an existing listener). In that case the
    # caller explicitly opted into the demotion via prepend_trigger or similar.
    SCHEDULER_TYPEID = "68da2fb4-8295-4568-9415-c47de58e6224"
    if (type_id == SCHEDULER_TYPEID and is_listener is False
            and not demoted_from_listener):
        raise ValueError(
            "Scheduler with is_listener=False is a footgun — it becomes a "
            "start node that doesn't actually fire. For one-off / ad-hoc "
            "workflows, use a real data source as the root (Get Values in "
            "Range from a Sheet, CSV reader, etc.). For cron-driven live "
            "automation, leave is_listener=True (the default for Scheduler). "
            "See create_workflow docstring for the start-node-vs-trigger "
            "distinction."
        )

    # Position: 400 px right of rightmost parent (or origin for triggers)
    if parent_node_ids:
        parents = [blocks_by_id[p] for p in parent_node_ids]
        if position_x is None:
            position_x = max(p["position"]["x"] for p in parents) + 400
        if position_y is None:
            position_y = sum(p["position"]["y"] for p in parents) / len(parents)
    else:
        if position_x is None: position_x = 100
        if position_y is None: position_y = -100

    # ── Resolve fieldLabels per setting ─────────────────────────────────────
    # Precedence: explicit field_labels > auto-resolve via field-options > None
    field_labels = dict(field_labels or {})
    settings_array_for_options = [{"field_name": k, "field_value": v} for k, v in settings.items()]
    resolved_labels: dict[str, str] = {}
    if auto_resolve_labels:
        for fname, fvalue in settings.items():
            if fname in field_labels:
                continue  # caller already provided
            if not _looks_like_dropdown_field(fname):
                continue
            label = _resolve_field_label(
                node_id=new_id, type_id=type_id, field_name=fname,
                target_value=fvalue, settings_array=settings_array_for_options,
            )
            if label:
                resolved_labels[fname] = label

    # Settings — wrap each field in the platform's _sf envelope; attach label if known
    def _label_for(fname: str) -> Optional[str]:
        if fname in field_labels:
            return field_labels[fname]
        return resolved_labels.get(fname)

    settings_field_values = [_sf(name=k, value=v, label=_label_for(k)) for k, v in settings.items()]

    # Outputs — only set columns_metadata if caller specified columns
    if output_columns:
        if output_dtypes is None:
            output_dtypes = ["string"] * len(output_columns)
        if len(output_dtypes) != len(output_columns):
            raise ValueError("output_dtypes length must match output_columns length")
        outputs = [{
            "columns": output_columns,
            "columns_metadata": _build_columns_metadata(
                output_columns, output_dtypes,
                origin_node_id=new_id, origin_node_name=name,
                origin_node_type=type_slug,
            ),
            "file": "",
            "handle_condition": "_default",
            "node_id": new_id,
        }]
    else:
        outputs = [{
            "columns": [], "columns_metadata": None, "file": "",
            "handle_condition": "_default", "node_id": new_id,
        }]

    new_block = {
        "id": new_id,
        "typeId": type_id,
        "variableName": name,
        "description": description,
        "settings_field_values": settings_field_values,
        "isTrigger": resolved_is_trigger,
        "isOrphan": False,
        "isPartOfActiveSwimlane": True,
        "isListener": resolved_is_listener,
        "isTestMode": False,
        # v0.2.23 Fix #3 — populate inputs from parent's outputs.columns_metadata
        # so downstream validation doesn't say "Fields not found in available
        # data" on first attach. Pre-v0.2.23 the inputs were always an empty
        # skeleton, and the only way to recover was remove_edge + add_edge.
        # This mirrors the v0.2.18 add_edge refresh logic.
        "inputs": _build_inputs_from_parents(parent_node_ids, blocks_by_id),
        "outputs": outputs,
        "toBlocks": [],
        "position": {"x": float(position_x), "y": float(position_y)},
        "creditCostPerItem": credit_cost_per_item,
        "column_operations": None,
        "node_config_error": None,
    }

    # Small-payload path (v0.2.8): paste-nodes for the new block, then put_node
    # on each parent to wire the edge. Avoids the full-workflow PUT that
    # silently breaks on workflows past ~50 blocks (HTTP 413, request too big).
    # paste-nodes reassigns our locally-generated UUID; the helper diffs the
    # response against existing block IDs to find the actual platform-assigned
    # id and returns it.
    parent_edges = [(pid, "_default", "_default") for pid in parent_node_ids]
    resp, actual_new_id = _attach_block_via_paste_and_wire(
        workflow_id=workflow_id,
        new_block=new_block,
        parent_edges=parent_edges,
        fallback_parents=blocks_by_id,
        existing_block_ids={b["id"] for b in wf["blocks"]},
    )
    err = _new_block_error_from_paste(resp, actual_new_id)

    # v0.2.20 Fix C: for Pipedream-flavored typeIds, validate that the caller's
    # field_names match the action's actual schema. Catches silent no-ops where
    # a typo'd or invented field name gets stored uselessly (e.g. `myColumnData`
    # on Add Single Row — the platform persists it but the Pipedream runtime
    # ignores it because the action never asked for that field). Issues are
    # warnings only — attach is still considered ok if the platform accepted
    # the PUT.
    pipedream_field_warnings: list[dict] = []
    is_pipedream_type = any(fn.startswith("pipedream-") for fn in settings.keys())
    if err is None and is_pipedream_type and actual_new_id:
        try:
            schema = api.updated_node_config(
                node_id=actual_new_id,
                node_definition_id=type_id,
                field_name_changed=next(iter(settings.keys())),
                setting_field_values=settings_field_values,
                settings_schema=[],
            )
            valid_names = set()
            for f in ((schema.get("nodeDefinition") or {}).get("fields") or []):
                if isinstance(f, dict) and f.get("name"):
                    valid_names.add(f["name"])
            for given in settings.keys():
                if valid_names and given not in valid_names:
                    pipedream_field_warnings.append({
                        "field_name": given,
                        "issue": "not in action schema — value will be stored but ignored",
                        "schema_field_names": sorted(valid_names),
                    })
        except Exception:
            pass

    return {
        "ok": err is None,
        "node_id": actual_new_id,
        "type_id": type_id,
        "name": name,
        "node_config_error": err,
        "workflowConfigError": resp.get("workflowConfigError"),
        "isRunable": resp.get("isRunable"),
        "is_trigger": resolved_is_trigger,   # final value applied to the block
        "is_listener": resolved_is_listener,  # final value applied to the block
        "demoted_from_listener": demoted_from_listener,  # v0.2.22
        "resolved_labels": resolved_labels,  # which dropdown fields got auto-resolved
        "explicit_labels": list(field_labels.keys()),  # which were caller-supplied
        "pipedream_field_warnings": pipedream_field_warnings,  # v0.2.20
        "validation": _maybe_validate(workflow_id, validate_after),
    }


@mcp.tool()
def prepend_trigger(
    workflow_id: str,
    existing_root_id: str,
    trigger_type_id: str,
    trigger_settings: dict,
    trigger_name: str = "Trigger",
    is_listener: Optional[bool] = None,
    validate_after: bool = True,
) -> dict:
    """Prepend a trigger node (typically Scheduler) before an existing root.

    v0.2.22 — convenience tool for the common "I want to convert this one-off
    workflow into a scheduled/triggered automation" pattern.

    Under the hood (two server-side steps, atomic from caller's view):
      1. attach_node(parent_node_ids=[], type_id=trigger_type_id, ...) —
         adds the trigger as a new root. If trigger_type_id is listener-
         capable (e.g. Scheduler), auto-set isListener=True.
      2. add_edge(new_trigger → existing_root_id) — wires the trigger to
         fire the existing root on each invocation. The v0.2.21 add_edge
         auto-flips the existing root's isTrigger=False so the workflow
         ends up with exactly ONE start node (the new trigger).

    GOTCHA — runtime behavior of the downstream action:
      The existing root's SETTINGS are NOT overridden by trigger output.
      Pipedream-flavored actions (Find Email, Search People, Get Values
      in Range, etc.) will use their OWN configured query/parameters each
      time the trigger fires. The trigger's output (e.g. Scheduler's
      `data.timestamp`) is available to the downstream node BUT only if
      the action's settings explicitly reference upstream columns via
      `{{data.timestamp}}` or similar Jinja-like templates.
      Verified live in v0.2.22 probe: Find Email with `q="from:..."` ran
      identically standalone vs prepended-with-Scheduler — the action's
      own settings drive what it does; the trigger just provides cadence.

    Args:
      workflow_id:        target workflow id
      existing_root_id:   the current root you want to prepend a trigger to.
                          Must currently be a root (no incoming edges).
      trigger_type_id:    typeId of the trigger (most commonly Scheduler's
                          typeId `68da2fb4-8295-4568-9415-c47de58e6224`)
      trigger_settings:   settings dict for the trigger (e.g. for Scheduler,
                          `{"automation-scheduler-interval": "Days"}`)
      trigger_name:       display name for the new trigger block
      is_listener:        auto-detected from catalog when None. For
                          Scheduler-like types this resolves to True. Pass
                          False explicitly to attach the trigger as a
                          non-listener start node (rare).
      validate_after:     whether to run validate_workflow after wiring.

    Returns: {ok, trigger_node_id, trigger_is_listener,
    target_isTrigger_flipped, target_isOrphan_refreshed, workflow_is_runable,
    note, validation}.
    """
    wf = api.get_workflow(workflow_id)
    target = next((b for b in wf["blocks"] if b["id"] == existing_root_id), None)
    if target is None:
        raise ValueError(f"existing_root_id {existing_root_id} not in workflow {workflow_id}")
    # Verify target IS currently a root (no incoming edges)
    has_incoming = any(
        any(t.get("toBlockId") == existing_root_id for t in (b.get("toBlocks") or []))
        for b in wf["blocks"] if b["id"] != existing_root_id
    )
    if has_incoming:
        return {
            "ok": False,
            "message": (
                f"existing_root_id {existing_root_id} already has incoming "
                f"edges — it's not currently a root. Use add_edge directly if "
                f"you want to add another upstream connection."
            ),
        }

    # Position the trigger 400px to the left of the existing root
    target_pos = target.get("position", {"x": 0, "y": 0}) or {}
    trigger_x = float(target_pos.get("x", 0)) - 400
    trigger_y = float(target_pos.get("y", 0))

    # Step 1: attach the trigger as a new root.
    # Use force_demote_listener=True so we don't fail if there's already a
    # listener — the prepend pattern intentionally demotes existing roots.
    attach_result = attach_node(
        workflow_id=workflow_id,
        parent_node_ids=[],
        type_id=trigger_type_id,
        name=trigger_name,
        settings=trigger_settings,
        is_listener=is_listener,
        position_x=trigger_x,
        position_y=trigger_y,
        validate_after=False,
        force_demote_listener=True,
    )
    if not attach_result.get("ok"):
        return {
            "ok": False,
            "stage": "attach_trigger",
            "message": "trigger attach failed",
            "details": attach_result,
        }
    trigger_id = attach_result["node_id"]

    # Step 2: wire trigger → existing root. The v0.2.21 add_edge auto-flips
    # target.isTrigger=False since the target now has a parent.
    edge_result = add_edge(
        workflow_id=workflow_id,
        source_node_id=trigger_id,
        target_node_id=existing_root_id,
        validate_after=validate_after,
    )

    target_name = target.get("variableName") or existing_root_id
    return {
        "ok": edge_result.get("ok", False),
        "trigger_node_id": trigger_id,
        "trigger_is_listener": attach_result.get("is_listener"),
        "trigger_demoted_from_listener": attach_result.get("demoted_from_listener", False),
        "target_isTrigger_flipped": edge_result.get("target_isTrigger_flipped", False),
        "target_isOrphan_refreshed": edge_result.get("target_isOrphan_refreshed", False),
        "workflow_is_runable": edge_result.get("isRunable"),
        "validation": edge_result.get("validation"),
        "note": (
            f"Trigger wired. The downstream node {target_name!r} will use its "
            f"OWN configured settings each time the trigger fires. To template "
            f"values from the trigger (e.g. Scheduler.data.timestamp) into the "
            f"action, manually edit the downstream node's settings to reference "
            f"upstream columns via {{{{ data.<field> }}}} templates."
        ),
    }


@mcp.tool()
def paste_nodes(
    workflow_id: str,
    nodes: list[dict],
    allow_multi_input: bool = False,
    validate_after: bool = True,
) -> dict:
    """Paste pre-made node specs into a workflow (mirrors the UI's drag-from-palette).

    For most use cases prefer `attach_node` — it's a higher-level wrapper that
    handles edge-wiring, position defaults, AND the v0.2.13+ guards. Use
    `paste_nodes` when you need the platform to auto-fill defaults from the
    node definition (e.g. for complex AI nodes with deeply-nested settings),
    or when you have a pre-built block dict from another workflow you want
    to drop in.

    `nodes` is a list of FULL block dicts. The platform's `/paste-nodes`
    endpoint does NOT auto-fill missing fields the way "drag from palette"
    in the UI implies — it 422s on missing fields and (worse) sometimes
    500s on partially-malformed bodies. Required fields per node, from
    live probing:

        id                          (UUID; platform will reassign — that's fine)
        typeId                      (the node-definition UUID)
        variableName                (display name)
        description                 (can be empty string)
        settings_field_values       (list of the platform's settings envelope —
                                     each entry needs field_name, field_value,
                                     fieldLabel, error, isUserInputInFormMandatory,
                                     selectedInputTypeIndex, isStale)
        isTrigger, isListener, isOrphan, isPartOfActiveSwimlane, isTestMode
        inputs                      (list, can be the default skeleton)
        outputs                     (list, can be the default skeleton)
        toBlocks                    (list of edge dicts — empty if no outgoing edges)
        position                    ({"x": float, "y": float})
        creditCostPerItem           (int, 0 if free)
        column_operations           (None ok)
        node_config_error           (None ok)

    The easiest way to get a valid block dict is to `get_node` an existing
    block of the same typeId and mutate the fields you want to change.

    The platform reassigns `id` on paste; if your block has internal
    self-references (outputs[].node_id, columns_metadata[].origin_node_id,
    Magic Node references), they'll point at the OLD id. Prefer
    `attach_node` for typical use — it handles the id rewriting via
    `_rewrite_block_id` automatically.

    SINGLE-INPUT GUARD (v0.2.15): mirrors the guard in `add_edge` /
    `splice_branch` / `attach_node`. If any of the pasted blocks would land
    a second `_default` incoming edge into an already-targeted node (either
    via a `toBlocks` entry on the pasted block itself, or by being targeted
    by an existing block), this tool refuses with a ValueError. Skips the
    check for `df1..df5` target handles (Magic Node fan-in). The check
    inspects the pasted `toBlocks` AND scans the workflow's existing blocks
    for `_default` edges into pasted block ids. Pass `allow_multi_input=True`
    to bypass (legacy Merge / experimental use). Closes the 5th leak the
    v0.2.13 review surfaced — `paste_nodes` was the one back door that
    bypassed every other guard.

    NOTE: this does NOT auto-wire edges between the pasted nodes or to
    existing blocks. Call `add_edge` afterwards.
    """
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("nodes must be a non-empty list of block dicts")

    # ── Single-input guard ──────────────────────────────────────────────────
    # Block the two ways paste_nodes can introduce a duplicate _default edge
    # into a single-input target:
    #   (a) A pasted block itself carries a `toBlocks` entry pointing to a
    #       node (existing OR also-pasted) on _default, where that node
    #       already has a _default incoming.
    #   (b) The workflow already has _default edges pointing at a pasted
    #       block id (only relevant if the caller is re-pasting a node id
    #       that the platform never reassigns — defensive but cheap).
    # The check is a pre-flight: if it fires, no API call is made. If it
    # passes, the actual paste proceeds.
    if not allow_multi_input:
        wf = api.get_workflow(workflow_id)
        existing_blocks = wf.get("blocks") or []
        pasted_ids = {n.get("id") for n in nodes if n.get("id")}
        # All edges that will exist post-paste, in the form
        # (target_id, target_handle, source_id).
        all_edges: list[tuple[str, str, str]] = []
        # 1. existing → existing / pasted
        for src in existing_blocks:
            for e in (src.get("toBlocks") or []):
                all_edges.append((
                    e.get("toBlockId"),
                    e.get("edge_target_handle_condition"),
                    src.get("id"),
                ))
        # 2. pasted → anything
        for src in nodes:
            for e in (src.get("toBlocks") or []):
                all_edges.append((
                    e.get("toBlockId"),
                    e.get("edge_target_handle_condition"),
                    src.get("id") or "<pasted>",
                ))
        # Group by (target, _default handle) and refuse if any target ends
        # up with 2+ _default incoming from distinct sources.
        per_target: dict[str, list[str]] = {}
        for target, handle, src in all_edges:
            if handle != "_default":
                continue  # Magic dfN handles + named handles exempt
            if not target:
                continue
            per_target.setdefault(target, []).append(src)
        for target, sources in per_target.items():
            distinct = [s for s in sources if s]
            if len(set(distinct)) >= 2:
                raise ValueError(
                    f"paste_nodes refuses to land multiple `_default` edges "
                    f"into {target} (would-be sources: {sorted(set(distinct))}). "
                    f"Multiple `_default` edges into one block silently break "
                    f"at execution. Use df1..df5 target handles for Magic "
                    f"Node fan-in, or remove all but one of the offending "
                    f"`toBlocks` entries from the pasted nodes. For the legacy "
                    f"Merge block specifically, pass allow_multi_input=True."
                )

    resp = api.paste_nodes(workflow_id, {"nodes": nodes})
    return {
        "ok": True,
        "response": resp,
        "validation": _maybe_validate(workflow_id, validate_after),
    }


@mcp.tool()
def duplicate_workflow(
    workflow_id: str,
    new_name: Optional[str] = None,
) -> dict:
    """Clone an entire workflow — all blocks, edges, settings — into a new one.

    The new workflow is independent: edits to the duplicate don't affect the
    source. Use this when you want to fork a workflow as the starting point
    for a customer-specific variant, or to safely experiment without touching
    the original.

    `new_name` defaults to "Copy of <original_name>". If omitted, this tool
    reads the source workflow's name and substitutes it (the platform's
    duplicate endpoint requires `name` in the body; v0.2.17 fix to honor
    the docstring promise that pre-v0.2.17 silently broke with HTTP 422).

    Returns the new workflow's id + name.
    """
    # v0.2.17: resolve the default name BEFORE calling the platform, since
    # the platform requires `name` in the body and returns HTTP 422 if absent.
    if not new_name:
        source = api.get_workflow(workflow_id)
        original_name = source.get("name") or "untitled"
        new_name = f"Copy of {original_name}"
    resp = api.duplicate_workflow(workflow_id, new_name=new_name)
    return {
        "ok": True,
        "source_workflow_id": workflow_id,
        "new_workflow_id": resp.get("id"),
        "new_workflow_name": resp.get("name"),
        "version": resp.get("version"),
    }


@mcp.tool()
def publish_workflow(
    workflow_id: str,
    toggle_live: bool = True,
) -> dict:
    """Publish a workflow to live mode (or take it off live).

    `toggle_live=True` (default) promotes the current draft to live: the
    workflow's `liveVersion` gets pinned and any configured trigger (cron
    Scheduler / webhook / Gmail-poll etc.) starts firing on its own.
    `toggle_live=False` takes the workflow off live — useful when you need
    to stop an automated workflow without deleting it.

    Required workflow state before publishing: at least one start node, a
    valid trigger if you want it to auto-fire, and no `workflowConfigError`
    or `node_config_error` on any block. `validate_workflow` shows you all
    three at once. Publishes against an invalid workflow are rejected.

    Returns the platform's response envelope. Publish is async — for some
    workflows the response is "queued" and you should poll
    `get_publish_status` for completion; for simpler workflows the response
    already carries the live-version id.
    """
    resp = api.publish_workflow(workflow_id, toggle_live=toggle_live)
    return {
        "ok": True,
        "workflow_id": workflow_id,
        "toggle_live": bool(toggle_live),
        "response": resp,
        "note": (
            "If the response shows a queued / pending state, call "
            "get_publish_status(workflow_id) to poll for completion."
        ),
    }


@mcp.tool()
def get_publish_status(workflow_id: str) -> dict:
    """Current live-publish status of a workflow.

    Use after `publish_workflow` to confirm the live version is actually
    serving requests (publishes can take a few seconds to propagate).

    Returns the platform's status envelope verbatim — typically includes
    fields like `status`, `liveVersion`, `progress`.
    """
    return api.get_publish_status(workflow_id)


@mcp.tool()
def delete_workflow(workflow_id: str, confirm: bool = False) -> dict:
    """Permanently delete a workflow and all its blocks.

    REQUIRES `confirm=True`. Without it, this tool returns a refusal
    (without making the API call) — guards against accidental deletes
    when the agent is iterating.

    There is no undo. If you want a safety copy first, call
    `duplicate_workflow` to fork it before deleting.

    Returns `{ok, deleted_workflow_id}` on success.
    """
    if not confirm:
        return {
            "ok": False,
            "deleted_workflow_id": None,
            "message": (
                f"delete_workflow refused: confirm=False (default). To "
                f"actually delete workflow {workflow_id}, retry with "
                f"confirm=True. There is no undo; consider "
                f"duplicate_workflow first if you need a safety copy."
            ),
        }
    try:
        api.delete_workflow(workflow_id)
    except Exception as e:
        return {
            "ok": False,
            "deleted_workflow_id": workflow_id,
            "error": str(e),
        }
    return {
        "ok": True,
        "deleted_workflow_id": workflow_id,
        "message": f"Workflow {workflow_id} deleted.",
    }


# ═══════════════════════════════════════════════════════════════════════════
# Sticky notes (workflow-level annotations) — v0.2.12
# ═══════════════════════════════════════════════════════════════════════════

# Default visual settings — match what the UI uses when you drop a new note.
_DEFAULT_STICKY_SIZE = {"width": 240.0, "height": 160.0}
_DEFAULT_STICKY_COLOR = "#FFEB3B"   # yellow
_DEFAULT_STICKY_COLOR_MODE = "background"  # one of: background, transparent, border


def _text_to_tiptap_content(text: str) -> dict:
    """Wrap a plain-text string in the Tiptap/ProseMirror JSON shape that the
    workflow editor's sticky-note renderer expects.

    Newlines split into separate paragraphs. Empty input produces an empty doc.

    The platform stores `content` as opaque JSON (additionalProperties: true on
    the schema). Sending `{"text": "..."}` is accepted on the wire but renders
    blank in the UI because the renderer treats `content` as a Tiptap doc.
    """
    if not text:
        return {"type": "doc", "content": []}
    paragraphs = []
    for line in text.split("\n"):
        if line:
            paragraphs.append({
                "type": "paragraph",
                "content": [{"type": "text", "text": line}],
            })
        else:
            paragraphs.append({"type": "paragraph"})
    return {"type": "doc", "content": paragraphs}


def _tiptap_content_to_text(content) -> str:
    """Inverse of _text_to_tiptap_content for displaying existing notes back to
    the caller. Walks the doc tree and joins all text runs with newlines
    between paragraphs. Returns "" if content isn't a recognized shape.
    """
    if not isinstance(content, dict):
        return ""
    # If the caller stored a non-tiptap shape (e.g. {"text": "..."}), surface
    # what we can.
    if "text" in content and isinstance(content["text"], str):
        return content["text"]
    lines: list[str] = []
    for block in (content.get("content") or []):
        if block.get("type") == "paragraph":
            runs = block.get("content") or []
            line = "".join(r.get("text", "") for r in runs if isinstance(r, dict))
            lines.append(line)
    return "\n".join(lines)


@mcp.tool()
def list_sticky_notes(workflow_id: str) -> dict:
    """List the sticky notes (workflow-level annotations) on a workflow.

    Returns: {count, notes: [{id, text, position, size, color, colorMode, zIndex}]}.
    `text` is the readable form of each note's Tiptap content; the original
    Tiptap doc is preserved under `content` for callers who want full
    fidelity (formatting, multiple runs per paragraph, etc.).
    """
    wf = api.get_workflow(workflow_id)
    raw = wf.get("stickyNotes") or []
    notes = [
        {
            "id": n.get("id"),
            "text": _tiptap_content_to_text(n.get("content")),
            "content": n.get("content"),
            "position": n.get("position"),
            "size": n.get("size"),
            "color": n.get("color"),
            "colorMode": n.get("colorMode"),
            "zIndex": n.get("zIndex"),
        }
        for n in raw
    ]
    return {"count": len(notes), "notes": notes}


@mcp.tool()
def add_sticky_note(
    workflow_id: str,
    text: str,
    position_x: float = 100.0,
    position_y: float = 100.0,
    width: float = 240.0,
    height: float = 160.0,
    color: str = "#FFEB3B",
    color_mode: str = "background",
    z_index: int = 0,
) -> dict:
    """Add a sticky note to a workflow. `text` is plain text; newlines become
    separate paragraphs in the rendered note.

    WHEN TO USE STICKY NOTES — planning aid, not decoration.
    Treat sticky notes like comments in code: write them when the workflow
    graph alone doesn't carry the WHY. Good uses: intent of a swimlane,
    non-obvious decisions ("using ASR not AMR because…"), open TODOs,
    known limitations, a one-line summary of a complex branch. Avoid:
    restating what the block names already say, decorative section
    headers, one note per block. If the workflow needs a sticky note to
    be understandable at all, the workflow itself probably needs renaming
    or restructuring first.

    `color` is a hex code (e.g. "#FFEB3B" yellow, "#80DEEA" cyan,
    "#F8BBD0" pink). `color_mode` is one of: background, transparent, border.

    Returns the new note's id + a refreshed list of all notes on the workflow.

    NOTE: writes via PATCH /workflows/{id}/no-validation which REPLACES the
    sticky-notes array server-side. We fetch current notes first, append,
    and PATCH the full list back — so concurrent edits from the UI could
    in principle race. In practice sticky notes are slow-moving so this
    isn't a real concern, but worth knowing.
    """
    if color_mode not in ("background", "transparent", "border"):
        raise ValueError(
            f"color_mode must be one of: background, transparent, border. "
            f"Got: {color_mode!r}"
        )

    wf = api.get_workflow(workflow_id)
    existing = wf.get("stickyNotes") or []

    new_note = {
        "id": str(uuid.uuid4()),
        "position": {"x": float(position_x), "y": float(position_y)},
        "size": {"width": float(width), "height": float(height)},
        "content": _text_to_tiptap_content(text),
        "color": color,
        "colorMode": color_mode,
        "zIndex": int(z_index),
    }
    updated = existing + [new_note]
    api.patch_workflow_no_validation(workflow_id, sticky_notes=updated)

    return {
        "ok": True,
        "note_id": new_note["id"],
        "count": len(updated),
        "note": "Refresh the workflow editor to see the new note.",
    }


@mcp.tool()
def update_sticky_note(
    workflow_id: str,
    note_id: str,
    text: Optional[str] = None,
    position_x: Optional[float] = None,
    position_y: Optional[float] = None,
    width: Optional[float] = None,
    height: Optional[float] = None,
    color: Optional[str] = None,
    color_mode: Optional[str] = None,
    z_index: Optional[int] = None,
) -> dict:
    """Update fields on an existing sticky note. Only fields you pass are
    changed; everything else stays as-is.

    WHEN TO USE STICKY NOTES — planning aid, not decoration.
    Treat sticky notes like comments in code: write them when the workflow
    graph alone doesn't carry the WHY. Good uses: intent of a swimlane,
    non-obvious decisions ("using ASR not AMR because…"), open TODOs,
    known limitations, a one-line summary of a complex branch. Avoid:
    restating what the block names already say, decorative section
    headers, one note per block. If the workflow needs a sticky note to
    be understandable at all, the workflow itself probably needs renaming
    or restructuring first.

    Use `list_sticky_notes(workflow_id)` first to find the note_id.
    """
    if color_mode is not None and color_mode not in ("background", "transparent", "border"):
        raise ValueError(
            f"color_mode must be one of: background, transparent, border. "
            f"Got: {color_mode!r}"
        )

    wf = api.get_workflow(workflow_id)
    existing = wf.get("stickyNotes") or []
    target_idx = next((i for i, n in enumerate(existing) if n.get("id") == note_id), None)
    if target_idx is None:
        raise ValueError(
            f"sticky note {note_id} not found on workflow {workflow_id}. "
            f"Use list_sticky_notes to see available ids."
        )

    note = dict(existing[target_idx])  # shallow copy
    if text is not None:
        note["content"] = _text_to_tiptap_content(text)
    if position_x is not None or position_y is not None:
        pos = dict(note.get("position") or {"x": 0.0, "y": 0.0})
        if position_x is not None:
            pos["x"] = float(position_x)
        if position_y is not None:
            pos["y"] = float(position_y)
        note["position"] = pos
    if width is not None or height is not None:
        sz = dict(note.get("size") or {"width": 240.0, "height": 160.0})
        if width is not None:
            sz["width"] = float(width)
        if height is not None:
            sz["height"] = float(height)
        note["size"] = sz
    if color is not None:
        note["color"] = color
    if color_mode is not None:
        note["colorMode"] = color_mode
    if z_index is not None:
        note["zIndex"] = int(z_index)

    updated = list(existing)
    updated[target_idx] = note
    api.patch_workflow_no_validation(workflow_id, sticky_notes=updated)
    return {"ok": True, "note_id": note_id, "count": len(updated)}


@mcp.tool()
def delete_sticky_note(workflow_id: str, note_id: str) -> dict:
    """Remove a sticky note from a workflow. Use list_sticky_notes first to
    find the id."""
    wf = api.get_workflow(workflow_id)
    existing = wf.get("stickyNotes") or []
    if not any(n.get("id") == note_id for n in existing):
        raise ValueError(
            f"sticky note {note_id} not found on workflow {workflow_id}."
        )
    updated = [n for n in existing if n.get("id") != note_id]
    api.patch_workflow_no_validation(workflow_id, sticky_notes=updated)
    return {
        "ok": True,
        "deleted_note_id": note_id,
        "remaining_count": len(updated),
    }


# ═══════════════════════════════════════════════════════════════════════════
# nRev Tables — v0.2.26 (separate service, same JWT)
# ═══════════════════════════════════════════════════════════════════════════
#
# 19 tools wrapping nrev-tables-service.public.prod.nurturev.com. Verified
# live on prod 2026-05-25. See docs/nrev_tables_api_investigation.md and
# docs/PROD_COMPREHENSIVE_TEST_2026_05_25.md for the surface, bugs, and
# design decisions.
#
# Auth: shared with workflow tools via the same set_jwt() — one JWT, two
# services. No separate auth tool needed.
#
# UX convention: row values come back from the platform keyed by COLUMN UUID
# (not column name). The wrapper tools below auto-translate names↔ids using
# a per-call schema fetch from get_table(). Pass `by_column_id=True` to opt
# out (advanced).


# ─── Helper: name ↔ id translation ──────────────────────────────────────


def _tables_resolve_name_map(table_id: str) -> dict:
    """Fetch a table's schema once and return {column_name: column_id}.
    Used by add_row / update_row / list_rows for name-keyed inputs/outputs."""
    schema = tables_api.get_table(table_id)
    return {c["name"]: c["id"] for c in (schema.get("columns") or [])}


def _tables_translate_to_ids(values: dict, name_to_id: dict) -> dict:
    """Translate a name-keyed dict to an id-keyed dict, surfacing any unknown
    column names as a clean error."""
    out = {}
    unknown = []
    for k, v in values.items():
        if k in name_to_id:
            out[name_to_id[k]] = v
        else:
            unknown.append(k)
    if unknown:
        raise ValueError(
            f"Unknown column(s) for this table: {unknown}. "
            f"Available: {sorted(name_to_id.keys())}"
        )
    return out


def _tables_project_to_names(row_values: dict, id_to_name: dict,
                              columns: Optional[list[str]] = None) -> dict:
    """Translate id-keyed row values to name-keyed.

    If `columns` is provided, also project to just those columns (drops
    system columns and anything else not requested).
    """
    out = {}
    for cid, v in row_values.items():
        name = id_to_name.get(cid, cid)
        out[name] = v
    if columns:
        out = {k: out.get(k) for k in columns}
    return out


# ─── Tables ─────────────────────────────────────────────────────────────


@mcp.tool()
def tables_list(name: Optional[str] = None,
                 creators: Optional[list[str]] = None,
                 skip: int = 0,
                 limit: int = 100) -> dict:
    """List nRev tables in this tenant.

    Tenant-scoped: returns tables created by anyone in the tenant, not just
    the calling user. Use `tables_list_creators()` to discover user_ids for
    the `creators` filter.

    `name` is substring match. Sorting is NOT exposed — the platform's
    sortBy is currently broken (every format returns 'Malformed sortBy
    entry'). Default order is by creation desc.
    """
    return tables_api.list_tables(name=name, creators=creators,
                                   skip=skip, limit=limit)


@mcp.tool()
def tables_list_creators() -> list:
    """List the distinct creators (users) of tables in this tenant.
    Use the returned user_ids to filter `tables_list(creators=...)`."""
    return tables_api.list_table_creators()


@mcp.tool()
def tables_get(table_id: str) -> dict:
    """Get full table schema including columns + UUIDs.

    Use this BEFORE calling tables_add_row / tables_update_row if you want
    to pass values by column name — those tools fetch the schema internally
    but if you're iterating in a loop, do one fetch up front and cache.
    """
    return tables_api.get_table(table_id)


@mcp.tool()
def tables_create(name: str, columns: Optional[list[dict]] = None) -> dict:
    """Create a new nRev table with optional inline columns.

    `columns` entries: {"name": str, "type": <type>, "position": int?}.
    Valid types: text | long_text | number | boolean | date | datetime | json.

    The platform auto-creates 3 system columns at positions 0/1/2: row_id
    (auto-incrementing int), added_at (datetime), last_updated_at
    (datetime). Your user columns start at position 3.
    """
    return tables_api.create_table(name=name, columns=columns)


@mcp.tool()
def tables_rename(table_id: str, new_name: str) -> dict:
    """Rename a table. table_id stays the same; only the display name
    changes. Name must be 5-64 chars."""
    return tables_api.rename_table(table_id, new_name)


@mcp.tool()
def tables_delete(table_id: str, confirm: bool = False) -> dict:
    """Delete a table.

    NOT YET LIVE — DELETE endpoints are M1 (not shipped as of 2026-05-25).
    Wrapper exists so the surface is stable. Calling today returns HTTP 405.

    `confirm=True` required (destructive, irreversible).
    """
    if not confirm:
        return {
            "ok": False,
            "message": "Destructive operation. Pass confirm=True to actually delete.",
        }
    return tables_api.delete_table(table_id)


# ─── Columns ────────────────────────────────────────────────────────────


@mcp.tool()
def tables_add_column(table_id: str, name: str, type: str,
                       position: Optional[int] = None) -> dict:
    """Add a new typed column to an existing table.

    Types: text | long_text | number | boolean | date | datetime | json.
    `row_id` is system-reserved.

    Column name must be unique within the table (409 on duplicate). Column
    UUIDs are stable across renames — downstream references won't break if
    you rename later.
    """
    return tables_api.add_column(table_id, name=name, col_type=type,
                                  position=position)


@mcp.tool()
def tables_rename_column(table_id: str, column_id_or_name: str,
                          new_name: str) -> dict:
    """Rename a column. Accepts either the column UUID or the current
    column name; auto-resolves names via get_table()."""
    cid = column_id_or_name
    if "-" not in cid or len(cid) < 30:
        # Looks like a name, not a UUID — resolve
        name_to_id = _tables_resolve_name_map(table_id)
        if column_id_or_name not in name_to_id:
            raise ValueError(
                f"Column '{column_id_or_name}' not found. "
                f"Available: {sorted(name_to_id.keys())}"
            )
        cid = name_to_id[column_id_or_name]
    return tables_api.rename_column(table_id, cid, new_name)


@mcp.tool()
def tables_reorder_column(table_id: str, column_id_or_name: str,
                            position: int) -> dict:
    """Move a column to a new position. System columns (row_id, added_at,
    last_updated_at) can't be reordered — 400 on attempt."""
    cid = column_id_or_name
    if "-" not in cid or len(cid) < 30:
        name_to_id = _tables_resolve_name_map(table_id)
        if column_id_or_name not in name_to_id:
            raise ValueError(
                f"Column '{column_id_or_name}' not found. "
                f"Available: {sorted(name_to_id.keys())}"
            )
        cid = name_to_id[column_id_or_name]
    return tables_api.reorder_column(table_id, cid, position)


@mcp.tool()
def tables_delete_column(table_id: str, column_id_or_name: str,
                          confirm: bool = False) -> dict:
    """Delete a column. NOT YET LIVE — M1 endpoint. Currently returns 405.
    `confirm=True` required."""
    if not confirm:
        return {
            "ok": False,
            "message": "Destructive operation. Pass confirm=True to actually delete.",
        }
    cid = column_id_or_name
    if "-" not in cid or len(cid) < 30:
        name_to_id = _tables_resolve_name_map(table_id)
        if column_id_or_name not in name_to_id:
            raise ValueError(
                f"Column '{column_id_or_name}' not found. "
                f"Available: {sorted(name_to_id.keys())}"
            )
        cid = name_to_id[column_id_or_name]
    return tables_api.delete_column(table_id, cid)


# ─── Rows ───────────────────────────────────────────────────────────────


@mcp.tool()
def tables_list_rows(table_id: str,
                      filter: Optional[dict] = None,
                      sort_by: Optional[str] = None,
                      sort_direction: str = "desc",
                      search: Optional[str] = None,
                      columns: Optional[list[str]] = None,
                      skip: int = 0,
                      limit: int = 100) -> dict:
    """List rows from a table with filter / sort / search / projection.

    `filter` shape: {"column": "<name or id>", "operator": "<op>",
                     "value": <scalar or list>}.
    Operators: eq, neq, contains (case-insensitive), gt, gte, lt, lte,
    is_empty, is_not_empty, in, not_in. For `in` / `not_in`, value is a list.

    `sort_by` is a column name OR id. Defaults to row_id desc (newest first).

    `columns` projects results to those columns only; values come back
    name-keyed (not id-keyed). If omitted, returns all columns name-keyed.

    `limit` auto-clamps to the nearest allowed value [100, 500, 1000, 5000,
    10000, 50000, 100000] — the platform rejects arbitrary integers.
    """
    schema = tables_api.get_table(table_id)
    name_to_id = {c["name"]: c["id"] for c in (schema.get("columns") or [])}
    id_to_name = {v: k for k, v in name_to_id.items()}

    def _resolve_col(cn):
        if cn in name_to_id:
            return name_to_id[cn]
        if cn in id_to_name:
            return cn  # already an id
        raise ValueError(f"Unknown column '{cn}'. Available: {sorted(name_to_id.keys())}")

    filter_column_id = None
    filter_operator = None
    filter_values = None
    if filter:
        filter_column_id = _resolve_col(filter.get("column"))
        filter_operator = filter.get("operator")
        v = filter.get("value")
        filter_values = v if isinstance(v, list) else ([v] if v is not None else None)

    sort_column_id = _resolve_col(sort_by) if sort_by else None

    resp = tables_api.list_rows(
        table_id,
        skip=skip, limit=limit,
        sort_column_id=sort_column_id,
        sort_direction=sort_direction,
        search=search,
        filter_column_id=filter_column_id,
        filter_operator=filter_operator,
        filter_values=filter_values,
    )

    # Project rows to name-keyed
    projected = []
    for row in (resp.get("data") or []):
        values = row.get("values") or {}
        projected.append({
            "row_id": row.get("row_id"),
            "values": _tables_project_to_names(values, id_to_name, columns),
            "created_at": row.get("created_at"),
            "last_updated_at": row.get("last_updated_at"),
        })
    return {"data": projected, "meta": resp.get("meta")}


@mcp.tool()
def tables_fetch_all_rows(table_id: str,
                            filter: Optional[dict] = None,
                            sort_by: Optional[str] = None,
                            sort_direction: str = "desc",
                            max_rows: int = 10000,
                            columns: Optional[list[str]] = None) -> dict:
    """Fetch ALL rows from a table (auto-paginates with limit=1000).

    Convenience wrapper around tables_list_rows for callers who don't want
    to deal with pagination. Hard cap at `max_rows` (default 10k) to prevent
    runaway. Beyond ~50k rows, prefer server-side aggregate when M2 ships.

    Returns: {data: [...], meta: {total_fetched: N, capped: bool}}.
    """
    if max_rows <= 0:
        raise ValueError("max_rows must be > 0")

    all_rows = []
    skip = 0
    page_size = 1000
    capped = False

    while len(all_rows) < max_rows:
        page = tables_list_rows(
            table_id, filter=filter, sort_by=sort_by,
            sort_direction=sort_direction, columns=columns,
            skip=skip, limit=page_size,
        )
        rows = page.get("data") or []
        if not rows:
            break
        all_rows.extend(rows)
        skip += len(rows)
        if len(rows) < page_size:
            break
        if len(all_rows) >= max_rows:
            all_rows = all_rows[:max_rows]
            capped = True
            break

    return {
        "data": all_rows,
        "meta": {
            "total_fetched": len(all_rows),
            "capped": capped,
            "max_rows": max_rows,
        },
    }


@mcp.tool()
def tables_add_row(table_id: str, values: dict,
                    by_column_id: bool = False) -> dict:
    """Insert one row into a table.

    `values`: dict keyed by COLUMN NAME (default) or COLUMN UUID
    (by_column_id=True).

    The platform stores rows keyed by column UUID. The default name-keyed
    path fetches the table schema once and translates names → UUIDs.
    Unknown column names raise a clear error.

    Type validation is platform-side: a string in a number column → 400
    "Cell type mismatch for column ...: expected number, got str."
    """
    if not by_column_id:
        name_to_id = _tables_resolve_name_map(table_id)
        values = _tables_translate_to_ids(values, name_to_id)
        # Project response back to name-keyed for consistency with list_rows
        resp = tables_api.add_row(table_id, values)
        id_to_name = {v: k for k, v in name_to_id.items()}
        if "row" in resp and "values" in resp["row"]:
            resp["row"]["values"] = _tables_project_to_names(
                resp["row"]["values"], id_to_name,
            )
        return resp
    return tables_api.add_row(table_id, values)


@mcp.tool()
def tables_update_row(table_id: str, row_id: int, values: dict,
                       by_column_id: bool = False) -> dict:
    """Update one row by row_id. Merge semantics (PATCH): unchanged fields
    keep their values; passing `null` for a field DELETES that field.

    `row_id` is the platform's auto-incrementing integer (1, 2, 3, ...).
    Get it from tables_list_rows / tables_fetch_all_rows responses.

    Unknown row_id → 404 with a clean message.
    """
    if not by_column_id:
        name_to_id = _tables_resolve_name_map(table_id)
        values = _tables_translate_to_ids(values, name_to_id)
        resp = tables_api.update_row(table_id, row_id, values)
        id_to_name = {v: k for k, v in name_to_id.items()}
        if isinstance(resp, dict) and "values" in resp:
            resp["values"] = _tables_project_to_names(resp["values"], id_to_name)
        return resp
    return tables_api.update_row(table_id, row_id, values)


@mcp.tool()
def tables_delete_row(table_id: str, row_id: int,
                       confirm: bool = False) -> dict:
    """Delete one row. NOT YET LIVE — M1 endpoint. Currently 405.
    `confirm=True` required."""
    if not confirm:
        return {
            "ok": False,
            "message": "Destructive operation. Pass confirm=True to actually delete.",
        }
    return tables_api.delete_row(table_id, row_id)


@mcp.tool()
def tables_bulk_add_rows(table_id: str, rows: list[dict],
                          by_column_id: bool = False,
                          stop_on_error: bool = False) -> dict:
    """Insert multiple rows. Loops over tables_add_row.

    Per-row cost on prod (2026-05-25 measurement): ~175ms sequential.
    For >100 rows, this gets slow. The platform has CSV import endpoints
    (POST /tables/csv/import + /csv/append) but they require an s3://
    URI which the MCP can't produce; the MCP-built CSV import path is
    deferred until the platform exposes inline-content or presigned-URL
    upload.

    `stop_on_error=False` (default): continue on per-row failures, collect
    them. `stop_on_error=True`: raise on first error.

    Returns: {inserted: [{row_id, index}, ...], errors: [{index, error}, ...],
              total_attempted: N}.
    """
    if not rows:
        return {"inserted": [], "errors": [], "total_attempted": 0}

    if not by_column_id:
        name_to_id = _tables_resolve_name_map(table_id)
        rows = [_tables_translate_to_ids(r, name_to_id) for r in rows]

    inserted = []
    errors = []
    for i, values in enumerate(rows):
        try:
            resp = tables_api.add_row(table_id, values)
            inserted.append({"row_id": resp["row"]["row_id"], "index": i})
        except Exception as e:
            errors.append({"index": i, "error": f"{type(e).__name__}: {str(e)[:300]}"})
            if stop_on_error:
                break
    return {
        "inserted": inserted,
        "errors": errors,
        "total_attempted": len(inserted) + len(errors),
    }


# ─── Misc ───────────────────────────────────────────────────────────────


@mcp.tool()
def get_plugin_version() -> dict:
    """Return the running MCP plugin version. Useful for the support
    workflow when README/docs drift from the actual code."""
    from . import __version__
    return {
        "version": __version__,
        "name": "nrev-wf-mcp",
        "homepage": "https://github.com/nurturev-dev/nrev-workflow-mcp",
    }


# ═══════════════════════════════════════════════════════════════════════════
# Entrypoint
# ═══════════════════════════════════════════════════════════════════════════

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
