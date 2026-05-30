"""Tests for v0.2.27 fixes:

  Fix #1: row-level error detection extended to nrev_tables.* nodes (was
          Pipedream-only). nrev_tables.add_row reports `status: completed,
          error: null` while every row failed a cell type-mismatch check —
          identical pattern to Pipedream silent failures. _block_has_silent_
          row_errors() now catches both.

  Fix #2: attach_node docstring includes nRev Tables section (envelope shape,
          template syntax, type-coercion gotcha).

  Fix #3: find_workflows_using_resource — the only monitoring tool greenlit.
          Parallel scan, name-pinpointed matches, embedded node info covers
          find_node_writing_to use case without a separate tool.
"""
from unittest.mock import patch
from nrev_wf_mcp.server import (
    attach_node,
    _block_has_silent_row_errors,
    _is_pipedream_block,
    _settings_contain_value,
    _RESOURCE_APP_MAP,
    find_workflows_using_resource,
    check_node_errors,
)


# ════════════════════════════════════════════════════════════════════════
# Fix #1 — row-level error detection covers nrev_tables nodes
# ════════════════════════════════════════════════════════════════════════


def test_block_has_silent_row_errors_detects_pipedream():
    """Backward compat — still True for Pipedream-shaped blocks."""
    block = {
        "settings_field_values": [
            {"field_name": "pipedream-slack_v2-slack_v2_send_message-text",
             "field_value": "hi"},
        ],
        "outputs": [],
    }
    assert _block_has_silent_row_errors(block) is True


def test_block_has_silent_row_errors_detects_nrev_tables_via_settings():
    """v0.2.27 — nrev_tables.add_row has same silent-row-error pattern."""
    block = {
        "settings_field_values": [
            {"field_name": "nrev_tables-add_row-table_id",
             "field_value": "abc-123"},
        ],
        "outputs": [],
    }
    assert _block_has_silent_row_errors(block) is True


def test_block_has_silent_row_errors_detects_nrev_tables_via_origin_type():
    """A node attached via paste_nodes may have nrev_tables origin in
    output columns_metadata even if settings haven't been hydrated yet."""
    block = {
        "settings_field_values": [],
        "outputs": [{
            "columns_metadata": [
                {"origin_node_type": "nrev_tables.add_row"}
            ]
        }],
    }
    assert _block_has_silent_row_errors(block) is True


def test_block_has_silent_row_errors_false_for_magic_node():
    """Magic Nodes don't have this silent-failure pattern."""
    block = {
        "settings_field_values": [
            {"field_name": "data_manipulation-magic_node-code",
             "field_value": "def run(df1): return df1"},
        ],
        "outputs": [{
            "columns_metadata": [
                {"origin_node_type": "data_manipulation.magic_node"}
            ]
        }],
    }
    assert _block_has_silent_row_errors(block) is False


def test_check_pipedream_row_error_handles_data_key():
    """v0.2.27 pre-existing bug fix: real prod responses put rows under
    `data`, not `entries` or `rows`. The helper was silently returning
    no-error for any real execution. Now accepts all three keys."""
    from nrev_wf_mcp.server import _check_pipedream_row_error
    fake_preview = {
        "data": [{"error": '{"add_single_row": "Cell type mismatch"}'}],
        "meta": {"total_entries": 1, "skip": 0, "limit": 1},
    }
    with patch("nrev_wf_mcp.server.api.get_node_preview", return_value=fake_preview):
        result = _check_pipedream_row_error("wf-1", "exec-1", "block-1")
    assert result["has_row_error"] is True
    assert "Cell type mismatch" in result["row_error"]


def test_check_node_errors_now_scans_nrev_tables_nodes():
    """v0.2.27 regression test: nrev_tables nodes were previously in
    skipped_non_pipedream. Now they're checked."""
    err_envelope = '{"name": "Error", "message": "Cell type mismatch for column ...: expected number, got str."}'
    fake_execution = {
        "id": "exec-1",
        "blockRuns": [
            {"workflowBlockId": "add-row-1", "workflowBlockName": "Add Row",
             "status": "completed", "error": None},
        ],
    }
    nrev_table_block = {
        "id": "add-row-1",
        "settings_field_values": [
            {"field_name": "nrev_tables-add_row-table_id", "field_value": "t1"},
        ],
        "outputs": [],
    }
    fake_wf = {"blocks": [nrev_table_block]}
    fake_preview = {"rows": [{"error": "[]", "error_1": err_envelope}]}

    with patch("nrev_wf_mcp.server.api.get_execution_detail", return_value=fake_execution), \
         patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.get_node_preview", return_value=fake_preview):
        result = check_node_errors(workflow_id="wf-1", execution_id="exec-1")

    # Pre-fix this would be 0 because nrev_tables.add_row failed _is_pipedream_block
    assert result["checked_block_count"] == 1
    assert len(result["blocks_with_errors"]) == 1
    assert "Cell type mismatch" in result["blocks_with_errors"][0]["row_error"]
    # And it's NOT in skipped_non_pipedream
    assert "add-row-1" not in result["skipped_non_pipedream"]


# ════════════════════════════════════════════════════════════════════════
# Fix #2 — attach_node docstring mentions nRev Tables nodes
# ════════════════════════════════════════════════════════════════════════


def test_attach_node_docstring_covers_nrev_tables():
    """The docstring teaches the 3 nrev_tables gotchas the 2026-05-26 prod
    test surfaced. Without this, agents will hit them blind."""
    doc = attach_node.__doc__ or ""
    # Section heading
    assert "NREV TABLES" in doc.upper(), "docstring should have a nrev_tables section"
    # The 4 typeIds documented
    assert "nrev_tables.query_table" in doc.lower() or "query table" in doc.lower()
    assert "add row" in doc.lower()
    # Envelope shape warning
    assert "envelope" in doc.lower() or "list-of-lists" in doc.lower() \
        or "{\"field_name\": \"column_id\"" in doc
    # Template syntax callout
    assert "{{column_name}}" in doc or "{{name}}" in doc or "data." in doc
    # Type-coercion gotcha
    assert "string" in doc.lower() and ("number" in doc.lower() or "boolean" in doc.lower())


# ════════════════════════════════════════════════════════════════════════
# Fix #3 — find_workflows_using_resource
# ════════════════════════════════════════════════════════════════════════


def test_resource_app_map_covers_known_apps():
    """At least Sheets, Slack, and nrev_tables are wired up."""
    assert "google_sheets" in _RESOURCE_APP_MAP
    assert "slack" in _RESOURCE_APP_MAP
    assert "nrev_tables" in _RESOURCE_APP_MAP
    for app, spec in _RESOURCE_APP_MAP.items():
        assert "type_ids" in spec and isinstance(spec["type_ids"], dict)
        # v0.2.28: field_fragment may be str OR tuple/list of str (Slack uses
        # ("conversation", "channel") because field naming is inconsistent).
        assert "field_fragment" in spec
        ff = spec["field_fragment"]
        assert isinstance(ff, (str, tuple, list))
        if isinstance(ff, (tuple, list)):
            assert all(isinstance(x, str) for x in ff)
        assert len(spec["type_ids"]) >= 1


def test_settings_contain_value_walks_nested_settings():
    """Settings can be deeply nested (e.g. magic_node-instructions_and_ref
    nests references inside instructions inside the parent). The walker
    must recurse."""
    nested_settings = [
        {"field_name": "outer", "field_value": [
            {"field_name": "inner", "field_value": "no-match"},
            {"field_name": "deep-sheetId", "field_value": "TARGET_SHEET"},
        ]},
    ]
    hits = _settings_contain_value(nested_settings, "TARGET_SHEET", "sheetId")
    assert len(hits) == 1
    assert hits[0]["field_name"] == "deep-sheetId"


def test_settings_contain_value_respects_key_fragment():
    """Don't match on every field that happens to have the same value."""
    settings = [
        {"field_name": "spreadsheet-sheetId", "field_value": "X"},
        {"field_name": "totally-unrelated-field", "field_value": "X"},
    ]
    hits = _settings_contain_value(settings, "X", key_fragment="sheetId")
    assert len(hits) == 1
    assert hits[0]["field_name"] == "spreadsheet-sheetId"


def test_find_workflows_using_resource_unknown_app_returns_error():
    """Don't silently scan zero workflows when the app keyword is bogus."""
    result = find_workflows_using_resource(app="bogus_app", resource_id="X")
    assert result["matches"] == []
    assert "error" in result["scan_meta"]
    assert "bogus_app" in result["scan_meta"]["error"]


def test_find_workflows_using_resource_filters_stale_workflows():
    """active_within_days=30 should skip workflows whose lastRunAt is older."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(days=5)).isoformat().replace("+00:00", "Z")
    stale = (now - timedelta(days=200)).isoformat().replace("+00:00", "Z")

    fake_list = {
        "data": [
            {"id": "wf-fresh", "lastRunAt": fresh},
            {"id": "wf-stale", "lastRunAt": stale},
            {"id": "wf-never", "lastRunAt": None},
        ]
    }

    # The fresh workflow has a matching Sheets node; stale + never don't
    fresh_wf = {
        "name": "Fresh WF",
        "blocks": [{
            "id": "n1", "typeId": "191db4a1-7c72-4c4a-af02-b507701ca61b",
            "variableName": "Add Row",
            "settings_field_values": [
                {"field_name": "pipedream-google_sheets-google_sheets_add_single_row-sheetId",
                 "field_value": "TARGET"},
            ],
        }],
    }

    def fake_get_workflow(wf_id):
        if wf_id == "wf-fresh":
            return fresh_wf
        return {"blocks": []}

    with patch("nrev_wf_mcp.server.api.list_workflows", return_value=fake_list), \
         patch("nrev_wf_mcp.server.api.get_workflow", side_effect=fake_get_workflow):
        result = find_workflows_using_resource(
            app="google_sheets", resource_id="TARGET", active_within_days=30
        )

    # Only fresh workflow was scanned; stale + never skipped
    assert result["scan_meta"]["workflows_scanned"] == 1
    assert result["scan_meta"]["workflows_skipped_stale"] == 2
    assert result["scan_meta"]["matches_count"] == 1
    assert result["matches"][0]["workflow_id"] == "wf-fresh"


def test_find_workflows_using_resource_match_includes_node_pinpoint():
    """Each match should include the exact nodes — this is what makes a
    separate find_node_writing_to tool redundant."""
    fake_wf = {
        "name": "Multi-Sheet WF",
        "blocks": [
            {
                "id": "node-1",
                "typeId": "ce01c704-f6bd-40d5-9b2b-f545495de14b",  # Get Values in Range
                "variableName": "Read leads",
                "settings_field_values": [
                    {"field_name": "pipedream-google_sheets-google_sheets_get_values_in_range-sheetId",
                     "field_value": "SHEET_A"},
                ],
            },
            {
                "id": "node-2",
                "typeId": "191db4a1-7c72-4c4a-af02-b507701ca61b",  # Add Single Row
                "variableName": "Write summary",
                "settings_field_values": [
                    {"field_name": "pipedream-google_sheets-google_sheets_add_single_row-sheetId",
                     "field_value": "SHEET_A"},
                ],
            },
            {
                "id": "node-3",
                "typeId": "ce01c704-f6bd-40d5-9b2b-f545495de14b",
                "variableName": "Read other sheet",
                "settings_field_values": [
                    {"field_name": "pipedream-google_sheets-google_sheets_get_values_in_range-sheetId",
                     "field_value": "DIFFERENT_SHEET"},
                ],
            },
        ],
    }

    with patch("nrev_wf_mcp.server.api.list_workflows",
                return_value={"data": [{"id": "wf-1", "lastRunAt": "2099-01-01T00:00:00Z"}]}), \
         patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf):
        result = find_workflows_using_resource(
            app="google_sheets", resource_id="SHEET_A",
            active_within_days=0,  # 0 = no filter (per docstring)
        )

    assert len(result["matches"]) == 1
    nodes = result["matches"][0]["matching_nodes"]
    assert len(nodes) == 2  # only the 2 nodes referencing SHEET_A
    node_ids = {n["node_id"] for n in nodes}
    assert node_ids == {"node-1", "node-2"}
    # Each node has the type label
    types = {n["type"] for n in nodes}
    assert "Get Values in Range" in types
    assert "Add Single Row" in types
    # And the matched_field for downstream inspection
    for n in nodes:
        assert "sheetId" in n["matched_field"]


def test_find_workflows_using_resource_handles_nrev_tables():
    """Same pattern works for nrev table id lookups."""
    fake_wf = {
        "name": "Uses Table X",
        "blocks": [{
            "id": "n1", "typeId": "a1b2c3d4-0001-4000-8000-000000000001",  # Add Row
            "variableName": "Insert",
            "settings_field_values": [
                {"field_name": "nrev_tables-add_row-table_id", "field_value": "TABLE_X"},
            ],
        }],
    }
    with patch("nrev_wf_mcp.server.api.list_workflows",
                return_value={"data": [{"id": "wf-1", "lastRunAt": "2099-01-01T00:00:00Z"}]}), \
         patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf):
        result = find_workflows_using_resource(
            app="nrev_tables", resource_id="TABLE_X",
            active_within_days=0,  # 0 = no filter (per docstring)
        )

    assert result["scan_meta"]["matches_count"] == 1
    assert result["matches"][0]["matching_nodes"][0]["type"] == "Add Row"
