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
    _RESOURCE_APP_MAP,
    _looks_like_google_sheet_id,
    _looks_like_worksheet_id,
    _settings_contain_value,
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


def test_cookbook_typeids_match_live_catalog():
    """Cookbook validation agent 2026-05-30 found 5 wrong/stub typeIds.
    Hard-pin the verified UUIDs here so they never regress to stubs.
    These were verified live against prod catalog on 2026-05-30."""
    import pathlib
    cookbook = pathlib.Path(__file__).parent.parent / "docs" / "NATIVE_NODE_SETTINGS_COOKBOOK.md"
    text = cookbook.read_text()
    verified = {
        # node name → real typeId (from list_node_definitions on prod)
        "Get Person Profile":  "4e5005c4-b1a5-417b-af59-453b86f489db",
        "Get Post by Person":  "c854f6d7-f44d-470f-8e9c-f3c42a24a888",
        "Enrich People":       "6439527f-abe7-44e5-b462-60e1a45be619",
        "Enrich Company":      "1e908fa8-d63b-4a67-bb58-004dc15052e2",
        "Fetch Jobs":          "d78f7f27-3759-4590-a6a7-525dbda774b1",
        "Search People":       "15145759-901a-4a87-8db3-84cd9e734a49",
        # nrev_tables literal-stub UUIDs that ARE the real catalog values
        "Query Table":         "a1b2c3d4-0003-4000-8000-000000000003",
        "Add Row":             "a1b2c3d4-0001-4000-8000-000000000001",
        "Update Row":          "a1b2c3d4-0002-4000-8000-000000000002",
        "Get Row":             "a1b2c3d4-0004-4000-8000-000000000004",
    }
    for name, type_id in verified.items():
        assert type_id in text, (
            f"Cookbook missing verified typeId {type_id} for {name!r}. "
            f"If the platform changed the typeId, re-run "
            f"`list_node_definitions(search={name!r})` and update both this "
            f"test AND the cookbook."
        )
    # The known-bad stubs must NOT have crept back in
    for stub in [
        "4e5005c4-86fa-46d6-8e76-13f0cbcd5d76",  # wrong Get Person Profile
        "cf30b3d8-3a90-4f70-bca6-2c0c87bd4ada",  # wrong Get Post by Person
        "6439527f-9aaf-441a-9c5c-7d9e5c7e3d96",  # wrong Enrich People
        "91ec0d74-",                               # Enrich Company placeholder prefix
    ]:
        assert stub not in text, (
            f"Cookbook still has the broken stub {stub!r} that the 2026-05-30 "
            f"validation flagged. Replace with the verified typeId."
        )


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


# ════════════════════════════════════════════════════════════════════════
# v0.2.28 Sheets sheetId/worksheetId trap — helpers + warnings + map
# (Colleague's 2026-05-30 friction: agents pass the human sheet name into
# `*-sheetId` because the field name is misleading. Resource scan also
# missed Update Cell / Update/Upsert Row.)
# ════════════════════════════════════════════════════════════════════════


def test_looks_like_google_sheet_id_accepts_real_ids():
    """Canonical Google Spreadsheet IDs are ~44 chars of [A-Za-z0-9_-]."""
    assert _looks_like_google_sheet_id(
        "1_k71sm0X8Cb5mo_5M7nuPvn6vv24qYxH_1UxU4TfrIQ"
    )
    # 30 chars is the lenient floor
    assert _looks_like_google_sheet_id("a" * 30)
    # Templates always pass — runtime resolves them
    assert _looks_like_google_sheet_id("{{spreadsheet_id}}")
    assert _looks_like_google_sheet_id("{{$.steps.trigger.event.sheetId}}")


def test_looks_like_google_sheet_id_rejects_human_names():
    """Free-text workbook names (the agent's mistake) must be rejected."""
    assert not _looks_like_google_sheet_id("MCP Testing")  # has space
    assert not _looks_like_google_sheet_id("Q4 Leads")
    assert not _looks_like_google_sheet_id("Outreach")  # too short
    assert not _looks_like_google_sheet_id("")
    assert not _looks_like_google_sheet_id(None)
    # Funny edge: a 29-char alphanumeric blob is rejected (not 30)
    assert not _looks_like_google_sheet_id("a" * 29)


def test_looks_like_worksheet_id_accepts_integers_and_digit_strings():
    """Worksheet tab gids are integers (often surfaced as digit strings)."""
    assert _looks_like_worksheet_id(101353668)
    assert _looks_like_worksheet_id("101353668")
    assert _looks_like_worksheet_id("0")
    assert _looks_like_worksheet_id("-1")  # signed digits allowed
    assert _looks_like_worksheet_id("{{worksheet_id}}")  # template


def test_looks_like_worksheet_id_rejects_tab_names():
    """The classic mistake: agent passes "Sheet1" instead of the gid."""
    assert not _looks_like_worksheet_id("Sheet1")
    assert not _looks_like_worksheet_id("Leads")
    assert not _looks_like_worksheet_id("")
    assert not _looks_like_worksheet_id(None)
    assert not _looks_like_worksheet_id("abc123")  # mixed


def test_resource_map_includes_new_sheets_typeids():
    """v0.2.28: Create Spreadsheet, Update Cell, Update/Upsert Row were
    missing — find_workflows_using_resource silently failed for those."""
    sheets_ids = _RESOURCE_APP_MAP["google_sheets"]["type_ids"]
    assert "5c4d12a3-8874-45e7-9dec-1196e3032ee7" in sheets_ids  # Create Spreadsheet
    assert "59c89f54-9b1f-4f93-a158-d1552658d222" in sheets_ids  # Update Cell
    assert "3df67eff-0724-4e43-b43e-681a6f01ea1f" in sheets_ids  # Update/Upsert Row
    # All 22 known typeIds should be present
    assert len(sheets_ids) >= 22


def test_resource_map_slack_field_fragment_is_tuple():
    """v0.2.28: Slack field naming is inconsistent (`-conversation` vs
    `-channel`). The fragment must match both."""
    slack_spec = _RESOURCE_APP_MAP["slack"]
    ff = slack_spec["field_fragment"]
    assert isinstance(ff, tuple)
    assert "conversation" in ff
    assert "channel" in ff


def test_resource_map_slack_includes_channel_bound_actions():
    """v0.2.28 expanded Slack from 6 → ~19 typeIds, picking up channel-
    scoped actions like Reply to Thread, Add Reaction, Archive Channel."""
    slack_ids = _RESOURCE_APP_MAP["slack"]["type_ids"]
    assert "ca1af997-c203-49c2-bfe7-8841987b1d7a" in slack_ids  # Reply to Thread
    assert "f2767b0f-6fb3-4249-bd09-6be21585773a" in slack_ids  # Add Emoji Reaction
    assert "ffe7471d-16a5-465f-8428-cb2226f3eecb" in slack_ids  # Invite User to Channel
    assert len(slack_ids) >= 15


def test_settings_contain_value_matches_any_fragment_in_tuple():
    """The walker must match a field whose name contains EITHER 'conversation'
    or 'channel' when given the tuple form."""
    settings = [
        {"field_name": "slack-send_message-conversation",
         "field_value": "C09KCQE6TFE"},
        {"field_name": "slack-archive_channel-channel",
         "field_value": "C09KCQE6TFE"},
        {"field_name": "slack-send_message-text",
         "field_value": "C09KCQE6TFE"},  # value matches but field doesn't
    ]
    hits = _settings_contain_value(
        settings, "C09KCQE6TFE", key_fragment=("conversation", "channel")
    )
    field_names = {h["field_name"] for h in hits}
    assert "slack-send_message-conversation" in field_names
    assert "slack-archive_channel-channel" in field_names
    # The -text field should NOT match (value matches but field doesn't)
    assert "slack-send_message-text" not in field_names


def test_settings_contain_value_legacy_string_fragment_still_works():
    """Don't regress the str-form caller — single string fragment must still
    work the way it did before v0.2.28."""
    settings = [
        {"field_name": "google_sheets-get_values_in_range-sheetId",
         "field_value": "1_k71sm0X8Cb5mo"},
        {"field_name": "google_sheets-add_single_row-sheetId",
         "field_value": "different"},
    ]
    hits = _settings_contain_value(
        settings, "1_k71sm0X8Cb5mo", key_fragment="sheetId"
    )
    assert len(hits) == 1
    assert hits[0]["field_name"] == "google_sheets-get_values_in_range-sheetId"


def _attach_node_with_sheets_settings(settings: dict):
    """Helper: invoke attach_node against a clean wf with the given settings,
    return the response so tests can inspect pipedream_field_warnings."""
    parent = {
        "id": "parent-1",
        "variableName": "Trigger",
        "position": {"x": 100, "y": 0},
        "outputs": [{"columns_metadata": []}],
    }
    fake_wf_pre = {"id": "wf-1", "blocks": [parent]}
    fake_wf_post = {
        "id": "wf-1",
        "isRunable": True,
        "workflowConfigError": None,
        "blocks": [
            {"id": "new-id", "variableName": "Get Values",
             "node_config_error": None},
            parent,
        ],
    }
    paste_resp = {"workflowConfigError": None, "isRunable": True,
                  "blocks": fake_wf_post["blocks"]}
    with patch("nrev_wf_mcp.server.api.get_workflow",
                side_effect=[fake_wf_pre, fake_wf_post, fake_wf_post]), \
         patch("nrev_wf_mcp.server._attach_block_via_paste_and_wire",
                return_value=(paste_resp, "new-id")), \
         patch("nrev_wf_mcp.server._new_block_error_from_paste", return_value=None), \
         patch("nrev_wf_mcp.server._lookup_node_def_flags", return_value=(False, False)):
        return attach_node(
            workflow_id="wf-1",
            parent_node_ids=["parent-1"],
            type_id="pipedream.google_sheets-get_values_in_range",
            name="Get Values",
            settings=settings,
        )


def test_attach_node_warns_when_sheet_id_looks_like_name():
    """The whole point: agent passes 'MCP Testing' instead of the URL ID.
    We must surface a loud pipedream_field_warning."""
    result = _attach_node_with_sheets_settings({
        "google_sheets-get_values_in_range-sheetId": "MCP Testing",
        "google_sheets-get_values_in_range-range": "Sheet1!A:Z",
    })
    warnings = result.get("pipedream_field_warnings") or []
    sheet_warnings = [w for w in warnings
                       if w.get("field_name", "").endswith("-sheetId")]
    assert len(sheet_warnings) == 1, \
        f"expected a sheet-id warning, got {warnings!r}"
    w = sheet_warnings[0]
    assert "Spreadsheet ID" in w["issue"] or "spreadsheet" in w["issue"].lower()
    assert "list_field_options" in w["issue"]
    assert w["expected"].startswith("google spreadsheet ID")


def test_attach_node_no_warning_for_legit_sheet_id():
    """A real 44-char ID must NOT trip the warning."""
    result = _attach_node_with_sheets_settings({
        "google_sheets-get_values_in_range-sheetId":
            "1_k71sm0X8Cb5mo_5M7nuPvn6vv24qYxH_1UxU4TfrIQ",
    })
    warnings = result.get("pipedream_field_warnings") or []
    sheet_warnings = [w for w in warnings
                       if w.get("field_name", "").endswith("-sheetId")]
    assert sheet_warnings == [], \
        f"valid ID should not warn, got {sheet_warnings!r}"


def test_attach_node_no_warning_for_templated_sheet_id():
    """Templates can't be checked statically — must not warn."""
    result = _attach_node_with_sheets_settings({
        "google_sheets-get_values_in_range-sheetId": "{{spreadsheet_id}}",
    })
    warnings = result.get("pipedream_field_warnings") or []
    sheet_warnings = [w for w in warnings
                       if w.get("field_name", "").endswith("-sheetId")]
    assert sheet_warnings == []


def test_attach_node_warns_when_worksheet_id_looks_like_name():
    """Agent passes 'Sheet1' (tab name) instead of the gid."""
    result = _attach_node_with_sheets_settings({
        "google_sheets-get_values_in_range-sheetId":
            "1_k71sm0X8Cb5mo_5M7nuPvn6vv24qYxH_1UxU4TfrIQ",  # valid
        "google_sheets-get_values_in_range-worksheetId": "Sheet1",  # bad
    })
    warnings = result.get("pipedream_field_warnings") or []
    ws_warnings = [w for w in warnings
                    if w.get("field_name", "").endswith("-worksheetId")]
    assert len(ws_warnings) == 1, \
        f"expected a worksheet-id warning, got {warnings!r}"
    w = ws_warnings[0]
    assert "Worksheet" in w["issue"] or "worksheet" in w["issue"].lower()
    assert "integer" in w["issue"].lower()


def test_attach_node_no_warning_for_legit_worksheet_id():
    """Numeric gid must not trip the warning."""
    result = _attach_node_with_sheets_settings({
        "google_sheets-get_values_in_range-sheetId":
            "1_k71sm0X8Cb5mo_5M7nuPvn6vv24qYxH_1UxU4TfrIQ",
        "google_sheets-get_values_in_range-worksheetId": "101353668",
    })
    warnings = result.get("pipedream_field_warnings") or []
    ws_warnings = [w for w in warnings
                    if w.get("field_name", "").endswith("-worksheetId")]
    assert ws_warnings == []


def test_cookbook_documents_sheetid_worksheetid_trap():
    """The cookbook must surface the sheetId/worksheetId trap explicitly so
    future agents see it before they make the mistake."""
    from pathlib import Path
    cookbook = Path(__file__).parent.parent / "docs" / "NATIVE_NODE_SETTINGS_COOKBOOK.md"
    text = cookbook.read_text()
    assert "sheetId" in text and "worksheetId" in text
    # The trap section should appear by name
    assert "trap" in text.lower() or "WRONG" in text
    # Should mention list_field_options as the recovery path
    assert "list_field_options" in text
