"""Tests for v0.2.28 fixes:

  Fix #1: attach_node returns ok=False when post-attach validation finds a
          node_config_error on the new block. Pre-fix: ok=True even when
          node_config_error was set (the 2026-05-29 friction analysis showed
          an agent reading ok:true and moving on for 30+ turns).

  Fix #2: get_node_dynamic_fields catches HTTP 500 from native nodes and
          returns structured guidance pointing at the cookbook, instead of
          raising. The /nodes/updated-config-and-status endpoint is
          Pipedream-only.

  Fix #3: attach_node defensive auto-retry for "Fields not found in available
          data" — v0.2.23's _build_inputs_from_parents didn't fire for
          linkedin_scraping.* typeIds. The retry re-PUTs the node to force
          input refresh.

  Fix #4: update_node_setting injects a `hint` field when the platform
          returns "Whoops! Missing a field - X" — points to the cookbook for
          reference-group envelope patterns.
"""
from unittest.mock import patch, MagicMock
import pytest
from nrev_wf_mcp.server import (
    attach_node,
    get_node_dynamic_fields,
    update_node_setting,
)


# ════════════════════════════════════════════════════════════════════════
# Fix #1 — attach_node ok reflects post-attach validation
# ════════════════════════════════════════════════════════════════════════


def test_attach_node_ok_false_when_post_attach_validation_finds_node_error():
    """The friction case: paste succeeds (no err), but validate_workflow finds
    a node_config_error on the new block. ok should be False, not True."""
    fake_wf_pre = {"id": "wf-1", "blocks": []}
    fake_wf_post = {
        "id": "wf-1",
        "isRunable": False,
        "workflowConfigError": None,
        "blocks": [
            {
                "id": "new-id",
                "variableName": "TestNode",
                "node_config_error": "Oops! No settings provided. Please fill in the settings and let's roll!",
            }
        ],
    }

    paste_resp = {
        "workflowConfigError": None,
        "isRunable": False,
        "blocks": fake_wf_post["blocks"],
    }

    with patch("nrev_wf_mcp.server.api.get_workflow", side_effect=[fake_wf_pre, fake_wf_post, fake_wf_post]), \
         patch("nrev_wf_mcp.server._attach_block_via_paste_and_wire",
                return_value=(paste_resp, "new-id")), \
         patch("nrev_wf_mcp.server._new_block_error_from_paste", return_value=None), \
         patch("nrev_wf_mcp.server._lookup_node_def_flags", return_value=(True, False)):
        result = attach_node(
            workflow_id="wf-1",
            parent_node_ids=[],
            type_id="some-native-typeid",
            name="TestNode",
            settings={},
            force_root=True,
        )

    assert result["ok"] is False, "ok should be False when validation finds node_config_error"
    assert "No settings provided" in (result["node_config_error"] or "")


def test_attach_node_ok_true_when_no_post_attach_validation_errors():
    """Sanity: when validation is clean, ok stays True."""
    fake_wf_pre = {"id": "wf-1", "blocks": []}
    fake_wf_post = {
        "id": "wf-1",
        "isRunable": True,
        "workflowConfigError": None,
        "blocks": [
            {"id": "new-id", "variableName": "TestNode", "node_config_error": None}
        ],
    }
    paste_resp = {"workflowConfigError": None, "isRunable": True, "blocks": fake_wf_post["blocks"]}

    with patch("nrev_wf_mcp.server.api.get_workflow", side_effect=[fake_wf_pre, fake_wf_post, fake_wf_post]), \
         patch("nrev_wf_mcp.server._attach_block_via_paste_and_wire",
                return_value=(paste_resp, "new-id")), \
         patch("nrev_wf_mcp.server._new_block_error_from_paste", return_value=None), \
         patch("nrev_wf_mcp.server._lookup_node_def_flags", return_value=(True, False)):
        result = attach_node(
            workflow_id="wf-1",
            parent_node_ids=[],
            type_id="some-native-typeid",
            name="TestNode",
            settings={"some_field": "some_value"},
            force_root=True,
        )

    assert result["ok"] is True
    assert result["node_config_error"] is None


# ════════════════════════════════════════════════════════════════════════
# Fix #2 — get_node_dynamic_fields native node detection
# ════════════════════════════════════════════════════════════════════════


def test_get_node_dynamic_fields_returns_guidance_on_native_node_500():
    """Native nodes return HTTP 500. The tool should catch + return
    structured guidance pointing at the cookbook, not raise."""
    fake_wf = {
        "blocks": [
            {
                "id": "n1",
                "typeId": "linkedin_scraping.get_person_profile",
                "settings_field_values": [
                    {"field_name": "linkedin_scraping-get_person_profile-linkedin_url",
                     "field_value": "https://linkedin.com/in/test"},
                ],
            }
        ]
    }

    def boom(*args, **kwargs):
        raise RuntimeError("HTTP 500 from /nodes/updated-config-and-status: Internal Server Error")

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.updated_node_config", side_effect=boom):
        result = get_node_dynamic_fields(workflow_id="wf-1", node_id="n1")

    assert result["ok"] is False
    assert result["error_kind"] == "native_or_unsupported"
    assert "RuntimeError" in result["raw_error"]
    assert "NATIVE_NODE_SETTINGS_COOKBOOK" in result["guidance"]
    assert result["fields"] is None


def test_get_node_dynamic_fields_normal_path_unaffected_by_fix_2():
    """When the Pipedream endpoint succeeds, behavior is unchanged."""
    fake_wf = {
        "blocks": [
            {
                "id": "n1",
                "typeId": "pipedream.gmail.gmail_send_email",
                "settings_field_values": [
                    {"field_name": "pipedream-gmail-gmail_send_email-gmail",
                     "field_value": "conn-id-123"},
                ],
            }
        ]
    }
    fake_resp = {
        "nodeId": "n1",
        "nodeDefinition": {
            "fields": [
                {"name": "to", "type": "text", "label": "To", "required": True},
            ]
        },
        "availableOptions": None,
        "settingFieldValues": [],
    }
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.updated_node_config", return_value=fake_resp):
        result = get_node_dynamic_fields(workflow_id="wf-1", node_id="n1")

    # The normal path returns the standard shape — no `ok` or `error_kind` keys
    assert "ok" not in result
    assert result["field_count"] == 1
    assert result["fields"][0]["name"] == "to"


# ════════════════════════════════════════════════════════════════════════
# Fix #3 — attach_node defensive input-refresh retry
# ════════════════════════════════════════════════════════════════════════


def test_attach_node_defensive_retry_on_fields_not_found():
    """When initial validation shows 'Fields not found in available data',
    re-PUT the node to force input refresh. If second validation passes,
    surface input_refresh_recovered=True."""
    parent_block = {
        "id": "parent-1",
        "variableName": "Parent",
        "position": {"x": 100, "y": 0},
        "outputs": [{"columns_metadata": [
            {"column_name": "linkedin_url", "data_type": "string"},
        ]}],
    }
    fake_wf_pre = {"id": "wf-1", "blocks": [parent_block]}
    # First validation: returns error
    fake_wf_post_err = {
        "id": "wf-1",
        "isRunable": False,
        "blocks": [
            {"id": "new-id", "variableName": "Get Person Profile",
             "node_config_error": "Fields not found in available data: linkedin_url"},
            parent_block,
        ],
    }
    # Second validation (after retry): clean
    fake_wf_post_clean = {
        "id": "wf-1",
        "isRunable": True,
        "blocks": [
            {"id": "new-id", "variableName": "Get Person Profile",
             "node_config_error": None},
            parent_block,
        ],
    }
    paste_resp = {"workflowConfigError": None, "isRunable": False,
                   "blocks": fake_wf_post_err["blocks"]}

    # Calls in order: 1) attach_node fetches wf, 2) first validate fetches wf,
    # 3) retry path fetches wf for the latest node, 4) second validate fetches wf
    with patch("nrev_wf_mcp.server.api.get_workflow",
                side_effect=[fake_wf_pre, fake_wf_post_err, fake_wf_post_err, fake_wf_post_clean]), \
         patch("nrev_wf_mcp.server._attach_block_via_paste_and_wire",
                return_value=(paste_resp, "new-id")), \
         patch("nrev_wf_mcp.server._new_block_error_from_paste", return_value=None), \
         patch("nrev_wf_mcp.server.api.put_node", return_value={"node_config_error": None}), \
         patch("nrev_wf_mcp.server._lookup_node_def_flags", return_value=(False, False)):
        result = attach_node(
            workflow_id="wf-1",
            parent_node_ids=["parent-1"],
            type_id="linkedin_scraping-get_person_profile-typeid",
            name="Get Person Profile",
            settings={"linkedin_scraping-get_person_profile-linkedin_url": "{{linkedin_url}}"},
        )

    assert result["ok"] is True, "retry should recover the attach"
    assert result["input_refresh_recovered"] is True
    assert result["node_config_error"] is None


def test_attach_node_retry_does_not_fire_for_non_matching_errors():
    """If the validation error is something other than 'Fields not found in
    available data', the retry should NOT fire — ok stays False."""
    parent_block = {
        "id": "parent-1",
        "variableName": "Parent",
        "position": {"x": 100, "y": 0},
        "outputs": [{"columns_metadata": []}],
    }
    fake_wf_pre = {"id": "wf-1", "blocks": [parent_block]}
    fake_wf_post = {
        "id": "wf-1",
        "isRunable": False,
        "blocks": [
            {"id": "new-id", "variableName": "X",
             "node_config_error": "Whoops! Missing a field - person_reference. Fill it in and let's roll!"},
            parent_block,
        ],
    }
    paste_resp = {"workflowConfigError": None, "isRunable": False,
                   "blocks": fake_wf_post["blocks"]}

    put_node_mock = MagicMock(return_value={"node_config_error": None})
    with patch("nrev_wf_mcp.server.api.get_workflow",
                side_effect=[fake_wf_pre, fake_wf_post, fake_wf_post]), \
         patch("nrev_wf_mcp.server._attach_block_via_paste_and_wire",
                return_value=(paste_resp, "new-id")), \
         patch("nrev_wf_mcp.server._new_block_error_from_paste", return_value=None), \
         patch("nrev_wf_mcp.server.api.put_node", put_node_mock), \
         patch("nrev_wf_mcp.server._lookup_node_def_flags", return_value=(False, False)):
        result = attach_node(
            workflow_id="wf-1",
            parent_node_ids=["parent-1"],
            type_id="some-typeid",
            name="X",
            settings={},
        )

    assert result["ok"] is False
    assert result["input_refresh_recovered"] is False
    put_node_mock.assert_not_called()  # retry didn't fire


# ════════════════════════════════════════════════════════════════════════
# Fix #4 — update_node_setting hints reference-group on "Missing a field"
# ════════════════════════════════════════════════════════════════════════


def test_update_node_setting_injects_hint_on_missing_field_error():
    """When platform returns 'Whoops! Missing a field - X', the response
    should include a `hint` pointing to the cookbook."""
    fake_node = {
        "id": "n1",
        "settings_field_values": [{"field_name": "domain", "field_value": "old"}],
    }
    fake_wf = {"blocks": [fake_node]}

    def fake_put(*args, **kwargs):
        return {
            "ok": False,
            "node_config_error": "Whoops! Missing a field - company_reference. Fill it in and let's roll!",
            "workflowConfigError": None,
            "isRunable": False,
            "validation": None,
        }

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server._put_node_and_validate", side_effect=fake_put):
        result = update_node_setting(
            workflow_id="wf-1", node_id="n1",
            field_path="domain", value="example.com",
        )

    assert "hint" in result
    assert "reference-group envelope" in result["hint"].lower()
    assert "cookbook" in result["hint"].lower()


def test_update_node_setting_no_hint_for_unrelated_errors():
    """Other error messages should NOT trigger the hint."""
    fake_node = {
        "id": "n1",
        "settings_field_values": [{"field_name": "x", "field_value": "old"}],
    }
    fake_wf = {"blocks": [fake_node]}

    def fake_put(*args, **kwargs):
        return {
            "ok": False,
            "node_config_error": "Some other unrelated error",
            "workflowConfigError": None,
            "isRunable": False,
            "validation": None,
        }

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server._put_node_and_validate", side_effect=fake_put):
        result = update_node_setting(
            workflow_id="wf-1", node_id="n1",
            field_path="x", value="y",
        )

    assert "hint" not in result


def test_update_node_setting_no_hint_when_ok():
    """Happy path: no hint added when there's no error."""
    fake_node = {
        "id": "n1",
        "settings_field_values": [{"field_name": "x", "field_value": "old"}],
    }
    fake_wf = {"blocks": [fake_node]}

    def fake_put(*args, **kwargs):
        return {
            "ok": True,
            "node_config_error": None,
            "workflowConfigError": None,
            "isRunable": True,
            "validation": None,
        }

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server._put_node_and_validate", side_effect=fake_put):
        result = update_node_setting(
            workflow_id="wf-1", node_id="n1",
            field_path="x", value="y",
        )

    assert "hint" not in result


# ════════════════════════════════════════════════════════════════════════
# Documentation sanity checks
# ════════════════════════════════════════════════════════════════════════


def test_cookbook_file_exists_and_covers_key_nodes():
    """The cookbook is the load-bearing fix from the 2026-05-29 friction
    analysis. Make sure it exists and covers the nodes that were specifically
    spelunked during the session."""
    import pathlib
    cookbook = pathlib.Path(__file__).parent.parent / "docs" / "NATIVE_NODE_SETTINGS_COOKBOOK.md"
    assert cookbook.exists(), "Cookbook must exist at docs/NATIVE_NODE_SETTINGS_COOKBOOK.md"
    text = cookbook.read_text()
    # Nodes the 2026-05-29 session struggled with
    for node in ["Get Person Profile", "Enrich Company", "Enrich People",
                  "Fetch Jobs", "Add Row", "Update Row", "Query Table"]:
        assert node in text, f"Cookbook missing entry for {node!r}"
    # Pattern documentation
    assert "reference-group" in text.lower() or "reference_group" in text.lower()
    assert "{{column_name}}" in text or "{{name}}" in text
    assert "Cell type mismatch" in text or "expected number" in text.lower()


def test_attach_node_docstring_promotes_template_syntax():
    """Template syntax must be in the top-level attach_node docstring, not
    buried in the nrev_tables section."""
    doc = attach_node.__doc__ or ""
    # Look for the universal template syntax section heading
    assert "TEMPLATE SYNTAX" in doc.upper() or "{{column_name}}" in doc
    # Cookbook reference
    assert "NATIVE_NODE_SETTINGS_COOKBOOK" in doc or "cookbook" in doc.lower()
    # Reference-group pattern call-out
    assert "reference-group" in doc.lower() or "REFERENCE-GROUP" in doc.upper()


def test_get_node_dynamic_fields_docstring_warns_native_only():
    """Docstring must tell agents this tool is Pipedream-only."""
    doc = get_node_dynamic_fields.__doc__ or ""
    assert "Pipedream" in doc and ("NATIVE" in doc or "native" in doc)
    assert "cookbook" in doc.lower() or "NATIVE_NODE_SETTINGS_COOKBOOK" in doc
