"""Tests for v0.2.32 — five changes, all about analyzing data outside the
100-row pagination window:

  Fix #1: get_node_output(search="...") — forwards to API's search_string,
          server-side cross-table substring filter (matches the UI's data-
          preview search).

  Fix #2: NEW download_node_output — auto-paginates internally and writes
          JSONL to disk for offline analysis with pandas/duckdb/jq.

  Fix #3: NEW tables_delete_rows — bulk delete via /rows/bulk-delete,
          handles single via 1-element list. Plus backward-compat
          tables_delete_row shim.

  Fix #4: NEW tables_aggregate — server-side count/sum/avg/min/max with
          group_by + cross-table joins. Quirks: filter shape uses
          `operator` not `op`, value as list, lowercase string booleans.
          Name-resolves keys back to readable column names.

  Fix #5: NEW tables_distinct_values — unique column values with filter +
          search + limit. Accepts column_id OR column_name.

  Fix #6: NEW tables_join — multi-table joins. Resolves API's prefix-keyed
          row dicts (base./j0./j1.) back to human-readable names, with
          table prefixing for same-named columns across tables.

  Fix #7: get_node_dynamic_fields — for native nodes, fall through to
          /node_definitions/{typeId} and return its `settings` array
          (with dataSource.options dropdown values + conditionalVisibility
          cross-field constraints). Replaces the v0.2.28 "use the cookbook"
          pointer with actual introspection.
"""
from __future__ import annotations

import json as _json
import os
import tempfile
from unittest.mock import patch, MagicMock

from nrev_wf_mcp.server import (
    download_node_output,
    get_node_dynamic_fields,
    get_node_output,
    tables_aggregate,
    tables_delete_row,
    tables_delete_rows,
    tables_distinct_values,
    tables_join,
)


# ════════════════════════════════════════════════════════════════════════
# Fix #1 — get_node_output(search=...)
# ════════════════════════════════════════════════════════════════════════


def test_get_node_output_forwards_search_to_search_string():
    """The search param must be forwarded to the API call as search_string."""
    fake_api = MagicMock(return_value={
        "data": [{"name": "acme corp"}, {"name": "acme labs"}],
        "meta": {"total_entries": 2, "skip": 0, "limit": 50},
    })
    with patch("nrev_wf_mcp.server.api.get_node_preview", fake_api):
        result = get_node_output(
            workflow_id="wf", execution_id="exec", node_id="node",
            search="acme",
        )
    fake_api.assert_called_once()
    assert fake_api.call_args.kwargs["search_string"] == "acme"
    assert result["rows"] == [{"name": "acme corp"}, {"name": "acme labs"}]


def test_get_node_output_omits_search_when_none():
    """Default behavior: no search param → search_string=None passed through."""
    fake_api = MagicMock(return_value={"data": [], "meta": {}})
    with patch("nrev_wf_mcp.server.api.get_node_preview", fake_api):
        get_node_output(workflow_id="wf", execution_id="exec", node_id="node")
    assert fake_api.call_args.kwargs["search_string"] is None


# ════════════════════════════════════════════════════════════════════════
# Fix #2 — download_node_output
# ════════════════════════════════════════════════════════════════════════


def _fake_paginated_api(total_rows: int, columns: list[str]):
    """Return a fake get_node_preview that simulates a paginated dataset."""
    rows = [{c: f"{c}_{i}" for c in columns} for i in range(total_rows)]
    def _f(wf, exec_id, node_id, **kwargs):
        skip = kwargs.get("skip", 0)
        limit = kwargs.get("limit", 100)
        page = rows[skip:skip + limit]
        return {"data": page, "meta": {"total_entries": total_rows,
                                          "skip": skip, "limit": limit}}
    return _f


def test_download_node_output_writes_jsonl_to_disk():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "out.jsonl")
        with patch("nrev_wf_mcp.server.api.get_node_preview",
                    _fake_paginated_api(5, ["a", "b"])):
            result = download_node_output(
                workflow_id="wf", execution_id="exec", node_id="node",
                target_path=target,
            )
        assert result["ok"] is True
        assert result["path"] == target
        assert result["total_rows_downloaded"] == 5
        assert result["complete"] is True
        # File exists, one JSON per line
        with open(target) as fh:
            lines = fh.read().strip().split("\n")
        assert len(lines) == 5
        for line in lines:
            d = _json.loads(line)
            assert "a" in d and "b" in d


def test_download_node_output_auto_paginates_at_api_cap():
    """5 pages of 100 = 500 rows in 5 round-trips."""
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "out.jsonl")
        fake_api = MagicMock(side_effect=_fake_paginated_api(500, ["x"]))
        with patch("nrev_wf_mcp.server.api.get_node_preview", fake_api):
            result = download_node_output(
                workflow_id="wf", execution_id="exec", node_id="node",
                target_path=target,
            )
        assert result["total_rows_downloaded"] == 500
        # First few calls used limit=100 (the API cap)
        first_call = fake_api.call_args_list[0]
        assert first_call.kwargs["limit"] == 100
        # ~5 rounds expected (500 / 100); could be 6 because of "last short page" logic
        assert result["rounds"] in (5, 6)


def test_download_node_output_respects_max_rows():
    """max_rows truncates the download before exhausting the dataset."""
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "out.jsonl")
        with patch("nrev_wf_mcp.server.api.get_node_preview",
                    _fake_paginated_api(1000, ["x"])):
            result = download_node_output(
                workflow_id="wf", execution_id="exec", node_id="node",
                target_path=target, max_rows=250,
            )
        assert result["total_rows_downloaded"] == 250
        assert result["complete"] is False
        assert "Truncated" in result["note"]


def test_download_node_output_refuses_existing_file_without_overwrite():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "exists.jsonl")
        with open(target, "w") as fh:
            fh.write("preexisting\n")
        with patch("nrev_wf_mcp.server.api.get_node_preview",
                    _fake_paginated_api(10, ["x"])):
            result = download_node_output(
                workflow_id="wf", execution_id="exec", node_id="node",
                target_path=target,
            )
        assert result["ok"] is False
        assert result["error_kind"] == "file_exists"
        # Untouched
        assert open(target).read() == "preexisting\n"


def test_download_node_output_overwrite_true_replaces_file():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "exists.jsonl")
        with open(target, "w") as fh:
            fh.write("preexisting\n")
        with patch("nrev_wf_mcp.server.api.get_node_preview",
                    _fake_paginated_api(2, ["x"])):
            result = download_node_output(
                workflow_id="wf", execution_id="exec", node_id="node",
                target_path=target, overwrite=True,
            )
        assert result["ok"] is True
        # Replaced
        with open(target) as fh:
            assert "preexisting" not in fh.read()


def test_download_node_output_forwards_search_to_api():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "out.jsonl")
        fake_api = MagicMock(side_effect=_fake_paginated_api(3, ["x"]))
        with patch("nrev_wf_mcp.server.api.get_node_preview", fake_api):
            download_node_output(
                workflow_id="wf", execution_id="exec", node_id="node",
                target_path=target, search="acme",
            )
        assert fake_api.call_args_list[0].kwargs["search_string"] == "acme"


def test_download_node_output_projects_columns():
    """columns=[...] writes only the requested keys per row."""
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "out.jsonl")
        with patch("nrev_wf_mcp.server.api.get_node_preview",
                    _fake_paginated_api(3, ["a", "b", "c"])):
            download_node_output(
                workflow_id="wf", execution_id="exec", node_id="node",
                target_path=target, columns=["a"],
            )
        with open(target) as fh:
            for line in fh:
                d = _json.loads(line)
                assert list(d.keys()) == ["a"]


def test_download_node_output_returns_sample_commands():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "out.jsonl")
        with patch("nrev_wf_mcp.server.api.get_node_preview",
                    _fake_paginated_api(2, ["x"])):
            result = download_node_output(
                workflow_id="wf", execution_id="exec", node_id="node",
                target_path=target,
            )
        cmds = result["sample_commands"]
        assert "pandas" in cmds
        assert "duckdb" in cmds
        # Each command embeds the actual path so it's copy-pasteable
        assert target in cmds["pandas"]
        assert target in cmds["duckdb"]


def test_download_node_output_rejects_max_rows_above_ceiling():
    """Hard cap at 1,000,000 — refuses larger values."""
    result = download_node_output(
        workflow_id="wf", execution_id="exec", node_id="node",
        max_rows=10_000_000,
    )
    assert result["ok"] is False
    assert result["error_kind"] == "max_rows_too_high"


# ════════════════════════════════════════════════════════════════════════
# Fix #3 — tables_delete_rows
# ════════════════════════════════════════════════════════════════════════


def test_tables_delete_rows_requires_confirm():
    """Destructive op — refuses without confirm=True."""
    result = tables_delete_rows(table_id="t1", row_ids=[1, 2])
    assert result["ok"] is False
    assert "confirm" in result["message"].lower()


def test_tables_delete_rows_calls_bulk_endpoint():
    fake_api = MagicMock(return_value={
        "deleted_row_ids": [1, 2, 3],
        "table": {"row_count": 5, "last_updated_at": "..."},
    })
    with patch("nrev_wf_mcp.server.tables_api.bulk_delete_rows", fake_api):
        result = tables_delete_rows(
            table_id="t1", row_ids=[1, 2, 3], confirm=True,
        )
    fake_api.assert_called_once_with("t1", [1, 2, 3])
    assert result["ok"] is True
    assert result["deleted_row_ids"] == [1, 2, 3]
    assert result["skipped_row_ids"] == []


def test_tables_delete_rows_surfaces_skipped_ids():
    """When the bulk endpoint silently skips missing ids, the wrapper
    computes the diff and reports skipped."""
    fake_api = MagicMock(return_value={
        "deleted_row_ids": [1, 3],  # 2 was missing
        "table": {"row_count": 5},
    })
    with patch("nrev_wf_mcp.server.tables_api.bulk_delete_rows", fake_api):
        result = tables_delete_rows(
            table_id="t1", row_ids=[1, 2, 3], confirm=True,
        )
    assert result["deleted_row_ids"] == [1, 3]
    assert result["skipped_row_ids"] == [2]


def test_tables_delete_rows_rejects_empty_input():
    result = tables_delete_rows(table_id="t1", row_ids=[], confirm=True)
    assert result["ok"] is False
    assert "empty" in result["message"].lower()


def test_tables_delete_rows_rejects_over_1000():
    """The bulk endpoint caps at 1000 — wrapper enforces."""
    result = tables_delete_rows(
        table_id="t1", row_ids=list(range(1001)), confirm=True,
    )
    assert result["ok"] is False
    assert "1000" in result["message"]


def test_tables_delete_row_backward_compat_shim():
    """The deprecated singular form still works — delegates to the new
    list-based tool with a 1-element list."""
    fake_api = MagicMock(return_value={
        "deleted_row_ids": [42], "table": {"row_count": 0},
    })
    with patch("nrev_wf_mcp.server.tables_api.bulk_delete_rows", fake_api):
        result = tables_delete_row(
            table_id="t1", row_id=42, confirm=True,
        )
    fake_api.assert_called_once_with("t1", [42])
    assert result["ok"] is True
    assert result["deleted_row_ids"] == [42]


# ════════════════════════════════════════════════════════════════════════
# Fix #4 — tables_aggregate
# ════════════════════════════════════════════════════════════════════════


def test_tables_aggregate_name_resolves_group_keys():
    """The API returns keys as {col_id: value}; the wrapper rewrites to
    {col_name: value} using the table schema for ergonomics."""
    fake_aggregate = MagicMock(return_value={
        "groups": [
            {"keys": {"col-region-id": "US"}, "measures": {"total": 590.0}},
            {"keys": {"col-region-id": "EU"}, "measures": {"total": 250.0}},
        ],
        "meta": {"group_count": 2, "truncated": False},
    })
    fake_schema = MagicMock(return_value={
        "columns": [{"id": "col-region-id", "name": "region"}]
    })
    with patch("nrev_wf_mcp.server.tables_api.aggregate", fake_aggregate), \
         patch("nrev_wf_mcp.server.tables_api.get_table", fake_schema):
        result = tables_aggregate(
            table_id="t1",
            measures=[{"op": "sum", "column_id": "col-amount-id", "alias": "total"}],
            group_by=[{"column_id": "col-region-id"}],
        )
    assert result["groups"] == [
        {"keys": {"region": "US"}, "measures": {"total": 590.0}},
        {"keys": {"region": "EU"}, "measures": {"total": 250.0}},
    ]


def test_tables_aggregate_resolve_key_names_can_be_disabled():
    """When the caller wants the raw API shape, pass resolve_key_names=False."""
    fake_aggregate = MagicMock(return_value={
        "groups": [{"keys": {"col-id": "X"}, "measures": {"n": 1}}],
        "meta": {"group_count": 1},
    })
    with patch("nrev_wf_mcp.server.tables_api.aggregate", fake_aggregate), \
         patch("nrev_wf_mcp.server.tables_api.get_table") as get_table:
        result = tables_aggregate(
            table_id="t1",
            measures=[{"op": "count", "alias": "n"}],
            group_by=[{"column_id": "col-id"}],
            resolve_key_names=False,
        )
    # No schema lookup performed when resolve is off
    get_table.assert_not_called()
    assert result["groups"][0]["keys"] == {"col-id": "X"}


def test_tables_aggregate_rejects_empty_measures():
    result = tables_aggregate(table_id="t1", measures=[])
    assert result["ok"] is False


def test_tables_aggregate_resolves_keys_across_joined_tables():
    """When grouping by a column in a joined table, the wrapper looks up
    BOTH tables' schemas to resolve the key name."""
    fake_aggregate = MagicMock(return_value={
        "groups": [{"keys": {"col-tier-id": "gold"}, "measures": {"total": 500.0}}],
        "meta": {"group_count": 1},
    })
    schemas = {
        "orders-id": {"columns": [{"id": "col-region-id", "name": "region"}]},
        "customers-id": {"columns": [{"id": "col-tier-id", "name": "tier"}]},
    }
    with patch("nrev_wf_mcp.server.tables_api.aggregate", fake_aggregate), \
         patch("nrev_wf_mcp.server.tables_api.get_table",
                side_effect=lambda tid: schemas[tid]):
        result = tables_aggregate(
            table_id="orders-id",
            measures=[{"op": "sum", "column_id": "col-amount", "alias": "total"}],
            group_by=[{"table_id": "customers-id", "column_id": "col-tier-id"}],
            joins=[{"type": "left", "table_id": "customers-id",
                    "on": {"base_column_id": "x", "joined_column_id": "y"}}],
        )
    assert result["groups"][0]["keys"] == {"tier": "gold"}


# ════════════════════════════════════════════════════════════════════════
# Fix #5 — tables_distinct_values
# ════════════════════════════════════════════════════════════════════════


def test_tables_distinct_values_accepts_uuid_directly():
    fake_api = MagicMock(return_value={
        "values": ["US", "EU"], "meta": {"total_distinct": 2}
    })
    with patch("nrev_wf_mcp.server.tables_api.distinct_values", fake_api):
        result = tables_distinct_values(
            table_id="t1",
            column_id_or_name="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        )
    fake_api.assert_called_once()
    assert fake_api.call_args.args[1] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert result["values"] == ["US", "EU"]


def test_tables_distinct_values_resolves_name_to_uuid():
    """Caller passes a human name → wrapper resolves via the schema."""
    fake_api = MagicMock(return_value={"values": ["US"], "meta": {}})
    fake_schema_pairs = {"region": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}
    with patch("nrev_wf_mcp.server.tables_api.distinct_values", fake_api), \
         patch("nrev_wf_mcp.server._tables_resolve_name_map",
                return_value=fake_schema_pairs):
        tables_distinct_values(
            table_id="t1", column_id_or_name="region",
        )
    # API got the resolved UUID, not the name
    assert fake_api.call_args.args[1] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_tables_distinct_values_reports_missing_column():
    with patch("nrev_wf_mcp.server._tables_resolve_name_map",
                return_value={"region": "col-id"}):
        result = tables_distinct_values(
            table_id="t1", column_id_or_name="bogus",
        )
    assert result["ok"] is False
    assert "bogus" in result["message"]


# ════════════════════════════════════════════════════════════════════════
# Fix #6 — tables_join
# ════════════════════════════════════════════════════════════════════════


def test_tables_join_resolves_prefixed_keys_to_names():
    """API returns rows with base.<col_id> / j0.<col_id> keys; the wrapper
    rewrites to readable names (with table prefix when collision)."""
    fake_join = MagicMock(return_value={
        "rows": [
            {"base.col-cust-id": "Acme",
             "base.col-amount-id": 120,
             "j0.col-tier-id": "gold"},
        ],
        "meta": {"total_entries": 1, "skip": 0, "limit": 100},
    })
    schemas = {
        "orders-id": {
            "name": "orders",
            "columns": [
                {"id": "col-cust-id", "name": "customer"},
                {"id": "col-amount-id", "name": "amount"},
            ],
        },
        "customers-id": {
            "name": "customers",
            "columns": [
                {"id": "col-cust-id-c", "name": "customer"},  # name collision
                {"id": "col-tier-id", "name": "tier"},
            ],
        },
    }
    with patch("nrev_wf_mcp.server.tables_api.join_tables", fake_join), \
         patch("nrev_wf_mcp.server.tables_api.get_table",
                side_effect=lambda tid: schemas[tid]):
        result = tables_join(
            base_table_id="orders-id",
            joins=[{"type": "left", "table_id": "customers-id",
                    "on": {"base_column_id": "col-cust-id",
                           "joined_column_id": "col-cust-id-c"}}],
        )
    row = result["rows"][0]
    # `customer` exists in BOTH tables → prefixed
    assert "orders.customer" in row
    assert row["orders.customer"] == "Acme"
    # `amount` is unique to orders → unprefixed
    assert row["amount"] == 120
    # `tier` is unique to customers → unprefixed
    assert row["tier"] == "gold"


def test_tables_join_can_disable_resolution():
    fake_join = MagicMock(return_value={
        "rows": [{"base.col-id": "v"}], "meta": {},
    })
    with patch("nrev_wf_mcp.server.tables_api.join_tables", fake_join), \
         patch("nrev_wf_mcp.server.tables_api.get_table") as get_table:
        result = tables_join(
            base_table_id="t1",
            joins=[{"type": "inner", "table_id": "t2",
                    "on": {"base_column_id": "a", "joined_column_id": "b"}}],
            resolve_column_names=False,
        )
    get_table.assert_not_called()
    assert result["rows"][0] == {"base.col-id": "v"}


def test_tables_join_rejects_empty_joins():
    result = tables_join(base_table_id="t1", joins=[])
    assert result["ok"] is False


# ════════════════════════════════════════════════════════════════════════
# Fix #7 — get_node_dynamic_fields native-node fallback
# ════════════════════════════════════════════════════════════════════════


def test_get_node_dynamic_fields_native_returns_catalog_settings():
    """When the dynamic-config call fails (native node), fall through to
    the catalog and return its `settings` array — NOT a "use cookbook"
    pointer."""
    fake_wf = {
        "blocks": [{
            "id": "node-1",
            "typeId": "ai-typeid",
            "settings_field_values": [{"field_name": "ai_toolkit-ask_ai-prompt"}],
        }],
    }
    fake_catalog = {
        "name": "Ask AI",
        "category": "AI Toolkit",
        "type": "ai_toolkit.ask_ai",
        "is_trigger": False,
        "isListener": False,
        "startingPrice": 1,
        "settings": [
            {
                "name": "ai_toolkit-ask_ai-model",
                "type": "select",
                "label": "Model",
                "required": False,
                "defaultValue": "gpt-4.1",
                "dataSource": {
                    "type": "static",
                    "options": [
                        {"label": "GPT-5", "value": "gpt-5"},
                        {"label": "Claude Opus 4.7",
                         "value": "CLAUDE_OPUS_4_7_INFERENCE_PROFILE_URN"},
                    ],
                },
            },
            {
                "name": "ai_toolkit-ask_ai-web_search_enabled",
                "type": "boolean",
                "label": "Web Search Enabled",
                "required": False,
                "defaultValue": False,
                "conditionalVisibility": {
                    "condition": "in",
                    "field_name": "ai_toolkit-ask_ai-model",
                    "field_value": ["gpt-5", "gpt-4.1"],
                },
            },
        ],
    }
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.updated_node_config",
                side_effect=Exception("HTTP 500")), \
         patch("nrev_wf_mcp.server.api.get_node_definition",
                return_value=fake_catalog):
        result = get_node_dynamic_fields(
            workflow_id="wf", node_id="node-1",
            field_name_changed="ai_toolkit-ask_ai-prompt",
        )

    assert result["ok"] is True
    assert result["source"] == "node_definition_catalog"
    assert result["node_name"] == "Ask AI"
    assert result["field_count"] == 2

    # Model dropdown options must be present
    model_field = next(f for f in result["fields"]
                       if f["name"] == "ai_toolkit-ask_ai-model")
    assert model_field["type"] == "select"
    assert model_field["options"][0]["value"] == "gpt-5"
    assert any("CLAUDE" in o["value"] for o in model_field["options"])

    # Conditional visibility on web_search must be preserved
    web = next(f for f in result["fields"]
               if f["name"] == "ai_toolkit-ask_ai-web_search_enabled")
    assert web["conditional_visibility"]["condition"] == "in"
    assert "gpt-5" in web["conditional_visibility"]["field_value"]

    # Select fields surface in dropdown_field_names for quick scanning
    assert "ai_toolkit-ask_ai-model" in result["dropdown_field_names"]


def test_get_node_dynamic_fields_native_catalog_failure_falls_back_to_guidance():
    """If the catalog fetch ALSO fails, return a structured error pointing
    at the cookbook (pre-v0.2.32 behavior preserved as final fallback)."""
    fake_wf = {
        "blocks": [{
            "id": "node-1",
            "typeId": "native-typeid",
            "settings_field_values": [{"field_name": "a"}],
        }],
    }
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.updated_node_config",
                side_effect=Exception("HTTP 500")), \
         patch("nrev_wf_mcp.server.api.get_node_definition",
                side_effect=Exception("Not Found")):
        result = get_node_dynamic_fields(
            workflow_id="wf", node_id="node-1", field_name_changed="a",
        )

    assert result["ok"] is False
    assert result["error_kind"] == "native_or_unsupported"
    assert "definition_fetch_error" in result
    assert "NATIVE_NODE_SETTINGS_COOKBOOK" in result["guidance"]
