"""Thin HTTP wrapper around the nRev workflow API.

Why this exists:
- One place to inject the JWT header, so tools don't each rebuild it.
- One place to handle the `{"node": {...}}` body wrapping required by `PUT /nodes/{id}`.
- One place to surface API errors as a typed exception with the URL + body.

Host is configurable via `NREV_WF_HOST` env var; defaults to prod.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx

from .auth import get_jwt


DEFAULT_HOST = os.environ.get("NREV_WF_HOST", "https://workflow.public.prod.nurturev.com")
TIMEOUT_SECONDS = float(os.environ.get("NREV_WF_TIMEOUT", "60"))


class WorkflowAPIError(RuntimeError):
    def __init__(self, status_code: int, body: str, url: str):
        super().__init__(f"HTTP {status_code} from {url}: {body[:500]}")
        self.status_code = status_code
        self.body = body
        self.url = url


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=DEFAULT_HOST,
        headers={
            "Authorization": f"Bearer {get_jwt()}",
            "Content-Type": "application/json",
        },
        timeout=httpx.Timeout(TIMEOUT_SECONDS),
    )


def request(
    method: str,
    path: str,
    json_body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> Any:
    with _client() as c:
        r = c.request(method, path, json=json_body, params=params)
        if r.status_code >= 400:
            raise WorkflowAPIError(r.status_code, r.text, str(r.request.url))
        if not r.content:
            return None
        try:
            return r.json()
        except Exception:
            return r.text


# ─── Convenience wrappers ────────────────────────────────────────────────────


def get_workflow(wf_id: str) -> dict:
    return request("GET", f"/workflows/{wf_id}")


def list_workflows(limit: int = 20, offset: int = 0, search: Optional[str] = None) -> dict:
    """GET /workflows — paginated list with optional name-substring search.

    Returns {data: [{id, name, description, updatedAt, lastRunAt, version, ...}], meta}.

    NOTE: the platform's filter param is `name` (substring match on workflow
    name), not `search`. We accept `search` in the wrapper for callsite
    intuitiveness and translate it to `name`.
    """
    params: dict = {"limit": int(limit), "skip": int(offset)}
    if search:
        params["name"] = search
    return request("GET", "/workflows", params=params)


def duplicate_workflow(wf_id: str, new_name: Optional[str] = None) -> dict:
    """POST /workflows/{id}/duplicate — clone an entire workflow.

    Returns the new workflow object. If `new_name` is provided, the duplicate
    is named accordingly; otherwise the platform auto-generates "Copy of X".
    """
    body = {"name": new_name} if new_name else {}
    return request("POST", f"/workflows/{wf_id}/duplicate", json_body=body)


def paste_nodes(wf_id: str, payload: dict) -> dict:
    """POST /workflows/{id}/paste-nodes — paste pre-made node definitions.

    Mirrors what the web UI does when you drag a node from the palette. The
    payload shape varies by node type — typically a list of partially-built
    block dicts with at minimum {typeId, position}; the platform fills in
    default settings from the node definition.
    """
    return request("POST", f"/workflows/{wf_id}/paste-nodes", json_body=payload)


def list_node_definitions(
    limit: int = 50,
    offset: int = 0,
    search: Optional[str] = None,
    category: Optional[str] = None,
) -> dict:
    """GET /node_definitions — paginated catalog of all node types.

    `limit` is clamped to 100 (platform returns 422 above).

    Returns {data: [{node_definition_id, value, name, category, description,
    is_trigger, ...}], meta}. The `node_definition_id` IS the typeId used in
    block.typeId throughout the platform.
    """
    params: dict = {"limit": max(1, min(int(limit), 100)), "skip": max(0, int(offset))}
    if search:
        params["search"] = search
    if category:
        params["category"] = category
    return request("GET", "/node_definitions", params=params)


def list_node_definition_categories(limit: int = 100) -> dict:
    """GET /node_definitions/categories — all categories the platform groups
    nodes by (Data Manipulation, Gmail, Google Sheets, etc.).
    """
    return request("GET", "/node_definitions/categories", params={"limit": int(limit)})


def list_connections() -> list:
    """GET /connections — user's authorized OAuth connections.

    Returns a flat list (not wrapped in `data`/`meta`) of objects with
    `connectionId`, `appName`, `connectionName`, `status`, etc. Needed when
    attaching app-backed nodes (Gmail, Sheets, Calendar) — those nodes
    require a `connectionId` in their settings.
    """
    return request("GET", "/connections")


def field_options(
    node_id: str,
    node_definition_id: str,
    field_name: str,
    settings: list[dict],
    search: Optional[str] = None,
) -> dict:
    """POST /nodes/field-options — fetch dropdown options for one field.

    This is what the platform's UI calls when populating dropdowns. For
    cascading dropdowns (like worksheetId depending on sheetId), include
    the prerequisite settings in `settings` so the platform can resolve.

    Returns {options: [{label, value}], nodeId, fieldName, errors, search, context}.

    NOTE: `nodeId` is for logging only — can be any UUID, no need to point
    at a real existing node. Use the about-to-be-created node's UUID for
    fresh attach flows.
    """
    body = {
        "nodeId": node_id,
        "nodeDefinitionId": node_definition_id,
        "fieldName": field_name,
        "settings": settings or [],
    }
    if search is not None:
        body["search"] = search
    return request("POST", "/nodes/field-options", json_body=body)


def list_connection_apps(
    limit: int = 50,
    offset: int = 0,
    category: Optional[str] = None,
    search: Optional[str] = None,
) -> dict:
    """GET /connections/apps — catalog of apps that CAN be connected.

    Use this when the user wants to add an app integration but hasn't yet
    connected the underlying account. Returns {data, meta} with each item
    carrying `connectionAppId`, `name`, `category`, `iconUrl`, `provider`.
    """
    params: dict = {"limit": int(limit), "skip": int(offset)}
    if category:
        params["category"] = category
    if search:
        params["search"] = search
    return request("GET", "/connections/apps", params=params)


def put_workflow(wf_id: str, payload: dict) -> dict:
    """PUT /workflows/{wf} — payload must already be wrapped in {'workflow_details': {...}}."""
    return request("PUT", f"/workflows/{wf_id}", json_body=payload)


def put_node(wf_id: str, node_id: str, node_patch: dict) -> dict:
    """PUT /workflows/{wf}/nodes/{id} — body MUST be wrapped in {'node': {...}}.

    Sending a raw block returns 422 'Field required: body.node'. This wrapper
    handles the wrapping so callers don't have to remember.
    """
    return request("PUT", f"/workflows/{wf_id}/nodes/{node_id}", json_body={"node": node_patch})


def execute_node(
    wf_id: str,
    node_id: str,
    prior_execution_id: Optional[str] = None,
) -> dict:
    """Execute a single node, optionally reusing cached upstream from a prior execution.

    Endpoint pattern (verified per NREV_WORKFLOW_GUIDE §7):
      POST /executions/workflow/{wf}/node/{node}/execute

    With body `{"workflowExecutionId": "<prev>"}` it reuses cached upstream from a
    prior execution and only re-runs from the changed node forward.
    """
    body: dict = {}
    if prior_execution_id:
        body["workflowExecutionId"] = prior_execution_id
    return request(
        "POST",
        f"/executions/workflow/{wf_id}/node/{node_id}/execute",
        json_body=body,
    )


def create_workflow(name: str, description: str = "") -> dict:
    """POST /workflows — creates an empty workflow with no blocks.

    Returns the new workflow object including its assigned `id`.

    NOTE: POST /workflows requires the body wrapped in {"workflow_details": {...}}
    same as PUT — earlier doc claims this was unwrapped were wrong.
    """
    return request("POST", "/workflows", json_body={
        "workflow_details": {
            "name": name,
            "description": description,
            "blocks": [],
        }
    })


def list_executions(wf_id: str, limit: int = 10) -> dict:
    return request("GET", f"/execution-logs/workflow/{wf_id}", params={"limit": limit})


def abort_execution(wf_id: str, exec_id: str) -> dict:
    """POST /executions/workflow/{wf}/workflow-execution/{exec}/abort

    NOTE: endpoint shape may need adjustment if the platform uses a different
    route. If this 404s, check the network tab in app.nrev.ai when clicking
    the stop button.
    """
    return request(
        "POST",
        f"/executions/workflow/{wf_id}/workflow-execution/{exec_id}/abort",
    )


def get_execution_detail(wf_id: str, exec_id: str) -> dict:
    return request(
        "GET",
        f"/execution-logs/workflow/{wf_id}/workflow-execution/{exec_id}",
    )


def get_node_preview(
    wf_id: str,
    exec_id: str,
    node_id: str,
    handle_condition: str = "_default",
    skip: int = 0,
    limit: int = 50,
) -> dict:
    """GET node-output preview for a specific past execution.

    Per NREV_WORKFLOW_GUIDE §8: max `limit` is 100 — passing higher silently
    returns 0 rows. We clamp to keep callers safe.
    """
    limit = max(1, min(int(limit), 100))
    skip = max(0, int(skip))
    return request(
        "GET",
        f"/executions/workflow/{wf_id}/workflow-execution/{exec_id}/node/{node_id}/preview",
        params={"handle_condition": handle_condition, "skip": skip, "limit": limit},
    )


def credit_balance(tenant_id: int = 0) -> int:
    """GET /credit-management/tenant/{tenant_id}/balance — returns the credit
    balance as a plain integer.

    NOTE: the `tenant_id` in the path is IGNORED by the server. The tenant is
    resolved from the JWT, so passing 0 works for any caller. We accept the
    arg only for documentation symmetry with the platform's path shape.
    """
    return request("GET", f"/credit-management/tenant/{int(tenant_id)}/balance")


def publish_workflow(wf_id: str, toggle_live: bool = True) -> dict:
    """POST /live/workflow/{wf}/publish — promote a workflow to live (or
    take it off live by passing toggle_live=False).

    Returns either a queued-request envelope (publishing is async on the
    platform side) OR a completed publish-response, depending on whether
    the platform short-circuits. Either shape is opaque to us; the caller
    can poll `get_publish_status` if they want to wait.
    """
    return request(
        "POST",
        f"/live/workflow/{wf_id}/publish",
        json_body={"toggleLive": bool(toggle_live)},
    )


def get_publish_status(wf_id: str) -> dict:
    """GET /live/workflow/{wf}/publish/status — current publish state.

    Useful after `publish_workflow` to know whether the live version is
    actually serving requests yet (publishes can be queued for a few
    seconds).
    """
    return request("GET", f"/live/workflow/{wf_id}/publish/status")


def delete_workflow(wf_id: str) -> Any:
    """DELETE /workflows/{wf} — remove the workflow entirely.

    Returns whatever the platform sends back (typically empty body / null).
    The HTTP 200 is the success signal — the wrapper raises on non-2xx via
    WorkflowAPIError.
    """
    return request("DELETE", f"/workflows/{wf_id}")


def patch_workflow_no_validation(
    wf_id: str,
    *,
    name: Optional[str] = None,
    sticky_notes: Optional[list[dict]] = None,
) -> dict:
    """PATCH /workflows/{wf}/no-validation — update workflow-level metadata
    (name, sticky notes) without triggering full workflow validation.

    Two gotchas the OpenAPI doesn't warn about:
      1. The body field is `stickyNotes` (camelCase), NOT `sticky_notes` despite
         what the published schema says. We translate here.
      2. The server requires at least one of `name`/`stickyNotes` to be set —
         calling with both None returns HTTP 400.

    `sticky_notes` REPLACES the entire array (it's a set, not an append). To
    add or update a single note, fetch the workflow first, mutate the list,
    then pass the full new list.
    """
    body: dict = {}
    if name is not None:
        body["name"] = name
    if sticky_notes is not None:
        body["stickyNotes"] = sticky_notes
    if not body:
        raise ValueError("must pass at least one of name / sticky_notes")
    return request("PATCH", f"/workflows/{wf_id}/no-validation", json_body=body)
