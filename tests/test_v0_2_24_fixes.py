"""Tests for v0.2.24 fixes:

  Fix #1: update_node_setting parses JSON-string values into lists/dicts
          before storing — without this, MCP transport coerces structured
          values to JSON strings which the platform validator then iterates
          over character-by-character (the Magic Node references bug).

  Fix #2: attach_python_block refuses by default and steers to attach_magic_node.
          Custom Code is silently broken — runtime ignores the user's code and
          passes parent data through verbatim. See docs/CC_BUG_REPRO_2026_05_25.md.
"""
from unittest.mock import patch
from nrev_wf_mcp.server import update_node_setting, attach_python_block


# ════════════════════════════════════════════════════════════════════════
# Fix #1 — update_node_setting parses JSON string values
# ════════════════════════════════════════════════════════════════════════


def test_update_node_setting_parses_json_array_string_into_list():
    """When value arrives as the literal string '["a","b"]', it should be
    parsed into the Python list ["a", "b"] before being stored.

    Reproduction: a separate Claude session tried to update a Magic Node's
    `references` field with a list and the MCP stored it as a JSON string.
    The platform validator then iterated character-by-character producing
    one warning per character ('[', '"', 'a', '"', ',', ...).
    """
    fake_node = {
        "id": "n1",
        "settings_field_values": [
            {"field_name": "refs", "field_value": ["old_ref"]}
        ],
    }
    fake_wf = {"id": "wf-1", "blocks": [fake_node]}

    captured = {}

    def fake_put(workflow_id, node_id, block, validate_after):
        # capture what was about to be sent
        captured["settings"] = block["settings_field_values"]
        return {"ok": True, "isRunable": True, "node_config_error": None,
                "workflowConfigError": None, "validation": {"valid": True}}

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server._put_node_and_validate", side_effect=fake_put):
        result = update_node_setting(
            workflow_id="wf-1",
            node_id="n1",
            field_path="refs",
            value='["edge_id_one", "edge_id_two"]',  # ← JSON string
        )

    assert result["ok"] is True
    stored = captured["settings"][0]["field_value"]
    assert isinstance(stored, list), f"expected list, got {type(stored).__name__}: {stored!r}"
    assert stored == ["edge_id_one", "edge_id_two"]


def test_update_node_setting_parses_json_object_string_into_dict():
    """Same as above but for dict-shaped values (e.g. PersonReference)."""
    fake_node = {
        "id": "n1",
        "settings_field_values": [
            {"field_name": "person_reference", "field_value": {}}
        ],
    }
    fake_wf = {"id": "wf-1", "blocks": [fake_node]}

    captured = {}

    def fake_put(workflow_id, node_id, block, validate_after):
        captured["settings"] = block["settings_field_values"]
        return {"ok": True, "isRunable": True, "node_config_error": None,
                "workflowConfigError": None, "validation": {"valid": True}}

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server._put_node_and_validate", side_effect=fake_put):
        result = update_node_setting(
            workflow_id="wf-1",
            node_id="n1",
            field_path="person_reference",
            value='{"name": "Sundar Pichai", "type": "name"}',
        )

    assert result["ok"] is True
    stored = captured["settings"][0]["field_value"]
    assert isinstance(stored, dict)
    assert stored == {"name": "Sundar Pichai", "type": "name"}


def test_update_node_setting_preserves_python_list_when_passed_directly():
    """When value is already a list (not a string), no parsing happens —
    pass through as-is."""
    fake_node = {
        "id": "n1",
        "settings_field_values": [
            {"field_name": "refs", "field_value": []}
        ],
    }
    fake_wf = {"id": "wf-1", "blocks": [fake_node]}

    captured = {}

    def fake_put(workflow_id, node_id, block, validate_after):
        captured["settings"] = block["settings_field_values"]
        return {"ok": True, "isRunable": True, "node_config_error": None,
                "workflowConfigError": None, "validation": {"valid": True}}

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server._put_node_and_validate", side_effect=fake_put):
        update_node_setting(
            workflow_id="wf-1",
            node_id="n1",
            field_path="refs",
            value=["a", "b", "c"],  # already a list
        )

    assert captured["settings"][0]["field_value"] == ["a", "b", "c"]


def test_update_node_setting_preserves_string_that_starts_with_bracket_but_isnt_json():
    """Edge case: a real string that happens to begin with [ should NOT be
    parsed (would corrupt the value). Only parse if json.loads succeeds AND
    returns a list/dict."""
    fake_node = {
        "id": "n1",
        "settings_field_values": [
            {"field_name": "label", "field_value": ""}
        ],
    }
    fake_wf = {"id": "wf-1", "blocks": [fake_node]}

    captured = {}

    def fake_put(workflow_id, node_id, block, validate_after):
        captured["settings"] = block["settings_field_values"]
        return {"ok": True, "isRunable": True, "node_config_error": None,
                "workflowConfigError": None, "validation": {"valid": True}}

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server._put_node_and_validate", side_effect=fake_put):
        update_node_setting(
            workflow_id="wf-1",
            node_id="n1",
            field_path="label",
            value="[draft] Q2 outreach campaign",  # starts with [ but not JSON
        )

    # Stays as the original string — json.loads would have failed/errored
    assert captured["settings"][0]["field_value"] == "[draft] Q2 outreach campaign"


def test_update_node_setting_preserves_scalar_values():
    """ints, floats, bools, None should pass through unchanged."""
    fake_node = {
        "id": "n1",
        "settings_field_values": [
            {"field_name": "limit", "field_value": 0}
        ],
    }
    fake_wf = {"id": "wf-1", "blocks": [fake_node]}

    captured = {}

    def fake_put(workflow_id, node_id, block, validate_after):
        captured["settings"] = block["settings_field_values"]
        return {"ok": True, "isRunable": True, "node_config_error": None,
                "workflowConfigError": None, "validation": {"valid": True}}

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server._put_node_and_validate", side_effect=fake_put):
        update_node_setting(workflow_id="wf-1", node_id="n1",
                             field_path="limit", value=42)
        assert captured["settings"][0]["field_value"] == 42


# ════════════════════════════════════════════════════════════════════════
# Fix #2 — attach_python_block refuses by default; steers to attach_magic_node
# ════════════════════════════════════════════════════════════════════════


def test_attach_python_block_refuses_by_default_and_proposes_magic_node():
    """The default call (no override) returns a structured refusal that
    tells the caller exactly what to call instead, including the auto-
    converted code (df → df1)."""
    # The function returns BEFORE calling api.get_workflow, so we don't need
    # to mock anything.
    result = attach_python_block(
        workflow_id="wf-1",
        parent_node_id="p1",
        name="some-cc",
        code="def run(df):\n    df['new'] = 1\n    return df\n",
        output_columns=["foo", "new"],
    )

    assert result["ok"] is False
    assert result["stage"] == "cc_silent_failure_guard"
    assert "Custom Code attach is broken" in result["message"]
    assert "attach_magic_node" in result["message"]
    # The use_instead block has everything the caller needs to retry
    use = result["use_instead"]
    assert use["tool"] == "attach_magic_node"
    assert use["args"]["parent_node_ids"] == ["p1"]
    assert use["args"]["output_columns"] == ["foo", "new"]
    # Code auto-converted: def run(df) → def run(df1)
    assert "def run(df1)" in use["args"]["code"]
    assert "def run(df)" not in use["args"]["code"]


def test_attach_python_block_override_proceeds_past_guard():
    """If the caller passes i_understand_cc_is_broken=True, the function
    proceeds to its normal flow (lint → paste-and-wire)."""
    plain_parent = {
        "id": "p1",
        "typeId": "ae54c44f-60ee-47c4-91d7-eae7fa849133",  # Custom Code
        "settings_field_values": [{"field_name": "data_manipulation-custom_code-code",
                                    "field_value": "def run(df): return df"}],
        "outputs": [{"columns": ["foo"], "columns_metadata": []}],
        "position": {"x": 0, "y": 0},
    }
    fake_wf = {"id": "wf-1", "blocks": [plain_parent]}

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.lint", return_value=[]), \
         patch("nrev_wf_mcp.server._attach_block_via_paste_and_wire",
               return_value=({"workflowConfigError": None, "isRunable": True,
                              "blocks": [plain_parent]}, "new-id")), \
         patch("nrev_wf_mcp.server._new_block_error_from_paste", return_value=None), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value={"valid": True}):
        result = attach_python_block(
            workflow_id="wf-1",
            parent_node_id="p1",
            name="override-cc",
            code="def run(df): return df",
            output_columns=["foo"],
            i_understand_cc_is_broken=True,
        )

    # Did NOT hit the silent-failure guard
    assert result.get("stage") != "cc_silent_failure_guard"


def test_attach_python_block_default_refusal_does_not_call_api():
    """Sanity: the guard returns before any API call. No mocks needed,
    no side effects."""
    # If the function tries to call api.get_workflow with no patch in place,
    # the test would fail with a network error. The fact that this passes
    # confirms the guard returns first.
    result = attach_python_block(
        workflow_id="wf-1",
        parent_node_id="p1",
        name="any-cc",
        code="def run(df): return df",
        output_columns=["x"],
    )
    assert result["ok"] is False
    assert result["stage"] == "cc_silent_failure_guard"
