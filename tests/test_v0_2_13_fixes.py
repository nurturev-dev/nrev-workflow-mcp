"""Tests for the v0.2.13 hotfix bundle.

Covers:
  Fix #1 — attach_node auto-trigger respects parent_node_ids
  Fix #3 — add_edge single-input guard
  Fix #4 — splice_branch single-input guard (when no replace target)
  Fix #5 — clone_node strips isTrigger/isListener
  Fix #7 — partial_execute surfaces non-2xx platform errors with hint
"""
from unittest.mock import patch

import pytest

from nrev_wf_mcp.server import (
    attach_node,
    add_edge,
    splice_branch,
    clone_node,
    partial_execute,
    _lookup_node_def_flags,
    _find_existing_default_incoming,
    _MAGIC_NODE_FAN_IN_HANDLES,
)


# Real-ish typeIds used in fixtures.
SCHEDULER_TYPE_ID = "68da2fb4-8295-4568-9415-c47de58e6224"  # is_trigger=true, is_listener=true
SHEETS_READ_TYPE_ID = "abcd1111-2222-3333-4444-555566667777"  # trigger-capable (poll-style)
ADD_ROW_TYPE_ID = "191db4a1-7c72-4c4a-af02-b507701ca61b"  # plain single-input


# ════════════════════════════════════════════════════════════════════════
# Fix #1 — attach_node auto-trigger ignores trigger-capable typeId when
# parent_node_ids is non-empty. This is the regression that produced
# orphan triggers in colleagues' workflows.
# ════════════════════════════════════════════════════════════════════════


def _mock_paste_capture(captured: dict):
    def fake_paste(wf_id, body):
        captured.setdefault("paste_bodies", []).append(body)
        return {"blocks": body["nodes"], "isRunable": True, "workflowConfigError": None}
    return fake_paste


def _mock_put_node_capture(captured: dict):
    def fake_put_node(wf_id, node_id, node_patch):
        captured.setdefault("put_node_calls", []).append((node_id, node_patch))
        return node_patch
    return fake_put_node


def test_attach_node_trigger_capable_type_attached_downstream_is_NOT_a_trigger():
    """The bug: pre-v0.2.13, attaching a trigger-capable type (Sheets Read
    Output Tab, Gmail New Email, etc.) downstream of an existing block
    silently set isTrigger=True. Result: orphan trigger sibling, broken
    workflow. After fix #1, parents-present always means non-trigger."""
    _lookup_node_def_flags.cache_clear()
    captured: dict = {}

    with patch("nrev_wf_mcp.server.api.get_workflow") as mock_get, \
         patch("nrev_wf_mcp.server.api.paste_nodes", side_effect=_mock_paste_capture(captured)), \
         patch("nrev_wf_mcp.server.api.put_node", side_effect=_mock_put_node_capture(captured)), \
         patch("nrev_wf_mcp.server.api.list_node_definitions") as mock_list, \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        mock_get.return_value = {
            "id": "wf-1", "name": "x", "description": "",
            "blocks": [{"id": "parent-1", "position": {"x": 0, "y": 0}, "toBlocks": []}],
        }
        # Simulate a trigger-capable node-def (e.g. Sheets Read Output Tab)
        mock_list.return_value = {"data": [{
            "node_definition_id": SHEETS_READ_TYPE_ID,
            "is_trigger": True,    # platform CAN use this as a trigger
            "isListener": True,    # ...and as a listener
        }]}
        result = attach_node(
            workflow_id="wf-1",
            parent_node_ids=["parent-1"],   # has a parent → NOT a trigger
            type_id=SHEETS_READ_TYPE_ID,
            name="Sheets read (downstream use)",
            settings={},
            auto_resolve_labels=False,
        )

    # Critical: even though the node-def says trigger-capable, the block
    # has parents, so it must NOT be marked as a trigger.
    assert result["is_trigger"] is False
    assert result["is_listener"] is False
    pasted = captured["paste_bodies"][0]["nodes"][0]
    assert pasted["isTrigger"] is False
    assert pasted["isListener"] is False


def test_attach_node_trigger_capable_type_with_no_parents_is_still_a_trigger():
    """Regression guard: don't accidentally break the original v0.2.7 use
    case. Scheduler with zero parents is the canonical workflow trigger."""
    _lookup_node_def_flags.cache_clear()
    captured: dict = {}

    with patch("nrev_wf_mcp.server.api.get_workflow") as mock_get, \
         patch("nrev_wf_mcp.server.api.paste_nodes", side_effect=_mock_paste_capture(captured)), \
         patch("nrev_wf_mcp.server.api.put_node", side_effect=_mock_put_node_capture(captured)), \
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
            parent_node_ids=[],   # no parents → trigger lookup runs
            type_id=SCHEDULER_TYPE_ID,
            name="Scheduler",
            settings={"automation-scheduler-interval": "Days"},
            auto_resolve_labels=False,
        )

    assert result["is_trigger"] is True
    assert result["is_listener"] is True


# ════════════════════════════════════════════════════════════════════════
# Fix #3 — add_edge single-input guard
# ════════════════════════════════════════════════════════════════════════


def _wf_with_blocks(blocks: list[dict]) -> dict:
    return {"id": "wf-1", "name": "x", "description": "", "blocks": blocks}


def test_add_edge_refuses_second_default_edge_into_same_target():
    """The leak that bypassed v0.2.11's attach_node guard. add_edge can wire
    a second `_default` edge into a single-input node — now refused."""
    blocks = [
        {"id": "src-a", "toBlocks": [{
            "edgeId": "src-a-_default-target-_default",
            "edge_source_handle_condition": "_default",
            "edge_target_handle_condition": "_default",
            "toBlockId": "target",
        }]},
        {"id": "src-b", "toBlocks": []},
        {"id": "target", "toBlocks": []},
    ]
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=_wf_with_blocks(blocks)):
        with pytest.raises(ValueError) as exc:
            add_edge(workflow_id="wf-1", source_node_id="src-b", target_node_id="target")
    msg = str(exc.value)
    assert "src-a" in msg               # names the existing source
    assert "target" in msg               # names the target
    assert "attach_magic_node" in msg    # points at the correct alternative
    assert "remove_edge" in msg          # points at the replace alternative


def test_add_edge_allows_magic_node_fan_in_to_df_handles():
    """df1..df5 are the Magic Node fan-in slots — guard must NOT fire there.
    A Magic Node may legitimately have df1=parent-a AND df2=parent-b."""
    blocks = [
        {"id": "src-a", "toBlocks": [{
            "edgeId": "src-a-_default-magic-df1",
            "edge_source_handle_condition": "_default",
            "edge_target_handle_condition": "df1",
            "toBlockId": "magic",
        }]},
        {"id": "src-b", "toBlocks": []},
        {"id": "magic", "toBlocks": []},
    ]
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=_wf_with_blocks(blocks)), \
         patch("nrev_wf_mcp.server.api._put_workflow_blocks") if False else \
         patch("nrev_wf_mcp.server._put_workflow_blocks", return_value={"workflowConfigError": None, "isRunable": True}), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        result = add_edge(
            workflow_id="wf-1",
            source_node_id="src-b",
            target_node_id="magic",
            target_handle="df2",  # different df slot — fine
        )
    assert result["ok"]
    assert result["edge_added"]


def test_add_edge_idempotent_same_source_no_error():
    """If the existing edge is from the same source we're re-adding, treat as
    a no-op (existing behavior). The guard must NOT mistake this for a
    multi-input violation."""
    blocks = [
        {"id": "src", "toBlocks": [{
            "edgeId": "src-_default-target-_default",
            "edge_source_handle_condition": "_default",
            "edge_target_handle_condition": "_default",
            "toBlockId": "target",
        }]},
        {"id": "target", "toBlocks": []},
    ]
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=_wf_with_blocks(blocks)), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        result = add_edge(workflow_id="wf-1", source_node_id="src", target_node_id="target")
    assert result["ok"]
    assert result["edge_existed"]
    assert not result["edge_added"]


def test_add_edge_allows_second_edge_when_opt_in_set():
    """allow_multi_input=True escape hatch for the legacy Merge block."""
    blocks = [
        {"id": "src-a", "toBlocks": [{
            "edgeId": "src-a-_default-target-_default",
            "edge_source_handle_condition": "_default",
            "edge_target_handle_condition": "_default",
            "toBlockId": "target",
        }]},
        {"id": "src-b", "toBlocks": []},
        {"id": "target", "toBlocks": []},
    ]
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=_wf_with_blocks(blocks)), \
         patch("nrev_wf_mcp.server._put_workflow_blocks", return_value={"workflowConfigError": None, "isRunable": True}), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        result = add_edge(
            workflow_id="wf-1",
            source_node_id="src-b",
            target_node_id="target",
            allow_multi_input=True,
        )
    assert result["ok"]
    assert result["edge_added"]


def test_find_existing_default_incoming_helper_returns_source_id():
    """Helper sanity: returns the source block id alongside the matching edge,
    so guard error messages can name the offending block."""
    blocks = [
        {"id": "src-a", "toBlocks": [{
            "toBlockId": "target",
            "edge_target_handle_condition": "_default",
        }]},
        {"id": "src-b", "toBlocks": []},
    ]
    found = _find_existing_default_incoming(blocks, "target")
    assert found is not None
    assert found["source_id"] == "src-a"

    not_found = _find_existing_default_incoming(blocks, "nonexistent")
    assert not_found is None


def test_magic_node_fan_in_handles_set_is_df1_through_df5():
    """Regression guard on the convention constant — if a future change
    accidentally narrows or widens it, this test catches it."""
    assert _MAGIC_NODE_FAN_IN_HANDLES == {"df1", "df2", "df3", "df4", "df5"}


# ════════════════════════════════════════════════════════════════════════
# Fix #4 — splice_branch single-input guard
# ════════════════════════════════════════════════════════════════════════


def test_splice_branch_without_replace_refuses_second_default_edge():
    """When called with replace_edge_from_node_id=None, splice_branch is
    effectively add_edge in disguise — the same single-input guard fires."""
    blocks = [
        {"id": "existing-src", "toBlocks": [{
            "edgeId": "existing-src-_default-downstream-_default",
            "edge_source_handle_condition": "_default",
            "edge_target_handle_condition": "_default",
            "toBlockId": "downstream",
        }]},
        {"id": "new-src", "toBlocks": []},
        {"id": "downstream", "toBlocks": []},
    ]
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=_wf_with_blocks(blocks)):
        with pytest.raises(ValueError) as exc:
            splice_branch(
                workflow_id="wf-1",
                new_terminal_node_id="new-src",
                downstream_target_node_id="downstream",
                # NO replace_edge_from_node_id → guard fires
            )
    msg = str(exc.value)
    assert "existing-src" in msg
    assert "replace_edge_from_node_id" in msg  # tells the caller how to use splice correctly


def test_splice_branch_with_replace_skips_guard():
    """The typical splice pattern (with replace_edge_from_node_id) removes
    the old edge before adding the new one — net incoming count stays at 1,
    so the guard doesn't need to fire."""
    blocks = [
        {"id": "old-src", "toBlocks": [{
            "edgeId": "old-src-_default-downstream-_default",
            "edge_source_handle_condition": "_default",
            "edge_target_handle_condition": "_default",
            "toBlockId": "downstream",
        }]},
        {"id": "new-src", "toBlocks": []},
        {"id": "downstream", "toBlocks": []},
    ]
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=_wf_with_blocks(blocks)), \
         patch("nrev_wf_mcp.server._put_workflow_blocks", return_value={"workflowConfigError": None, "isRunable": True}), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        result = splice_branch(
            workflow_id="wf-1",
            new_terminal_node_id="new-src",
            downstream_target_node_id="downstream",
            replace_edge_from_node_id="old-src",  # ← replace target → guard skipped
        )
    assert result["ok"]
    assert result["spliced"]
    assert result["old_edges_removed"] == 1


# ════════════════════════════════════════════════════════════════════════
# Fix #5 — clone_node strips isTrigger / isListener
# ════════════════════════════════════════════════════════════════════════


def _scheduler_source_block() -> dict:
    return {
        "id": "scheduler-1",
        "typeId": SCHEDULER_TYPE_ID,
        "variableName": "Scheduler",
        "isTrigger": True,
        "isListener": True,
        "isOrphan": False,
        "isPartOfActiveSwimlane": True,
        "isTestMode": False,
        "position": {"x": 100.0, "y": 100.0},
        "settings_field_values": [],
        "inputs": [],
        "outputs": [{"node_id": "scheduler-1", "columns": [], "columns_metadata": None,
                     "file": "", "handle_condition": "_default"}],
        "toBlocks": [],
    }


def test_clone_node_strips_trigger_flags_from_clone():
    """Pre-fix: deepcopy carried isTrigger/isListener through, creating a
    second trigger silently. Post-fix: always False on the clone."""
    source = _scheduler_source_block()

    with patch("nrev_wf_mcp.server.api.get_workflow",
               return_value={"id": "wf-1", "name": "x", "description": "", "blocks": [source]}), \
         patch("nrev_wf_mcp.server._put_workflow_blocks",
               return_value={"blocks": [source], "workflowConfigError": None}), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        result = clone_node(workflow_id="wf-1", source_node_id="scheduler-1")

    # The clone must NOT be a trigger / listener.
    assert "trigger_stripped" in result
    assert "isTrigger=True" in result["trigger_stripped"] or "isListener" in result["trigger_stripped"]


def test_clone_node_quiet_when_source_not_a_trigger():
    """Cloning a non-trigger node shouldn't surface a trigger_stripped note
    — that'd be noise."""
    source = {
        "id": "cc-1", "typeId": "any", "variableName": "Custom Code",
        "isTrigger": False, "isListener": False, "isOrphan": False,
        "isPartOfActiveSwimlane": True, "isTestMode": False,
        "position": {"x": 0, "y": 0}, "settings_field_values": [],
        "inputs": [], "outputs": [],
        "toBlocks": [],
    }
    with patch("nrev_wf_mcp.server.api.get_workflow",
               return_value={"id": "wf-1", "name": "x", "description": "", "blocks": [source]}), \
         patch("nrev_wf_mcp.server._put_workflow_blocks",
               return_value={"blocks": [source], "workflowConfigError": None}), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        result = clone_node(workflow_id="wf-1", source_node_id="cc-1")
    assert "trigger_stripped" not in result


# ════════════════════════════════════════════════════════════════════════
# Fix #7 — partial_execute surfaces non-2xx platform errors with hint
# ════════════════════════════════════════════════════════════════════════


def test_partial_execute_surfaces_executable_gate_error_with_orphan_hint():
    """When the platform refuses with 'Workflow must be executable...',
    partial_execute returns ok=False with the platform's message AND a hint
    pointing at orphan triggers (the most common root cause)."""
    from nrev_wf_mcp.client import WorkflowAPIError

    err = WorkflowAPIError(
        status_code=400,
        body="Workflow must be executable to perform this action",
        url="https://workflow.public.prod.nurturev.com/executions/...",
    )
    with patch("nrev_wf_mcp.server.api.execute_node", side_effect=err):
        result = partial_execute(workflow_id="wf-1", target_node_id="node-1")

    assert result["ok"] is False
    assert result["stage"] == "target_execute"
    assert result["stuck_at_node"] == "node-1"
    assert "Workflow must be executable" in result["message"]
    assert "hint" in result
    assert "orphan trigger" in result["hint"].lower()


def test_partial_execute_surfaces_other_errors_without_hint():
    """For unknown errors, return ok=False with the message — but no
    speculative orphan-trigger hint (don't mislead the agent)."""
    from nrev_wf_mcp.client import WorkflowAPIError

    err = WorkflowAPIError(
        status_code=500,
        body="something else entirely",
        url="https://workflow.public.prod.nurturev.com/executions/...",
    )
    with patch("nrev_wf_mcp.server.api.execute_node", side_effect=err):
        result = partial_execute(workflow_id="wf-1", target_node_id="node-1")

    assert result["ok"] is False
    assert "something else entirely" in result["message"]
    assert "hint" not in result


def test_partial_execute_succeeds_when_platform_returns_2xx():
    """The happy path: api.execute_node returns a dict, partial_execute
    returns ok=True with the response."""
    with patch("nrev_wf_mcp.server.api.execute_node",
               return_value={"executionId": "abc", "status": "running"}):
        result = partial_execute(workflow_id="wf-1", target_node_id="node-1")
    assert result["ok"] is True
    assert result["response"]["executionId"] == "abc"
