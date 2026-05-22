"""Tests for v0.2.11 single-input guard in attach_node.

The bug: pre-v0.2.11 attach_node silently accepted any number of
parent_node_ids and wired each as a `_default` edge. For Magic Node we use
df1..dfN handles (attach_magic_node, correct), but for everything else this
produces multiple `_default` edges into a single-input block — looks fine in
the UI, fails silently at execution.

v0.2.11 guard: attach_node raises ValueError when len(parent_node_ids) > 1
and allow_multi_input=False (the default). The caller is forced to either
switch to attach_magic_node or opt in explicitly via allow_multi_input.
"""
from unittest.mock import patch

import pytest

from nrev_wf_mcp.server import attach_node
from nrev_wf_mcp import block_types


def test_attach_node_refuses_two_parents_by_default():
    """The bug from the screenshot: passing 2 parents to a single-input
    node (HubSpot search, etc.) creates a silently-broken workflow."""
    HUBSPOT_SEARCH = "ad982760-6de8-4d5f-a14e-893bc4cd7cdd"  # any single-input typeId
    with pytest.raises(ValueError) as exc:
        attach_node(
            workflow_id="wf-1",
            parent_node_ids=["parent-a", "parent-b"],
            type_id=HUBSPOT_SEARCH,
            name="Search CRM",
            settings={},
        )
    msg = str(exc.value)
    assert "refuses to wire 2 parents" in msg
    assert "attach_magic_node" in msg  # points the caller at the right tool
    assert "allow_multi_input" in msg   # documents the escape hatch


def test_attach_node_with_magic_typeid_hints_at_attach_magic_node():
    """If the caller passes the Magic Node typeId to attach_node, the error
    message should specifically point them at attach_magic_node — Magic Node
    needs df1..dfN handles + a references list that attach_node won't set."""
    with pytest.raises(ValueError) as exc:
        attach_node(
            workflow_id="wf-1",
            parent_node_ids=["p1", "p2"],
            type_id=block_types.MAGIC_NODE,
            name="My Magic Node",
            settings={},
        )
    msg = str(exc.value)
    assert "attach_magic_node" in msg
    assert "df1..dfN" in msg


def test_attach_node_allows_zero_parents_for_triggers():
    """Trigger nodes (Scheduler etc.) take zero parents — guard must not
    interfere with that path."""
    SCHEDULER = "68da2fb4-8295-4568-9415-c47de58e6224"
    captured = {}

    def fake_paste(wf_id, body):
        captured["paste"] = body
        return {"blocks": body["nodes"], "isRunable": True, "workflowConfigError": None}

    with patch("nrev_wf_mcp.server.api.get_workflow") as mock_get, \
         patch("nrev_wf_mcp.server.api.paste_nodes", side_effect=fake_paste), \
         patch("nrev_wf_mcp.server.api.put_node"), \
         patch("nrev_wf_mcp.server._lookup_node_def_flags", return_value=(True, True)), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        mock_get.return_value = {"id": "wf-1", "name": "x", "description": "", "blocks": []}
        result = attach_node(
            workflow_id="wf-1",
            parent_node_ids=[],
            type_id=SCHEDULER,
            name="Scheduler",
            settings={"automation-scheduler-interval": "Days"},
            auto_resolve_labels=False,
        )
    assert result["ok"]


def test_attach_node_allows_one_parent_default_case():
    """The common case: app-backed node with exactly one upstream. Must work
    without any extra flags."""
    APP_NODE = "ad982760-6de8-4d5f-a14e-893bc4cd7cdd"
    captured = {}

    def fake_paste(wf_id, body):
        captured["paste"] = body
        return {"blocks": body["nodes"], "isRunable": True, "workflowConfigError": None}

    with patch("nrev_wf_mcp.server.api.get_workflow") as mock_get, \
         patch("nrev_wf_mcp.server.api.paste_nodes", side_effect=fake_paste), \
         patch("nrev_wf_mcp.server.api.put_node"), \
         patch("nrev_wf_mcp.server._lookup_node_def_flags", return_value=(False, False)), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        mock_get.return_value = {
            "id": "wf-1", "name": "x", "description": "",
            "blocks": [{"id": "parent-1", "position": {"x": 0, "y": 0}, "toBlocks": []}],
        }
        result = attach_node(
            workflow_id="wf-1",
            parent_node_ids=["parent-1"],
            type_id=APP_NODE,
            name="App node",
            settings={},
            auto_resolve_labels=False,
        )
    assert result["ok"]


def test_attach_node_allows_multi_input_with_explicit_opt_in():
    """Power-user escape hatch: legacy Merge block legitimately takes 2 inputs.
    With allow_multi_input=True the guard steps aside."""
    LEGACY_MERGE = "00000000-0000-0000-0000-merge0000000"  # placeholder typeId
    captured = {}

    def fake_paste(wf_id, body):
        captured["paste"] = body
        return {"blocks": body["nodes"], "isRunable": True, "workflowConfigError": None}

    def fake_put_node(wf_id, node_id, node_patch):
        captured.setdefault("put_node", []).append((node_id, node_patch))
        return node_patch

    with patch("nrev_wf_mcp.server.api.get_workflow") as mock_get, \
         patch("nrev_wf_mcp.server.api.paste_nodes", side_effect=fake_paste), \
         patch("nrev_wf_mcp.server.api.put_node", side_effect=fake_put_node), \
         patch("nrev_wf_mcp.server._lookup_node_def_flags", return_value=(False, False)), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        mock_get.return_value = {
            "id": "wf-1", "name": "x", "description": "",
            "blocks": [
                {"id": "parent-a", "position": {"x": 0, "y": 0}, "toBlocks": []},
                {"id": "parent-b", "position": {"x": 100, "y": 100}, "toBlocks": []},
            ],
        }
        result = attach_node(
            workflow_id="wf-1",
            parent_node_ids=["parent-a", "parent-b"],
            type_id=LEGACY_MERGE,
            name="Legacy Merge",
            settings={},
            allow_multi_input=True,  # opt in explicitly
            auto_resolve_labels=False,
        )
    assert result["ok"]
    # Both parents got an edge-wiring put_node call
    assert len(captured["put_node"]) == 2
