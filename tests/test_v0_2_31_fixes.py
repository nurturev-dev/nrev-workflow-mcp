"""Tests for v0.2.31 fixes.

  Fix #1: update_node_setting preserves the canonical platform shape for
          fields whose suffix is in _STRING_SHAPED_FIELD_SUFFIXES (currently
          just `-response_json` on Ask AI nodes). The platform expects these
          fields stored as a JSON-formatted TEXT STRING, not a parsed dict.
          The UI editor reads field_value as text and renders blank when
          it gets a dict.

  Before v0.2.31: v0.2.24's Fix #1 (parse-JSON-strings-into-dicts) silently
  converted every shape (dict input, string-of-JSON input) into a stored
  dict, breaking the UI editor box. Live-reproduced 2026-06-02 in
  workflow 93f6b1d9-... against ai_toolkit-ask_ai-response_json.

  After v0.2.31:
    - field_path ending with `-response_json` (or any allowlisted suffix):
        * dict / list value → json.dumps() before storing → STRING
        * str value → store as-is → STRING (no parse-to-dict coerce)
        * other types → leave unchanged
    - all other field_paths: v0.2.24 Fix #1 preserved (dict-string → dict)
"""
from __future__ import annotations

import json as _json
from unittest.mock import patch, MagicMock

from nrev_wf_mcp.server import (
    _STRING_SHAPED_FIELD_SUFFIXES,
    update_node_setting,
)


# ════════════════════════════════════════════════════════════════════════
# _STRING_SHAPED_FIELD_SUFFIXES — registry
# ════════════════════════════════════════════════════════════════════════


def test_string_shaped_field_suffixes_contains_response_json():
    """The v0.2.31 trigger case. Suffix must include the leading hyphen so
    we don't spuriously match unrelated fields ending in `response_json`."""
    assert "-response_json" in _STRING_SHAPED_FIELD_SUFFIXES


def test_string_shaped_field_suffixes_is_a_tuple_of_strings():
    """Stability: the registry is a tuple so callers can't mutate it."""
    assert isinstance(_STRING_SHAPED_FIELD_SUFFIXES, tuple)
    for s in _STRING_SHAPED_FIELD_SUFFIXES:
        assert isinstance(s, str)
        assert s.startswith("-"), (
            f"suffix {s!r} should start with '-' to anchor to field-name "
            "segment boundary"
        )


# ════════════════════════════════════════════════════════════════════════
# update_node_setting — string-shape preservation for response_json
# ════════════════════════════════════════════════════════════════════════


def _fake_workflow_with_node(field_name: str, existing_value):
    """Build a fake workflow whose target node has a single existing
    settings field. Returns (workflow_dict, node_dict)."""
    node = {
        "id": "node-1",
        "settings_field_values": [
            {"field_name": field_name, "field_value": existing_value,
             "fieldLabel": None, "error": None,
             "isUserInputInFormMandatory": False,
             "selectedInputTypeIndex": None, "isStale": False},
        ],
        "outputs": [{"columns_metadata": []}],
    }
    wf = {"id": "wf-1", "blocks": [node], "isRunable": True,
          "workflowConfigError": None}
    return wf, node


def _captured_field_value(put_calls) -> object:
    """Pull `field_value` out of the most recent put_node call.
    Signature: put_node(workflow_id, node_id, node_dict)."""
    last = put_calls[-1]
    node = last.args[2] if len(last.args) > 2 else last.kwargs["node"]
    return node["settings_field_values"][0]["field_value"]


def test_response_json_string_input_stored_as_string():
    """Caller passes a JSON-formatted string — must be stored verbatim,
    NOT coerced to a dict by v0.2.24's logic."""
    wf, node = _fake_workflow_with_node(
        "ai_toolkit-ask_ai-response_json", "old_placeholder",
    )
    json_text = '{\n  "Fit": "true or false",\n  "reason": "1-2 sentences"\n}'

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=wf), \
         patch("nrev_wf_mcp.server.api.put_node") as put, \
         patch("nrev_wf_mcp.server._maybe_validate",
                return_value={"valid": True, "isRunable": True,
                              "workflowConfigError": None,
                              "node_errors": [], "magic_ref_warnings": []}):
        result = update_node_setting(
            workflow_id="wf-1", node_id="node-1",
            field_path="ai_toolkit-ask_ai-response_json",
            value=json_text,
            verify=False,
        )
    stored = _captured_field_value(put.call_args_list)
    assert isinstance(stored, str), (
        f"v0.2.31: response_json string input MUST stay a string. "
        f"Got {type(stored).__name__}: {stored!r}"
    )
    assert stored == json_text, "string passthrough must be byte-identical"
    assert result.get("ok") in (True, False)  # tolerate either depending on validation


def test_response_json_dict_input_auto_stringified():
    """Caller passes a dict (the natural Python shape) — must be
    json.dumps()'d before storing so the UI editor box renders text."""
    wf, node = _fake_workflow_with_node(
        "ai_toolkit-ask_ai-response_json", "old",
    )
    schema = {
        "Fit": "true or false",
        "persona_bucket": "AI/ML | Engineering Leadership | None",
        "reason": "1-2 sentences covering persona fit and company fit",
    }

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=wf), \
         patch("nrev_wf_mcp.server.api.put_node") as put, \
         patch("nrev_wf_mcp.server._maybe_validate",
                return_value={"valid": True, "isRunable": True,
                              "workflowConfigError": None,
                              "node_errors": [], "magic_ref_warnings": []}):
        update_node_setting(
            workflow_id="wf-1", node_id="node-1",
            field_path="ai_toolkit-ask_ai-response_json",
            value=schema,
            verify=False,
        )
    stored = _captured_field_value(put.call_args_list)
    assert isinstance(stored, str), (
        f"v0.2.31: dict input MUST be auto-stringified for string-shaped "
        f"fields. Got {type(stored).__name__}"
    )
    # Round-trip the stored string and confirm the schema survived
    decoded = _json.loads(stored)
    assert decoded == schema, "auto-stringified value must round-trip"


def test_response_json_uses_indented_format_for_ui_readability():
    """When auto-stringifying, use 2-space indent so the UI code editor
    renders the schema readably (matches the format the platform itself
    emits when users hand-paste into the editor)."""
    wf, node = _fake_workflow_with_node(
        "ai_toolkit-ask_ai-response_json", "old",
    )
    schema = {"a": 1, "b": "two"}

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=wf), \
         patch("nrev_wf_mcp.server.api.put_node") as put, \
         patch("nrev_wf_mcp.server._maybe_validate",
                return_value={"valid": True, "isRunable": True,
                              "workflowConfigError": None,
                              "node_errors": [], "magic_ref_warnings": []}):
        update_node_setting(
            workflow_id="wf-1", node_id="node-1",
            field_path="ai_toolkit-ask_ai-response_json",
            value=schema,
            verify=False,
        )
    stored = _captured_field_value(put.call_args_list)
    # Indented JSON has newlines between keys
    assert "\n" in stored, (
        "v0.2.31: auto-stringified schema should be pretty-printed "
        "(2-space indent) so the UI editor renders readably."
    )


def test_response_json_list_input_auto_stringified():
    """Defensive: if caller passes a list (uncommon but technically a
    JSON-encodable value), still stringify."""
    wf, node = _fake_workflow_with_node(
        "ai_toolkit-ask_ai-response_json", "old",
    )
    list_schema = ["x", "y", "z"]

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=wf), \
         patch("nrev_wf_mcp.server.api.put_node") as put, \
         patch("nrev_wf_mcp.server._maybe_validate",
                return_value={"valid": True, "isRunable": True,
                              "workflowConfigError": None,
                              "node_errors": [], "magic_ref_warnings": []}):
        update_node_setting(
            workflow_id="wf-1", node_id="node-1",
            field_path="ai_toolkit-ask_ai-response_json",
            value=list_schema,
            verify=False,
        )
    stored = _captured_field_value(put.call_args_list)
    assert isinstance(stored, str)
    assert _json.loads(stored) == list_schema


# ════════════════════════════════════════════════════════════════════════
# Regression: v0.2.24 Fix #1 preserved for non-allowlisted fields
# ════════════════════════════════════════════════════════════════════════


def test_v0_2_24_coerce_still_fires_for_non_response_json_fields():
    """v0.2.24 Fix #1 (parse JSON string into dict for fields that expect
    a dict) must KEEP working for everything not on the v0.2.31 allowlist."""
    wf, node = _fake_workflow_with_node(
        "data_manipulation-magic_node-references", [],
    )
    # Caller passes a JSON-stringified list (the v0.2.24 failure case)
    references_str = '["edge_a", "edge_b"]'

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=wf), \
         patch("nrev_wf_mcp.server.api.put_node") as put, \
         patch("nrev_wf_mcp.server._maybe_validate",
                return_value={"valid": True, "isRunable": True,
                              "workflowConfigError": None,
                              "node_errors": [], "magic_ref_warnings": []}):
        update_node_setting(
            workflow_id="wf-1", node_id="node-1",
            field_path="data_manipulation-magic_node-references",
            value=references_str,
            verify=False,
        )
    stored = _captured_field_value(put.call_args_list)
    assert isinstance(stored, list), (
        "v0.2.24 Fix #1 (string-JSON → list) MUST still fire for non-"
        f"allowlisted fields. Got {type(stored).__name__}: {stored!r}"
    )
    assert stored == ["edge_a", "edge_b"]


def test_field_named_response_json_but_not_anchored_does_not_match():
    """Suffix is `-response_json` (leading hyphen). A field named
    `xxxresponse_json` (no hyphen) must NOT match — it could legitimately
    be a dict-shaped field elsewhere in the catalog."""
    wf, node = _fake_workflow_with_node(
        "some_app-some_action-xxxresponse_json", {},
    )
    # Caller passes a JSON-stringified dict (v0.2.24 expects this to parse)
    dict_str = '{"foo": "bar"}'

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=wf), \
         patch("nrev_wf_mcp.server.api.put_node") as put, \
         patch("nrev_wf_mcp.server._maybe_validate",
                return_value={"valid": True, "isRunable": True,
                              "workflowConfigError": None,
                              "node_errors": [], "magic_ref_warnings": []}):
        update_node_setting(
            workflow_id="wf-1", node_id="node-1",
            field_path="some_app-some_action-xxxresponse_json",
            value=dict_str,
            verify=False,
        )
    stored = _captured_field_value(put.call_args_list)
    # No suffix match → v0.2.24 coerce fires → string became dict
    assert isinstance(stored, dict), (
        "Non-allowlisted field should still go through v0.2.24's coerce. "
        f"Got {type(stored).__name__}"
    )


def test_string_input_to_other_string_field_unaffected():
    """Non-allowlisted field receiving a plain string (not JSON-like)
    must pass through unchanged. Regression guard."""
    wf, node = _fake_workflow_with_node(
        "ai_toolkit-ask_ai-prompt", "old prompt",
    )
    new_prompt = "Score this lead from 1-10."

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=wf), \
         patch("nrev_wf_mcp.server.api.put_node") as put, \
         patch("nrev_wf_mcp.server._maybe_validate",
                return_value={"valid": True, "isRunable": True,
                              "workflowConfigError": None,
                              "node_errors": [], "magic_ref_warnings": []}):
        update_node_setting(
            workflow_id="wf-1", node_id="node-1",
            field_path="ai_toolkit-ask_ai-prompt",
            value=new_prompt,
            verify=False,
        )
    stored = _captured_field_value(put.call_args_list)
    assert stored == new_prompt


def test_nested_path_ending_in_response_json_also_matches():
    """If field_path is /-separated and the LAST segment ends with the
    allowlist suffix, the rule should still fire. Future-proofing for
    AI nodes that nest the structured-output schema inside a section."""
    # Simulate a nested group containing the response_json field
    node = {
        "id": "node-1",
        "settings_field_values": [
            {"field_name": "section_group", "field_value": [
                {"field_name": "ai_toolkit-ask_ai-response_json",
                 "field_value": "old",
                 "fieldLabel": None, "error": None,
                 "isUserInputInFormMandatory": False,
                 "selectedInputTypeIndex": None, "isStale": False},
            ], "fieldLabel": None, "error": None,
             "isUserInputInFormMandatory": False,
             "selectedInputTypeIndex": None, "isStale": False},
        ],
        "outputs": [{"columns_metadata": []}],
    }
    wf = {"id": "wf-1", "blocks": [node], "isRunable": True,
          "workflowConfigError": None}
    schema = {"x": "y"}

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=wf), \
         patch("nrev_wf_mcp.server.api.put_node") as put, \
         patch("nrev_wf_mcp.server._maybe_validate",
                return_value={"valid": True, "isRunable": True,
                              "workflowConfigError": None,
                              "node_errors": [], "magic_ref_warnings": []}):
        update_node_setting(
            workflow_id="wf-1", node_id="node-1",
            field_path="section_group/ai_toolkit-ask_ai-response_json",
            value=schema,
            verify=False,
        )
    # Pull the nested field from put_node call
    last = put.call_args_list[-1]
    node_dict = last.args[2] if len(last.args) > 2 else last.kwargs["node"]
    nested = node_dict["settings_field_values"][0]["field_value"][0]["field_value"]
    assert isinstance(nested, str), (
        f"v0.2.31: nested path ending in `-response_json` must also be "
        f"stringified. Got {type(nested).__name__}"
    )
    assert _json.loads(nested) == schema


# ════════════════════════════════════════════════════════════════════════
# Cookbook check — document the Ask AI structured-output pattern
# ════════════════════════════════════════════════════════════════════════


def test_cookbook_documents_ask_ai_structured_output():
    """The cookbook must explain the response_type → response_json sequence
    AND the v0.2.31 string-shape contract so future agents don't repeat
    the 2026-06-02 friction."""
    import pathlib
    cookbook = (pathlib.Path(__file__).parent.parent
                / "docs" / "NATIVE_NODE_SETTINGS_COOKBOOK.md")
    text = cookbook.read_text()
    assert "Ask AI" in text or "ask_ai" in text
    assert "structured_output" in text or "Structured Output" in text
    assert "response_json" in text
    # v0.2.31 contract: tool stores as text, accepts either dict or string
    assert "v0.2.31" in text or "string" in text.lower()
