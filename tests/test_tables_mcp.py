"""Tests for the nRev Tables MCP tools (v0.2.26).

Coverage:
  - tables_client.py limit clamping (BUG #6 workaround)
  - Name ↔ id translation helpers
  - tables_add_row / tables_update_row name resolution
  - tables_list_rows projection + filter resolution
  - tables_fetch_all_rows pagination + cap
  - tables_bulk_add_rows error collection
  - Destructive ops require confirm=True
  - get_plugin_version
"""
from unittest.mock import patch, MagicMock
import pytest
from nrev_wf_mcp import tables_client
from nrev_wf_mcp.server import (
    tables_get,
    tables_create,
    tables_add_row,
    tables_update_row,
    tables_list_rows,
    tables_fetch_all_rows,
    tables_bulk_add_rows,
    tables_delete,
    tables_delete_row,
    tables_delete_column,
    tables_rename_column,
    tables_reorder_column,
    get_plugin_version,
    _tables_translate_to_ids,
    _tables_project_to_names,
)


# ════════════════════════════════════════════════════════════════════════
# tables_client — limit clamp
# ════════════════════════════════════════════════════════════════════════


def test_clamp_limit_snaps_to_next_allowed():
    """The platform rejects arbitrary limits — only [100, 500, 1000, 5000,
    10000, 50000, 100000] are valid. Clamper rounds UP to the next allowed."""
    assert tables_client._clamp_limit(1) == 100
    assert tables_client._clamp_limit(50) == 100
    assert tables_client._clamp_limit(100) == 100
    assert tables_client._clamp_limit(101) == 500
    assert tables_client._clamp_limit(500) == 500
    assert tables_client._clamp_limit(750) == 1000
    assert tables_client._clamp_limit(1000) == 1000
    assert tables_client._clamp_limit(2000) == 5000
    assert tables_client._clamp_limit(50000) == 50000
    assert tables_client._clamp_limit(99999) == 100000


def test_clamp_limit_handles_zero_and_negative():
    """Zero or negative → 100 (safe default)."""
    assert tables_client._clamp_limit(0) == 100
    assert tables_client._clamp_limit(-1) == 100


def test_clamp_limit_caps_at_max():
    """Anything above 100,000 → 100,000."""
    assert tables_client._clamp_limit(150000) == 100000
    assert tables_client._clamp_limit(999999) == 100000


# ════════════════════════════════════════════════════════════════════════
# Name ↔ id translation helpers
# ════════════════════════════════════════════════════════════════════════


def test_translate_to_ids_basic():
    name_to_id = {"name": "uuid-1", "score": "uuid-2"}
    out = _tables_translate_to_ids({"name": "Alice", "score": 99}, name_to_id)
    assert out == {"uuid-1": "Alice", "uuid-2": 99}


def test_translate_to_ids_raises_on_unknown_column():
    name_to_id = {"name": "uuid-1"}
    with pytest.raises(ValueError) as exc_info:
        _tables_translate_to_ids({"name": "Alice", "bogus": "x"}, name_to_id)
    assert "bogus" in str(exc_info.value)
    assert "name" in str(exc_info.value)  # lists available


def test_project_to_names_basic():
    id_to_name = {"uuid-1": "name", "uuid-2": "score"}
    out = _tables_project_to_names({"uuid-1": "Alice", "uuid-2": 99}, id_to_name)
    assert out == {"name": "Alice", "score": 99}


def test_project_to_names_with_column_filter():
    """columns=[...] projects to ONLY those columns."""
    id_to_name = {"uuid-1": "name", "uuid-2": "score", "uuid-3": "extra"}
    out = _tables_project_to_names(
        {"uuid-1": "Alice", "uuid-2": 99, "uuid-3": "x"},
        id_to_name,
        columns=["name", "score"],
    )
    assert out == {"name": "Alice", "score": 99}
    assert "extra" not in out


def test_project_to_names_preserves_unknown_ids_as_id():
    """If a column id isn't in the map (e.g. system column we don't have),
    use the id as the key — caller can spot the leak."""
    id_to_name = {"uuid-1": "name"}
    out = _tables_project_to_names({"uuid-1": "Alice", "uuid-unmapped": "x"},
                                     id_to_name)
    assert out == {"name": "Alice", "uuid-unmapped": "x"}


# ════════════════════════════════════════════════════════════════════════
# tables_add_row name resolution
# ════════════════════════════════════════════════════════════════════════


def test_tables_add_row_translates_names_to_ids():
    fake_table = {
        "id": "t1",
        "columns": [
            {"id": "uuid-name", "name": "name", "type": "text", "is_system": False},
            {"id": "uuid-score", "name": "score", "type": "number", "is_system": False},
        ],
    }
    captured = {}

    def fake_add_row(table_id, values):
        captured["values"] = values
        return {"row": {"row_id": 1, "values": values}}

    with patch.object(tables_client, "get_table", return_value=fake_table), \
         patch.object(tables_client, "add_row", side_effect=fake_add_row):
        result = tables_add_row("t1", {"name": "Alice", "score": 99})

    # Got translated to UUIDs before hitting the API
    assert captured["values"] == {"uuid-name": "Alice", "uuid-score": 99}
    assert result["row"]["row_id"] == 1


def test_tables_add_row_passes_through_when_by_column_id():
    """When by_column_id=True, no schema fetch, no translation."""
    with patch.object(tables_client, "get_table") as get_table_mock, \
         patch.object(tables_client, "add_row",
                       return_value={"row": {"row_id": 1, "values": {}}}) as add_mock:
        tables_add_row("t1", {"uuid-already": "value"}, by_column_id=True)

    get_table_mock.assert_not_called()  # no schema lookup
    add_mock.assert_called_with("t1", {"uuid-already": "value"})


def test_tables_add_row_raises_on_unknown_name():
    fake_table = {
        "id": "t1",
        "columns": [
            {"id": "uuid-name", "name": "name", "type": "text", "is_system": False},
        ],
    }
    with patch.object(tables_client, "get_table", return_value=fake_table):
        with pytest.raises(ValueError) as exc_info:
            tables_add_row("t1", {"name": "Alice", "bogus_col": "x"})

    assert "bogus_col" in str(exc_info.value)


# ════════════════════════════════════════════════════════════════════════
# tables_list_rows projection
# ════════════════════════════════════════════════════════════════════════


def test_tables_list_rows_projects_to_name_keyed():
    """Default behavior: response rows come back name-keyed (not UUID-keyed)."""
    fake_table = {
        "id": "t1",
        "columns": [
            {"id": "uuid-name", "name": "name", "type": "text", "is_system": False},
            {"id": "uuid-score", "name": "score", "type": "number", "is_system": False},
        ],
    }
    fake_rows = {
        "data": [
            {"row_id": 1,
             "values": {"uuid-name": "Alice", "uuid-score": 99},
             "created_at": "2026-05-25T00:00:00Z",
             "last_updated_at": "2026-05-25T00:00:00Z"},
        ],
        "meta": {"total_entries": 1, "skip": 0, "limit": 100},
    }

    with patch.object(tables_client, "get_table", return_value=fake_table), \
         patch.object(tables_client, "list_rows", return_value=fake_rows):
        result = tables_list_rows("t1")

    assert result["data"][0]["values"] == {"name": "Alice", "score": 99}
    assert result["data"][0]["row_id"] == 1


def test_tables_list_rows_resolves_filter_column_name_to_id():
    """filter.column accepts a name; gets resolved to uuid before API call."""
    fake_table = {
        "id": "t1",
        "columns": [
            {"id": "uuid-score", "name": "score", "type": "number", "is_system": False},
        ],
    }
    fake_rows = {"data": [], "meta": {}}
    captured = {}

    def capture_list_rows(*args, **kwargs):
        captured.update(kwargs)
        return fake_rows

    with patch.object(tables_client, "get_table", return_value=fake_table), \
         patch.object(tables_client, "list_rows", side_effect=capture_list_rows):
        tables_list_rows("t1", filter={"column": "score", "operator": "gt", "value": 50})

    assert captured["filter_column_id"] == "uuid-score"
    assert captured["filter_operator"] == "gt"
    assert captured["filter_values"] == [50]


def test_tables_list_rows_projects_to_requested_columns_only():
    fake_table = {
        "id": "t1",
        "columns": [
            {"id": "uuid-name", "name": "name", "type": "text", "is_system": False},
            {"id": "uuid-score", "name": "score", "type": "number", "is_system": False},
            {"id": "uuid-extra", "name": "extra", "type": "text", "is_system": False},
        ],
    }
    fake_rows = {
        "data": [{"row_id": 1, "values": {"uuid-name": "Alice", "uuid-score": 99, "uuid-extra": "x"}}],
        "meta": {},
    }

    with patch.object(tables_client, "get_table", return_value=fake_table), \
         patch.object(tables_client, "list_rows", return_value=fake_rows):
        result = tables_list_rows("t1", columns=["name", "score"])

    assert result["data"][0]["values"] == {"name": "Alice", "score": 99}
    assert "extra" not in result["data"][0]["values"]


# ════════════════════════════════════════════════════════════════════════
# tables_fetch_all_rows pagination
# ════════════════════════════════════════════════════════════════════════


def test_tables_fetch_all_rows_paginates_until_exhausted():
    """Calls list_rows in 1000-row chunks until the response is short."""
    fake_table = {
        "id": "t1",
        "columns": [{"id": "uuid-name", "name": "name", "type": "text", "is_system": False}],
    }
    # Mock pages: 1000, 1000, 200 = 2200 total rows
    pages = [
        {"data": [{"row_id": i, "values": {"uuid-name": f"r{i}"}}
                   for i in range(1, 1001)], "meta": {}},
        {"data": [{"row_id": i, "values": {"uuid-name": f"r{i}"}}
                   for i in range(1001, 2001)], "meta": {}},
        {"data": [{"row_id": i, "values": {"uuid-name": f"r{i}"}}
                   for i in range(2001, 2201)], "meta": {}},
    ]
    call_count = [0]

    def page_fn(*args, **kwargs):
        idx = call_count[0]
        call_count[0] += 1
        return pages[idx] if idx < len(pages) else {"data": [], "meta": {}}

    with patch.object(tables_client, "get_table", return_value=fake_table), \
         patch.object(tables_client, "list_rows", side_effect=page_fn):
        result = tables_fetch_all_rows("t1")

    assert result["meta"]["total_fetched"] == 2200
    assert result["meta"]["capped"] is False
    assert len(result["data"]) == 2200


def test_tables_fetch_all_rows_caps_at_max_rows():
    """When the table has more rows than max_rows, stops + sets capped=True."""
    fake_table = {
        "id": "t1",
        "columns": [{"id": "uuid-name", "name": "name", "type": "text", "is_system": False}],
    }
    # Always return a full page
    big_page = {"data": [{"row_id": i, "values": {"uuid-name": f"r{i}"}}
                          for i in range(1, 1001)], "meta": {}}

    with patch.object(tables_client, "get_table", return_value=fake_table), \
         patch.object(tables_client, "list_rows", return_value=big_page):
        result = tables_fetch_all_rows("t1", max_rows=2500)

    assert result["meta"]["total_fetched"] == 2500
    assert result["meta"]["capped"] is True


# ════════════════════════════════════════════════════════════════════════
# tables_bulk_add_rows
# ════════════════════════════════════════════════════════════════════════


def test_tables_bulk_add_rows_collects_errors_by_default():
    """stop_on_error=False (default): keep going on per-row failures."""
    fake_table = {
        "id": "t1",
        "columns": [
            {"id": "uuid-name", "name": "name", "type": "text", "is_system": False},
        ],
    }
    call_count = [0]

    def add_row_mock(table_id, values):
        call_count[0] += 1
        if call_count[0] == 2:
            raise tables_client.TablesAPIError(400, "Cell type mismatch", "url")
        return {"row": {"row_id": call_count[0]}}

    with patch.object(tables_client, "get_table", return_value=fake_table), \
         patch.object(tables_client, "add_row", side_effect=add_row_mock):
        result = tables_bulk_add_rows("t1", [
            {"name": "Alice"},
            {"name": "Bob"},  # this one fails
            {"name": "Carol"},
        ])

    assert result["total_attempted"] == 3
    assert len(result["inserted"]) == 2
    assert len(result["errors"]) == 1
    assert result["errors"][0]["index"] == 1
    assert "Cell type mismatch" in result["errors"][0]["error"]


def test_tables_bulk_add_rows_stops_on_first_error_when_flagged():
    fake_table = {"id": "t1", "columns": [
        {"id": "uuid-name", "name": "name", "type": "text", "is_system": False},
    ]}

    def boom(*a, **k):
        raise tables_client.TablesAPIError(400, "boom", "url")

    with patch.object(tables_client, "get_table", return_value=fake_table), \
         patch.object(tables_client, "add_row", side_effect=boom):
        result = tables_bulk_add_rows("t1", [
            {"name": "Alice"}, {"name": "Bob"}, {"name": "Carol"},
        ], stop_on_error=True)

    assert result["total_attempted"] == 1
    assert len(result["errors"]) == 1


def test_tables_bulk_add_rows_handles_empty_list():
    result = tables_bulk_add_rows("t1", [])
    assert result == {"inserted": [], "errors": [], "total_attempted": 0}


# ════════════════════════════════════════════════════════════════════════
# Destructive ops require confirm=True
# ════════════════════════════════════════════════════════════════════════


def test_tables_delete_refuses_without_confirm():
    result = tables_delete("t1")
    assert result["ok"] is False
    assert "confirm=True" in result["message"]


def test_tables_delete_row_refuses_without_confirm():
    result = tables_delete_row("t1", 1)
    assert result["ok"] is False
    assert "confirm=True" in result["message"]


def test_tables_delete_column_refuses_without_confirm():
    result = tables_delete_column("t1", "name")
    assert result["ok"] is False
    assert "confirm=True" in result["message"]


# ════════════════════════════════════════════════════════════════════════
# get_plugin_version
# ════════════════════════════════════════════════════════════════════════


def test_get_plugin_version_returns_current_version():
    from nrev_wf_mcp import __version__
    result = get_plugin_version()
    assert result["version"] == __version__
    assert result["name"] == "nrev-wf-mcp"
    assert "github.com" in result["homepage"]
