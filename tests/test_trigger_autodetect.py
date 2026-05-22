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


# ── attach_node integration: flags propagate to the PUT body ─────────────


def test_attach_node_auto_sets_trigger_listener_for_scheduler():
    """End-to-end: attach a Scheduler with default flags. The PUT body must
    carry isTrigger=True AND isListener=True so the workflow can go live."""
    from nrev_wf_mcp.server import attach_node

    _lookup_node_def_flags.cache_clear()
    captured = {}

    def fake_put(wf_id, payload):
        captured["payload"] = payload
        return {"blocks": payload["workflow_details"]["blocks"], "isRunable": True}

    with patch("nrev_wf_mcp.server.api.get_workflow") as mock_get, \
         patch("nrev_wf_mcp.server.api.put_workflow", side_effect=fake_put), \
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
    new_block = captured["payload"]["workflow_details"]["blocks"][0]
    assert new_block["isTrigger"] is True
    assert new_block["isListener"] is True


def test_attach_node_keeps_non_trigger_default_for_app_node():
    """Add Single Row is not a trigger — both flags must be False even when
    the caller leaves them at the default."""
    from nrev_wf_mcp.server import attach_node

    _lookup_node_def_flags.cache_clear()
    captured = {}

    def fake_put(wf_id, payload):
        captured["payload"] = payload
        return {"blocks": payload["workflow_details"]["blocks"]}

    with patch("nrev_wf_mcp.server.api.get_workflow") as mock_get, \
         patch("nrev_wf_mcp.server.api.put_workflow", side_effect=fake_put), \
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
    new_block = next(b for b in captured["payload"]["workflow_details"]["blocks"] if b["id"] == result["node_id"])
    assert new_block["isTrigger"] is False
    assert new_block["isListener"] is False


def test_attach_node_caller_override_wins():
    """Explicit is_trigger=False MUST stick even on a trigger-capable type.
    Same for is_listener. The caller knows best when they pass non-None."""
    from nrev_wf_mcp.server import attach_node

    _lookup_node_def_flags.cache_clear()
    captured = {}

    def fake_put(wf_id, payload):
        captured["payload"] = payload
        return {"blocks": payload["workflow_details"]["blocks"]}

    with patch("nrev_wf_mcp.server.api.get_workflow") as mock_get, \
         patch("nrev_wf_mcp.server.api.put_workflow", side_effect=fake_put), \
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
