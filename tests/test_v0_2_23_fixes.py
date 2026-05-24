"""Tests for v0.2.23 — three must-fix correctness bugs the v0.2.22 GTM stress
test surfaced.

#1: update_magic_node columns_metadata missing origin_* keys → 422
#2: save_and_execute returns stale execution_id when one is already running
#3: attach_node leaves inputs[] empty for downstream blocks → validation
    "Fields not found in available data" until remove_edge + add_edge
"""
from unittest.mock import patch, MagicMock
import pytest

from nrev_wf_mcp.server import (
    update_magic_node, save_and_execute, attach_node,
    _build_inputs_from_parents, _find_in_flight_execution,
    _lookup_node_def_flags,
)
import nrev_wf_mcp.block_types as block_types


SCHEDULER_TYPE_ID = "68da2fb4-8295-4568-9415-c47de58e6224"
GVR_TYPE_ID = "ce01c704-f6bd-40d5-9b2b-f545495de14b"
ASR_TYPE_ID = "191db4a1-7c72-4c4a-af02-b507701ca61b"


# ════════════════════════════════════════════════════════════════════════
# Fix #1 — update_magic_node columns_metadata auto-populate origin_*
# ════════════════════════════════════════════════════════════════════════


def test_update_magic_node_columns_metadata_includes_origin_keys():
    """v0.2.23 — when output_columns is provided, the emitted columns_metadata
    entries MUST include origin_node_id / origin_node_name / origin_node_type
    pointing at the magic node itself. Pre-v0.2.23 these were missing and
    the platform 422'd, blocking iterative Magic Node editing."""
    magic_block = {
        "id": "mn-1",
        "typeId": block_types.MAGIC_NODE,
        "variableName": "My Transform",
        "settings_field_values": [
            {"field_name": "data_manipulation-magic_node-code_section",
             "field_value": [
                 {"field_name": "data_manipulation-magic_node-code",
                  "field_value": "def run(df1): return df1"}
             ]}
        ],
        "outputs": [{
            "columns": [], "columns_metadata": [], "file": "",
            "handle_condition": "_default", "node_id": "mn-1",
        }],
    }
    fake_wf = {"id": "wf-1", "blocks": [magic_block]}
    captured = {}

    def fake_put_node(wf_id, node_id, patch_):
        captured["patch"] = patch_
        return {**patch_, "workflowConfigError": None, "isRunable": True}

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.put_node", side_effect=fake_put_node), \
         patch("nrev_wf_mcp.server.lint", return_value=[]), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value={"valid": True}):
        result = update_magic_node(
            "wf-1", "mn-1",
            output_columns=["score", "rationale"],
            output_dtypes=["integer", "string"],
            validate_after=False,
        )

    # Verify origin_* keys present on every columns_metadata entry
    outs = captured["patch"]["outputs"][0]
    cms = outs["columns_metadata"]
    assert len(cms) == 2
    for cm in cms:
        # Pre-v0.2.23 these were missing → 422
        assert cm.get("origin_node_id") == "mn-1"
        assert cm.get("origin_node_name") == "My Transform"
        assert cm.get("origin_node_type") == "data_manipulation.magic_node"
        # Required scalars
        assert cm.get("column_name") in {"score", "rationale"}
        assert cm.get("data_type") in {"integer", "string"}
        assert "is_nullable" in cm


def test_update_magic_node_columns_metadata_uses_renamed_name():
    """If the caller passes name= alongside output_columns, origin_node_name
    in columns_metadata should use the NEW name (since the variableName is
    being updated in the same call)."""
    magic_block = {
        "id": "mn-1",
        "typeId": block_types.MAGIC_NODE,
        "variableName": "Old Name",
        "settings_field_values": [],
        "outputs": [{"columns": [], "columns_metadata": [],
                     "file": "", "handle_condition": "_default", "node_id": "mn-1"}],
    }
    fake_wf = {"id": "wf-1", "blocks": [magic_block]}
    captured = {}

    def fake_put_node(wf_id, node_id, patch_):
        captured["patch"] = patch_
        return {**patch_, "workflowConfigError": None, "isRunable": True}

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.put_node", side_effect=fake_put_node), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value={"valid": True}):
        update_magic_node(
            "wf-1", "mn-1",
            name="New Name",
            output_columns=["x"],
            validate_after=False,
        )

    outs = captured["patch"]["outputs"][0]
    assert outs["columns_metadata"][0]["origin_node_name"] == "New Name"


# ════════════════════════════════════════════════════════════════════════
# Fix #2 — save_and_execute in-flight detection
# ════════════════════════════════════════════════════════════════════════


def test_find_in_flight_execution_detects_running_status():
    """Helper picks the first non-terminal execution from list_executions."""
    fake_executions = {
        "data": [
            {"id": "e1", "status": "running"},
            {"id": "e2", "status": "completed"},
        ]
    }
    with patch("nrev_wf_mcp.server.api.list_executions", return_value=fake_executions):
        result = _find_in_flight_execution("wf-1")
    assert result == {"id": "e1", "status": "running"}


def test_find_in_flight_execution_returns_none_when_all_terminal():
    fake_executions = {"data": [
        {"id": "e1", "status": "completed"},
        {"id": "e2", "status": "failed"},
    ]}
    with patch("nrev_wf_mcp.server.api.list_executions", return_value=fake_executions):
        assert _find_in_flight_execution("wf-1") is None


def test_find_in_flight_execution_swallows_api_errors():
    """Best-effort: list_executions failure shouldn't block save_and_execute."""
    with patch("nrev_wf_mcp.server.api.list_executions", side_effect=Exception("boom")):
        assert _find_in_flight_execution("wf-1") is None


def test_save_and_execute_refuses_when_in_flight_default():
    """Default if_in_flight='refuse': detect in-flight, surface clear error."""
    fake_wf = {"id": "wf-1", "blocks": [{"id": "n1"}]}
    in_flight = {"id": "exec-running", "status": "running"}
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.list_executions",
               return_value={"data": [in_flight]}), \
         patch("nrev_wf_mcp.server.api.update_workflow_and_execute") as mock_exec:
        result = save_and_execute("wf-1", "n1")

    assert result["ok"] is False
    assert result["stage"] == "in_flight_guard"
    assert result["in_flight_execution_id"] == "exec-running"
    # Did NOT call execute — refused
    assert mock_exec.call_count == 0


def test_save_and_execute_return_existing_when_in_flight():
    """if_in_flight='return_existing': return the in-flight id with the
    was_in_flight flag set."""
    fake_wf = {"id": "wf-1", "blocks": [{"id": "n1"}]}
    in_flight = {"id": "exec-running", "status": "running"}
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.list_executions",
               return_value={"data": [in_flight]}), \
         patch("nrev_wf_mcp.server.api.update_workflow_and_execute") as mock_exec:
        result = save_and_execute("wf-1", "n1", if_in_flight="return_existing")

    assert result["ok"] is True
    assert result["execution_id"] == "exec-running"
    assert result["was_in_flight"] is True
    assert mock_exec.call_count == 0  # did NOT start a new run


def test_save_and_execute_fires_fresh_when_no_in_flight():
    """No in-flight execution → normal path. Returns was_in_flight=False so
    caller can distinguish."""
    fake_wf = {"id": "wf-1", "blocks": [{"id": "n1"}]}
    fresh_exec = {"id": "exec-new", "status": "running", "startedAt": "2026-05-24T00:00:00Z"}
    api_resp = {"workflow": fake_wf, "execution": {"response": fresh_exec}}
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.list_executions", return_value={"data": []}), \
         patch("nrev_wf_mcp.server.api.update_workflow_and_execute",
               return_value=api_resp) as mock_exec:
        result = save_and_execute("wf-1", "n1")

    assert result["ok"] is True
    assert result["execution_id"] == "exec-new"
    assert result["was_in_flight"] is False
    assert mock_exec.call_count == 1


def test_save_and_execute_rejects_invalid_if_in_flight():
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value={"blocks": [{"id": "n1"}]}):
        with pytest.raises(ValueError, match="if_in_flight must be one of"):
            save_and_execute("wf-1", "n1", if_in_flight="bogus")


# ════════════════════════════════════════════════════════════════════════
# Fix #3 — attach_node populates inputs from parent's outputs
# ════════════════════════════════════════════════════════════════════════


def test_build_inputs_from_parents_empty_returns_root_skeleton():
    """No parents → default empty inputs skeleton (root case)."""
    result = _build_inputs_from_parents([], {})
    assert result == [{
        "columns": [], "columns_metadata": None, "file": "",
        "handle_condition": "_default", "node_id": None,
    }]


def test_build_inputs_from_parents_carries_parent_outputs_columns():
    """For each parent, copy its outputs[0].columns + columns_metadata into
    the new block's inputs. This is what add_edge does in the v0.2.18 refresh."""
    parent = {
        "id": "p1",
        "outputs": [{
            "node_id": "p1",
            "file": "dataframe",
            "handle_condition": "_default",
            "columns": ["a", "b", "c"],
            "columns_metadata": [
                {"column_name": "a", "origin_node_id": "p1", "data_type": "string"},
                {"column_name": "b", "origin_node_id": "p1", "data_type": "integer"},
                {"column_name": "c", "origin_node_id": "p1", "data_type": "boolean"},
            ],
        }],
    }
    blocks_by_id = {"p1": parent}
    result = _build_inputs_from_parents(["p1"], blocks_by_id)
    assert len(result) == 1
    inp = result[0]
    assert inp["node_id"] == "p1"
    assert inp["columns"] == ["a", "b", "c"]
    assert len(inp["columns_metadata"]) == 3
    assert inp["columns_metadata"][0]["column_name"] == "a"
    assert inp["file"] == "dataframe"


def test_attach_node_downstream_has_populated_inputs():
    """End-to-end via attach_node: a downstream block's inputs[0] now carries
    the parent's columns_metadata, NOT an empty skeleton. Closes the
    'Fields not found in available data' first-attach footgun."""
    _lookup_node_def_flags.cache_clear()
    parent = {
        "id": "p1",
        "typeId": GVR_TYPE_ID,
        "position": {"x": 0, "y": 0},
        "outputs": [{
            "node_id": "p1", "file": "", "handle_condition": "_default",
            "columns": ["who", "what"],
            "columns_metadata": [
                {"column_name": "who", "origin_node_id": "p1",
                 "origin_node_type": "pipedream.x", "data_type": "string"},
                {"column_name": "what", "origin_node_id": "p1",
                 "origin_node_type": "pipedream.x", "data_type": "string"},
            ],
        }],
        "settings_field_values": [], "toBlocks": [],
    }
    fake_wf = {"id": "wf-1", "blocks": [parent]}
    captured = {}

    def fake_wire(*, workflow_id, new_block, parent_edges, fallback_parents, existing_block_ids):
        captured["new_block"] = new_block
        return ({"workflowConfigError": None, "isRunable": True,
                 "blocks": [parent, new_block]}, new_block["id"])

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server._attach_block_via_paste_and_wire",
               side_effect=fake_wire), \
         patch("nrev_wf_mcp.server._new_block_error_from_paste", return_value=None), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value={"valid": True}):
        attach_node(
            workflow_id="wf-1",
            parent_node_ids=["p1"],
            type_id="ae54c44f-60ee-47c4-91d7-eae7fa849133",  # Custom Code
            name="Downstream CC",
            settings={"data_manipulation-custom_code-code": "def run(df): return df"},
            auto_resolve_labels=False,
        )

    # The new block's inputs[0] should carry parent's outputs metadata
    new_block = captured["new_block"]
    assert len(new_block["inputs"]) == 1
    inp = new_block["inputs"][0]
    assert inp["node_id"] == "p1"
    assert inp["columns"] == ["who", "what"]
    # columns_metadata propagated — no more "Fields not found in available data"
    assert inp["columns_metadata"] is not None
    assert len(inp["columns_metadata"]) == 2
    assert inp["columns_metadata"][0]["column_name"] == "who"


def test_attach_node_root_keeps_empty_inputs():
    """Sanity: root attachment (no parents) still gets the default empty
    inputs skeleton — only downstream attaches get the new refresh behavior."""
    _lookup_node_def_flags.cache_clear()
    fake_wf = {"id": "wf-1", "blocks": []}
    captured = {}

    def fake_wire(*, workflow_id, new_block, parent_edges, fallback_parents, existing_block_ids):
        captured["new_block"] = new_block
        return ({"workflowConfigError": None, "isRunable": True,
                 "blocks": [new_block]}, new_block["id"])

    fake_catalog = [{"node_definition_id": GVR_TYPE_ID,
                     "is_trigger": True, "isListener": False}]
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.list_node_definitions",
               return_value={"data": fake_catalog}), \
         patch("nrev_wf_mcp.server._attach_block_via_paste_and_wire",
               side_effect=fake_wire), \
         patch("nrev_wf_mcp.server._new_block_error_from_paste", return_value=None), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value={"valid": True}):
        attach_node(
            workflow_id="wf-1",
            parent_node_ids=[],
            type_id=GVR_TYPE_ID,
            name="Root GVR",
            settings={"x": "y"},
            auto_resolve_labels=False,
        )

    new_block = captured["new_block"]
    assert new_block["inputs"] == [{
        "columns": [], "columns_metadata": None, "file": "",
        "handle_condition": "_default", "node_id": None,
    }]
