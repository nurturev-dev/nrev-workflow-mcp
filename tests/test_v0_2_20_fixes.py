"""Tests for v0.2.20 fixes.

Background — the v0.2.19 release unblocked cross-tenant Pipedream node
configuration. The follow-on live probe (workflow 4319ebfd-...) revealed:

  1. Add Single Row's schema NEVER exposes row-data fields. Setting
     `myColumnData` on the node persists silently and is ignored by the
     Pipedream runtime. Row data must come from the UPSTREAM block.
  2. Block-level execution status `completed / error:null` HIDES Pipedream-
     layer errors. The real error lives in row[0].error of the output.
     F2's "Sheets append succeeded" was actually a row-level error that
     looked like success at the block level.
  3. update_node_setting couldn't ADD new field paths — only modify
     existing ones. Pipedream nodes start with only the connection bound
     and need fields ADDED as the schema progresses.

This release ships six fixes (A-F) addressing these.
"""
from unittest.mock import patch, MagicMock

from nrev_wf_mcp.server import (
    update_node_setting,
    _check_pipedream_row_error,
    _is_pipedream_block,
    _maybe_enrich_pipedream_errors,
    _extract_pipedream_app_slug,
    _resolve_connection_label,
    attach_python_block,
)


# ════════════════════════════════════════════════════════════════════════
# Fix B — update_node_setting can ADD new field paths
# ════════════════════════════════════════════════════════════════════════


def _pipedream_block_min(node_id="n1"):
    """Pipedream Add Single Row block with just the connection field set —
    the state right after attach_node."""
    return {
        "id": node_id,
        "typeId": "191db4a1-7c72-4c4a-af02-b507701ca61b",
        "variableName": "ASR",
        "settings_field_values": [
            {"field_name": "pipedream-google_sheets-google_sheets_add_single_row-googleSheets_connection_id",
             "field_value": "conn-1", "fieldLabel": None, "error": None,
             "isUserInputInFormMandatory": False, "selectedInputTypeIndex": None, "isStale": False},
        ],
        "outputs": [],
        "inputs": [],
        "isTrigger": True,
        "isOrphan": False,
        "isListener": False,
        "isPartOfActiveSwimlane": True,
        "isTestMode": False,
        "creditCostPerItem": 0,
        "column_operations": None,
        "node_config_error": None,
        "position": {"x": 0, "y": 0},
        "toBlocks": [],
        "description": "",
    }


def test_update_node_setting_adds_new_top_level_field_when_add_if_missing_true():
    """Fix B: setting a brand-new top-level field path should append a new
    entry to settings_field_values (with the proper envelope shape)."""
    block = _pipedream_block_min()
    fake_wf = {"id": "wf-1", "blocks": [block]}
    captured = {}

    def fake_put_node(wf_id, node_id, node_patch):
        captured["patch"] = node_patch
        # client.put_node wraps in {"node": ...} before sending; the server-side
        # function receives the raw block dict, so echo it back as the response
        return {
            **node_patch,
            "workflowConfigError": None,
            "isRunable": True,
        }

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.put_node", side_effect=fake_put_node), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value={"valid": True}):
        result = update_node_setting(
            "wf-1", "n1",
            field_path="pipedream-google_sheets-google_sheets_add_single_row-drive",
            value="My Drive",
            validate_after=False,
        )

    assert result["added_new_field"] is True
    # Confirm the patched envelope has the new field with proper shape
    sfv = captured["patch"]["settings_field_values"]
    new_entry = next((e for e in sfv if e["field_name"].endswith("-drive")), None)
    assert new_entry is not None
    assert new_entry["field_value"] == "My Drive"
    assert "fieldLabel" in new_entry
    assert "isUserInputInFormMandatory" in new_entry
    assert new_entry["isStale"] is False


def test_update_node_setting_modifies_existing_field_returns_added_false():
    """Sanity: modifying an existing field still works and reports added_new_field=False."""
    block = _pipedream_block_min()
    fake_wf = {"id": "wf-1", "blocks": [block]}

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.put_node",
               return_value={"node": block, "workflowConfigError": None, "isRunable": True}), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value={"valid": True}):
        result = update_node_setting(
            "wf-1", "n1",
            field_path="pipedream-google_sheets-google_sheets_add_single_row-googleSheets_connection_id",
            value="conn-2",
            validate_after=False,
        )

    assert result["added_new_field"] is False


def test_update_node_setting_refuses_to_add_when_add_if_missing_false():
    """Opt-out: caller explicitly forbids auto-adding."""
    block = _pipedream_block_min()
    fake_wf = {"id": "wf-1", "blocks": [block]}

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf):
        result = update_node_setting(
            "wf-1", "n1",
            field_path="some-missing-field",
            value="x",
            add_if_missing=False,
        )

    assert result["ok"] is False
    assert "not found" in result["message"]
    assert "available_paths" in result


def test_update_node_setting_refuses_to_add_nested_paths():
    """Safety: we only auto-add at the top level. Nested adds would need to
    construct the parent group entry too, which is more delicate."""
    block = _pipedream_block_min()
    fake_wf = {"id": "wf-1", "blocks": [block]}

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf):
        result = update_node_setting(
            "wf-1", "n1",
            field_path="some-group/nested-field",
            value="x",
            add_if_missing=True,
        )

    assert result["ok"] is False
    assert "nested" in result["message"]


def test_update_node_setting_with_field_label_sets_label():
    """field_label param: when adding OR modifying, fieldLabel envelope key
    should be populated."""
    block = _pipedream_block_min()
    fake_wf = {"id": "wf-1", "blocks": [block]}
    captured = {}

    def fake_put_node(wf_id, node_id, node_patch):
        captured["patch"] = node_patch
        return {**node_patch, "workflowConfigError": None, "isRunable": True}

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.put_node", side_effect=fake_put_node), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value={"valid": True}):
        update_node_setting(
            "wf-1", "n1",
            field_path="pipedream-google_sheets-google_sheets_add_single_row-sheetId",
            value="sheet-uuid",
            field_label="My Important Sheet",
            validate_after=False,
        )

    sfv = captured["patch"]["settings_field_values"]
    new_entry = next(e for e in sfv if e["field_name"].endswith("-sheetId"))
    assert new_entry["fieldLabel"] == "My Important Sheet"


# ════════════════════════════════════════════════════════════════════════
# Fix A — attach_python_block refuses empty output_columns on Pipedream parent
# ════════════════════════════════════════════════════════════════════════


def test_attach_python_block_refuses_empty_output_columns_for_pipedream_parent():
    """Fix A: prevents the silent-schema-overwrite footgun."""
    pipedream_parent = {
        "id": "p1",
        "typeId": "191db4a1-7c72-4c4a-af02-b507701ca61b",
        "settings_field_values": [
            {"field_name": "pipedream-google_sheets-google_sheets_add_single_row-conn",
             "field_value": "x"},
        ],
        "outputs": [{"columns": ["error", "summary", "payload"], "columns_metadata": None}],
        "position": {"x": 0, "y": 0},
    }
    fake_wf = {"id": "wf-1", "blocks": [pipedream_parent]}

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.lint", return_value=[]):
        result = attach_python_block(
            workflow_id="wf-1",
            parent_node_id="p1",
            name="bad-cc",
            code="def run(df):\n    return df\n",
            output_columns=[],  # the footgun
        )

    assert result["ok"] is False
    assert "Pipedream" in result["message"]
    assert result["stage"] == "pipedream_parent_schema_guard"


def test_attach_python_block_allows_empty_output_columns_for_non_pipedream_parent():
    """Sanity: the guard only applies to Pipedream parents — a CC→CC chain
    where the user truly doesn't need new columns should still work."""
    plain_parent = {
        "id": "p1",
        "typeId": "ae54c44f-60ee-47c4-91d7-eae7fa849133",  # Custom Code
        "settings_field_values": [{"field_name": "data_manipulation-custom_code-code",
                                    "field_value": "def run(df): return df"}],
        "outputs": [{"columns": ["foo", "bar"], "columns_metadata": []}],
        "position": {"x": 0, "y": 0},
    }
    fake_wf = {"id": "wf-1", "blocks": [plain_parent]}

    # The attach should NOT fail the Pipedream guard — it'll proceed past
    # the guard and hit the paste-and-wire path.
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.lint", return_value=[]), \
         patch("nrev_wf_mcp.server._attach_block_via_paste_and_wire",
               return_value=({"workflowConfigError": None, "isRunable": True, "blocks": [plain_parent]}, "new-id")), \
         patch("nrev_wf_mcp.server._new_block_error_from_paste", return_value=None), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value={"valid": True}):
        result = attach_python_block(
            workflow_id="wf-1",
            parent_node_id="p1",
            name="ok-cc",
            code="def run(df): return df",
            output_columns=[],  # legitimate for plain chain
        )

    # No Pipedream-guard failure
    assert result.get("stage") != "pipedream_parent_schema_guard"


# ════════════════════════════════════════════════════════════════════════
# Fix F — Pipedream row-level error detection
# ════════════════════════════════════════════════════════════════════════


def test_is_pipedream_block_detects_pipedream_settings():
    """Setting field_name starting with 'pipedream-' marks the block as Pipedream."""
    block = {"settings_field_values": [
        {"field_name": "pipedream-slack_v2-slack_v2_send_message-text", "field_value": "hi"},
    ]}
    assert _is_pipedream_block(block) is True


def test_is_pipedream_block_detects_pipedream_origin_node_type():
    """Block with outputs.columns_metadata[].origin_node_type=='pipedream.*'."""
    block = {
        "settings_field_values": [],
        "outputs": [{"columns_metadata": [
            {"column_name": "error", "origin_node_type": "pipedream.google_sheets.google_sheets_add_single_row"},
        ]}],
    }
    assert _is_pipedream_block(block) is True


def test_is_pipedream_block_false_for_plain_custom_code():
    block = {
        "settings_field_values": [
            {"field_name": "data_manipulation-custom_code-code", "field_value": "x"},
        ],
        "outputs": [{"columns_metadata": [
            {"column_name": "foo", "origin_node_type": "data_manipulation.custom_code"},
        ]}],
    }
    assert _is_pipedream_block(block) is False


def test_check_pipedream_row_error_finds_error_in_row():
    """When row[0].error_1 contains a JSON error envelope, the helper
    returns has_row_error=True with the parsed message."""
    err_envelope = '{"name": "Error", "message": "undefined is not an array or an array-like", "attribution": {"origin": "response_parsing"}}'
    fake_preview = {
        "rows": [{"error": "[]", "error_1": err_envelope, "summary": None, "payload": None}],
    }
    with patch("nrev_wf_mcp.server.api.get_node_preview", return_value=fake_preview):
        result = _check_pipedream_row_error("wf-1", "exec-1", "block-1")

    assert result is not None
    assert result["has_row_error"] is True
    assert "undefined is not an array" in result["row_error"]
    assert result["error_attribution"] == {"origin": "response_parsing"}


def test_check_pipedream_row_error_no_error_returns_false():
    """When row[0].error is empty / null, no error is reported."""
    fake_preview = {"rows": [{"error": None, "error_1": None, "summary": "ok", "payload": "{}"}]}
    with patch("nrev_wf_mcp.server.api.get_node_preview", return_value=fake_preview):
        result = _check_pipedream_row_error("wf-1", "exec-1", "block-1")

    assert result is not None
    assert result["has_row_error"] is False


def test_check_pipedream_row_error_parses_list_envelope():
    """The error column can be a JSON list-of-events envelope:
    [{"ts": ..., "k": "error", "err": {"name": ..., "message": ...}}]
    Helper should extract the message from err[0].err.message."""
    err_list = '[{"ts": 1779535877859, "k": "error", "err": {"name": "Error", "message": "Service unavailable"}}]'
    fake_preview = {"rows": [{"error": err_list, "summary": None, "payload": None}]}
    with patch("nrev_wf_mcp.server.api.get_node_preview", return_value=fake_preview):
        result = _check_pipedream_row_error("wf-1", "exec-1", "block-1")

    assert result["has_row_error"] is True
    assert "Service unavailable" in result["row_error"]


def test_maybe_enrich_pipedream_errors_skips_non_pipedream_blocks():
    """Only blocks marked as Pipedream get the row-level check."""
    plain_block = {"id": "plain", "settings_field_values": [
        {"field_name": "data_manipulation-custom_code-code", "field_value": "x"},
    ], "outputs": []}
    pipedream_block = {"id": "pd", "settings_field_values": [
        {"field_name": "pipedream-foo-bar-baz", "field_value": "y"},
    ], "outputs": []}

    raw_execution = {
        "id": "exec-1",
        "blockRuns": [
            {"workflowBlockId": "plain", "status": "completed", "error": None},
            {"workflowBlockId": "pd", "status": "completed", "error": None},
        ],
    }
    err_envelope = '{"name": "X", "message": "test error"}'

    fake_wf = {"blocks": [plain_block, pipedream_block]}

    def fake_preview(wf, exec_id, node_id, **kw):
        return {"rows": [{"error_1": err_envelope}]}

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.get_node_preview", side_effect=fake_preview):
        result = _maybe_enrich_pipedream_errors("wf-1", raw_execution)

    # Only the Pipedream block gets enriched
    assert "pd" in result
    assert "plain" not in result
    assert result["pd"]["has_row_error"] is True


def test_maybe_enrich_pipedream_errors_skips_blocks_with_block_level_error():
    """If the block reports a block-level error, no need to dig into rows —
    the surfacing happened at the right layer."""
    pipedream_block = {"id": "pd", "settings_field_values": [
        {"field_name": "pipedream-x-y-z", "field_value": "v"},
    ], "outputs": []}

    raw_execution = {
        "id": "exec-1",
        "blockRuns": [
            {"workflowBlockId": "pd", "status": "completed",
             "error": "platform-level error msg"},  # block-level error present
        ],
    }
    with patch("nrev_wf_mcp.server.api.get_workflow",
               return_value={"blocks": [pipedream_block]}):
        # We shouldn't call get_node_preview at all
        with patch("nrev_wf_mcp.server.api.get_node_preview") as gp:
            result = _maybe_enrich_pipedream_errors("wf-1", raw_execution)
            assert gp.call_count == 0
    assert result == {}


# ════════════════════════════════════════════════════════════════════════
# Fix D — cross-tenant connection label fallback
# ════════════════════════════════════════════════════════════════════════


def test_extract_pipedream_app_slug_from_field_name():
    """Helper for Fix D: extract app slug from field name."""
    assert _extract_pipedream_app_slug(
        "pipedream-google_sheets-google_sheets_add_single_row-googleSheets_connection_id"
    ) == "google_sheets"
    assert _extract_pipedream_app_slug(
        "pipedream-slack_v2-slack_v2_send_message-slack_v2"
    ) == "slack_v2"
    assert _extract_pipedream_app_slug("not-a-pipedream-field") is None
    assert _extract_pipedream_app_slug("") is None
    assert _extract_pipedream_app_slug(None) is None


def test_resolve_connection_label_falls_back_to_filtered_list_for_cross_tenant():
    """Fix D: when the JWT-user's own connections don't include the target_value,
    fall back to list_connections(connection_app_id=...) to pick up teammates'
    connections."""
    # First call (unfiltered) returns only the JWT user's connections — no match
    first_call_connections = [
        {"connectionId": "my-own-conn", "connectionName": "My Gmail"},
    ]
    # Second call (filtered by app_id) includes teammate's connection
    second_call_connections = [
        {"connectionId": "my-own-conn", "connectionName": "My Gmail"},
        {"connectionId": "teammate-conn", "connectionName": "Sayanta's Sheets"},
    ]

    call_count = {"n": 0}

    def fake_list_conns(connection_app_id=None):
        call_count["n"] += 1
        if connection_app_id is None:
            return first_call_connections
        return second_call_connections

    with patch("nrev_wf_mcp.server.api.list_connections", side_effect=fake_list_conns), \
         patch("nrev_wf_mcp.server._connection_app_id_for_slug", return_value="app-id-1"):
        label = _resolve_connection_label(
            "teammate-conn",
            field_name="pipedream-google_sheets-google_sheets_add_single_row-googleSheets_connection_id",
        )

    assert label == "Sayanta's Sheets"
    assert call_count["n"] == 2  # unfiltered first, then filtered


def test_resolve_connection_label_returns_label_from_first_call_if_match():
    """If the JWT-user owns the connection, the first call suffices and we
    don't need the fallback."""
    own = [{"connectionId": "owned", "connectionName": "Mine"}]
    with patch("nrev_wf_mcp.server.api.list_connections", return_value=own) as lc:
        label = _resolve_connection_label("owned", field_name="pipedream-x-y-z")
    assert label == "Mine"
    # Only one call — no fallback needed
    assert lc.call_count == 1


def test_resolve_connection_label_returns_none_when_unresolvable():
    """If no match at all, return None silently — don't raise."""
    with patch("nrev_wf_mcp.server.api.list_connections", return_value=[]), \
         patch("nrev_wf_mcp.server._connection_app_id_for_slug", return_value=None):
        label = _resolve_connection_label("nope", field_name="pipedream-x-y-z")
    assert label is None
