"""Tests for v0.2.16 small-payload conversions.

Six tools converted from full-workflow PUT to per-block put_node:
  - update_node_setting
  - update_magic_node
  - update_ai_prompt
  - set_node_output_schema
  - add_edge
  - remove_edge

The headline assertion across all of them: **api.put_workflow is NEVER
called**. That's what was 413'ing on big workflows. Each tool should now
call api.put_node exactly once with the affected block.
"""
from unittest.mock import patch

from nrev_wf_mcp.server import (
    update_node_setting,
    update_magic_node,
    update_ai_prompt,
    set_node_output_schema,
    add_edge,
    remove_edge,
    _put_node_and_validate,
)


# Reusable fixture builders
def _cc_block(block_id, *, code="def run(df1):\n    return df1\n", toBlocks=None):
    return {
        "id": block_id,
        "typeId": "ae54c44f-60ee-47c4-91d7-eae7fa849133",  # Custom Code
        "variableName": f"block-{block_id}",
        "position": {"x": 0, "y": 0},
        "settings_field_values": [{
            "field_name": "data_manipulation-custom_code-code",
            "field_value": code,
            "fieldLabel": None, "error": None,
            "isUserInputInFormMandatory": False,
            "selectedInputTypeIndex": None, "isStale": False,
        }],
        "isTrigger": False, "isListener": False, "isOrphan": False,
        "isPartOfActiveSwimlane": True, "isTestMode": False,
        "inputs": [], "outputs": [],
        "toBlocks": toBlocks or [],
    }


def _ai_block(block_id, *, prompt="explain X"):
    return {
        "id": block_id,
        "typeId": "ai_toolkit_ask_ai_typeid",
        "variableName": f"ai-{block_id}",
        "position": {"x": 0, "y": 0},
        "settings_field_values": [
            {
                "field_name": "ai_toolkit-ask_ai-prompt",
                "field_value": prompt,
                "fieldLabel": None, "error": None,
                "isUserInputInFormMandatory": False,
                "selectedInputTypeIndex": None, "isStale": False,
            },
        ],
        "isTrigger": False, "isListener": False, "isOrphan": False,
        "isPartOfActiveSwimlane": True, "isTestMode": False,
        "inputs": [], "outputs": [],
        "toBlocks": [],
    }


def _magic_block(block_id, *, code="def run(df1):\n    return df1\nresult = run(df1)"):
    return {
        "id": block_id,
        "typeId": "69f5628d-2c3b-4816-ac80-6825a1058ed5",  # Magic Node
        "variableName": f"magic-{block_id}",
        "position": {"x": 0, "y": 0},
        "settings_field_values": [
            {
                "field_name": "data_manipulation-magic_node-code_section",
                "field_value": [{
                    "field_name": "data_manipulation-magic_node-code",
                    "field_value": code,
                    "fieldLabel": None, "error": None,
                    "isUserInputInFormMandatory": False,
                    "selectedInputTypeIndex": None, "isStale": False,
                }],
                "fieldLabel": None, "error": None,
                "isUserInputInFormMandatory": False,
                "selectedInputTypeIndex": None, "isStale": False,
            },
            {
                "field_name": "data_manipulation-magic_node-instructions_and_ref",
                "field_value": [
                    {
                        "field_name": "data_manipulation-magic_node-instructions",
                        "field_value": [{
                            "field_name": "data_manipulation-magic_node-instructions_text",
                            "field_value": "describe X",
                            "fieldLabel": "Instructions Text", "error": None,
                            "isUserInputInFormMandatory": False,
                            "selectedInputTypeIndex": None, "isStale": False,
                        }],
                        "fieldLabel": None, "error": None,
                        "isUserInputInFormMandatory": False,
                        "selectedInputTypeIndex": None, "isStale": False,
                    },
                    {
                        "field_name": "data_manipulation-magic_node-references",
                        "field_value": ["parent-_default-magic-df1"],
                        "fieldLabel": None, "error": None,
                        "isUserInputInFormMandatory": False,
                        "selectedInputTypeIndex": None, "isStale": False,
                    },
                ],
                "fieldLabel": None, "error": None,
                "isUserInputInFormMandatory": False,
                "selectedInputTypeIndex": None, "isStale": False,
            },
        ],
        "isTrigger": False, "isListener": False, "isOrphan": False,
        "isPartOfActiveSwimlane": True, "isTestMode": False,
        "inputs": [], "outputs": [{
            "columns": ["x"], "columns_metadata": [],
            "file": "", "handle_condition": "_default", "node_id": block_id,
        }],
        "toBlocks": [],
    }


# ════════════════════════════════════════════════════════════════════════
# Helper sanity
# ════════════════════════════════════════════════════════════════════════


def test_put_node_and_validate_returns_standard_shape():
    """The shared helper returns a consistent dict that the 6 tools merge
    into their richer returns."""
    with patch("nrev_wf_mcp.server.api.put_node", return_value={"node_config_error": None}), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value={
             "valid": True, "isRunable": True, "workflowConfigError": None,
             "node_errors": [], "magic_ref_warnings": [],
         }):
        out = _put_node_and_validate("wf-1", "n-1", {"id": "n-1"}, validate_after=True)
    assert out["ok"] is True
    assert out["node_config_error"] is None
    assert out["workflowConfigError"] is None
    assert out["isRunable"] is True
    assert out["validation"]["valid"] is True


def test_put_node_and_validate_when_validation_skipped():
    """validate_after=False → no validation GET, workflow-level fields are None."""
    with patch("nrev_wf_mcp.server.api.put_node",
               return_value={"node_config_error": None}) as mock_put:
        out = _put_node_and_validate("wf-1", "n-1", {"id": "n-1"}, validate_after=False)
    mock_put.assert_called_once()
    assert out["ok"] is True
    assert out["workflowConfigError"] is None
    assert out["isRunable"] is None
    assert out["validation"] is None


def test_put_node_and_validate_surfaces_node_config_error():
    with patch("nrev_wf_mcp.server.api.put_node",
               return_value={"node_config_error": "missing connection_id"}), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        out = _put_node_and_validate("wf-1", "n-1", {"id": "n-1"}, validate_after=False)
    assert out["ok"] is False
    assert out["node_config_error"] == "missing connection_id"


# ════════════════════════════════════════════════════════════════════════
# The six converted tools — all assert put_workflow is NEVER called
# ════════════════════════════════════════════════════════════════════════


def _assert_no_full_workflow_put(mock_put_workflow, mock_put_node, *, expected_node_id):
    """Reusable assertion bundle: full PUT not called, per-node PUT called
    once with the right node id."""
    mock_put_workflow.assert_not_called()
    assert mock_put_node.call_count == 1
    args, kwargs = mock_put_node.call_args
    # Positional or kwarg, the 2nd positional is node_id
    node_id_arg = args[1] if len(args) >= 2 else kwargs.get("node_id")
    assert node_id_arg == expected_node_id


def test_update_node_setting_uses_per_node_put():
    fake_wf = {"id": "wf-1", "name": "x", "description": "",
               "blocks": [_cc_block("target")]}
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.put_workflow") as mock_put_wf, \
         patch("nrev_wf_mcp.server.api.put_node",
               return_value={"node_config_error": None}) as mock_put_node, \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        result = update_node_setting(
            workflow_id="wf-1",
            node_id="target",
            field_path="data_manipulation-custom_code-code",
            value="def run(df1):\n    return df1.head(5)\n",
            validate_after=False,
        )
    assert result["ok"]
    _assert_no_full_workflow_put(mock_put_wf, mock_put_node, expected_node_id="target")


def test_update_magic_node_uses_per_node_put():
    fake_wf = {"id": "wf-1", "name": "x", "description": "",
               "blocks": [_magic_block("magic")]}
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.put_workflow") as mock_put_wf, \
         patch("nrev_wf_mcp.server.api.put_node",
               return_value={"node_config_error": None}) as mock_put_node, \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        result = update_magic_node(
            workflow_id="wf-1",
            node_id="magic",
            code="def run(df1):\n    return df1.tail(2)\nresult = run(df1)\n",
            validate_after=False,
        )
    assert result["ok"]
    _assert_no_full_workflow_put(mock_put_wf, mock_put_node, expected_node_id="magic")


def test_update_ai_prompt_uses_per_node_put():
    fake_wf = {"id": "wf-1", "name": "x", "description": "",
               "blocks": [_ai_block("ai")]}
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.put_workflow") as mock_put_wf, \
         patch("nrev_wf_mcp.server.api.put_node",
               return_value={"node_config_error": None}) as mock_put_node, \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        result = update_ai_prompt(
            workflow_id="wf-1",
            node_id="ai",
            new_prompt="explain Y differently",
            validate_after=False,
        )
    assert result["ok"]
    _assert_no_full_workflow_put(mock_put_wf, mock_put_node, expected_node_id="ai")


def test_set_node_output_schema_uses_per_node_put():
    fake_wf = {"id": "wf-1", "name": "x", "description": "",
               "blocks": [_cc_block("transform")]}
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.put_workflow") as mock_put_wf, \
         patch("nrev_wf_mcp.server.api.put_node",
               return_value={"node_config_error": None}) as mock_put_node, \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        result = set_node_output_schema(
            workflow_id="wf-1",
            node_id="transform",
            columns=[{"name": "a"}, {"name": "b", "dtype": "integer"}],
            validate_after=False,
        )
    assert result["ok"]
    assert result["column_count"] == 2
    _assert_no_full_workflow_put(mock_put_wf, mock_put_node, expected_node_id="transform")


def test_add_edge_uses_per_node_put_on_source():
    """add_edge mutates the SOURCE block's toBlocks; the per-node PUT
    targets the source, not the downstream node."""
    fake_wf = {"id": "wf-1", "name": "x", "description": "",
               "blocks": [_cc_block("src"), _cc_block("tgt")]}
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.put_workflow") as mock_put_wf, \
         patch("nrev_wf_mcp.server.api.put_node",
               return_value={"node_config_error": None}) as mock_put_node, \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        result = add_edge(
            workflow_id="wf-1",
            source_node_id="src",
            target_node_id="tgt",
            validate_after=False,
        )
    assert result["ok"]
    assert result["edge_added"]
    _assert_no_full_workflow_put(mock_put_wf, mock_put_node, expected_node_id="src")


def test_remove_edge_uses_per_node_put_on_source():
    """remove_edge similarly mutates the source — per-node PUT targets the source."""
    fake_wf = {
        "id": "wf-1", "name": "x", "description": "",
        "blocks": [
            _cc_block("src", toBlocks=[{
                "edgeId": "src-_default-tgt-_default",
                "edge_source_handle_condition": "_default",
                "edge_target_handle_condition": "_default",
                "toBlockId": "tgt",
            }]),
            _cc_block("tgt"),
        ],
    }
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.put_workflow") as mock_put_wf, \
         patch("nrev_wf_mcp.server.api.put_node",
               return_value={"node_config_error": None}) as mock_put_node, \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        result = remove_edge(
            workflow_id="wf-1",
            source_node_id="src",
            target_node_id="tgt",
            validate_after=False,
        )
    assert result["ok"]
    assert result["removed_count"] == 1
    _assert_no_full_workflow_put(mock_put_wf, mock_put_node, expected_node_id="src")


def test_remove_edge_noop_skips_put_entirely():
    """If no matching edge exists, remove_edge is a no-op — no PUT at all."""
    fake_wf = {"id": "wf-1", "name": "x", "description": "",
               "blocks": [_cc_block("src", toBlocks=[]), _cc_block("tgt")]}
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.put_workflow") as mock_put_wf, \
         patch("nrev_wf_mcp.server.api.put_node") as mock_put_node, \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        result = remove_edge(
            workflow_id="wf-1",
            source_node_id="src",
            target_node_id="tgt",
            validate_after=False,
        )
    assert result["ok"]
    assert result["removed_count"] == 0
    mock_put_wf.assert_not_called()
    mock_put_node.assert_not_called()


# ════════════════════════════════════════════════════════════════════════
# Verify the remaining (NOT-yet-converted) tools still use the full PUT
# This is a regression guard for the scope split — confirms we haven't
# accidentally converted tools that v0.2.17+ will handle.
# ════════════════════════════════════════════════════════════════════════


def test_unconverted_tools_still_use_put_workflow_blocks():
    """Sanity guard: pin the remaining callers of the legacy full-PUT
    helper, so a future change that converts one without going through
    review trips this test.

    v0.2.16 left 4 callers + the definition itself = 5 references total:
      - delete_node (BLOCKED — needs platform DELETE node endpoint)
      - splice_branch (v0.2.17 candidate — atomicity concession)
      - clone_node (v0.2.17 candidate — id-reassignment via paste-nodes)
      - bulk_set_test_mode (deferred — slower for small workflows)

    If this count changes, audit the diff to confirm the change was
    intentional and reviewed.
    """
    import inspect
    from nrev_wf_mcp import server
    source = inspect.getsource(server)
    callers = source.count("_put_workflow_blocks(")
    assert callers == 5, (
        f"Expected exactly 5 references to _put_workflow_blocks (1 def + "
        f"4 v0.2.17+ callers: delete_node, splice_branch, clone_node, "
        f"bulk_set_test_mode), but found {callers}. A conversion may have "
        f"landed without review, or a new caller was added — audit recent "
        f"changes."
    )
