"""FastMCP server — registers all v0.1 tools.

Run with:  python -m nrev_wf_mcp.server   (or via the `nrev-wf-mcp` entrypoint)
"""
from __future__ import annotations

import ast
import uuid
from typing import Optional

from fastmcp import FastMCP

from . import auth
from . import block_types
from . import client as api
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


# ═══════════════════════════════════════════════════════════════════════════
# Read
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_workflow(workflow_id: str) -> dict:
    """Slim view of a workflow's block graph.

    Returns name, isRunable, workflowConfigError, and a list of blocks each with
    {id, name, typeId, position, isTestMode, node_config_error, toBlocks, creditCostPerItem}.

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
# Build
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def create_workflow(name: str, description: str = "", validate_after: bool = True) -> dict:
    """Create a new empty workflow.

    Returns the new workflow including its assigned `id`. The workflow has no
    blocks yet — add a Scheduler trigger and downstream nodes via attach_*
    tools, or paste a starter via the web UI.

    This is the safe sandbox-creation tool: spin up a throwaway workflow when
    you want to experiment without touching anything live.
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

    # Add the df{N} edges to each parent (idempotent — won't duplicate).
    for i, pid in enumerate(parent_node_ids):
        parent = blocks_by_id[pid]
        edges = parent.get("toBlocks") or []
        target_handle = f"df{i + 1}"
        already = any(
            e.get("toBlockId") == new_id
            and e.get("edge_target_handle_condition") == target_handle
            for e in edges
        )
        if not already:
            edges.append({
                "edgeId": f"{pid}-_default-{new_id}-{target_handle}",
                "edge_source_handle_condition": "_default",
                "edge_target_handle_condition": target_handle,
                "toBlockId": new_id,
            })
        parent["toBlocks"] = edges

    all_blocks = wf["blocks"] + [new_block]
    payload = {"workflow_details": {
        "id": workflow_id,
        "name": wf.get("name"),
        "description": wf.get("description"),
        "blocks": all_blocks,
    }}
    resp = api.put_workflow(workflow_id, payload)

    err = None
    for b in resp.get("blocks", []):
        if b["id"] == new_id:
            err = b.get("node_config_error")
            break

    return {
        "ok": err is None,
        "node_id": new_id,
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
) -> dict:
    """Attach a Custom Code (Python) block downstream of an existing node.

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

    `code` must define `def run(...):` — the first arg is the upstream df.
    `output_columns` is the list of column names this block produces.
    `output_dtypes` is parallel to output_columns; defaults to "string" for each.
    Position defaults to 400 px right of parent.
    """
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

    # Add edge from parent → new block (idempotent: skip if already present).
    parent_edges = parent.get("toBlocks") or []
    if not any(e.get("toBlockId") == new_id for e in parent_edges):
        parent_edges.append({
            "edgeId": f"{parent_node_id}-_default-{new_id}-_default",
            "edge_source_handle_condition": "_default",
            "edge_target_handle_condition": "_default",
            "toBlockId": new_id,
        })
    parent["toBlocks"] = parent_edges

    all_blocks = wf["blocks"] + [new_block]
    payload = {"workflow_details": {
        "id": workflow_id,
        "name": wf.get("name"),
        "description": wf.get("description"),
        "blocks": all_blocks,
    }}
    resp = api.put_workflow(workflow_id, payload)

    err = None
    for b in resp.get("blocks", []):
        if b["id"] == new_id:
            err = b.get("node_config_error")
            break

    return {
        "ok": err is None,
        "node_id": new_id,
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
            return {
                "ok": False,
                "stage": "refresh_chain",
                "stuck_at_node": refresh_node_id,
                "refresh_results": refresh_results,
                "message": str(e),
            }

    resp = api.execute_node(workflow_id, target_node_id, prior_execution_id)
    result = {"ok": True, "response": resp}
    if refresh_results:
        result["refresh_results"] = refresh_results
    return result


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
                return _slim_execution(raw, timed_out=False)

        elif wait_until == "block_completed":
            target_br = next((br for br in block_runs if br.get("workflowBlockId") == target_block_id), None)
            if target_br and target_br.get("status") in ("completed", "failed"):
                return _slim_execution(raw, timed_out=False)

        elif wait_until == "any_change":
            current = {br.get("workflowBlockId"): br.get("status") for br in block_runs}
            if last_block_statuses and current != last_block_statuses:
                return _slim_execution(raw, timed_out=False)
            last_block_statuses = current

        time.sleep(poll_seconds)

    # Timed out — return current state
    raw = api.get_execution_detail(workflow_id, execution_id)
    return _slim_execution(raw, timed_out=True)


def _slim_execution(raw: dict, timed_out: bool) -> dict:
    """Compact representation of an execution snapshot."""
    block_runs = raw.get("blockRuns") or []
    return {
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
            }
            for br in block_runs
        ],
        "timed_out": timed_out,
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
        api.put_node(workflow_id, node_id, {"isTestMode": on})
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

@mcp.tool()
def add_edge(
    workflow_id: str,
    source_node_id: str,
    target_node_id: str,
    source_handle: str = "_default",
    target_handle: str = "_default",
    validate_after: bool = True,
) -> dict:
    """Add an edge from source_node → target_node.

    Idempotent: if an identical edge already exists (same source, target, both
    handles), this is a no-op.

    For Magic Node fan-in, set `target_handle` to `df1` … `df5` to pick the
    dataframe slot. NOTE: adding an edge to a Magic Node also requires updating
    the Magic Node's `references` list to include the new edge ID — `add_edge`
    does NOT do this. Use `update_magic_node(...)` separately if you need to
    update references.

    Returns `{ok, edge_existed, workflowConfigError}`.
    """
    wf = api.get_workflow(workflow_id)
    blocks_by_id = {b["id"]: b for b in wf["blocks"]}
    if source_node_id not in blocks_by_id:
        raise ValueError(f"source_node_id {source_node_id} not in workflow {workflow_id}")
    if target_node_id not in blocks_by_id:
        raise ValueError(f"target_node_id {target_node_id} not in workflow {workflow_id}")

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

    resp = _put_workflow_blocks(workflow_id, wf)
    return {
        "ok": not resp.get("workflowConfigError"),
        "edge_existed": False,
        "edge_added": True,
        "workflowConfigError": resp.get("workflowConfigError"),
        "isRunable": resp.get("isRunable"),
        "validation": _maybe_validate(workflow_id, validate_after),
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

    resp = _put_workflow_blocks(workflow_id, wf)
    return {
        "ok": not resp.get("workflowConfigError"),
        "removed_count": removed,
        "workflowConfigError": resp.get("workflowConfigError"),
        "isRunable": resp.get("isRunable"),
        "validation": _maybe_validate(workflow_id, validate_after),
    }


@mcp.tool()
def delete_node(workflow_id: str, node_id: str, validate_after: bool = True) -> dict:
    """Delete a block and cascade-clean all incident edges.

    Removes:
      - the block itself
      - every outgoing edge from the deleted block (gone with it)
      - every incoming edge from any other block to the deleted block

    Does NOT auto-fix Magic Nodes that referenced this block as a `references`
    entry — call `update_magic_node(magic_id, ...)` separately if needed, or
    `validate_workflow` to surface the dangling references.

    Returns `{ok, deleted_node_name, incoming_edges_removed, outgoing_edges_removed}`.
    """
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
    resp = _put_workflow_blocks(workflow_id, wf)
    return {
        "ok": not resp.get("workflowConfigError"),
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
    validate_after: bool = True,
) -> dict:
    """Splice a new branch into the live path. Atomic add + optional replace.

    The "splice in" half of the parallel-branch test pattern (NREV_WORKFLOW_GUIDE
    §11.8). Adds an edge from `new_terminal_node_id` to `downstream_target_node_id`,
    optionally removing the equivalent edge from `replace_edge_from_node_id` to
    the same downstream — all in a single PUT for atomicity.

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
    """Internal helper: PUT the workflow with current blocks (used after wiring edits)."""
    payload = {"workflow_details": {
        "id": workflow_id,
        "name": wf.get("name"),
        "description": wf.get("description"),
        "blocks": wf["blocks"],
    }}
    return api.put_workflow(workflow_id, payload)


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
) -> dict:
    """Replace a single setting value on a node, identified by its field path.

    `field_path` is `/`-separated. Each segment matches a `field_name` in the
    settings tree. For top-level settings, just the field name. For nested
    group fields, e.g. for a Magic Node code:

        "data_manipulation-magic_node-code_section/data_manipulation-magic_node-code"

    If the path isn't found, returns the list of all leaf paths so you can spot
    the right one. Use `get_node` or `list_node_settings` first to inspect.

    `verify=True` (default False) runs `partial_execute` on this node after the
    update, reusing cached upstream from the most recent completed execution,
    and returns the new row_count in the response. NOTE: for paid nodes
    (creditCostPerItem > 0, typically AI nodes), additional opt-in is required —
    pass `verify_cost_ack=True` to confirm the spend. Without ack, verify is
    skipped on paid nodes and an explanatory note is returned.
    """
    wf = api.get_workflow(workflow_id)
    target = next((b for b in wf["blocks"] if b["id"] == node_id), None)
    if target is None:
        raise ValueError(f"node {node_id} not in workflow {workflow_id}")

    settings = target.get("settings_field_values") or []
    segments = field_path.split("/")
    if not _walk_settings_set(settings, segments, value):
        return {
            "ok": False,
            "message": f"field_path {field_path!r} not found in node settings.",
            "available_paths": _list_field_paths(settings),
        }

    resp = _put_workflow_blocks(workflow_id, wf)
    err = next((b.get("node_config_error") for b in resp.get("blocks", []) if b["id"] == node_id), None)

    result = {
        "ok": err is None and not resp.get("workflowConfigError"),
        "node_id": node_id,
        "field_path": field_path,
        "node_config_error": err,
        "workflowConfigError": resp.get("workflowConfigError"),
        "validation": _maybe_validate(workflow_id, validate_after),
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
        return {
            "status": "completed",
            "prior_execution_id": prior,
            "total_entries": (preview or {}).get("meta", {}).get("total_entries"),
        }
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
        target["outputs"] = [{
            "columns": output_columns,
            "columns_metadata": [
                {"column_name": c, "data_type": d, "is_nullable": True}
                for c, d in zip(output_columns, output_dtypes)
            ],
            "file": "",
            "handle_condition": "_default",
            "node_id": node_id,
        }]

    resp = _put_workflow_blocks(workflow_id, wf)
    err = next((b.get("node_config_error") for b in resp.get("blocks", []) if b["id"] == node_id), None)
    return {
        "ok": err is None and not resp.get("workflowConfigError"),
        "node_id": node_id,
        "node_config_error": err,
        "workflowConfigError": resp.get("workflowConfigError"),
        "isRunable": resp.get("isRunable"),
        "lint_warnings": lint_warnings,
        "validation": _maybe_validate(workflow_id, validate_after),
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

    resp = _put_workflow_blocks(workflow_id, wf)
    err = next((b.get("node_config_error") for b in resp.get("blocks", []) if b["id"] == node_id), None)
    return {
        "ok": err is None and not resp.get("workflowConfigError"),
        "node_id": node_id,
        "field_path": top_path,
        "score": top_score,
        "node_config_error": err,
        "workflowConfigError": resp.get("workflowConfigError"),
        "validation": _maybe_validate(workflow_id, validate_after),
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

    resp = _put_workflow_blocks(workflow_id, wf)
    err = next((b.get("node_config_error") for b in resp.get("blocks", []) if b["id"] == node_id), None)
    return {
        "ok": err is None and not resp.get("workflowConfigError"),
        "node_id": node_id,
        "column_count": len(col_names),
        "columns": col_names,
        "node_config_error": err,
        "workflowConfigError": resp.get("workflowConfigError"),
        "validation": _maybe_validate(workflow_id, validate_after),
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

    return {
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
) -> dict:
    """Catalog of every node type the platform supports.

    Use this to discover typeIds before calling `attach_node`. Each result
    carries `node_definition_id` (the typeId you pass to attach_node), `name`,
    `category`, and `description`.

    Use search to filter by name ("Gmail", "Scheduler", "Magic Node").
    Use category to scope to one section (e.g. "Data Manipulation", "Gmail").

    Combined search+category narrows further.
    """
    raw = api.list_node_definitions(limit=limit, offset=offset,
                                     search=search, category=category)
    items = raw.get("data", []) if isinstance(raw, dict) else []
    meta = (raw.get("meta") or {}) if isinstance(raw, dict) else {}
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
def list_connections() -> dict:
    """List the user's authorized OAuth connections (Gmail, Sheets, Slack, ...).

    Use the returned `connection_id` when attaching app-backed nodes — those
    nodes typically require a `connectionId` in their settings to know which
    of your accounts to operate on.

    Returns: {count, connections: [{connection_id, app_name, connection_name,
    status, provider, created_at}]}.
    """
    raw = api.list_connections()
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
    return {"count": len(slim), "connections": slim}


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


# ═══════════════════════════════════════════════════════════════════════════
# Generic build (any node type) + workflow duplication — v0.2.5
# ═══════════════════════════════════════════════════════════════════════════

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
    is_trigger: bool = False,
    credit_cost_per_item: int = 0,
    validate_after: bool = True,
) -> dict:
    """Generic block-attach for ANY node type (Scheduler, Gmail, Calendar, AI, etc.).

    Use `list_node_definitions(search="...")` first to find the typeId for the
    node you want, plus understand its expected settings shape. Then construct
    the `settings` dict (mapping field_name → value) and pass it here.

    For app-backed nodes (Gmail send, Sheets read/write, Calendar list, etc.),
    use `list_connections()` to find the connection_id and include it in the
    settings as the platform expects (typically a field like
    `pipedream-<app>-<action>-connectionId`).

    `parent_node_ids` can be empty for trigger nodes (Scheduler etc.) — in
    that case set `is_trigger=True` and the new node will be wired as a root.

    `output_columns` is optional. Most app-backed nodes don't need explicit
    output schema (the platform fills it in from the node definition); supply
    it only when the platform validator complains.
    """
    if not type_id:
        raise ValueError("type_id is required (use list_node_definitions to find it)")
    if not isinstance(settings, dict):
        raise ValueError("settings must be a dict mapping field_name → value")

    wf = api.get_workflow(workflow_id)
    blocks_by_id = {b["id"]: b for b in wf["blocks"]}
    missing = [p for p in parent_node_ids if p not in blocks_by_id]
    if missing:
        raise ValueError(f"parent_node_ids not found in workflow {workflow_id}: {missing}")

    new_id = str(uuid.uuid4())
    type_slug = _typeid_to_value_slug(type_id)

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

    # Settings — wrap each field in the platform's _sf envelope
    settings_field_values = [_sf(name=k, value=v) for k, v in settings.items()]

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
        "isTrigger": is_trigger,
        "isOrphan": False,
        "isPartOfActiveSwimlane": True,
        "isListener": False,
        "isTestMode": False,
        "inputs": [{
            "columns": [], "columns_metadata": None, "file": "",
            "handle_condition": "_default", "node_id": None,
        }],
        "outputs": outputs,
        "toBlocks": [],
        "position": {"x": float(position_x), "y": float(position_y)},
        "creditCostPerItem": credit_cost_per_item,
        "column_operations": None,
        "node_config_error": None,
    }

    # Wire edges from each parent
    for pid in parent_node_ids:
        parent = blocks_by_id[pid]
        edges = parent.get("toBlocks") or []
        if not any(e.get("toBlockId") == new_id for e in edges):
            edges.append({
                "edgeId": f"{pid}-_default-{new_id}-_default",
                "edge_source_handle_condition": "_default",
                "edge_target_handle_condition": "_default",
                "toBlockId": new_id,
            })
        parent["toBlocks"] = edges

    all_blocks = wf["blocks"] + [new_block]
    payload = {"workflow_details": {
        "id": workflow_id,
        "name": wf.get("name"),
        "description": wf.get("description"),
        "blocks": all_blocks,
    }}
    resp = api.put_workflow(workflow_id, payload)

    err = next((b.get("node_config_error") for b in resp.get("blocks", []) if b["id"] == new_id), None)
    return {
        "ok": err is None,
        "node_id": new_id,
        "type_id": type_id,
        "name": name,
        "node_config_error": err,
        "workflowConfigError": resp.get("workflowConfigError"),
        "isRunable": resp.get("isRunable"),
        "validation": _maybe_validate(workflow_id, validate_after),
    }


@mcp.tool()
def paste_nodes(
    workflow_id: str,
    nodes: list[dict],
    validate_after: bool = True,
) -> dict:
    """Paste pre-made node specs into a workflow (mirrors the UI's drag-from-palette).

    For most use cases prefer `attach_node` — it's a higher-level wrapper that
    handles edge-wiring and position defaults. Use `paste_nodes` when you need
    the platform to auto-fill defaults from the node definition (e.g. for
    complex AI nodes with deeply-nested settings).

    `nodes` is a list of partial block dicts; the platform fills in defaults
    from each node's typeId. Each entry should contain at minimum:
        {"typeId": "<uuid>", "position": {"x": ..., "y": ...}}

    Optionally include "variableName", "settings_field_values" (to override
    defaults), and any other top-level block fields.

    NOTE: this does NOT auto-wire edges between the pasted nodes or to existing
    blocks. Call `add_edge` afterwards.
    """
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("nodes must be a non-empty list of block dicts")

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

    `new_name` defaults to "Copy of <original_name>".

    Returns the new workflow's id + name.
    """
    resp = api.duplicate_workflow(workflow_id, new_name=new_name)
    return {
        "ok": True,
        "source_workflow_id": workflow_id,
        "new_workflow_id": resp.get("id"),
        "new_workflow_name": resp.get("name"),
        "version": resp.get("version"),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Entrypoint
# ═══════════════════════════════════════════════════════════════════════════

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
