"""Tests for v0.2.7 trigger/listener auto-detection in attach_node.

The bug we're guarding against: pre-v0.2.7 attach_node defaulted isTrigger=False
and hardcoded isListener=False. Attaching a Scheduler with no parents produced
a block that the platform marked as a "trigger" if you remembered to pass
is_trigger=True — but isListener stayed False either way, leaving the workflow
unable to go live with the misleading "Add a Trigger Node" tooltip in the UI.

v0.2.7 fix: when either flag is left as the default (None), look up the
node-definition and apply the catalog's values automatically.
"""
from unittest.mock import patch

from nrev_wf_mcp.server import _lookup_node_def_flags


# ── _lookup_node_def_flags ────────────────────────────────────────────────


SCHEDULER_TYPE_ID = "68da2fb4-8295-4568-9415-c47de58e6224"
ADD_ROW_TYPE_ID = "191db4a1-7c72-4c4a-af02-b507701ca61b"


def _mock_catalog_page(items, has_more=False):
    """Helper to build a list_node_definitions-style response."""
    return {"data": items, "meta": {"total_entries": len(items) + (1 if has_more else 0)}}


def test_lookup_finds_trigger_listener_type():
    """Scheduler is both is_trigger=true AND isListener=true in the catalog."""
    _lookup_node_def_flags.cache_clear()
    with patch("nrev_wf_mcp.server.api.list_node_definitions") as mock_list:
        mock_list.return_value = _mock_catalog_page([
            {
                "node_definition_id": SCHEDULER_TYPE_ID,
                "name": "Scheduler Node",
                "is_trigger": True,
                "isListener": True,
            },
        ])
        assert _lookup_node_def_flags(SCHEDULER_TYPE_ID) == (True, True)


def test_lookup_finds_non_trigger_type():
    """Add Single Row is neither trigger nor listener."""
    _lookup_node_def_flags.cache_clear()
    with patch("nrev_wf_mcp.server.api.list_node_definitions") as mock_list:
        mock_list.return_value = _mock_catalog_page([
            {
                "node_definition_id": ADD_ROW_TYPE_ID,
                "name": "Add Single Row",
                "is_trigger": False,
                "isListener": False,
            },
        ])
        assert _lookup_node_def_flags(ADD_ROW_TYPE_ID) == (False, False)


def test_lookup_unknown_type_returns_none():
    """If the typeId isn't in the catalog, return (None, None) so the caller
    knows to fall back to defaults rather than incorrectly setting False."""
    _lookup_node_def_flags.cache_clear()
    with patch("nrev_wf_mcp.server.api.list_node_definitions") as mock_list:
        # Catalog returns one page of unrelated entries
        mock_list.return_value = _mock_catalog_page([
            {"node_definition_id": "some-other-id", "is_trigger": False, "isListener": False},
        ])
        assert _lookup_node_def_flags("unknown-typeid-not-in-catalog") == (None, None)


def test_lookup_silent_on_api_error():
    """A catalog API failure must not crash attach_node — return (None, None)."""
    _lookup_node_def_flags.cache_clear()
    with patch(
        "nrev_wf_mcp.server.api.list_node_definitions",
        side_effect=Exception("network blip"),
    ):
        assert _lookup_node_def_flags(SCHEDULER_TYPE_ID) == (None, None)


def test_lookup_paginates_until_match():
    """Match found on page 3 — confirm we keep paging until we find it (or run out)."""
    _lookup_node_def_flags.cache_clear()
    pages = [
        _mock_catalog_page([{"node_definition_id": "a", "is_trigger": False, "isListener": False}] * 100),
        _mock_catalog_page([{"node_definition_id": "b", "is_trigger": False, "isListener": False}] * 100),
        _mock_catalog_page([
            {"node_definition_id": SCHEDULER_TYPE_ID, "is_trigger": True, "isListener": True},
        ] + [{"node_definition_id": "c", "is_trigger": False, "isListener": False}] * 99),
    ]
    with patch("nrev_wf_mcp.server.api.list_node_definitions", side_effect=pages):
        assert _lookup_node_def_flags(SCHEDULER_TYPE_ID) == (True, True)


def test_lookup_stops_when_page_is_short():
    """If a page returns fewer than PAGE items, stop paging — that's the end."""
    _lookup_node_def_flags.cache_clear()
    with patch("nrev_wf_mcp.server.api.list_node_definitions") as mock_list:
        # First page returns 50 (less than PAGE=100) — should NOT request page 2
        mock_list.return_value = _mock_catalog_page([
            {"node_definition_id": "x", "is_trigger": False, "isListener": False},
        ] * 50)
        assert _lookup_node_def_flags("missing-id") == (None, None)
        assert mock_list.call_count == 1


def test_lookup_caches_repeated_calls():
    """Same typeId twice → only one underlying API call thanks to lru_cache."""
    _lookup_node_def_flags.cache_clear()
    with patch("nrev_wf_mcp.server.api.list_node_definitions") as mock_list:
        mock_list.return_value = _mock_catalog_page([
            {"node_definition_id": SCHEDULER_TYPE_ID, "is_trigger": True, "isListener": True},
        ])
        _lookup_node_def_flags(SCHEDULER_TYPE_ID)
        _lookup_node_def_flags(SCHEDULER_TYPE_ID)
        _lookup_node_def_flags(SCHEDULER_TYPE_ID)
        assert mock_list.call_count == 1


# ── attach_node integration: flags propagate to the paste-nodes body ─────


def _mock_paste_capture(captured: dict, preserve_id: bool = True):
    """Build a fake paste_nodes that records the body sent, then echoes back
    the pasted block. By default preserves the sent id so the helper's
    no-reassignment path is exercised. Pass preserve_id=False to simulate
    the platform's id-reassignment behavior.

    The fake also includes any pre-existing blocks from
    `captured["existing_blocks"]` (if set) so the helper's id-diff logic
    has the right baseline.
    """
    def fake_paste(wf_id, body):
        captured.setdefault("paste_bodies", []).append(body)
        echoed_nodes = []
        for n in body["nodes"]:
            copy = dict(n)
            if not preserve_id:
                copy["id"] = f"platform-{n['id']}"
            echoed_nodes.append(copy)
        return {
            "blocks": (captured.get("existing_blocks") or []) + echoed_nodes,
            "isRunable": True,
            "workflowConfigError": None,
        }
    return fake_paste


def _mock_put_node_capture(captured: dict):
    """Records every put_node call so the test can inspect which parents got
    edge updates."""
    def fake_put_node(wf_id, node_id, node_patch):
        captured.setdefault("put_node_calls", []).append((node_id, node_patch))
        return node_patch
    return fake_put_node


def test_attach_node_auto_sets_trigger_listener_for_scheduler():
    """End-to-end: attach a Scheduler with default flags. The block sent to
    paste-nodes must carry isTrigger=True AND isListener=True so the
    workflow can go live."""
    from nrev_wf_mcp.server import attach_node

    _lookup_node_def_flags.cache_clear()
    captured: dict = {}

    with patch("nrev_wf_mcp.server.api.get_workflow") as mock_get, \
         patch("nrev_wf_mcp.server.api.paste_nodes", side_effect=_mock_paste_capture(captured)), \
         patch("nrev_wf_mcp.server.api.put_node", side_effect=_mock_put_node_capture(captured)), \
         patch("nrev_wf_mcp.server.api.put_workflow") as mock_put_wf, \
         patch("nrev_wf_mcp.server.api.list_node_definitions") as mock_list, \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        mock_get.return_value = {
            "id": "wf-1", "name": "x", "description": "",
            "blocks": [],
        }
        mock_list.return_value = _mock_catalog_page([
            {"node_definition_id": SCHEDULER_TYPE_ID, "is_trigger": True, "isListener": True},
        ])
        result = attach_node(
            workflow_id="wf-1",
            parent_node_ids=[],
            type_id=SCHEDULER_TYPE_ID,
            name="Scheduler",
            settings={"automation-scheduler-interval": "Days"},
            auto_resolve_labels=False,  # avoid field-options calls
        )

    assert result["ok"]
    assert result["is_trigger"] is True
    assert result["is_listener"] is True
    # The new path MUST NOT call put_workflow (the giant-PUT that 413s on big WFs)
    mock_put_wf.assert_not_called()
    # paste-nodes called once, with our block
    assert len(captured["paste_bodies"]) == 1
    pasted = captured["paste_bodies"][0]["nodes"][0]
    assert pasted["isTrigger"] is True
    assert pasted["isListener"] is True
    # No parents → no put_node edge calls
    assert captured.get("put_node_calls", []) == []


def test_attach_node_keeps_non_trigger_default_for_app_node():
    """Add Single Row is not a trigger — both flags must be False, and the
    one parent must get exactly one put_node edge-wiring call."""
    from nrev_wf_mcp.server import attach_node

    _lookup_node_def_flags.cache_clear()
    captured: dict = {}

    with patch("nrev_wf_mcp.server.api.get_workflow") as mock_get, \
         patch("nrev_wf_mcp.server.api.paste_nodes", side_effect=_mock_paste_capture(captured)), \
         patch("nrev_wf_mcp.server.api.put_node", side_effect=_mock_put_node_capture(captured)), \
         patch("nrev_wf_mcp.server.api.put_workflow") as mock_put_wf, \
         patch("nrev_wf_mcp.server.api.list_node_definitions") as mock_list, \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        mock_get.return_value = {
            "id": "wf-1", "name": "x", "description": "",
            "blocks": [{
                "id": "parent-1", "variableName": "P",
                "position": {"x": 0, "y": 0}, "toBlocks": [],
            }],
        }
        mock_list.return_value = _mock_catalog_page([
            {"node_definition_id": ADD_ROW_TYPE_ID, "is_trigger": False, "isListener": False},
        ])
        result = attach_node(
            workflow_id="wf-1",
            parent_node_ids=["parent-1"],
            type_id=ADD_ROW_TYPE_ID,
            name="Add Row",
            settings={"some-field": "value"},
            auto_resolve_labels=False,
        )

    assert result["is_trigger"] is False
    assert result["is_listener"] is False
    mock_put_wf.assert_not_called()
    pasted = captured["paste_bodies"][0]["nodes"][0]
    assert pasted["isTrigger"] is False
    assert pasted["isListener"] is False
    # Single parent → exactly one put_node edge call on parent-1
    assert len(captured["put_node_calls"]) == 1
    parent_id, patched_parent = captured["put_node_calls"][0]
    assert parent_id == "parent-1"
    assert any(e["toBlockId"] == result["node_id"] for e in patched_parent["toBlocks"])


def test_attach_node_caller_override_wins():
    """Explicit is_trigger=False MUST stick even on a trigger-capable type.
    Same for is_listener. The caller knows best when they pass non-None."""
    from nrev_wf_mcp.server import attach_node

    _lookup_node_def_flags.cache_clear()
    captured: dict = {}

    with patch("nrev_wf_mcp.server.api.get_workflow") as mock_get, \
         patch("nrev_wf_mcp.server.api.paste_nodes", side_effect=_mock_paste_capture(captured)), \
         patch("nrev_wf_mcp.server.api.put_node", side_effect=_mock_put_node_capture(captured)), \
         patch("nrev_wf_mcp.server.api.put_workflow") as mock_put_wf, \
         patch("nrev_wf_mcp.server.api.list_node_definitions") as mock_list, \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        mock_get.return_value = {
            "id": "wf-1", "name": "x", "description": "",
            "blocks": [],
        }
        mock_list.return_value = _mock_catalog_page([
            {"node_definition_id": SCHEDULER_TYPE_ID, "is_trigger": True, "isListener": True},
        ])
        result = attach_node(
            workflow_id="wf-1",
            parent_node_ids=[],
            type_id=SCHEDULER_TYPE_ID,
            name="Scheduler (forced non-trigger)",
            settings={"automation-scheduler-interval": "Days"},
            is_trigger=False,
            is_listener=False,
            auto_resolve_labels=False,
        )

    assert result["is_trigger"] is False
    assert result["is_listener"] is False
    mock_put_wf.assert_not_called()
    pasted = captured["paste_bodies"][0]["nodes"][0]
    assert pasted["isTrigger"] is False
    assert pasted["isListener"] is False
