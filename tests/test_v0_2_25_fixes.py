"""Tests for v0.2.25 fixes:

  Fix #1: New `check_node_errors` MCP tool — explicit row-level Pipedream
          error check that callers can invoke regardless of whether
          tail_execution auto-detected it. Plus improved diagnostic on the
          underlying _check_pipedream_row_error helper.

  Fix #2: `delete_node` accepts `confirm` as a no-op for ergonomic symmetry
          with `delete_workflow(confirm=True)`.

  Fix #3: `validate_workflow` docstring documents the stale-cache behavior
          for recently-deleted nodes.

  Bugs that surfaced during the 2026-05-25 prod comprehensive test.
"""
from unittest.mock import patch
from nrev_wf_mcp.server import (
    check_node_errors,
    delete_node,
    validate_workflow,
    _check_pipedream_row_error,
)


# ════════════════════════════════════════════════════════════════════════
# Fix #1 — check_node_errors tool + helper diagnostic
# ════════════════════════════════════════════════════════════════════════


def test_check_node_errors_surfaces_pipedream_row_error():
    """Given a completed execution with a Pipedream block whose row has an
    error_1 column, check_node_errors returns it in blocks_with_errors."""
    err_envelope = '{"name": "Error", "message": "Error: An API error occurred: invalid_blocks", "attribution": {"origin": "response_parsing"}}'
    fake_execution = {
        "id": "exec-1",
        "status": "completed",
        "blockRuns": [
            {"workflowBlockId": "slack-1", "workflowBlockName": "Slack: post",
             "status": "completed", "error": None},
        ],
    }
    pipedream_block = {
        "id": "slack-1",
        "settings_field_values": [
            {"field_name": "pipedream-slack_v2-slack_v2_send_message_to_channel-text",
             "field_value": "hi"},
        ],
        "outputs": [],
    }
    fake_wf = {"blocks": [pipedream_block]}
    fake_preview = {"rows": [{"error": "[]", "error_1": err_envelope}]}

    with patch("nrev_wf_mcp.server.api.get_execution_detail", return_value=fake_execution), \
         patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.get_node_preview", return_value=fake_preview):
        result = check_node_errors(workflow_id="wf-1", execution_id="exec-1")

    assert result["checked_block_count"] == 1
    assert len(result["blocks_with_errors"]) == 1
    err = result["blocks_with_errors"][0]
    assert err["block_id"] == "slack-1"
    assert err["block_name"] == "Slack: post"
    assert "invalid_blocks" in err["row_error"]
    assert err["error_attribution"] == {"origin": "response_parsing"}


def test_check_node_errors_skips_non_pipedream_blocks():
    """A Custom Code or Magic Node should be in skipped_non_pipedream,
    not checked for row errors."""
    fake_execution = {
        "id": "exec-1",
        "blockRuns": [
            {"workflowBlockId": "mn-1", "workflowBlockName": "Magic Node",
             "status": "completed", "error": None},
        ],
    }
    mn_block = {
        "id": "mn-1",
        "settings_field_values": [
            {"field_name": "data_manipulation-magic_node-code",
             "field_value": "def run(df1): return df1"},
        ],
        "outputs": [],
    }
    fake_wf = {"blocks": [mn_block]}

    with patch("nrev_wf_mcp.server.api.get_execution_detail", return_value=fake_execution), \
         patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf):
        result = check_node_errors(workflow_id="wf-1", execution_id="exec-1")

    assert result["checked_block_count"] == 0
    assert result["skipped_non_pipedream"] == ["mn-1"]
    assert result["blocks_with_errors"] == []


def test_check_node_errors_filters_to_single_node_id():
    """When node_id is provided, only that block is checked even if there
    are multiple Pipedream blocks in the execution."""
    fake_execution = {
        "id": "exec-1",
        "blockRuns": [
            {"workflowBlockId": "slack-1", "workflowBlockName": "S1",
             "status": "completed", "error": None},
            {"workflowBlockId": "slack-2", "workflowBlockName": "S2",
             "status": "completed", "error": None},
        ],
    }
    blocks = [
        {"id": "slack-1", "settings_field_values": [{"field_name": "pipedream-x"}], "outputs": []},
        {"id": "slack-2", "settings_field_values": [{"field_name": "pipedream-y"}], "outputs": []},
    ]
    fake_wf = {"blocks": blocks}
    fake_preview = {"rows": [{"error": None, "error_1": None}]}  # no errors

    with patch("nrev_wf_mcp.server.api.get_execution_detail", return_value=fake_execution), \
         patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.get_node_preview", return_value=fake_preview) as preview_mock:
        result = check_node_errors(workflow_id="wf-1", execution_id="exec-1",
                                    node_id="slack-2")

    # Only slack-2 was checked, slack-1 was filtered out at the for-loop
    assert preview_mock.call_count == 1
    preview_mock.assert_called_with("wf-1", "exec-1", "slack-2",
                                     handle_condition="_default", skip=0, limit=1)
    assert result["checked_block_count"] == 1
    assert "slack-2" in result["blocks_without_errors"]


def test_check_node_errors_handles_no_block_runs():
    """Execution with no blockRuns (still pending or just-created) returns
    a clean empty result with a note."""
    fake_execution = {"id": "exec-1", "blockRuns": []}

    with patch("nrev_wf_mcp.server.api.get_execution_detail", return_value=fake_execution):
        result = check_node_errors(workflow_id="wf-1", execution_id="exec-1")

    assert result["checked_block_count"] == 0
    assert "still running" in result["note"].lower()


def test_check_pipedream_row_error_surfaces_diagnostic_on_api_failure():
    """When get_node_preview throws (e.g. expired execution, handle mismatch),
    the helper now returns a diagnostic instead of silent None."""
    def boom(*args, **kwargs):
        raise RuntimeError("Execution rows no longer available (TTL)")

    with patch("nrev_wf_mcp.server.api.get_node_preview", side_effect=boom):
        result = _check_pipedream_row_error("wf-1", "exec-1", "block-1")

    assert result is not None
    assert result["has_row_error"] is False
    assert "_diagnostic" in result
    assert "RuntimeError" in result["_diagnostic"]
    assert "TTL" in result["_diagnostic"]


def test_check_node_errors_propagates_diagnostic_from_helper():
    """When a Pipedream block's preview fetch fails, check_node_errors
    surfaces the diagnostic in the diagnostics list (not silently dropped)."""
    fake_execution = {
        "id": "exec-1",
        "blockRuns": [
            {"workflowBlockId": "slack-1", "workflowBlockName": "S",
             "status": "completed", "error": None},
        ],
    }
    fake_wf = {"blocks": [
        {"id": "slack-1", "settings_field_values": [{"field_name": "pipedream-x"}], "outputs": []},
    ]}

    def boom(*args, **kwargs):
        raise RuntimeError("preview API 500")

    with patch("nrev_wf_mcp.server.api.get_execution_detail", return_value=fake_execution), \
         patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.get_node_preview", side_effect=boom):
        result = check_node_errors(workflow_id="wf-1", execution_id="exec-1")

    assert len(result["diagnostics"]) == 1
    assert result["diagnostics"][0]["block_id"] == "slack-1"
    assert "preview API 500" in result["diagnostics"][0]["diagnostic"]


# ════════════════════════════════════════════════════════════════════════
# Fix #2 — delete_node accepts confirm=True as no-op
# ════════════════════════════════════════════════════════════════════════


def test_delete_node_accepts_confirm_true():
    """delete_node(confirm=True) should not raise Pydantic validation error.
    Previously, the param wasn't declared and agents who learned the
    delete_workflow(confirm=True) pattern got 'Unexpected keyword argument'."""
    fake_wf = {
        "id": "wf-1",
        "blocks": [
            {"id": "n1", "variableName": "n1", "toBlocks": [], "outputs": [], "settings_field_values": []},
        ],
    }

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server._put_workflow_blocks", return_value={"isRunable": True}), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value={"valid": True, "isRunable": True}):
        # Should NOT raise
        result = delete_node(workflow_id="wf-1", node_id="n1", confirm=True)

    assert result["ok"] is True
    assert result["deleted_node_id"] == "n1"


def test_delete_node_accepts_confirm_false():
    """confirm=False (the default) also works — delete still fires
    (it's a no-op param)."""
    fake_wf = {
        "id": "wf-1",
        "blocks": [
            {"id": "n1", "variableName": "n1", "toBlocks": [], "outputs": [], "settings_field_values": []},
        ],
    }

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server._put_workflow_blocks", return_value={"isRunable": True}), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value={"valid": True, "isRunable": True}):
        result = delete_node(workflow_id="wf-1", node_id="n1", confirm=False)

    assert result["ok"] is True


def test_delete_node_works_without_confirm_param():
    """The original no-arg call still works (backward compatibility)."""
    fake_wf = {
        "id": "wf-1",
        "blocks": [
            {"id": "n1", "variableName": "n1", "toBlocks": [], "outputs": [], "settings_field_values": []},
        ],
    }

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server._put_workflow_blocks", return_value={"isRunable": True}), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value={"valid": True, "isRunable": True}):
        result = delete_node(workflow_id="wf-1", node_id="n1")

    assert result["ok"] is True


# ════════════════════════════════════════════════════════════════════════
# Fix #3 — validate_workflow docstring documents staleness
# ════════════════════════════════════════════════════════════════════════


def test_validate_workflow_docstring_mentions_staleness():
    """The docstring tells callers about the stale-cache behavior so they
    don't waste time investigating ghost node errors after deletes."""
    doc = validate_workflow.__doc__ or ""
    assert "cache" in doc.lower() or "stale" in doc.lower(), \
        "validate_workflow docstring should mention cache staleness (see v0.2.25 Fix #3)"
    # Both terms ideally
    assert "deleted" in doc.lower() or "lag" in doc.lower(), \
        "docstring should mention deleted-node-id ghost-error scenario"
