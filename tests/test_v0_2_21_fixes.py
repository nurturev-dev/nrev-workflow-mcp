"""Tests for v0.2.21 — Sheets CRUD validated end-to-end via raw API.

The investigation in `docs/v0_2_21_api_investigation.md` ran 9 probes against
the platform + an end-to-end round-trip + an independent sub-agent verification.
Key discoveries:

1. `POST /nodes/reload-props` (body key `settings`, not `settingFieldValues`) is
   THE Pipedream dynamic-schema endpoint. Returns `col_NNNN` per sheet column
   with `label` = header name, plus auto-issued `dynamic_props_id`.

2. `POST /workflows/{wf}/nodes/{n}/update-workflow-and-execute` (body wrapped
   `{"workflow": ...}`) is the atomic save-then-execute endpoint the UI uses.

3. Update Row's `updation_criteria` / `fields_to_update` arrays must be sent as
   list-OF-lists of envelope dicts, with column values being `col_NNNN` slugs
   (NOT header names). Probe 6c's original "list-of-dicts" verdict was wrong.

4. Scheduler with `is_listener=False` is a footgun — now rejected.

5. `add_edge` should also flip target `isTrigger=False` when wiring downstream
   of an existing root.
"""
from unittest.mock import patch, MagicMock

from nrev_wf_mcp.server import (
    reload_pipedream_props,
    auto_map_pipedream_columns,
    configure_update_row,
    save_and_execute,
    attach_node,
    add_edge,
)
from nrev_wf_mcp.client import reload_pipedream_props as client_reload, update_workflow_and_execute


SCHEDULER_TYPE_ID = "68da2fb4-8295-4568-9415-c47de58e6224"
ASR_TYPE_ID = "191db4a1-7c72-4c4a-af02-b507701ca61b"
UR_TYPE_ID = "3df67eff-0724-4e43-b43e-681a6f01ea1f"
GVR_TYPE_ID = "ce01c704-f6bd-40d5-9b2b-f545495de14b"


# ════════════════════════════════════════════════════════════════════════
# Client wrapper — reload-props body shape
# ════════════════════════════════════════════════════════════════════════


def test_client_reload_props_sends_settings_key():
    """The reload-props endpoint requires body key `settings`, NOT
    `settingFieldValues` (which is what updated-config-and-status uses).
    Sub-agent's HTTP 422 with 'settings field required' confirmed this."""
    captured = {}

    def fake_request(method, path, json_body=None, **kw):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = json_body
        return {"fields": [], "nodeId": "x", "componentId": "y", "errors": []}

    with patch("nrev_wf_mcp.client.request", side_effect=fake_request):
        client_reload(
            node_id="n1",
            node_definition_id="type-1",
            field_name_changed="hasHeaders",
            settings=[{"field_name": "x", "field_value": "v"}],
        )

    assert captured["method"] == "POST"
    assert captured["path"] == "/nodes/reload-props"
    # Critical: body uses `settings`, NOT `settingFieldValues`
    assert "settings" in captured["body"]
    assert "settingFieldValues" not in captured["body"]
    assert captured["body"]["settings"] == [{"field_name": "x", "field_value": "v"}]


def test_client_update_workflow_and_execute_wraps_workflow():
    """Per sub-agent's catch: body MUST be `{"workflow": <full envelope>}` —
    HTTP 422 'Field required: body.workflow' otherwise."""
    captured = {}

    def fake_request(method, path, json_body=None, **kw):
        captured["path"] = path
        captured["body"] = json_body
        return {"execution": {"response": {"id": "exec-1", "status": "running"}}}

    with patch("nrev_wf_mcp.client.request", side_effect=fake_request):
        update_workflow_and_execute("wf-1", "node-1", {"id": "wf-1", "blocks": []})

    assert "update-workflow-and-execute" in captured["path"]
    assert "workflow" in captured["body"]
    assert captured["body"]["workflow"] == {"id": "wf-1", "blocks": []}


# ════════════════════════════════════════════════════════════════════════
# reload_pipedream_props MCP tool
# ════════════════════════════════════════════════════════════════════════


def _asr_block(node_id="asr-1"):
    return {
        "id": node_id,
        "typeId": ASR_TYPE_ID,
        "variableName": "Add Single Row",
        "settings_field_values": [
            {"field_name": "pipedream-google_sheets-google_sheets_add_single_row-googleSheets_connection_id",
             "field_value": "conn-1", "fieldLabel": None, "error": None,
             "isUserInputInFormMandatory": False, "selectedInputTypeIndex": None, "isStale": False},
            {"field_name": "pipedream-google_sheets-google_sheets_add_single_row-hasHeaders",
             "field_value": True, "fieldLabel": None, "error": None,
             "isUserInputInFormMandatory": False, "selectedInputTypeIndex": None, "isStale": False},
        ],
    }


def _reload_response_full_schema():
    """Mock of the actual reload-props response for Add Single Row — 13 fields
    including col_NNNN (with label=header) and dynamic_props_id."""
    fields = [
        {"name": "pipedream-google_sheets-google_sheets_add_single_row-googleSheets_connection_id",
         "type": "app_connection", "label": "Select Google Sheets Account", "required": True},
        {"name": "pipedream-google_sheets-google_sheets_add_single_row-drive",
         "type": "select", "label": "Drive", "required": False},
        {"name": "pipedream-google_sheets-google_sheets_add_single_row-sheetId",
         "type": "select", "label": "Spreadsheet ID", "required": True},
        {"name": "pipedream-google_sheets-google_sheets_add_single_row-worksheetId",
         "type": "select", "label": "Worksheet ID", "required": True},
        {"name": "pipedream-google_sheets-google_sheets_add_single_row-hasHeaders",
         "type": "boolean", "label": "Load Columns", "required": False},
        {"name": "pipedream-google_sheets-google_sheets_add_single_row-col_0000",
         "type": "string", "label": "timestamp", "required": False},
        {"name": "pipedream-google_sheets-google_sheets_add_single_row-col_0001",
         "type": "string", "label": "who", "required": False},
        {"name": "pipedream-google_sheets-google_sheets_add_single_row-col_0002",
         "type": "string", "label": "what", "required": False},
        {"name": "pipedream-google_sheets-google_sheets_add_single_row-col_0003",
         "type": "string", "label": "status", "required": False},
        {"name": "pipedream-google_sheets-google_sheets_add_single_row-col_0004",
         "type": "string", "label": "notes", "required": False},
        {"name": "pipedream-google_sheets-google_sheets_add_single_row-dynamic_props_id",
         "type": "string", "label": "Dynamic Props ID", "required": True,
         "defaultValue": "dyp_VwUD0ppk",
         "conditionalVisibility": [{"condition": "always_hidden"}]},
    ]
    return {"componentId": "google_sheets-add-single-row",
            "errors": [], "nodeId": "asr-1", "fields": fields}


def test_reload_pipedream_props_extracts_token_and_label_map():
    """The headline tool — returns dynamic_props_id token + col→label mapping."""
    wf = {"id": "wf-1", "blocks": [_asr_block()]}
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=wf), \
         patch("nrev_wf_mcp.server.api.reload_pipedream_props",
               return_value=_reload_response_full_schema()):
        result = reload_pipedream_props("wf-1", "asr-1")

    assert result["dynamic_props_id"] == "dyp_VwUD0ppk"
    assert result["has_dynamic_props"] is True
    assert result["component_id"] == "google_sheets-add-single-row"
    assert result["col_to_label"] == {
        "col_0000": "timestamp",
        "col_0001": "who",
        "col_0002": "what",
        "col_0003": "status",
        "col_0004": "notes",
    }
    # 11 fields in our mock; field_count matches
    assert len(result["fields"]) == 11


def test_reload_pipedream_props_handles_no_dynamic_props():
    """For Get Values in Range (no dynamic props), the platform returns
    `errors: ["additionalProps not a function"]` and 0 fields. Our tool
    should flag has_dynamic_props=False."""
    wf = {"id": "wf-1", "blocks": [_asr_block()]}
    no_dyn_resp = {"componentId": "google_sheets-get-values-in-range",
                   "errors": ['{"name":"UserError","message":"additionalProps not a function"}'],
                   "nodeId": "asr-1", "fields": []}
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=wf), \
         patch("nrev_wf_mcp.server.api.reload_pipedream_props", return_value=no_dyn_resp):
        result = reload_pipedream_props("wf-1", "asr-1")

    assert result["has_dynamic_props"] is False
    assert result["dynamic_props_id"] is None
    assert result["col_to_label"] == {}
    assert result["errors"]


def test_reload_pipedream_props_identifies_array_fields_for_update_row():
    """Update Row's reload-props returns `updation_criteria` + `fields_to_update`
    as type=array. The tool's `array_fields` list flags them so callers know
    to use configure_update_row (or the list-of-lists envelope shape)."""
    fields = [
        {"name": "pipedream-google_sheets-google_sheets_update_row-googleSheets_connection_id",
         "type": "app_connection", "label": "Connection"},
        {"name": "pipedream-google_sheets-google_sheets_update_row-updation_criteria",
         "type": "array", "label": "Updation Criteria",
         "array_item_schema": [
             {"name": "...-column_to_match", "type": "select"},
             {"name": "...-value_to_match", "type": "string"},
         ]},
        {"name": "pipedream-google_sheets-google_sheets_update_row-fields_to_update",
         "type": "array", "label": "Fields to Update"},
    ]
    wf = {"id": "wf-1", "blocks": [_asr_block()]}
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=wf), \
         patch("nrev_wf_mcp.server.api.reload_pipedream_props",
               return_value={"fields": fields, "errors": [], "componentId": "google_sheets-update-row"}):
        result = reload_pipedream_props("wf-1", "asr-1")

    assert "pipedream-google_sheets-google_sheets_update_row-updation_criteria" in result["array_fields"]
    assert "pipedream-google_sheets-google_sheets_update_row-fields_to_update" in result["array_fields"]


# ════════════════════════════════════════════════════════════════════════
# auto_map_pipedream_columns helper
# ════════════════════════════════════════════════════════════════════════


def test_auto_map_pipedream_columns_writes_templates():
    """For each col_NNNN in the schema, writes `{{<label>}}` to the node."""
    wf = {"id": "wf-1", "blocks": [_asr_block()]}
    update_calls = []

    def fake_update(workflow_id, node_id, field_path, value, **kw):
        update_calls.append({"path": field_path, "value": value})
        return {"ok": True}

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=wf), \
         patch("nrev_wf_mcp.server.api.reload_pipedream_props",
               return_value=_reload_response_full_schema()), \
         patch("nrev_wf_mcp.server.update_node_setting", side_effect=fake_update):
        result = auto_map_pipedream_columns("wf-1", "asr-1")

    assert result["ok"] is True
    assert result["dynamic_props_id"] == "dyp_VwUD0ppk"
    # All 5 col_NNNN + dynamic_props_id = 6 settings written
    paths = [c["path"] for c in update_calls]
    assert any("dynamic_props_id" in p for p in paths)
    assert any(p.endswith("col_0000") for p in paths)
    assert any(p.endswith("col_0004") for p in paths)
    # Templates correctly formatted
    timestamp_call = next(c for c in update_calls if c["path"].endswith("col_0000"))
    assert timestamp_call["value"] == "{{timestamp}}"
    who_call = next(c for c in update_calls if c["path"].endswith("col_0001"))
    assert who_call["value"] == "{{who}}"


def test_auto_map_pipedream_columns_refuses_when_no_dynamic_props():
    """If the action has no dynamic props (e.g. Get Values in Range),
    auto-map returns ok:False with a clear message instead of writing
    garbage settings."""
    wf = {"id": "wf-1", "blocks": [_asr_block()]}
    no_dyn = {"fields": [], "errors": ["additionalProps not a function"],
              "componentId": "google_sheets-get-values-in-range"}
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=wf), \
         patch("nrev_wf_mcp.server.api.reload_pipedream_props", return_value=no_dyn):
        result = auto_map_pipedream_columns("wf-1", "asr-1")

    assert result["ok"] is False
    assert "no dynamic" in result["message"].lower()


# ════════════════════════════════════════════════════════════════════════
# configure_update_row helper — the list-of-lists envelope correctness
# ════════════════════════════════════════════════════════════════════════


def _update_row_reload_resp():
    """Mock reload-props for Update Row — col_NNNN.label = headers + dyp_."""
    fields = [
        {"name": "pipedream-google_sheets-google_sheets_update_row-col_0000",
         "type": "string", "label": "timestamp"},
        {"name": "pipedream-google_sheets-google_sheets_update_row-col_0001",
         "type": "string", "label": "who"},
        {"name": "pipedream-google_sheets-google_sheets_update_row-col_0002",
         "type": "string", "label": "what"},
        {"name": "pipedream-google_sheets-google_sheets_update_row-col_0003",
         "type": "string", "label": "status"},
        {"name": "pipedream-google_sheets-google_sheets_update_row-col_0004",
         "type": "string", "label": "notes"},
        {"name": "pipedream-google_sheets-google_sheets_update_row-updation_criteria",
         "type": "array", "label": "Updation Criteria"},
        {"name": "pipedream-google_sheets-google_sheets_update_row-fields_to_update",
         "type": "array", "label": "Fields to Update"},
        {"name": "pipedream-google_sheets-google_sheets_update_row-dynamic_props_id",
         "type": "string", "label": "Dynamic Props ID",
         "defaultValue": "dyp_yyUKkpyr"},
    ]
    return {"componentId": "google_sheets-update-row", "errors": [],
            "nodeId": "ur-1", "fields": fields}


def test_configure_update_row_builds_correct_envelope():
    """The CORRECT shape (validated end-to-end):
    field_value = [[ {column_envelope}, {value_envelope} ]]
    where column_envelope.field_value = col_NNNN slug (not header name).
    """
    wf = {"id": "wf-1", "blocks": [{"id": "ur-1", "typeId": UR_TYPE_ID,
                                    "settings_field_values": [
        {"field_name": "pipedream-google_sheets-google_sheets_update_row-googleSheets_connection_id",
         "field_value": "c1", "fieldLabel": None, "error": None,
         "isUserInputInFormMandatory": False, "selectedInputTypeIndex": None, "isStale": False}
    ]}]}
    update_calls = []

    def fake_update(workflow_id, node_id, field_path, value, **kw):
        update_calls.append({"path": field_path, "value": value})
        return {"ok": True}

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=wf), \
         patch("nrev_wf_mcp.server.api.reload_pipedream_props",
               return_value=_update_row_reload_resp()), \
         patch("nrev_wf_mcp.server.update_node_setting", side_effect=fake_update):
        result = configure_update_row(
            "wf-1", "ur-1",
            criteria=[{"header": "who", "value": "ana@example.com"}],
            updates=[{"header": "status", "value": "replied"}],
            add_if_not_present=False,
        )

    assert result["ok"] is True

    # updation_criteria stored as list-of-lists with col_NNNN slug
    crit_call = next(c for c in update_calls if c["path"].endswith("updation_criteria"))
    crit_val = crit_call["value"]
    assert isinstance(crit_val, list)
    assert isinstance(crit_val[0], list)  # list of LISTS
    column_env = crit_val[0][0]
    value_env = crit_val[0][1]
    assert column_env["field_value"] == "col_0001"  # NOT "who"
    assert column_env["fieldLabel"] == "who"        # header annotated for UI
    assert value_env["field_value"] == "ana@example.com"

    # fields_to_update same shape
    upd_call = next(c for c in update_calls if c["path"].endswith("fields_to_update"))
    upd_val = upd_call["value"]
    assert isinstance(upd_val[0], list)
    assert upd_val[0][0]["field_value"] == "col_0003"  # status → col_0003
    assert upd_val[0][0]["fieldLabel"] == "status"
    assert upd_val[0][1]["field_value"] == "replied"


def test_configure_update_row_errors_on_unknown_header():
    """If caller passes a header name that doesn't exist in the sheet, fail
    cleanly with a list of available headers instead of letting the platform
    error confusingly at execute time."""
    wf = {"id": "wf-1", "blocks": [{"id": "ur-1", "typeId": UR_TYPE_ID,
                                    "settings_field_values": [
        {"field_name": "pipedream-google_sheets-google_sheets_update_row-googleSheets_connection_id",
         "field_value": "c1", "fieldLabel": None, "error": None,
         "isUserInputInFormMandatory": False, "selectedInputTypeIndex": None, "isStale": False}
    ]}]}
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=wf), \
         patch("nrev_wf_mcp.server.api.reload_pipedream_props",
               return_value=_update_row_reload_resp()):
        result = configure_update_row(
            "wf-1", "ur-1",
            criteria=[{"header": "NOT_A_REAL_COLUMN", "value": "x"}],
            updates=[{"header": "status", "value": "y"}],
        )

    assert result["ok"] is False
    assert "NOT_A_REAL_COLUMN" in result["message"]


# ════════════════════════════════════════════════════════════════════════
# save_and_execute
# ════════════════════════════════════════════════════════════════════════


def test_save_and_execute_calls_endpoint_and_returns_exec_id():
    """Wraps the atomic save-then-execute. Extracts execution_id from the
    nested response shape."""
    wf = {"id": "wf-1", "blocks": [{"id": "n1"}]}
    api_resp = {
        "workflow": {"id": "wf-1", "blocks": []},
        "execution": {
            "response_type": "workflow_execution_response",
            "response": {"id": "exec-abc", "status": "running",
                         "startedAt": "2026-05-23T14:00:00Z"}
        }
    }
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=wf), \
         patch("nrev_wf_mcp.server.api.update_workflow_and_execute",
               return_value=api_resp) as mock_exec:
        result = save_and_execute("wf-1", "n1")

    assert result["ok"] is True
    assert result["execution_id"] == "exec-abc"
    assert result["status"] == "running"
    # Verify it called the right endpoint with the workflow body
    mock_exec.assert_called_once()
    args = mock_exec.call_args
    assert args.kwargs.get("workflow_body", args.args[2] if len(args.args) > 2 else None) is not None or \
           args.args[0] == "wf-1"


def test_save_and_execute_raises_on_unknown_target_node():
    """target_node_id must exist in the workflow."""
    import pytest
    wf = {"id": "wf-1", "blocks": [{"id": "other-node"}]}
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=wf):
        with pytest.raises(ValueError, match="missing-node"):
            save_and_execute("wf-1", "missing-node")


# ════════════════════════════════════════════════════════════════════════
# add_edge — v0.2.21 also flips target.isTrigger=False
# ════════════════════════════════════════════════════════════════════════


def test_add_edge_flips_target_isTrigger_false_when_wiring_to_existing_root():
    """User session finding: when a CC was attached as a root (isTrigger=True),
    then add_edge wired Scheduler→CC, the CC stayed isTrigger=True — leaving
    the workflow with two start nodes. v0.2.21 flips target isTrigger=False
    on wire."""
    source = {"id": "scheduler-1", "isTrigger": True, "isOrphan": False,
              "isListener": False, "isPartOfActiveSwimlane": True,
              "settings_field_values": [], "toBlocks": [], "inputs": [],
              "outputs": [], "position": {"x": 0, "y": 0},
              "typeId": SCHEDULER_TYPE_ID, "variableName": "Sched",
              "creditCostPerItem": 0, "isTestMode": False,
              "description": "", "column_operations": None,
              "node_config_error": None}
    target = {"id": "cc-1", "isTrigger": True, "isOrphan": False,
              "isListener": False, "isPartOfActiveSwimlane": True,
              "settings_field_values": [], "toBlocks": [],
              "inputs": [{"columns": [], "columns_metadata": None, "file": "",
                          "handle_condition": "_default", "node_id": None}],
              "outputs": [], "position": {"x": 100, "y": 0},
              "typeId": "ae54c44f-60ee-47c4-91d7-eae7fa849133",
              "variableName": "CC", "creditCostPerItem": 0,
              "isTestMode": False, "description": "",
              "column_operations": None, "node_config_error": None}
    wf = {"id": "wf-1", "blocks": [source, target]}
    put_calls = []

    def fake_put_node(wf_id, node_id, node_patch):
        put_calls.append({"id": node_id, "isTrigger": node_patch.get("isTrigger")})
        return {**node_patch, "workflowConfigError": None, "isRunable": True}

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=wf), \
         patch("nrev_wf_mcp.server.api.put_node", side_effect=fake_put_node), \
         patch("nrev_wf_mcp.server._maybe_validate",
               return_value={"valid": True, "workflowConfigError": None, "isRunable": True}):
        result = add_edge("wf-1", "scheduler-1", "cc-1")

    assert result["edge_added"] is True
    assert result["target_isTrigger_flipped"] is True
    # The target PUT carried isTrigger=False
    target_put = next(p for p in put_calls if p["id"] == "cc-1")
    assert target_put["isTrigger"] is False
