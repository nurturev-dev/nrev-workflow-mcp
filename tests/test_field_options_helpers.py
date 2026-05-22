"""Tests for v0.2.6 helpers: dropdown-field detection, settings-flattening,
and label resolution against mock field-options responses."""
from unittest.mock import patch

from nrev_wf_mcp.server import (
    _looks_like_dropdown_field,
    _settings_as_value_array,
    _resolve_field_label,
)


# ── _looks_like_dropdown_field ────────────────────────────────────────────


def test_dropdown_detection_sheet_id():
    assert _looks_like_dropdown_field("pipedream-google_sheets-google_sheets_add_single_row-sheetId")


def test_dropdown_detection_worksheet_id():
    assert _looks_like_dropdown_field("pipedream-google_sheets-google_sheets_add_single_row-worksheetId")


def test_dropdown_detection_connection_id_variants():
    assert _looks_like_dropdown_field("pipedream-slack-slack_send_message-connection_id")
    assert _looks_like_dropdown_field("pipedream-gmail-gmail_send-googleSheets_connection_id")


def test_dropdown_detection_other_id_fields():
    assert _looks_like_dropdown_field("pipedream-slack-channelId")
    assert _looks_like_dropdown_field("pipedream-google_calendar-calendarId")


def test_dropdown_detection_non_dropdown_fields_skipped():
    assert not _looks_like_dropdown_field("ai_toolkit-ask_ai-prompt")
    assert not _looks_like_dropdown_field("ai_toolkit-ask_ai-temperature")
    assert not _looks_like_dropdown_field("data_manipulation-custom_code-code")
    assert not _looks_like_dropdown_field("pipedream-google_sheets-google_sheets_add_single_row-hasHeaders")
    assert not _looks_like_dropdown_field("pipedream-google_sheets-google_sheets_add_single_row-drive")
    assert not _looks_like_dropdown_field("")


# ── _settings_as_value_array ──────────────────────────────────────────────


def test_settings_flatten_simple_leaves():
    sfv = [
        {"field_name": "a", "field_value": "x"},
        {"field_name": "b", "field_value": 1},
    ]
    out = _settings_as_value_array(sfv)
    assert out == [
        {"field_name": "a", "field_value": "x"},
        {"field_name": "b", "field_value": 1},
    ]


def test_settings_flatten_recurses_into_groups():
    """Magic Node-style nested settings: outer group contains inner leaves."""
    sfv = [
        {
            "field_name": "outer-group",
            "field_value": [
                {"field_name": "inner-leaf-a", "field_value": "x"},
                {"field_name": "inner-leaf-b", "field_value": "y"},
            ],
        },
    ]
    out = _settings_as_value_array(sfv)
    # Only leaves are emitted; group itself is omitted
    assert out == [
        {"field_name": "inner-leaf-a", "field_value": "x"},
        {"field_name": "inner-leaf-b", "field_value": "y"},
    ]


def test_settings_flatten_handles_empty():
    assert _settings_as_value_array([]) == []
    assert _settings_as_value_array(None) == []


def test_settings_flatten_skips_entries_without_name():
    sfv = [
        {"field_value": "orphan"},  # no field_name → skip
        {"field_name": "kept", "field_value": "y"},
    ]
    assert _settings_as_value_array(sfv) == [{"field_name": "kept", "field_value": "y"}]


# ── _resolve_field_label ──────────────────────────────────────────────────


def test_resolve_label_finds_string_match():
    """Matching value type — string == string."""
    with patch("nrev_wf_mcp.server.api.field_options") as mock_fo:
        mock_fo.return_value = {
            "options": [
                {"label": "Competitive tracking", "value": "1BspIposg"},
                {"label": "Other Sheet", "value": "1xyz"},
            ],
        }
        label = _resolve_field_label(
            node_id="n1", type_id="t1", field_name="sheetId",
            target_value="1BspIposg", settings_array=[],
        )
        assert label == "Competitive tracking"


def test_resolve_label_handles_float_to_string_value():
    """Worksheet IDs come back as floats (410711210.0) but stored as strings
    ("410711210"). Resolver must match across types."""
    with patch("nrev_wf_mcp.server.api.field_options") as mock_fo:
        mock_fo.return_value = {
            "options": [
                {"label": "Competitor tagged posts", "value": 410711210.0},
            ],
        }
        label = _resolve_field_label(
            node_id="n1", type_id="t1", field_name="worksheetId",
            target_value="410711210", settings_array=[],
        )
        assert label == "Competitor tagged posts"


def test_resolve_label_no_match_returns_none():
    with patch("nrev_wf_mcp.server.api.field_options") as mock_fo:
        mock_fo.return_value = {"options": [{"label": "A", "value": "1"}]}
        label = _resolve_field_label(
            node_id="n1", type_id="t1", field_name="sheetId",
            target_value="999_no_match", settings_array=[],
        )
        assert label is None


def test_resolve_label_empty_value_returns_none_without_call():
    """Don't waste an API call for empty values."""
    with patch("nrev_wf_mcp.server.api.field_options") as mock_fo:
        for empty in (None, ""):
            label = _resolve_field_label(
                node_id="n1", type_id="t1", field_name="sheetId",
                target_value=empty, settings_array=[],
            )
            assert label is None
        mock_fo.assert_not_called()


def test_resolve_label_silent_on_api_error():
    """API errors during auto-resolve must not break attach_node."""
    with patch("nrev_wf_mcp.server.api.field_options",
               side_effect=Exception("network blip")):
        label = _resolve_field_label(
            node_id="n1", type_id="t1", field_name="sheetId",
            target_value="x", settings_array=[],
        )
        assert label is None  # silent failure
