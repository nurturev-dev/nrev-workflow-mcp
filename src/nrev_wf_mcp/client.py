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
    """
    return request("POST", "/workflows", json_body={
        "name": name,
        "description": description,
        "blocks": [],
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
