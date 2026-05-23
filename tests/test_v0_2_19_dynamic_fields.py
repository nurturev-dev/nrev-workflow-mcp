"""Tests for v0.2.19 get_node_dynamic_fields.

Background — the v0.2.18 Phase-2 stress test couldn't fully test Slack /
Calendar / Sheets in the multi-user nurturev tenant because cross-tenant
connections appeared to block cascading-dropdown discovery. The fix turned
out to be using the right field names — and the right field names come
from `POST /nodes/updated-config-and-status`, an endpoint the platform UI
calls internally that we hadn't wrapped.

Confirmed live: this endpoint accepts cross-tenant connection_ids and
returns the full action schema. With the schema in hand, `list_field_options`
also works cross-tenant (returned 189 Slack channels for sayanta's
cross-tenant connection).
"""
from unittest.mock import patch

from nrev_wf_mcp.server import get_node_dynamic_fields
from nrev_wf_mcp.client import updated_node_config


# ════════════════════════════════════════════════════════════════════════
# Client wrapper
# ════════════════════════════════════════════════════════════════════════


def test_client_updated_node_config_sends_correct_body():
    """Wrapper sends the right body shape to the platform."""
    captured = {}

    def fake_request(method, path, json_body=None, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = json_body
        return {"nodeId": "n", "nodeDefinition": {"fields": []}}

    with patch("nrev_wf_mcp.client.request", side_effect=fake_request):
        updated_node_config(
            node_id="abc",
            node_definition_id="type-1",
            field_name_changed="connection_field",
            setting_field_values=[{"field_name": "x", "field_value": "y"}],
        )

    assert captured["method"] == "POST"
    assert captured["path"] == "/nodes/updated-config-and-status"
    assert captured["body"] == {
        "nodeId": "abc",
        "nodeDefinitionId": "type-1",
        "fieldNameChanged": "connection_field",
        "settingFieldValues": [{"field_name": "x", "field_value": "y"}],
        "settingsSchema": [],
    }


# ════════════════════════════════════════════════════════════════════════
# get_node_dynamic_fields MCP tool
# ════════════════════════════════════════════════════════════════════════


def _slack_new_message_block(node_id="slack-1", conn_field="pipedream-slack_v2-slack_v2_new_message_in_channels-slack_connection_id", conn_value="conn-abc"):
    """A Slack New Message block with just the connection field set."""
    return {
        "id": node_id,
        "typeId": "8e6110dd-979a-4f73-9815-0bcbe31d7cb3",
        "variableName": "Slack New Msg",
        "settings_field_values": [
            {"field_name": conn_field, "field_value": conn_value,
             "fieldLabel": None, "error": None,
             "isUserInputInFormMandatory": False, "selectedInputTypeIndex": None, "isStale": False},
        ],
    }


def _ucas_response_with_channel_field():
    """Mock response mirroring what the live API returned during v0.2.19 probing."""
    return {
        "nodeId": "slack-1",
        "nodeDefinition": {
            "fields": [
                {
                    "name": "pipedream-slack_v2-slack_v2_new_message_in_channels-slack_connection_id",
                    "type": "app_connection",
                    "label": "Select Slack V2 Account",
                    "required": True,
                    "placeholder": "Select your Slack V2 account",
                    "defaultValue": None,
                    "dataSource": {"type": "api_driven", "endpoint": "/connections"},
                },
                {
                    "name": "pipedream-slack_v2-slack_v2_new_message_in_channels-conversations",
                    "type": "multi_select",
                    "label": "Channels",
                    "required": False,
                    "placeholder": "Select one or more channels to monitor for new messages.",
                    "conditionalVisibility": [{"condition": "is_not_empty", "field_name": "pipedream-slack_v2-slack_v2_new_message_in_channels-slack_connection_id"}],
                    "inputTypes": [{
                        "type": "multi_select", "index": 0, "label": "Select Channels (Multiple)",
                        "dataSource": {"type": "api_driven", "endpoint": "/nodes/field-options"},
                    }],
                },
                {
                    "name": "pipedream-slack_v2-slack_v2_new_message_in_channels-resolveNames",
                    "type": "boolean", "label": "Resolve Names", "required": False,
                    "defaultValue": False,
                },
                {
                    "name": "pipedream-slack_v2-slack_v2_new_message_in_channels-ignoreBot",
                    "type": "boolean", "label": "Ignore Bots", "required": False,
                    "defaultValue": False,
                },
            ],
        },
        "availableOptions": {},
        "settingFieldValues": [],
    }


def test_get_node_dynamic_fields_returns_full_schema_with_dropdown_list():
    """Headline test: agent calls the tool, gets back the field schema +
    a convenient `dropdown_field_names` list to feed into list_field_options."""
    block = _slack_new_message_block()
    fake_wf = {"id": "wf-1", "blocks": [block]}
    captured = {}

    def fake_ucas(node_id, node_definition_id, field_name_changed, setting_field_values, settings_schema=None):
        captured["called_with"] = {
            "node_id": node_id,
            "field_name_changed": field_name_changed,
        }
        return _ucas_response_with_channel_field()

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.updated_node_config", side_effect=fake_ucas):
        result = get_node_dynamic_fields("wf-1", "slack-1")

    # Inferred field_name_changed = the connection_id field (auto-detected)
    assert "slack_connection_id" in captured["called_with"]["field_name_changed"]
    assert result["field_count"] == 4
    # The two real dropdowns (connection field + channels) are in dropdown_field_names
    assert "pipedream-slack_v2-slack_v2_new_message_in_channels-slack_connection_id" in result["dropdown_field_names"]
    assert "pipedream-slack_v2-slack_v2_new_message_in_channels-conversations" in result["dropdown_field_names"]
    # Boolean fields are NOT in dropdown_field_names
    assert "pipedream-slack_v2-slack_v2_new_message_in_channels-resolveNames" not in result["dropdown_field_names"]
    # v0.2.21: note now points at reload_pipedream_props for dynamic fields
    assert "reload_pipedream_props" in result["note"] or "DYNAMIC" in result["note"]


def test_get_node_dynamic_fields_honors_explicit_field_name_changed():
    """If caller passes field_name_changed explicitly, don't auto-infer."""
    block = _slack_new_message_block()
    fake_wf = {"id": "wf-1", "blocks": [block]}
    captured = {}

    def fake_ucas(node_id, node_definition_id, field_name_changed, **kw):
        captured["fnc"] = field_name_changed
        return _ucas_response_with_channel_field()

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.updated_node_config", side_effect=fake_ucas):
        get_node_dynamic_fields("wf-1", "slack-1", field_name_changed="some-other-field")

    assert captured["fnc"] == "some-other-field"


def test_get_node_dynamic_fields_raises_when_node_has_no_settings():
    """Defensive — if the node has no settings yet, we can't infer a
    field_name_changed. Raise a clear ValueError telling the caller what to do."""
    import pytest
    block = {"id": "slack-1", "typeId": "t", "settings_field_values": []}
    fake_wf = {"id": "wf-1", "blocks": [block]}

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf):
        with pytest.raises(ValueError) as exc:
            get_node_dynamic_fields("wf-1", "slack-1")
    msg = str(exc.value)
    assert "placeholder connection_id" in msg or "attach the node" in msg.lower()


def test_get_node_dynamic_fields_raises_when_node_not_in_workflow():
    """Standard not-found behavior."""
    import pytest
    fake_wf = {"id": "wf-1", "blocks": []}
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf):
        with pytest.raises(ValueError) as exc:
            get_node_dynamic_fields("wf-1", "missing-node")
    assert "missing-node" in str(exc.value)


def test_dropdown_field_names_includes_fields_with_inputTypes_datasource():
    """Regression guard: the channel field has dataSource buried in
    inputTypes[0].dataSource (not at the top level). Make sure we detect
    that variant."""
    fields = [
        # No top-level dataSource — only inputTypes[].dataSource
        {"name": "channels", "type": "multi_select",
         "inputTypes": [{"dataSource": {"endpoint": "/nodes/field-options"}}]},
        # Boolean (should NOT appear in dropdown list)
        {"name": "resolveNames", "type": "boolean"},
        # app_connection — counts as a dropdown
        {"name": "connection_id", "type": "app_connection"},
    ]
    fake_wf = {"id": "wf-1", "blocks": [{"id": "n", "typeId": "t",
        "settings_field_values": [{"field_name": "connection_id", "field_value": "x"}]}]}

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.updated_node_config",
               return_value={"nodeId": "n", "nodeDefinition": {"fields": fields}}):
        result = get_node_dynamic_fields("wf-1", "n")

    assert "channels" in result["dropdown_field_names"]
    assert "connection_id" in result["dropdown_field_names"]
    assert "resolveNames" not in result["dropdown_field_names"]
