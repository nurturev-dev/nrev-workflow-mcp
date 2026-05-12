"""Tests for the bulk_set_test_mode target-selection helper.

The tool itself requires API calls; the target-picker is pure and unit-testable.
"""
from nrev_wf_mcp.server import _pick_test_mode_targets, _describe_test_mode_scope


# Synthetic workflow: Scheduler → AI(paid) → Filter(free) → CSV(free)
#                                            ↓
#                                            ↘ AI2(paid) → CSV2(free)
BLOCKS = [
    {"id": "sched", "variableName": "Scheduler", "creditCostPerItem": 0, "isTestMode": False,
     "toBlocks": [{"toBlockId": "ai1", "edge_source_handle_condition": "_default",
                   "edge_target_handle_condition": "_default"}]},
    {"id": "ai1", "variableName": "AI 1", "creditCostPerItem": 8, "isTestMode": False,
     "toBlocks": [{"toBlockId": "filter1", "edge_source_handle_condition": "_default",
                   "edge_target_handle_condition": "_default"}]},
    {"id": "filter1", "variableName": "Filter", "creditCostPerItem": 0, "isTestMode": False,
     "toBlocks": [
         {"toBlockId": "csv1", "edge_source_handle_condition": "_default",
          "edge_target_handle_condition": "_default"},
         {"toBlockId": "ai2", "edge_source_handle_condition": "_default",
          "edge_target_handle_condition": "_default"},
     ]},
    {"id": "csv1", "variableName": "CSV 1", "creditCostPerItem": 0, "isTestMode": False,
     "toBlocks": []},
    {"id": "ai2", "variableName": "AI 2", "creditCostPerItem": 5, "isTestMode": False,
     "toBlocks": [{"toBlockId": "csv2", "edge_source_handle_condition": "_default",
                   "edge_target_handle_condition": "_default"}]},
    {"id": "csv2", "variableName": "CSV 2", "creditCostPerItem": 0, "isTestMode": False,
     "toBlocks": []},
]


# ── default scope = all blocks ────────────────────────────────────────────


def test_no_scope_returns_all_blocks():
    targets, missing = _pick_test_mode_targets(BLOCKS, node_ids=None, downstream_of=None)
    assert len(targets) == 6
    assert missing == []


# ── explicit node_ids ─────────────────────────────────────────────────────


def test_explicit_node_ids():
    targets, missing = _pick_test_mode_targets(BLOCKS, node_ids=["ai1", "ai2"], downstream_of=None)
    assert {t["id"] for t in targets} == {"ai1", "ai2"}
    assert missing == []


def test_explicit_node_ids_with_unknowns():
    targets, missing = _pick_test_mode_targets(
        BLOCKS, node_ids=["ai1", "does-not-exist", "csv1"], downstream_of=None,
    )
    assert {t["id"] for t in targets} == {"ai1", "csv1"}
    assert missing == ["does-not-exist"]


# ── downstream_of (DFS) ───────────────────────────────────────────────────


def test_downstream_includes_start_node():
    """downstream_of=X should include X itself (the user usually wants the start node too)."""
    targets, missing = _pick_test_mode_targets(BLOCKS, node_ids=None, downstream_of="ai1")
    target_ids = {t["id"] for t in targets}
    assert "ai1" in target_ids
    assert target_ids == {"ai1", "filter1", "csv1", "ai2", "csv2"}
    assert missing == []


def test_downstream_walks_branches():
    """Filter has two children (csv1 and ai2); both should be reached."""
    targets, missing = _pick_test_mode_targets(BLOCKS, node_ids=None, downstream_of="filter1")
    target_ids = {t["id"] for t in targets}
    assert target_ids == {"filter1", "csv1", "ai2", "csv2"}


def test_downstream_terminal_node():
    """downstream_of a sink returns just the sink itself."""
    targets, missing = _pick_test_mode_targets(BLOCKS, node_ids=None, downstream_of="csv1")
    target_ids = {t["id"] for t in targets}
    assert target_ids == {"csv1"}


def test_downstream_missing_node():
    """downstream_of a non-existent node returns no targets + missing-list."""
    targets, missing = _pick_test_mode_targets(BLOCKS, node_ids=None, downstream_of="does-not-exist")
    assert targets == []
    assert missing == ["does-not-exist"]


def test_downstream_avoids_cycles():
    """A cyclic graph shouldn't loop forever."""
    cyclic = [
        {"id": "a", "variableName": "A", "creditCostPerItem": 0,
         "toBlocks": [{"toBlockId": "b", "edge_source_handle_condition": "_default",
                       "edge_target_handle_condition": "_default"}]},
        {"id": "b", "variableName": "B", "creditCostPerItem": 0,
         "toBlocks": [{"toBlockId": "a", "edge_source_handle_condition": "_default",
                       "edge_target_handle_condition": "_default"}]},
    ]
    targets, _ = _pick_test_mode_targets(cyclic, node_ids=None, downstream_of="a")
    assert {t["id"] for t in targets} == {"a", "b"}


# ── precedence: node_ids beats downstream_of ──────────────────────────────


def test_node_ids_precedence_over_downstream_of():
    targets, _ = _pick_test_mode_targets(
        BLOCKS, node_ids=["ai1"], downstream_of="sched",
    )
    # Explicit list wins; we get just ai1
    assert [t["id"] for t in targets] == ["ai1"]


# ── scope descriptions ────────────────────────────────────────────────────


def test_scope_description_explicit():
    assert _describe_test_mode_scope(["a", "b", "c"], None) == "explicit (3 node IDs)"


def test_scope_description_downstream():
    assert _describe_test_mode_scope(None, "abc") == "downstream_of:abc"


def test_scope_description_all():
    assert _describe_test_mode_scope(None, None) == "all_in_workflow"
