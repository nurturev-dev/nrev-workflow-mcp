"""Tests for v0.2.8 small-payload attach path.

Before v0.2.8 every attach_* call did a full `PUT /workflows/{id}` that
re-sent every existing block. On workflows past ~50 blocks the body exceeded
the platform's request-size limit (HTTP 413) and the user couldn't add nodes
at all.

The new path POSTs only the new block via `paste-nodes`, then PUTs each
parent individually with the new edge appended. These tests pin the
behavior: no full-workflow PUTs, paste-nodes called once, put_node called
once per parent (with the right edge handles), idempotent on rerun.
"""
from unittest.mock import patch

from nrev_wf_mcp.server import (
    _attach_block_via_paste_and_wire,
    _new_block_error_from_paste,
    _rewrite_block_id,
)


# ── helpers ──────────────────────────────────────────────────────────────


def _make_block(block_id: str, **overrides) -> dict:
    """Minimal block dict for tests."""
    base = {
        "id": block_id,
        "typeId": "type-1",
        "variableName": f"block-{block_id}",
        "position": {"x": 0, "y": 0},
        "toBlocks": [],
        "isTrigger": False,
        "isListener": False,
    }
    base.update(overrides)
    return base


def _capture_calls(reassign_id_to: str = None):
    """Returns (paste_fn, put_node_fn, captured_dict).

    If `reassign_id_to` is set, the fake paste-nodes will echo back the new
    block with `id` rewritten — this mirrors what the real platform does,
    which always reassigns ids regardless of what we send.
    """
    captured = {"paste": [], "put_node": []}

    def fake_paste(wf_id, body):
        captured["paste"].append((wf_id, body))
        echoed_nodes = []
        for n in body["nodes"]:
            copy = dict(n)
            if reassign_id_to:
                copy["id"] = reassign_id_to
            echoed_nodes.append(copy)
        return {
            "blocks": echoed_nodes,
            "isRunable": True,
            "workflowConfigError": None,
        }

    def fake_put_node(wf_id, node_id, node_patch):
        captured["put_node"].append((wf_id, node_id, node_patch))
        return node_patch

    return fake_paste, fake_put_node, captured


# ── _attach_block_via_paste_and_wire ─────────────────────────────────────


def test_paste_and_wire_no_parents_skips_put_node():
    """Trigger node (no parents), no id reassignment → paste-nodes once,
    zero put_node calls (no fixup needed, no edges to wire)."""
    paste_fn, put_node_fn, captured = _capture_calls()  # no reassignment
    new_block = _make_block("new-1")

    with patch("nrev_wf_mcp.server.api.paste_nodes", side_effect=paste_fn), \
         patch("nrev_wf_mcp.server.api.put_node", side_effect=put_node_fn):
        resp, actual_id = _attach_block_via_paste_and_wire(
            workflow_id="wf-1",
            new_block=new_block,
            parent_edges=[],
            fallback_parents={},
            existing_block_ids=set(),
        )

    assert len(captured["paste"]) == 1
    assert captured["put_node"] == []
    assert resp["isRunable"] is True
    assert actual_id == "new-1"


def test_paste_and_wire_uses_reassigned_id_when_platform_changes_it():
    """The platform always reassigns block ids on paste. The helper must:
      1. Detect the new id via diff against existing_block_ids
      2. Put_node a corrected block (fixing self-references)
      3. Wire edges using the new id
      4. Return the new id, not our locally-generated one
    """
    paste_fn, put_node_fn, captured = _capture_calls(reassign_id_to="platform-id")
    new_block = _make_block("local-id")
    parent = _make_block("parent-1")

    with patch("nrev_wf_mcp.server.api.paste_nodes", side_effect=paste_fn), \
         patch("nrev_wf_mcp.server.api.put_node", side_effect=put_node_fn):
        resp, actual_id = _attach_block_via_paste_and_wire(
            workflow_id="wf-1",
            new_block=new_block,
            parent_edges=[("parent-1", "_default", "_default")],
            fallback_parents={"parent-1": parent},
            existing_block_ids={"parent-1"},
        )

    assert actual_id == "platform-id"
    # Two put_node calls: one for id-fixup on the new block, one for parent edge wiring
    assert len(captured["put_node"]) == 2
    fixup_call = captured["put_node"][0]
    edge_call = captured["put_node"][1]
    assert fixup_call[1] == "platform-id"          # fixup targets new id
    assert fixup_call[2]["id"] == "platform-id"    # block id rewritten
    assert edge_call[1] == "parent-1"              # edge wiring on parent
    edge = edge_call[2]["toBlocks"][0]
    assert edge["toBlockId"] == "platform-id"      # edge points to new id
    assert edge["edgeId"] == "parent-1-_default-platform-id-_default"


def test_paste_and_wire_single_parent_wires_default_handle():
    """One parent, no id reassignment → paste + one put_node with the edge."""
    paste_fn, put_node_fn, captured = _capture_calls()
    parent = _make_block("parent-1")

    with patch("nrev_wf_mcp.server.api.paste_nodes", side_effect=paste_fn), \
         patch("nrev_wf_mcp.server.api.put_node", side_effect=put_node_fn):
        _attach_block_via_paste_and_wire(
            workflow_id="wf-1",
            new_block=_make_block("new-1"),
            parent_edges=[("parent-1", "_default", "_default")],
            fallback_parents={"parent-1": parent},
            existing_block_ids={"parent-1"},
        )

    assert len(captured["put_node"]) == 1
    wf_id, parent_id, patched = captured["put_node"][0]
    assert parent_id == "parent-1"
    edges = patched["toBlocks"]
    assert len(edges) == 1
    assert edges[0]["toBlockId"] == "new-1"
    assert edges[0]["edge_source_handle_condition"] == "_default"
    assert edges[0]["edge_target_handle_condition"] == "_default"
    assert edges[0]["edgeId"] == "parent-1-_default-new-1-_default"


def test_paste_and_wire_multi_parent_uses_df_handles():
    """Magic-Node style: 3 parents → 3 put_node calls with df1/df2/df3 handles."""
    paste_fn, put_node_fn, captured = _capture_calls()
    parents = {f"p{i}": _make_block(f"p{i}") for i in range(1, 4)}
    parent_edges = [
        ("p1", "_default", "df1"),
        ("p2", "_default", "df2"),
        ("p3", "_default", "df3"),
    ]

    with patch("nrev_wf_mcp.server.api.paste_nodes", side_effect=paste_fn), \
         patch("nrev_wf_mcp.server.api.put_node", side_effect=put_node_fn):
        _attach_block_via_paste_and_wire(
            workflow_id="wf-1",
            new_block=_make_block("magic-1"),
            parent_edges=parent_edges,
            fallback_parents=parents,
            existing_block_ids={"p1", "p2", "p3"},
        )

    assert len(captured["put_node"]) == 3
    handles = [call[2]["toBlocks"][0]["edge_target_handle_condition"]
               for call in captured["put_node"]]
    assert handles == ["df1", "df2", "df3"]


def test_paste_and_wire_preserves_existing_parent_edges():
    """If a parent already has outbound edges, the new edge is appended — not
    overwritten."""
    paste_fn, put_node_fn, captured = _capture_calls()
    parent = _make_block("parent-1", toBlocks=[
        {
            "edgeId": "parent-1-_default-existing-_default",
            "edge_source_handle_condition": "_default",
            "edge_target_handle_condition": "_default",
            "toBlockId": "existing-downstream",
        },
    ])

    with patch("nrev_wf_mcp.server.api.paste_nodes", side_effect=paste_fn), \
         patch("nrev_wf_mcp.server.api.put_node", side_effect=put_node_fn):
        _attach_block_via_paste_and_wire(
            workflow_id="wf-1",
            new_block=_make_block("new-1"),
            parent_edges=[("parent-1", "_default", "_default")],
            fallback_parents={"parent-1": parent},
            existing_block_ids={"parent-1", "existing-downstream"},
        )

    _, _, patched = captured["put_node"][0]
    edges = patched["toBlocks"]
    assert len(edges) == 2
    assert {e["toBlockId"] for e in edges} == {"existing-downstream", "new-1"}


def test_paste_and_wire_is_idempotent_on_rerun():
    """Calling twice with the same target handle should NOT create a duplicate
    edge — important when the caller retries after a transient error."""
    paste_fn, put_node_fn, captured = _capture_calls()
    parent = _make_block("parent-1", toBlocks=[{
        "edgeId": "parent-1-_default-new-1-_default",
        "edge_source_handle_condition": "_default",
        "edge_target_handle_condition": "_default",
        "toBlockId": "new-1",
    }])

    with patch("nrev_wf_mcp.server.api.paste_nodes", side_effect=paste_fn), \
         patch("nrev_wf_mcp.server.api.put_node", side_effect=put_node_fn):
        _attach_block_via_paste_and_wire(
            workflow_id="wf-1",
            new_block=_make_block("new-1"),
            parent_edges=[("parent-1", "_default", "_default")],
            fallback_parents={"parent-1": parent},
            existing_block_ids={"parent-1"},
        )

    # The edge already existed → no put_node call needed
    assert captured["put_node"] == []


def test_paste_and_wire_raises_on_unknown_parent():
    """If the caller passes a parent_id that isn't in fallback_parents,
    we refuse to wire (better than silently dropping the edge)."""
    paste_fn, put_node_fn, _ = _capture_calls()

    with patch("nrev_wf_mcp.server.api.paste_nodes", side_effect=paste_fn), \
         patch("nrev_wf_mcp.server.api.put_node", side_effect=put_node_fn):
        try:
            _attach_block_via_paste_and_wire(
                workflow_id="wf-1",
                new_block=_make_block("new-1"),
                parent_edges=[("missing-parent", "_default", "_default")],
                fallback_parents={},  # parent not present
                existing_block_ids=set(),
            )
            assert False, "expected ValueError"
        except ValueError as e:
            assert "missing-parent" in str(e)


def test_paste_and_wire_raises_when_paste_response_doesnt_identify_new_block():
    """Defensive: if paste-nodes returns a response we can't make sense of
    (no new id detectable in the diff), bail rather than wire dangling edges."""
    captured = {"paste": [], "put_node": []}

    def fake_paste_no_new_block(wf_id, body):
        captured["paste"].append((wf_id, body))
        # Echo back ONLY the existing blocks — no new id, simulating an
        # unexpected response shape
        return {"blocks": [{"id": "existing-1"}]}

    def fake_put_node(*a, **kw):
        captured["put_node"].append((a, kw))
        return {}

    with patch("nrev_wf_mcp.server.api.paste_nodes", side_effect=fake_paste_no_new_block), \
         patch("nrev_wf_mcp.server.api.put_node", side_effect=fake_put_node):
        try:
            _attach_block_via_paste_and_wire(
                workflow_id="wf-1",
                new_block=_make_block("new-1"),
                parent_edges=[],
                fallback_parents={},
                existing_block_ids={"existing-1"},
            )
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "0 new block ids" in str(e) or "expected 1" in str(e)


# ── _rewrite_block_id ────────────────────────────────────────────────────


def test_rewrite_block_id_swaps_top_level_id():
    block = {"id": "old-uuid", "typeId": "t", "settings_field_values": []}
    out = _rewrite_block_id(block, "old-uuid", "new-uuid")
    assert out["id"] == "new-uuid"


def test_rewrite_block_id_swaps_nested_self_references():
    """The platform's outputs.node_id and columns_metadata.origin_node_id both
    carry self-references; both must be rewritten so downstream lookups work."""
    block = {
        "id": "old-uuid",
        "outputs": [{
            "node_id": "old-uuid",
            "columns_metadata": [{"origin_node_id": "old-uuid", "column_name": "x"}],
        }],
        "settings_field_values": [],
    }
    out = _rewrite_block_id(block, "old-uuid", "new-uuid")
    assert out["id"] == "new-uuid"
    assert out["outputs"][0]["node_id"] == "new-uuid"
    assert out["outputs"][0]["columns_metadata"][0]["origin_node_id"] == "new-uuid"


def test_rewrite_block_id_swaps_magic_node_references():
    """Magic Node carries edge-id-shaped strings in settings that embed new_id
    — those also need rewriting."""
    block = {
        "id": "old-uuid",
        "settings_field_values": [{
            "field_name": "data_manipulation-magic_node-instructions_and_ref",
            "field_value": [{
                "field_name": "data_manipulation-magic_node-references",
                "field_value": ["parent-_default-old-uuid-df1"],
            }],
        }],
    }
    out = _rewrite_block_id(block, "old-uuid", "new-uuid")
    refs = out["settings_field_values"][0]["field_value"][0]["field_value"]
    assert refs == ["parent-_default-new-uuid-df1"]


def test_rewrite_block_id_doesnt_touch_unrelated_uuids():
    block = {"id": "old-uuid", "outputs": [{"node_id": "unrelated-uuid"}]}
    out = _rewrite_block_id(block, "old-uuid", "new-uuid")
    assert out["outputs"][0]["node_id"] == "unrelated-uuid"


# ── _new_block_error_from_paste ──────────────────────────────────────────


def test_error_extractor_finds_new_block():
    paste_resp = {
        "blocks": [
            {"id": "old", "node_config_error": None},
            {"id": "new-1", "node_config_error": "bad settings"},
        ],
    }
    assert _new_block_error_from_paste(paste_resp, "new-1") == "bad settings"


def test_error_extractor_returns_none_when_new_block_missing():
    """If paste-nodes echo doesn't include our id (shouldn't happen, but guard)
    we return None rather than crash."""
    paste_resp = {"blocks": [{"id": "other", "node_config_error": None}]}
    assert _new_block_error_from_paste(paste_resp, "new-1") is None


def test_error_extractor_returns_none_on_empty_response():
    assert _new_block_error_from_paste({}, "new-1") is None
    assert _new_block_error_from_paste({"blocks": []}, "new-1") is None
