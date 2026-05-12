"""Tests for the row-projection helper used by get_node_output.

The helper is pure — extracted from the tool so it's unit-testable without
mocking API calls.
"""
from nrev_wf_mcp.server import _project_rows


# Synthetic rows resembling the LinkedIn Connection Manager Ask AI output —
# 4 scalar columns + 2 nested JSON columns
ROWS = [
    {
        "Name": "Scott",
        "Owner": "Raj G",
        "connection_note": "Scott, Raj here...",
        "character_count": "180",
        "person_linkedin_profile": {"first_name": "Scott", "headline": "VP Engineering", "experiences": "..."},
        "response": {"linkedin_profile_id": 12345},
    },
    {
        "Name": "Khadija",
        "Owner": "Raj G",
        "connection_note": "Khadija, Raj here...",
        "character_count": "210",
        "person_linkedin_profile": {"first_name": "Khadija", "headline": "Director SF Architect"},
        "response": {"linkedin_profile_id": 67890},
    },
]


# ── columns projection ────────────────────────────────────────────────────


def test_project_columns_keeps_only_listed():
    out, kept = _project_rows(ROWS, columns=["Name", "connection_note"], drop_json_columns=False)
    assert kept == ["Name", "connection_note"]
    for r in out:
        assert set(r.keys()) == {"Name", "connection_note"}


def test_project_columns_preserves_caller_order():
    out, kept = _project_rows(ROWS, columns=["connection_note", "Owner", "Name"], drop_json_columns=False)
    # Order in the returned `kept` list matches caller's order
    assert kept == ["connection_note", "Owner", "Name"]
    # And each row has those keys (dict order in Python 3.7+ is insertion-order)
    for r in out:
        assert list(r.keys()) == ["connection_note", "Owner", "Name"]


def test_project_columns_missing_key_gives_none():
    out, kept = _project_rows(ROWS, columns=["Name", "does_not_exist"], drop_json_columns=False)
    assert kept == ["Name", "does_not_exist"]
    for r in out:
        assert r["does_not_exist"] is None


# ── drop_json_columns ─────────────────────────────────────────────────────


def test_drop_json_columns_removes_dict_columns():
    out, kept = _project_rows(ROWS, columns=None, drop_json_columns=True)
    # person_linkedin_profile and response are dicts → dropped
    assert "person_linkedin_profile" not in kept
    assert "response" not in kept
    # scalar columns remain
    assert "Name" in kept
    assert "Owner" in kept
    assert "connection_note" in kept
    assert "character_count" in kept
    for r in out:
        for k in r:
            assert not isinstance(r[k], (dict, list))


def test_drop_json_columns_with_mixed_rows_drops_if_any_is_json():
    """If a column is a dict in ANY row, drop it. Don't leave half-projected output."""
    rows = [
        {"a": "scalar", "b": "scalar"},
        {"a": {"nested": 1}, "b": "scalar"},  # 'a' is JSON in this row
    ]
    out, kept = _project_rows(rows, columns=None, drop_json_columns=True)
    assert kept == ["b"]


# ── precedence + edge cases ──────────────────────────────────────────────


def test_columns_takes_precedence_over_drop_json():
    """columns is explicit — if you ask for a JSON column, you get it."""
    out, kept = _project_rows(ROWS, columns=["person_linkedin_profile"], drop_json_columns=True)
    assert kept == ["person_linkedin_profile"]
    # The JSON value is preserved (not dropped by the precedence-overridden flag)
    assert out[0]["person_linkedin_profile"] is not None


def test_empty_rows_returns_empty_no_crash():
    out, kept = _project_rows([], columns=["x", "y"], drop_json_columns=False)
    assert out == []
    assert kept == []


def test_no_projection_returns_rows_unchanged_with_first_row_keys():
    out, kept = _project_rows(ROWS, columns=None, drop_json_columns=False)
    assert out == ROWS
    # When neither projection mode is on, we return rows as-is + the first row's keys
    assert "Name" in kept
