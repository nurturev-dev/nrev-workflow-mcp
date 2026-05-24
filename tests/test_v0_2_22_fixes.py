"""Tests for v0.2.22 — start-node-vs-trigger guards + prepend_trigger.

Background from the v0.2.22 prep:
- Live probe (Gmail Find Email) confirmed that Pipedream "trigger-flavored"
  start-only nodes DO run when wired downstream — they just ignore upstream
  input and use their own configured settings. So `prepend_trigger` can be
  a simple attach + add_edge wrapper.
- The MCP needs to guard against two footguns: attaching action-only nodes
  as roots (Custom Code → "No input data provided" runtime fail) and
  attaching a 2nd listener-capable node as root (platform allows only one
  listener per workflow).
"""
from unittest.mock import patch, MagicMock
import pytest

from nrev_wf_mcp.server import (
    attach_node, prepend_trigger, list_node_definitions,
    _lookup_node_def_flags,
)
from nrev_wf_mcp.client import list_node_definitions as client_list_node_defs


SCHEDULER_TYPE_ID = "68da2fb4-8295-4568-9415-c47de58e6224"
GVR_TYPE_ID = "ce01c704-f6bd-40d5-9b2b-f545495de14b"
CUSTOM_CODE_TYPE_ID = "ae54c44f-60ee-47c4-91d7-eae7fa849133"


def _mock_catalog_lookup(type_id, is_trigger, is_listener):
    """Helper: return a function that mocks _lookup_node_def_flags."""
    def _mock(t):
        if t == type_id:
            return (is_trigger, is_listener)
        return (None, None)
    return _mock


# ════════════════════════════════════════════════════════════════════════
# Fix #1 — refuse non-trigger-capable as root
# ════════════════════════════════════════════════════════════════════════


def test_attach_node_refuses_custom_code_as_root():
    """Custom Code has is_trigger=False in catalog. Attaching as root must
    raise with a clear error pointing at real data sources."""
    _lookup_node_def_flags.cache_clear()
    fake_wf = {"id": "wf-1", "blocks": []}
    fake_catalog = [{"node_definition_id": CUSTOM_CODE_TYPE_ID,
                     "is_trigger": False, "isListener": False}]
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.list_node_definitions",
               return_value={"data": fake_catalog}):
        with pytest.raises(ValueError) as exc:
            attach_node(
                workflow_id="wf-1",
                parent_node_ids=[],
                type_id=CUSTOM_CODE_TYPE_ID,
                name="Bad CC root",
                settings={"data_manipulation-custom_code-code": "def run(df): return df"},
                auto_resolve_labels=False,
            )
    msg = str(exc.value)
    assert "is_trigger=False" in msg
    assert "real data source" in msg.lower()
    assert "force_root=True" in msg


def test_attach_node_allows_custom_code_as_root_with_force_root():
    """force_root=True escape hatch lets caller bypass the guard."""
    _lookup_node_def_flags.cache_clear()
    fake_wf = {"id": "wf-1", "blocks": []}
    fake_catalog = [{"node_definition_id": CUSTOM_CODE_TYPE_ID,
                     "is_trigger": False, "isListener": False}]
    fake_paste_resp = {"workflowConfigError": None, "isRunable": True,
                       "blocks": [{"id": "new-1", "typeId": CUSTOM_CODE_TYPE_ID}]}
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.list_node_definitions",
               return_value={"data": fake_catalog}), \
         patch("nrev_wf_mcp.server._attach_block_via_paste_and_wire",
               return_value=(fake_paste_resp, "new-1")), \
         patch("nrev_wf_mcp.server._new_block_error_from_paste", return_value=None), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value={"valid": True}):
        result = attach_node(
            workflow_id="wf-1",
            parent_node_ids=[],
            type_id=CUSTOM_CODE_TYPE_ID,
            name="Forced CC root",
            settings={"data_manipulation-custom_code-code": "x"},
            force_root=True,
            auto_resolve_labels=False,
        )
    # Got past the guard — would have raised before reaching the wire helper
    assert result.get("ok") is True
    assert result.get("node_id") == "new-1"


def test_attach_node_allows_trigger_capable_as_root():
    """Sanity: nodes with is_trigger=True in catalog (Scheduler, GVR) still
    attach cleanly as roots."""
    _lookup_node_def_flags.cache_clear()
    fake_wf = {"id": "wf-1", "blocks": []}
    fake_catalog = [{"node_definition_id": GVR_TYPE_ID,
                     "is_trigger": True, "isListener": False}]
    fake_paste_resp = {"workflowConfigError": None, "isRunable": True,
                       "blocks": [{"id": "new-1", "typeId": GVR_TYPE_ID}]}
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.list_node_definitions",
               return_value={"data": fake_catalog}), \
         patch("nrev_wf_mcp.server._attach_block_via_paste_and_wire",
               return_value=(fake_paste_resp, "new-1")), \
         patch("nrev_wf_mcp.server._new_block_error_from_paste", return_value=None), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value={"valid": True}):
        result = attach_node(
            workflow_id="wf-1",
            parent_node_ids=[],
            type_id=GVR_TYPE_ID,
            name="GVR root",
            settings={"x": "y"},
            auto_resolve_labels=False,
        )
    assert result.get("ok") is True


def test_attach_node_allows_non_trigger_with_parent():
    """A non-trigger-capable node IS allowed when it has a parent."""
    _lookup_node_def_flags.cache_clear()
    parent_block = {"id": "p1", "typeId": GVR_TYPE_ID, "position": {"x": 0, "y": 0},
                    "settings_field_values": [], "toBlocks": [], "outputs": []}
    fake_wf = {"id": "wf-1", "blocks": [parent_block]}
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.paste_nodes",
               return_value={"workflowConfigError": None, "isRunable": True,
                             "blocks": [parent_block, {"id": "new-1", "typeId": CUSTOM_CODE_TYPE_ID}]}), \
         patch("nrev_wf_mcp.server.api.put_node", return_value={}), \
         patch("nrev_wf_mcp.server._new_block_error_from_paste", return_value=None), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value={"valid": True}):
        result = attach_node(
            workflow_id="wf-1",
            parent_node_ids=["p1"],
            type_id=CUSTOM_CODE_TYPE_ID,
            name="CC with parent",
            settings={"x": "y"},
            auto_resolve_labels=False,
        )
    # Didn't refuse — CC with parent is fine
    assert "is_trigger=False" not in str(result.get("message", ""))


# ════════════════════════════════════════════════════════════════════════
# Fix #2 — one-listener-per-workflow guard
# ════════════════════════════════════════════════════════════════════════


def test_attach_node_refuses_second_listener_as_root():
    """Workflow already has a Scheduler (isListener=True). Attaching another
    listener-capable node as root should raise unless force_demote_listener=True."""
    _lookup_node_def_flags.cache_clear()
    existing_listener = {
        "id": "existing-sched", "typeId": SCHEDULER_TYPE_ID,
        "variableName": "First Scheduler", "isListener": True,
        "settings_field_values": [], "outputs": [], "toBlocks": [],
    }
    fake_wf = {"id": "wf-1", "blocks": [existing_listener]}
    fake_catalog = [{"node_definition_id": SCHEDULER_TYPE_ID,
                     "is_trigger": True, "isListener": True}]
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.list_node_definitions",
               return_value={"data": fake_catalog}):
        with pytest.raises(ValueError) as exc:
            attach_node(
                workflow_id="wf-1",
                parent_node_ids=[],
                type_id=SCHEDULER_TYPE_ID,
                name="Second Scheduler",
                settings={"automation-scheduler-interval": "Days"},
                auto_resolve_labels=False,
            )
    msg = str(exc.value)
    assert "already has a listener" in msg
    assert "First Scheduler" in msg
    assert "force_demote_listener" in msg


def test_attach_node_force_demote_listener_auto_flips_is_listener_false():
    """With force_demote_listener=True, the new block attaches with
    isListener=False (demoted) instead of raising. Scheduler-specific
    is_listener=False guard is skipped in this case."""
    _lookup_node_def_flags.cache_clear()
    existing_listener = {
        "id": "existing-sched", "typeId": SCHEDULER_TYPE_ID,
        "variableName": "First Scheduler", "isListener": True,
        "settings_field_values": [], "outputs": [], "toBlocks": [],
    }
    fake_wf = {"id": "wf-1", "blocks": [existing_listener]}
    fake_catalog = [{"node_definition_id": SCHEDULER_TYPE_ID,
                     "is_trigger": True, "isListener": True}]
    captured_block = {}

    def fake_wire(*, workflow_id, new_block, parent_edges, fallback_parents, existing_block_ids):
        captured_block["block"] = new_block
        return ({"workflowConfigError": None, "isRunable": True,
                 "blocks": [existing_listener, new_block]}, new_block["id"])

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.list_node_definitions",
               return_value={"data": fake_catalog}), \
         patch("nrev_wf_mcp.server._attach_block_via_paste_and_wire",
               side_effect=fake_wire), \
         patch("nrev_wf_mcp.server._new_block_error_from_paste", return_value=None), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value={"valid": True}):
        result = attach_node(
            workflow_id="wf-1",
            parent_node_ids=[],
            type_id=SCHEDULER_TYPE_ID,
            name="Second Scheduler",
            settings={"automation-scheduler-interval": "Days"},
            force_demote_listener=True,
            auto_resolve_labels=False,
        )

    # The new block was attached with isListener=False
    assert captured_block["block"]["isListener"] is False
    assert captured_block["block"]["isTrigger"] is True  # still a start node
    # Response flag surfaces the demotion
    assert result.get("demoted_from_listener") is True
    assert result.get("is_listener") is False


def test_attach_node_explicit_is_listener_false_bypasses_guard():
    """If caller explicitly passes is_listener=False, the second-listener
    check shouldn't fire (caller already knows what they're doing)."""
    _lookup_node_def_flags.cache_clear()
    existing_listener = {
        "id": "existing-sched", "typeId": SCHEDULER_TYPE_ID,
        "variableName": "First Scheduler", "isListener": True,
        "settings_field_values": [], "outputs": [], "toBlocks": [],
    }
    fake_wf = {"id": "wf-1", "blocks": [existing_listener]}
    fake_catalog = [{"node_definition_id": GVR_TYPE_ID,
                     "is_trigger": True, "isListener": True}]
    captured_block = {}

    def fake_paste(wf_id, payload):
        captured_block["block"] = payload["nodes"][0]
        return {"workflowConfigError": None, "isRunable": True,
                "blocks": [existing_listener, payload["nodes"][0]]}

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.api.list_node_definitions",
               return_value={"data": fake_catalog}), \
         patch("nrev_wf_mcp.server.api.paste_nodes", side_effect=fake_paste), \
         patch("nrev_wf_mcp.server._new_block_error_from_paste", return_value=None), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value={"valid": True}):
        result = attach_node(
            workflow_id="wf-1",
            parent_node_ids=[],
            type_id=GVR_TYPE_ID,
            name="GVR one-shot",
            settings={"x": "y"},
            is_listener=False,
            auto_resolve_labels=False,
        )

    assert captured_block["block"]["isListener"] is False
    # demoted_from_listener should be False — caller explicitly set, not auto-demoted
    assert result.get("demoted_from_listener") is False


# ════════════════════════════════════════════════════════════════════════
# Fix #3 — prepend_trigger
# ════════════════════════════════════════════════════════════════════════


def test_prepend_trigger_attaches_scheduler_and_wires_edge():
    """End-to-end: prepend Scheduler to an existing root, verify it attached
    + edge was added + downstream's isTrigger flipped."""
    _lookup_node_def_flags.cache_clear()
    existing_root = {
        "id": "existing-gvr", "typeId": GVR_TYPE_ID, "variableName": "Existing GVR",
        "isTrigger": True, "isListener": False, "isOrphan": False,
        "settings_field_values": [], "outputs": [], "toBlocks": [],
        "inputs": [], "position": {"x": 100, "y": 0},
    }
    fake_wf = {"id": "wf-1", "blocks": [existing_root]}

    # Mock attach_node + add_edge — verify both got called
    attach_calls = []
    edge_calls = []

    def fake_attach(*args, **kw):
        attach_calls.append(kw)
        return {"ok": True, "node_id": "trigger-new",
                "is_listener": True, "demoted_from_listener": False}

    def fake_add_edge(*args, **kw):
        edge_calls.append(kw)
        return {"ok": True, "edge_added": True,
                "target_isTrigger_flipped": True,
                "target_isOrphan_refreshed": True,
                "isRunable": True,
                "validation": {"valid": True}}

    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf), \
         patch("nrev_wf_mcp.server.attach_node", side_effect=fake_attach), \
         patch("nrev_wf_mcp.server.add_edge", side_effect=fake_add_edge):
        result = prepend_trigger(
            workflow_id="wf-1",
            existing_root_id="existing-gvr",
            trigger_type_id=SCHEDULER_TYPE_ID,
            trigger_settings={"automation-scheduler-interval": "Days"},
            trigger_name="My Scheduler",
        )

    assert result["ok"] is True
    assert result["trigger_node_id"] == "trigger-new"
    assert result["target_isTrigger_flipped"] is True
    assert result["target_isOrphan_refreshed"] is True
    assert result["workflow_is_runable"] is True
    # Both helpers called
    assert len(attach_calls) == 1
    assert len(edge_calls) == 1
    # attach called with parent_node_ids=[] (root)
    assert attach_calls[0]["parent_node_ids"] == []
    assert attach_calls[0]["type_id"] == SCHEDULER_TYPE_ID
    # attach uses force_demote_listener so it doesn't fail if existing has listener
    assert attach_calls[0]["force_demote_listener"] is True
    # edge wires trigger → existing root
    assert edge_calls[0]["source_node_id"] == "trigger-new"
    assert edge_calls[0]["target_node_id"] == "existing-gvr"
    # Note explains the gotcha
    assert "OWN configured settings" in result["note"]


def test_prepend_trigger_refuses_if_target_not_a_root():
    """If existing_root_id already has incoming edges, prepend is wrong move."""
    not_a_root = {
        "id": "downstream-node", "typeId": GVR_TYPE_ID, "isTrigger": False,
        "settings_field_values": [], "outputs": [], "toBlocks": [],
    }
    has_parent = {
        "id": "actual-root", "typeId": GVR_TYPE_ID, "isTrigger": True,
        "toBlocks": [{"toBlockId": "downstream-node", "edgeId": "x"}],
        "settings_field_values": [], "outputs": [],
    }
    fake_wf = {"id": "wf-1", "blocks": [has_parent, not_a_root]}
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf):
        result = prepend_trigger(
            workflow_id="wf-1",
            existing_root_id="downstream-node",
            trigger_type_id=SCHEDULER_TYPE_ID,
            trigger_settings={"automation-scheduler-interval": "Days"},
        )
    assert result["ok"] is False
    assert "already has incoming edges" in result["message"]


def test_prepend_trigger_raises_when_target_not_in_workflow():
    fake_wf = {"id": "wf-1", "blocks": []}
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value=fake_wf):
        with pytest.raises(ValueError, match="missing-id"):
            prepend_trigger(
                workflow_id="wf-1",
                existing_root_id="missing-id",
                trigger_type_id=SCHEDULER_TYPE_ID,
                trigger_settings={},
            )


# ════════════════════════════════════════════════════════════════════════
# Fix #4 — list_node_definitions filter params
# ════════════════════════════════════════════════════════════════════════


def test_client_list_node_definitions_passes_only_trigger():
    """Client wrapper should pass `onlyTrigger=true` query param when caller
    sets only_trigger=True."""
    captured = {}

    def fake_request(method, path, params=None, **kw):
        captured["params"] = params
        return {"data": [], "meta": {}}

    with patch("nrev_wf_mcp.client.request", side_effect=fake_request):
        client_list_node_defs(only_trigger=True)
    assert captured["params"].get("onlyTrigger") == "true"


def test_client_list_node_definitions_passes_only_action():
    captured = {}

    def fake_request(method, path, params=None, **kw):
        captured["params"] = params
        return {"data": [], "meta": {}}

    with patch("nrev_wf_mcp.client.request", side_effect=fake_request):
        client_list_node_defs(only_action=True)
    assert captured["params"].get("onlyAction") == "true"


def test_server_list_node_definitions_filters_only_listener_client_side():
    """only_listener is a client-side filter (platform doesn't expose this).
    Test that we filter the raw response to is_listener=True entries."""
    raw_resp = {"data": [
        {"node_definition_id": "a", "name": "Scheduler",
         "is_trigger": True, "isListener": True},
        {"node_definition_id": "b", "name": "Get Values in Range",
         "is_trigger": True, "isListener": False},
        {"node_definition_id": "c", "name": "Gmail New Message",
         "is_trigger": True, "isListener": True},
    ], "meta": {"total_entries": 3}}
    with patch("nrev_wf_mcp.server.api.list_node_definitions", return_value=raw_resp):
        result = list_node_definitions(only_listener=True)
    names = [n["name"] for n in result["node_definitions"]]
    assert "Scheduler" in names
    assert "Gmail New Message" in names
    assert "Get Values in Range" not in names  # is_listener=False, filtered out
    assert result["only_listener"] is True


def test_server_list_node_definitions_passes_only_trigger_to_client():
    """The server tool should plumb only_trigger through to the client method
    which adds the onlyTrigger query param."""
    raw_resp = {"data": [], "meta": {}}
    captured = {}

    def fake_client_call(**kw):
        captured.update(kw)
        return raw_resp

    with patch("nrev_wf_mcp.server.api.list_node_definitions", side_effect=fake_client_call):
        list_node_definitions(only_trigger=True)
    assert captured.get("only_trigger") is True


# ════════════════════════════════════════════════════════════════════════
# Sticky note docstring guidance (Fix #5)
# ════════════════════════════════════════════════════════════════════════


def test_sticky_note_docstrings_contain_planning_aid_guidance():
    """Both add_sticky_note and update_sticky_note docstrings should include
    the 'planning aid, not decoration' philosophy."""
    from nrev_wf_mcp.server import add_sticky_note, update_sticky_note
    for fn in (add_sticky_note, update_sticky_note):
        doc = fn.__doc__ or ""
        assert "WHEN TO USE STICKY NOTES" in doc
        assert "comments in code" in doc.lower() or "comments" in doc.lower()
        assert "decoration" in doc.lower()


# ════════════════════════════════════════════════════════════════════════
# Fix #6 — docstring overhaul on add_edge / attach_node
# ════════════════════════════════════════════════════════════════════════


def test_add_edge_docstring_mentions_start_node_vs_trigger_and_prepend_trigger():
    """add_edge docstring should reference the start-node-vs-trigger semantics
    and point at prepend_trigger for the common conversion case."""
    from nrev_wf_mcp.server import add_edge as add_edge_fn
    doc = add_edge_fn.__doc__ or ""
    assert "START-NODE" in doc.upper() or "start node" in doc.lower()
    assert "prepend_trigger" in doc


def test_attach_node_docstring_mentions_v022_guards():
    """attach_node docstring should document force_root + force_demote_listener."""
    doc = attach_node.__doc__ or ""
    assert "force_root" in doc
    assert "force_demote_listener" in doc
    assert "is_trigger=False" in doc  # the catalog flag check explanation
