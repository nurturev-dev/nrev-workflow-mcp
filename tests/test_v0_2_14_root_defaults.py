"""Tests for v0.2.14 root-block default flag behavior.

Background:

The platform models two distinct flags on each block:
  isTrigger  = "this block is a START NODE" (swimlane entry point).
               Every workflow needs at least one. Multiple allowed.
  isListener = "this block is the workflow's automation trigger"
               (polls/subscribes so the workflow runs on its own).
               Only ONE per workflow.

v0.2.13 conflated these (auto-resolve set both together from the catalog).
v0.2.14 separates them:
  - parent-empty root → isTrigger=True ALWAYS (every root is a start node).
  - isListener auto-detected from catalog (Scheduler/Gmail/Sheets read
    → True if listener-capable; CC / plain transforms → False).
  - explicit overrides always win.

These tests pin the four NEW behavioral branches that didn't exist in
v0.2.13. The pre-existing tests in test_trigger_autodetect.py and
test_v0_2_13_fixes.py still pass unchanged — they cover the parent-
present branch (both False), the catalog-trigger-+-listener-capable
case (Scheduler → both True), and the explicit-override case.
"""
from unittest.mock import patch

from nrev_wf_mcp.server import attach_node, _lookup_node_def_flags


SCHEDULER_TYPE_ID = "68da2fb4-8295-4568-9415-c47de58e6224"  # is_trigger=true, is_listener=true
CUSTOM_CODE_TYPE_ID = "ae54c44f-60ee-47c4-91d7-eae7fa849133"  # is_trigger=false, is_listener=false


def _mock_paste_capture(captured: dict):
    def fake_paste(wf_id, body):
        captured.setdefault("paste_bodies", []).append(body)
        return {"blocks": body["nodes"], "isRunable": True, "workflowConfigError": None}
    return fake_paste


def test_custom_code_root_with_no_parents_becomes_start_node():
    """Headline v0.2.14 fix. A plain Custom Code attached as a workflow
    root MUST become a start node (isTrigger=True), even though the
    catalog says is_trigger=false. Otherwise the workflow would fail
    with 'no start nodes' on every save.

    isListener stays False because Custom Code isn't listener-capable
    in the catalog.

    Pre-v0.2.14: both flags False (caller had to remember to mark the
    root as a start). Now: isTrigger=True, isListener=False — the agent
    can build a one-off Custom Code workflow without ceremony.
    """
    _lookup_node_def_flags.cache_clear()
    captured: dict = {}

    with patch("nrev_wf_mcp.server.api.get_workflow") as mock_get, \
         patch("nrev_wf_mcp.server.api.paste_nodes", side_effect=_mock_paste_capture(captured)), \
         patch("nrev_wf_mcp.server.api.put_node"), \
         patch("nrev_wf_mcp.server.api.list_node_definitions") as mock_list, \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        mock_get.return_value = {"id": "wf-1", "name": "x", "description": "", "blocks": []}
        mock_list.return_value = {"data": [{
            "node_definition_id": CUSTOM_CODE_TYPE_ID,
            "is_trigger": False,   # catalog says CC is NOT a trigger
            "isListener": False,   # ...and NOT a listener
        }]}
        result = attach_node(
            workflow_id="wf-1",
            parent_node_ids=[],
            type_id=CUSTOM_CODE_TYPE_ID,
            name="CC root",
            settings={"data_manipulation-custom_code-code": "def run(df1):\n    return df1\n"},
            auto_resolve_labels=False,
        )

    assert result["is_trigger"] is True, "every parent-empty root must be a start node"
    assert result["is_listener"] is False, "CC isn't listener-capable in the catalog"
    pasted = captured["paste_bodies"][0]["nodes"][0]
    assert pasted["isTrigger"] is True
    assert pasted["isListener"] is False


def test_scheduler_root_with_explicit_is_listener_false_stays_non_listener():
    """The 'one-off run of an otherwise-pollable type' override. A
    Scheduler attached as a root with is_listener=False explicitly
    should produce: isTrigger=True (still a start node), isListener=False
    (no live polling). Useful when the user wants to manually run a
    Scheduler-rooted workflow once without scheduling it.
    """
    _lookup_node_def_flags.cache_clear()
    captured: dict = {}

    with patch("nrev_wf_mcp.server.api.get_workflow") as mock_get, \
         patch("nrev_wf_mcp.server.api.paste_nodes", side_effect=_mock_paste_capture(captured)), \
         patch("nrev_wf_mcp.server.api.put_node"), \
         patch("nrev_wf_mcp.server.api.list_node_definitions") as mock_list, \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        mock_get.return_value = {"id": "wf-1", "name": "x", "description": "", "blocks": []}
        mock_list.return_value = {"data": [{
            "node_definition_id": SCHEDULER_TYPE_ID,
            "is_trigger": True,
            "isListener": True,
        }]}
        result = attach_node(
            workflow_id="wf-1",
            parent_node_ids=[],
            type_id=SCHEDULER_TYPE_ID,
            name="One-off Scheduler",
            settings={"automation-scheduler-interval": "Days"},
            is_listener=False,   # ← explicit override: don't poll
            auto_resolve_labels=False,
        )

    assert result["is_trigger"] is True
    assert result["is_listener"] is False, "explicit override must beat catalog auto-detect"


def test_explicit_is_trigger_false_on_root_stays_false():
    """A caller who explicitly passes is_trigger=False on a parent-empty
    block is choosing to create an invalid workflow (no start node) —
    presumably because they'll add a start node next. Don't override
    them; they know what they're doing.
    """
    _lookup_node_def_flags.cache_clear()
    captured: dict = {}

    with patch("nrev_wf_mcp.server.api.get_workflow") as mock_get, \
         patch("nrev_wf_mcp.server.api.paste_nodes", side_effect=_mock_paste_capture(captured)), \
         patch("nrev_wf_mcp.server.api.put_node"), \
         patch("nrev_wf_mcp.server.api.list_node_definitions") as mock_list, \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        mock_get.return_value = {"id": "wf-1", "name": "x", "description": "", "blocks": []}
        mock_list.return_value = {"data": [{
            "node_definition_id": CUSTOM_CODE_TYPE_ID,
            "is_trigger": False, "isListener": False,
        }]}
        result = attach_node(
            workflow_id="wf-1",
            parent_node_ids=[],
            type_id=CUSTOM_CODE_TYPE_ID,
            name="Deliberate non-start root",
            settings={"data_manipulation-custom_code-code": "def run(df1):\n    return df1\n"},
            is_trigger=False,   # ← explicit override
            auto_resolve_labels=False,
        )

    assert result["is_trigger"] is False
    assert result["is_listener"] is False


def test_multiple_start_node_roots_can_be_created():
    """The platform allows multiple start nodes (each begins its own
    swimlane). This test pins that the wrapper doesn't add a hidden
    'only one start' guard — two separate attach_node calls with empty
    parents should each produce isTrigger=True.

    This is the swimlane case the independent reviewer flagged and the
    user later confirmed: 'a workflow can have multiple start nodes for
    sure.'
    """
    _lookup_node_def_flags.cache_clear()
    captured: dict = {}

    with patch("nrev_wf_mcp.server.api.get_workflow") as mock_get, \
         patch("nrev_wf_mcp.server.api.paste_nodes", side_effect=_mock_paste_capture(captured)), \
         patch("nrev_wf_mcp.server.api.put_node"), \
         patch("nrev_wf_mcp.server.api.list_node_definitions") as mock_list, \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        # Simulate one existing start node already in the workflow.
        existing_start = {
            "id": "existing-start", "typeId": CUSTOM_CODE_TYPE_ID,
            "variableName": "Existing Start", "position": {"x": 0, "y": 0},
            "isTrigger": True, "isListener": False, "toBlocks": [],
        }
        mock_get.return_value = {
            "id": "wf-1", "name": "x", "description": "",
            "blocks": [existing_start],
        }
        mock_list.return_value = {"data": [{
            "node_definition_id": CUSTOM_CODE_TYPE_ID,
            "is_trigger": False, "isListener": False,
        }]}
        # Add a SECOND start node with no parents.
        result = attach_node(
            workflow_id="wf-1",
            parent_node_ids=[],   # also empty → also a start
            type_id=CUSTOM_CODE_TYPE_ID,
            name="Second Start",
            settings={"data_manipulation-custom_code-code": "def run(df1):\n    return df1\n"},
            auto_resolve_labels=False,
        )

    assert result["is_trigger"] is True, "wrapper must allow multiple start nodes"
    assert result["is_listener"] is False, "CC isn't listener-capable"
