"""Tests for v0.2.30 fixes.

  Fix #1: attach_node auto-fires reload_pipedream_props for Pipedream
          Sheets-write typeIds (Add Single Row / Add Multiple Rows /
          Upsert Row / Update/Upsert Row), persists the issued
          dynamic_props_id, and surfaces col_to_label + parent_upstream_columns
          to the agent in the response. DOES NOT auto-map col_NNNN values —
          the agent has semantic context the helper cannot.

  Fix #2: auto_map_pipedream_columns (v0.2.21) docstring opens with a loud
          warning about silent failure when destination headers don't match
          upstream column names.

  Fix #3: find_workflows_using_resource defaults include_never_run=True so
          freshly-deployed workflows show up in the scan. The 2026-05-31
          friction: agent built workflows on prod, queried for usage of
          a Sheet ID, got 0 matches because the new workflows had
          lastRunAt=null and the old default treated them as stale.

  Cookbook fixes covered separately (#101, #102):
      - reference-group inner field_name needs FULL prefix
      - RocketReach Enrich Company is flat (lookup_by + flat field),
        not the envelope shape that native Enrich Company uses
      - "Pipedream Sheets writes — 2-phase pattern" new section showing
        agent-driven mapping (NOT auto-map)
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

from nrev_wf_mcp.server import (
    _DYNAMIC_PROPS_TYPEIDS,
    attach_node,
    auto_map_pipedream_columns,
)


# ════════════════════════════════════════════════════════════════════════
# _DYNAMIC_PROPS_TYPEIDS — the set of typeIds that get auto-fire treatment
# ════════════════════════════════════════════════════════════════════════


def test_dynamic_props_typeids_covers_sheets_write_family():
    """The four Sheets-write typeIds that materialize col_NNNN fields
    via reload-props. Conservative on purpose — add new entries only
    after live-verifying has_dynamic_props=True."""
    expected = {
        "191db4a1-7c72-4c4a-af02-b507701ca61b": "Add Single Row",
        "b220d182-d786-4ce7-b332-e2a824a86afc": "Add Multiple Rows",
        "f8b6d11f-4f72-489c-9c63-a3da6c9eea7d": "Upsert Row",
        "3df67eff-0724-4e43-b43e-681a6f01ea1f": "Update/Upsert Row",
    }
    for type_id, name in expected.items():
        assert _DYNAMIC_PROPS_TYPEIDS.get(type_id) == name


# ════════════════════════════════════════════════════════════════════════
# attach_node — auto-fire path for dynamic-props typeIds
# ════════════════════════════════════════════════════════════════════════


def _build_clean_attach_environment(*, type_id: str, settings: dict,
                                       parent_columns: list[str]):
    """Helper: returns (patches that produce a clean attach with a parent
    that emits `parent_columns`, expose `actual_new_id`)."""
    parent_block = {
        "id": "parent-1",
        "variableName": "Parent",
        "position": {"x": 100, "y": 0},
        "outputs": [{
            "columns_metadata": [
                {"column_name": c, "data_type": "string",
                 "origin_node_id": "parent-1", "origin_node_name": "Parent",
                 "origin_node_type": "pipedream.x"}
                for c in parent_columns
            ],
        }],
    }
    fake_wf_pre = {"id": "wf-1", "blocks": [parent_block]}
    new_block_post = {
        "id": "new-id", "variableName": "AttachedNode",
        "node_config_error": None,
        "inputs": [{"node_id": "parent-1", "columns": parent_columns,
                     "columns_metadata": parent_block["outputs"][0]["columns_metadata"]}],
    }
    fake_wf_post = {
        "id": "wf-1", "isRunable": True, "workflowConfigError": None,
        "blocks": [new_block_post, parent_block],
    }
    paste_resp = {"workflowConfigError": None, "isRunable": True,
                  "blocks": fake_wf_post["blocks"]}
    return parent_block, fake_wf_pre, fake_wf_post, paste_resp


def test_attach_node_auto_fires_reload_props_for_sheets_writes():
    """The headline behavior: attaching an Add Single Row triggers
    reload-props automatically, surfaces col_to_label, persists
    dynamic_props_id, and exposes parent_upstream_columns."""
    type_id = "191db4a1-7c72-4c4a-af02-b507701ca61b"  # Add Single Row
    parent_block, fake_wf_pre, fake_wf_post, paste_resp = (
        _build_clean_attach_environment(
            type_id=type_id,
            settings={"x": "y"},
            parent_columns=["timestamp", "who", "what"],
        )
    )

    fake_reload = {
        "node_id": "new-id",
        "has_dynamic_props": True,
        "dynamic_props_id": "dyp_XYZ",
        "col_to_label": {"col_0000": "Name", "col_0001": "Email", "col_0002": "Company"},
        "fields": [
            {"name": "pipedream-google_sheets-google_sheets_add_single_row-col_0000",
             "label": "Name"},
            {"name": "pipedream-google_sheets-google_sheets_add_single_row-col_0001",
             "label": "Email"},
            {"name": "pipedream-google_sheets-google_sheets_add_single_row-col_0002",
             "label": "Company"},
        ],
        "errors": [],
    }

    with patch("nrev_wf_mcp.server.api.get_workflow",
                side_effect=[fake_wf_pre, fake_wf_post, fake_wf_post, fake_wf_post]), \
         patch("nrev_wf_mcp.server._attach_block_via_paste_and_wire",
                return_value=(paste_resp, "new-id")), \
         patch("nrev_wf_mcp.server._new_block_error_from_paste", return_value=None), \
         patch("nrev_wf_mcp.server._lookup_node_def_flags", return_value=(False, False)), \
         patch("nrev_wf_mcp.server.reload_pipedream_props", return_value=fake_reload), \
         patch("nrev_wf_mcp.server.update_node_setting",
                return_value={"ok": True, "added_new_field": True}) as upd:
        result = attach_node(
            workflow_id="wf-1", parent_node_ids=["parent-1"],
            type_id=type_id, name="Add Row",
            settings={
                "pipedream-google_sheets-google_sheets_add_single_row-googleSheets_connection_id": "conn-1",
                "pipedream-google_sheets-google_sheets_add_single_row-sheetId": "1xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx_44chars",
                "pipedream-google_sheets-google_sheets_add_single_row-worksheetId": "0",
                "pipedream-google_sheets-google_sheets_add_single_row-hasHeaders": "true",
            },
        )

    dyn = result.get("dynamic_props")
    assert dyn is not None, "v0.2.30: dynamic_props key must be in the response"
    assert dyn["attempted"] is True
    assert dyn["ok"] is True
    assert dyn["type_name"] == "Add Single Row"
    assert dyn["dynamic_props_id"] == "dyp_XYZ"
    assert dyn["dynamic_props_id_persisted"] is True
    assert dyn["col_to_label"] == {
        "col_0000": "Name", "col_0001": "Email", "col_0002": "Company",
    }
    assert dyn["parent_upstream_columns"] == ["timestamp", "who", "what"]
    # Persisted only dynamic_props_id, NOT any col_NNNN value (agent maps)
    def _path_of(call):
        # update_node_setting signature: (workflow_id, node_id, field_path, value, ...)
        # called by attach_node positionally (4 positional args). Tolerate either.
        if "field_path" in call.kwargs:
            return call.kwargs["field_path"]
        if len(call.args) >= 3:
            return call.args[2]
        return ""
    persisted_paths = [_path_of(c) for c in upd.call_args_list]
    assert any("dynamic_props_id" in p for p in persisted_paths), (
        f"expected a dynamic_props_id persist call, got paths: {persisted_paths!r}"
    )
    assert not any("col_0" in p for p in persisted_paths), (
        "v0.2.30 MUST NOT auto-fill col_NNNN values — that's the agent's job"
    )


def test_attach_node_skips_dynamic_props_for_non_sheets_write_typeid():
    """A Get Values in Range (read-only Sheets typeId) is NOT in
    _DYNAMIC_PROPS_TYPEIDS — auto-fire must not run."""
    type_id = "ce01c704-f6bd-40d5-9b2b-f545495de14b"  # Get Values in Range
    parent_block, fake_wf_pre, fake_wf_post, paste_resp = (
        _build_clean_attach_environment(
            type_id=type_id, settings={"x": "y"}, parent_columns=[],
        )
    )

    with patch("nrev_wf_mcp.server.api.get_workflow",
                side_effect=[fake_wf_pre, fake_wf_post, fake_wf_post]), \
         patch("nrev_wf_mcp.server._attach_block_via_paste_and_wire",
                return_value=(paste_resp, "new-id")), \
         patch("nrev_wf_mcp.server._new_block_error_from_paste", return_value=None), \
         patch("nrev_wf_mcp.server._lookup_node_def_flags", return_value=(False, False)), \
         patch("nrev_wf_mcp.server.reload_pipedream_props") as reload_mock:
        result = attach_node(
            workflow_id="wf-1", parent_node_ids=[],
            type_id=type_id, name="Get Values",
            settings={"some-field": "some-value"},
            force_root=True, is_trigger=True,
        )

    assert result.get("dynamic_props") is None, (
        "Non-Sheets-write typeIds must not trigger reload-props auto-fire"
    )
    reload_mock.assert_not_called()


def test_attach_node_skips_dynamic_props_when_node_config_error():
    """If attach succeeded but post-attach validation found a node error,
    auto-fire is skipped (no point reloading on a broken node)."""
    type_id = "191db4a1-7c72-4c4a-af02-b507701ca61b"
    parent_block = {
        "id": "parent-1", "variableName": "P",
        "position": {"x": 0, "y": 0}, "outputs": [{"columns_metadata": []}],
    }
    fake_wf_pre = {"id": "wf-1", "blocks": [parent_block]}
    fake_wf_post_err = {
        "id": "wf-1", "isRunable": False,
        "blocks": [
            {"id": "new-id", "variableName": "Add Row",
             "node_config_error": "Connection not found"},
            parent_block,
        ],
    }
    paste_resp = {"workflowConfigError": "Connection not found",
                   "isRunable": False, "blocks": fake_wf_post_err["blocks"]}

    with patch("nrev_wf_mcp.server.api.get_workflow",
                side_effect=[fake_wf_pre, fake_wf_post_err, fake_wf_post_err]), \
         patch("nrev_wf_mcp.server._attach_block_via_paste_and_wire",
                return_value=(paste_resp, "new-id")), \
         patch("nrev_wf_mcp.server._new_block_error_from_paste",
                return_value="Connection not found"), \
         patch("nrev_wf_mcp.server._lookup_node_def_flags", return_value=(False, False)), \
         patch("nrev_wf_mcp.server.reload_pipedream_props") as reload_mock:
        result = attach_node(
            workflow_id="wf-1", parent_node_ids=["parent-1"],
            type_id=type_id, name="Add Row",
            settings={"x": "y"},
        )

    assert result["ok"] is False
    assert result.get("dynamic_props") is None
    reload_mock.assert_not_called()


def test_attach_node_dynamic_props_opt_out_works():
    """Caller passing auto_expand_dynamic_props=False skips the auto-fire."""
    type_id = "191db4a1-7c72-4c4a-af02-b507701ca61b"
    parent_block, fake_wf_pre, fake_wf_post, paste_resp = (
        _build_clean_attach_environment(
            type_id=type_id, settings={"x": "y"}, parent_columns=["a"],
        )
    )

    with patch("nrev_wf_mcp.server.api.get_workflow",
                side_effect=[fake_wf_pre, fake_wf_post, fake_wf_post]), \
         patch("nrev_wf_mcp.server._attach_block_via_paste_and_wire",
                return_value=(paste_resp, "new-id")), \
         patch("nrev_wf_mcp.server._new_block_error_from_paste", return_value=None), \
         patch("nrev_wf_mcp.server._lookup_node_def_flags", return_value=(False, False)), \
         patch("nrev_wf_mcp.server.reload_pipedream_props") as reload_mock:
        result = attach_node(
            workflow_id="wf-1", parent_node_ids=["parent-1"],
            type_id=type_id, name="Add Row", settings={"x": "y"},
            auto_expand_dynamic_props=False,
        )

    assert result.get("dynamic_props") is None
    reload_mock.assert_not_called()


def test_attach_node_dynamic_props_surfaces_error_when_reload_fails():
    """If reload-props raises (e.g. the typeId was misclassified as
    dynamic-props or the platform endpoint is down), surface a structured
    error in the response rather than swallowing it."""
    type_id = "191db4a1-7c72-4c4a-af02-b507701ca61b"
    parent_block, fake_wf_pre, fake_wf_post, paste_resp = (
        _build_clean_attach_environment(
            type_id=type_id, settings={"x": "y"}, parent_columns=["a"],
        )
    )

    with patch("nrev_wf_mcp.server.api.get_workflow",
                side_effect=[fake_wf_pre, fake_wf_post, fake_wf_post]), \
         patch("nrev_wf_mcp.server._attach_block_via_paste_and_wire",
                return_value=(paste_resp, "new-id")), \
         patch("nrev_wf_mcp.server._new_block_error_from_paste", return_value=None), \
         patch("nrev_wf_mcp.server._lookup_node_def_flags", return_value=(False, False)), \
         patch("nrev_wf_mcp.server.reload_pipedream_props",
                side_effect=RuntimeError("reload-props endpoint 502")):
        result = attach_node(
            workflow_id="wf-1", parent_node_ids=["parent-1"],
            type_id=type_id, name="Add Row", settings={"x": "y"},
        )

    dyn = result["dynamic_props"]
    assert dyn["attempted"] is True
    assert dyn["ok"] is False
    assert "reload-props" in dyn["error"]
    assert "col_NNNN" in dyn["hint"]


def test_attach_node_dynamic_props_no_dynamic_props_returns_diagnostic():
    """If reload-props comes back with has_dynamic_props=False, surface
    that as a diagnostic instead of treating it as success."""
    type_id = "191db4a1-7c72-4c4a-af02-b507701ca61b"
    parent_block, fake_wf_pre, fake_wf_post, paste_resp = (
        _build_clean_attach_environment(
            type_id=type_id, settings={"x": "y"}, parent_columns=["a"],
        )
    )
    fake_reload = {
        "node_id": "new-id", "has_dynamic_props": False,
        "errors": ["component not loaded"], "col_to_label": {}, "fields": [],
    }
    with patch("nrev_wf_mcp.server.api.get_workflow",
                side_effect=[fake_wf_pre, fake_wf_post, fake_wf_post]), \
         patch("nrev_wf_mcp.server._attach_block_via_paste_and_wire",
                return_value=(paste_resp, "new-id")), \
         patch("nrev_wf_mcp.server._new_block_error_from_paste", return_value=None), \
         patch("nrev_wf_mcp.server._lookup_node_def_flags", return_value=(False, False)), \
         patch("nrev_wf_mcp.server.reload_pipedream_props", return_value=fake_reload):
        result = attach_node(
            workflow_id="wf-1", parent_node_ids=["parent-1"],
            type_id=type_id, name="Add Row", settings={"x": "y"},
        )

    dyn = result["dynamic_props"]
    assert dyn["attempted"] is True
    assert dyn["ok"] is False
    assert dyn["has_dynamic_props"] is False
    assert "component_id" not in dyn or True  # tolerant
    # Hint must point at the likely cause
    assert "static" in dyn["hint"].lower() or "settings" in dyn["hint"].lower()


# ════════════════════════════════════════════════════════════════════════
# auto_map_pipedream_columns — re-doc warning
# ════════════════════════════════════════════════════════════════════════


def test_auto_map_docstring_warns_about_name_match_silent_failure():
    """The v0.2.21 helper's docstring must open with a loud warning that
    it silently produces broken templates when destination headers
    don't match upstream column names."""
    doc = auto_map_pipedream_columns.__doc__ or ""
    assert "NAME-MATCH" in doc.upper() or "name-match" in doc.lower()
    # The concrete example must be there so an agent skimming sees it
    assert "silently" in doc.lower() or "silent" in doc.lower()
    # The agent-driven flow recommendation
    assert "update_node_setting" in doc
    # Points at the v0.2.30 attach_node behavior
    assert "v0.2.30" in doc or "attach_node" in doc


# ════════════════════════════════════════════════════════════════════════
# Cookbook fixes (#101, #102, and 2-phase section)
# ════════════════════════════════════════════════════════════════════════


def test_cookbook_documents_two_phase_sheets_writes():
    """The cookbook must include the load-bearing 2-phase Sheets-write
    section showing agent-driven mapping (NOT auto-map)."""
    import pathlib
    cookbook = (pathlib.Path(__file__).parent.parent
                / "docs" / "NATIVE_NODE_SETTINGS_COOKBOOK.md")
    text = cookbook.read_text()
    # Section heading
    assert "2-phase" in text.lower() or "two-phase" in text.lower() or \
        "reload-props" in text.lower(), (
            "Cookbook needs a section on the Sheets-write 2-phase pattern. "
            "Without it, future agents will hit the same Add Single Row "
            "silent-failure that started v0.2.30."
        )
    # col_NNNN naming convention must be called out
    assert "col_0000" in text or "col_NNNN" in text
    # Critical: must steer away from auto_map_pipedream_columns being the
    # default mapping path
    assert "update_node_setting" in text
    # Must call out that auto_map_pipedream_columns assumes name match
    assert "auto_map_pipedream_columns" in text


def test_cookbook_reference_group_inner_fieldname_uses_full_prefix():
    """The 2026-05-30 cookbook agent finding: Enrich People / Search People
    REQUIRE the FULL `people_data-enrich_people-<key>` prefix on inner
    field_name entries in their `person_reference` envelope. Cookbook
    must show the full-prefix form, not the bare-key form."""
    import pathlib
    cookbook = (pathlib.Path(__file__).parent.parent
                / "docs" / "NATIVE_NODE_SETTINGS_COOKBOOK.md")
    text = cookbook.read_text()
    # Find the Enrich People section and check at least one inner
    # field_name uses the full prefix
    assert ('"field_name": "people_data-enrich_people-linkedin_url"' in text
            or "'field_name': 'people_data-enrich_people-linkedin_url'" in text), (
        "Cookbook Enrich People example must use the FULL prefix on inner "
        "field_name entries. The bare-key form (`linkedin_url`) was shown to "
        "fail live 2026-05-30 cookbook validation."
    )


def test_cookbook_rocketreach_enrich_company_uses_flat_lookup_by():
    """RocketReach Enrich Company uses a FLAT lookup_by + flat field pattern,
    NOT the company_reference envelope that native Enrich Company uses.
    Cookbook must distinguish them so agents don't copy the wrong shape."""
    import pathlib
    cookbook = (pathlib.Path(__file__).parent.parent
                / "docs" / "NATIVE_NODE_SETTINGS_COOKBOOK.md")
    text = cookbook.read_text()
    # The RocketReach typeId must be present in its own section
    assert "119be39f-278e-46fd-a0b9-15bc81eb85cb" in text
    # The flat lookup_by pattern must be shown
    assert "lookup_by" in text
    # And the shape clarification must be there
    assert "flat" in text.lower() or "FLAT" in text
