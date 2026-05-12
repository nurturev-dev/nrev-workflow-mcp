"""Tests for the columns_metadata builder + typeId-to-slug helper.

The bug v0.2.5 fixes: prior versions omitted `origin_node_id`, causing the
platform to return HTTP 422 when the metadata was used downstream (e.g. on
column rename via set_node_output_schema). The builder now defaults
origin_node_id to the current node and includes the full envelope the
platform requires.
"""
from nrev_wf_mcp.server import _build_columns_metadata, _typeid_to_value_slug
from nrev_wf_mcp import block_types


# ── _build_columns_metadata ─────────────────────────────────────────────


def test_build_includes_origin_node_id():
    """The bug fix: every entry must carry origin_node_id."""
    out = _build_columns_metadata(
        ["a", "b"], ["string", "integer"],
        origin_node_id="node-uuid-123",
        origin_node_name="My Node",
        origin_node_type="data_manipulation.custom_code",
    )
    assert len(out) == 2
    for entry in out:
        assert entry["origin_node_id"] == "node-uuid-123"
        assert entry["origin_node_name"] == "My Node"
        assert entry["origin_node_type"] == "data_manipulation.custom_code"


def test_build_full_envelope():
    """Every entry has the complete shape the platform expects."""
    out = _build_columns_metadata(["x"], ["string"], origin_node_id="n1")
    assert set(out[0].keys()) == {
        "column_name", "data_type", "is_nullable",
        "origin_node_id", "origin_node_name", "origin_node_type",
        "nested_fields",
    }


def test_build_preserves_dtype_per_column():
    out = _build_columns_metadata(
        ["company", "score"], ["string", "integer"],
        origin_node_id="n1",
    )
    assert out[0]["column_name"] == "company"
    assert out[0]["data_type"] == "string"
    assert out[1]["column_name"] == "score"
    assert out[1]["data_type"] == "integer"


def test_build_default_optional_args_empty_strings():
    """Caller can omit origin_node_name/type — defaults to empty string,
    not None, since the platform's JSON validator may reject nulls."""
    out = _build_columns_metadata(["a"], ["string"], origin_node_id="n1")
    assert out[0]["origin_node_name"] == ""
    assert out[0]["origin_node_type"] == ""
    assert out[0]["nested_fields"] is None  # this one IS allowed null


def test_build_with_empty_columns_returns_empty_list():
    assert _build_columns_metadata([], [], origin_node_id="n1") == []


# ── _typeid_to_value_slug ───────────────────────────────────────────────


def test_typeid_to_slug_known_types():
    assert _typeid_to_value_slug(block_types.CUSTOM_CODE) == "data_manipulation.custom_code"
    assert _typeid_to_value_slug(block_types.MAGIC_NODE) == "data_manipulation.magic_node"
    assert _typeid_to_value_slug(block_types.CSV_WRITE) == "file_management.csv_write"


def test_typeid_to_slug_unknown_returns_typeid():
    """For unrecognized typeIds, return the typeId itself — the platform will
    accept or reject it. Better than crashing."""
    unknown = "abcdef00-0000-0000-0000-000000000000"
    assert _typeid_to_value_slug(unknown) == unknown


def test_typeid_to_slug_handles_none_or_empty():
    assert _typeid_to_value_slug(None) == ""
    assert _typeid_to_value_slug("") == ""
