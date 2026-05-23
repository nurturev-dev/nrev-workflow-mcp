"""Tests for v0.2.15 — four small wins.

Covers:
  - get_workflow surfaces per-block isTrigger/isListener AND workflow-level
    status/liveVersion/playVersion
  - publish_workflow / get_publish_status thin wrappers
  - delete_workflow with confirm guard (defaults to refuse)
  - paste_nodes single-input hardening (the 5th leak v0.2.13 review surfaced)
"""
from unittest.mock import patch

import pytest

from nrev_wf_mcp.server import (
    get_workflow,
    publish_workflow,
    get_publish_status,
    delete_workflow,
    paste_nodes,
)


# ════════════════════════════════════════════════════════════════════════
# get_workflow slim view exposes flag fields
# ════════════════════════════════════════════════════════════════════════


def test_get_workflow_surfaces_per_block_trigger_and_listener_flags():
    """Audit aid (v0.2.15): so an agent can verify the v0.2.13/.14 fixes
    worked without calling get_node per block."""
    fake_wf = {
        "id": "wf-1", "name": "x",
        "status": "draft", "liveVersion": None, "playVersion": None,
        "isRunable": True, "isTestMode": False, "workflowConfigError": None,
        "blocks": [
            {"id": "scheduler", "variableName": "Scheduler",
             "typeId": "t1", "position": {"x": 0, "y": 0},
             "isTestMode": False, "isTrigger": True, "isListener": True,
             "node_config_error": None, "toBlocks": []},
            {"id": "downstream", "variableName": "Gmail downstream",
             "typeId": "t2", "position": {"x": 400, "y": 0},
             "isTestMode": False, "isTrigger": False, "isListener": False,
             "node_config_error": None, "toBlocks": []},
        ],
    }
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf):
        result = get_workflow("wf-1")

    assert result["block_count"] == 2
    sched = next(b for b in result["blocks"] if b["id"] == "scheduler")
    downstream = next(b for b in result["blocks"] if b["id"] == "downstream")
    assert sched["isTrigger"] is True
    assert sched["isListener"] is True
    assert downstream["isTrigger"] is False
    assert downstream["isListener"] is False


def test_get_workflow_surfaces_workflow_level_status_fields():
    """Agent can tell at a glance whether a workflow is draft / live and
    which version is currently serving."""
    fake_wf = {
        "id": "wf-1", "name": "x",
        "status": "live", "liveVersion": 5, "playVersion": 3,
        "isRunable": True, "isTestMode": False, "workflowConfigError": None,
        "blocks": [],
    }
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf):
        result = get_workflow("wf-1")

    assert result["status"] == "live"
    assert result["liveVersion"] == 5
    assert result["playVersion"] == 3


def test_get_workflow_handles_missing_status_fields_gracefully():
    """Older platform responses may not include the new fields. Should not
    crash — surfaces None for whatever's missing."""
    fake_wf = {
        "id": "wf-1", "name": "x", "isRunable": True, "blocks": [],
    }
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf):
        result = get_workflow("wf-1")
    assert result["status"] is None
    assert result["liveVersion"] is None
    assert result["playVersion"] is None


# ════════════════════════════════════════════════════════════════════════
# publish_workflow + get_publish_status
# ════════════════════════════════════════════════════════════════════════


def test_publish_workflow_calls_client_with_toggle_live_true_by_default():
    captured = {}

    def fake_publish(wf_id, toggle_live):
        captured["wf_id"] = wf_id
        captured["toggle_live"] = toggle_live
        return {"status": "queued", "request_id": "abc"}

    with patch("nrev_wf_mcp.server.api.publish_workflow", side_effect=fake_publish):
        result = publish_workflow("wf-1")

    assert captured["wf_id"] == "wf-1"
    assert captured["toggle_live"] is True
    assert result["ok"] is True
    assert result["toggle_live"] is True
    assert result["response"] == {"status": "queued", "request_id": "abc"}
    assert "get_publish_status" in result["note"]


def test_publish_workflow_can_take_workflow_off_live():
    captured = {}

    def fake_publish(wf_id, toggle_live):
        captured["toggle_live"] = toggle_live
        return {}

    with patch("nrev_wf_mcp.server.api.publish_workflow", side_effect=fake_publish):
        result = publish_workflow("wf-1", toggle_live=False)
    assert captured["toggle_live"] is False
    assert result["toggle_live"] is False


def test_get_publish_status_returns_platform_response_verbatim():
    with patch("nrev_wf_mcp.server.api.get_publish_status",
               return_value={"status": "live", "liveVersion": 5}):
        result = get_publish_status("wf-1")
    assert result == {"status": "live", "liveVersion": 5}


# ════════════════════════════════════════════════════════════════════════
# delete_workflow with confirm guard
# ════════════════════════════════════════════════════════════════════════


def test_delete_workflow_refuses_without_confirm():
    """Default behavior: refuse + return a clear refusal. No API call made.
    Critical safety guard — agents iterating shouldn't blow away workflows
    accidentally."""
    with patch("nrev_wf_mcp.server.api.delete_workflow") as mock_delete:
        result = delete_workflow("wf-1")

    mock_delete.assert_not_called()
    assert result["ok"] is False
    assert result["deleted_workflow_id"] is None
    assert "confirm=True" in result["message"]
    assert "duplicate_workflow" in result["message"]


def test_delete_workflow_proceeds_with_confirm_true():
    with patch("nrev_wf_mcp.server.api.delete_workflow",
               return_value=None) as mock_delete:
        result = delete_workflow("wf-1", confirm=True)

    mock_delete.assert_called_once_with("wf-1")
    assert result["ok"] is True
    assert result["deleted_workflow_id"] == "wf-1"


def test_delete_workflow_surfaces_api_error_without_crashing():
    """Network blip / 404 / etc. → return ok=False with the error message
    rather than propagating an exception."""
    with patch("nrev_wf_mcp.server.api.delete_workflow",
               side_effect=Exception("HTTP 404: not found")):
        result = delete_workflow("wf-1", confirm=True)

    assert result["ok"] is False
    assert "404" in result["error"]


# ════════════════════════════════════════════════════════════════════════
# paste_nodes single-input hardening
# ════════════════════════════════════════════════════════════════════════


def _node(node_id, *, toBlocks=None):
    """Minimal node-spec for paste_nodes tests."""
    return {
        "id": node_id, "typeId": "t1",
        "position": {"x": 0, "y": 0},
        "toBlocks": toBlocks or [],
    }


def _default_edge(to_id, *, handle="_default"):
    return {
        "toBlockId": to_id,
        "edge_source_handle_condition": "_default",
        "edge_target_handle_condition": handle,
    }


def test_paste_nodes_refuses_when_pasted_blocks_double_target_existing_node():
    """A pasted block whose toBlocks point to an existing target that
    already has a _default incoming edge → refuse."""
    existing = {
        "id": "existing-src",
        "toBlocks": [_default_edge("existing-target")],
    }
    fake_wf = {"id": "wf-1", "blocks": [existing, {"id": "existing-target", "toBlocks": []}]}
    pasted = _node("p1", toBlocks=[_default_edge("existing-target")])

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.paste_nodes") as mock_paste:
        with pytest.raises(ValueError) as exc:
            paste_nodes(workflow_id="wf-1", nodes=[pasted])

    mock_paste.assert_not_called()  # pre-flight refusal — no API call
    msg = str(exc.value)
    assert "existing-target" in msg
    assert "existing-src" in msg
    assert "df1..df5" in msg or "Magic Node" in msg


def test_paste_nodes_refuses_when_two_pasted_blocks_both_target_same_node():
    """Two pasted blocks both have toBlocks pointing to the same _default
    target → refuse (even without any existing edge into that target)."""
    pasted_target = _node("target")
    pasted_a = _node("pasted-a", toBlocks=[_default_edge("target")])
    pasted_b = _node("pasted-b", toBlocks=[_default_edge("target")])
    fake_wf = {"id": "wf-1", "blocks": []}

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.paste_nodes") as mock_paste:
        with pytest.raises(ValueError) as exc:
            paste_nodes(workflow_id="wf-1", nodes=[pasted_target, pasted_a, pasted_b])

    mock_paste.assert_not_called()
    msg = str(exc.value)
    assert "target" in msg


def test_paste_nodes_allows_magic_node_dfN_fan_in():
    """The guard must exempt df1..df5 (Magic Node fan-in)."""
    pasted_magic = _node("magic")
    src_a = _node("src-a", toBlocks=[_default_edge("magic", handle="df1")])
    src_b = _node("src-b", toBlocks=[_default_edge("magic", handle="df2")])
    fake_wf = {"id": "wf-1", "blocks": []}

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.paste_nodes",
               return_value={"blocks": [pasted_magic, src_a, src_b]}), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        result = paste_nodes(workflow_id="wf-1", nodes=[pasted_magic, src_a, src_b])

    assert result["ok"] is True


def test_paste_nodes_allows_single_default_edge():
    """One block targeting one node on _default = normal case, not a violation."""
    pasted = [
        _node("source", toBlocks=[_default_edge("target")]),
        _node("target"),
    ]
    fake_wf = {"id": "wf-1", "blocks": []}

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.paste_nodes",
               return_value={"blocks": pasted}), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        result = paste_nodes(workflow_id="wf-1", nodes=pasted)
    assert result["ok"] is True


def test_paste_nodes_allow_multi_input_skips_guard():
    """Escape hatch for legacy Merge: allow_multi_input=True bypasses the
    guard entirely and doesn't even fetch the workflow for pre-flight."""
    pasted = [
        _node("a", toBlocks=[_default_edge("target")]),
        _node("b", toBlocks=[_default_edge("target")]),
        _node("target"),
    ]

    with patch("nrev_wf_mcp.server.api.get_workflow") as mock_get, \
         patch("nrev_wf_mcp.server.api.paste_nodes",
               return_value={"blocks": pasted}), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        result = paste_nodes(workflow_id="wf-1", nodes=pasted, allow_multi_input=True)

    mock_get.assert_not_called()  # guard skipped entirely → no pre-flight GET
    assert result["ok"] is True
