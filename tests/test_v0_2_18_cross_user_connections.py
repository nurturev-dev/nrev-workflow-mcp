"""Tests for v0.2.18 — cross-user connections + add_edge orphan refresh.

Background — both fixes came out of the Phase 2 Pipedream stress test:

1. **list_connections cross-user filter**: in multi-user nRev tenants,
   `list_connections()` returned ONLY the JWT user's own OAuth connections
   — so an agent acting as common.dev@nurturev.com couldn't discover the
   Gmail/Slack/Sheets/Calendar connections that sayanta@nurturev.com had
   set up. The Pipedream-action nodes those connections power were
   unreachable.

   Live probing surfaced the workaround: `GET /connections?connectionAppId=<id>`
   returns ALL tenant connections for that app. v0.2.18 plumbs this through
   the wrapper as the optional `connection_app_id` parameter.

2. **add_edge target orphan refresh**: pre-v0.2.18, `add_edge` PUT only the
   source block's `toBlocks`. The target's `isOrphan` and `inputs` were not
   touched. When the target had been created as an orphan (e.g. via paste_nodes
   without a parent — the platform leaves it `isOrphan=True, inputs=[]`),
   subsequent execution failed with `"Node is orphan"`. The platform doesn't
   auto-recompute these fields when a sibling's `toBlocks` changes; the
   wrapper has to mutate the target itself.
"""
from unittest.mock import patch

from nrev_wf_mcp.server import list_connections, add_edge
from nrev_wf_mcp.client import list_connections as client_list_connections


# ════════════════════════════════════════════════════════════════════════
# Fixtures (shared with add_edge tests below)
# ════════════════════════════════════════════════════════════════════════


def _block(block_id, *, isOrphan=False, inputs=None, toBlocks=None):
    """Minimal block dict for orphan-refresh tests."""
    base_inputs = [{
        "columns": [], "columns_metadata": None, "file": "",
        "handle_condition": "_default", "node_id": None,
    }]
    return {
        "id": block_id,
        "typeId": "type-1",
        "variableName": f"block-{block_id}",
        "position": {"x": 0, "y": 0},
        "settings_field_values": [],
        "isTrigger": False, "isListener": False,
        "isOrphan": isOrphan,
        "isPartOfActiveSwimlane": True, "isTestMode": False,
        "inputs": inputs if inputs is not None else base_inputs,
        "outputs": [],
        "toBlocks": toBlocks or [],
    }


def test_list_connections_unfiltered_sends_no_params():
    """Default behavior unchanged from pre-v0.2.18: no filter param = JWT
    user's own connections only."""
    captured = {}

    def fake_request(method, path, params=None, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["params"] = params
        return []  # empty for JWT user

    with patch("nrev_wf_mcp.client.request", side_effect=fake_request):
        result = client_list_connections()

    assert captured["method"] == "GET"
    assert captured["path"] == "/connections"
    assert captured["params"] == {}  # no filter sent


def test_list_connections_filtered_sends_connectionAppId():
    """With connection_app_id set, the platform's connectionAppId filter is
    sent — this exposes cross-user connections in multi-user tenants."""
    captured = {}

    def fake_request(method, path, params=None, **kwargs):
        captured["params"] = params
        return [{"connectionId": "x", "appName": "Gmail"}]

    with patch("nrev_wf_mcp.client.request", side_effect=fake_request):
        client_list_connections(connection_app_id="gmail-app-id")

    assert captured["params"] == {"connectionAppId": "gmail-app-id"}


def test_server_list_connections_passes_filter_through():
    """The MCP-tool layer passes the filter to the client correctly and
    surfaces the slim shape."""
    fake_conns = [
        {
            "connectionId": "conn-a",
            "connectionAppId": "gmail-app-id",
            "appName": "Gmail",
            "connectionName": "Gmail - alice@example.com",
            "status": "active",
            "provider": "google",
            "createdAt": "2026-01-01",
            "updatedAt": "2026-01-01",
        },
        {
            "connectionId": "conn-b",
            "connectionAppId": "gmail-app-id",
            "appName": "Gmail",
            "connectionName": "Gmail - bob@example.com",
            "status": "active",
            "provider": "google",
            "createdAt": "2026-01-02",
            "updatedAt": "2026-01-02",
        },
    ]
    with patch("nrev_wf_mcp.server.api.list_connections",
               return_value=fake_conns) as mock_call:
        result = list_connections(connection_app_id="gmail-app-id")

    mock_call.assert_called_once_with(connection_app_id="gmail-app-id")
    assert result["count"] == 2
    assert result["filtered_by_app_id"] == "gmail-app-id"
    assert result["connections"][0]["connection_id"] == "conn-a"
    assert result["connections"][0]["connection_name"] == "Gmail - alice@example.com"
    assert result["connections"][1]["connection_name"] == "Gmail - bob@example.com"


def test_server_list_connections_defaults_filter_to_none():
    """When the MCP tool is called without the filter, the client gets
    connection_app_id=None and the platform call is unfiltered."""
    with patch("nrev_wf_mcp.server.api.list_connections",
               return_value=[]) as mock_call:
        result = list_connections()

    mock_call.assert_called_once_with(connection_app_id=None)
    assert result["count"] == 0
    assert result["filtered_by_app_id"] is None


def test_server_list_connections_handles_dict_envelope():
    """Defensive — some platform endpoints wrap lists in {data:[...]}.
    list_connections returns a flat list today but the wrapper unwraps a
    dict envelope just in case the API shape changes."""
    with patch("nrev_wf_mcp.server.api.list_connections",
               return_value={"data": [{"connectionId": "x", "appName": "Gmail"}]}):
        result = list_connections()
    assert result["count"] == 1


# ════════════════════════════════════════════════════════════════════════
# add_edge target orphan refresh
# ════════════════════════════════════════════════════════════════════════


def _capture_puts():
    """Returns (fake_put_node, captured_dict). Captures every put_node call."""
    captured = {"put_node_calls": []}

    def fake_put_node(wf_id, node_id, body):
        captured["put_node_calls"].append((node_id, body))
        return {}

    return fake_put_node, captured


def test_add_edge_refreshes_orphan_target_with_isOrphan_false():
    """Headline v0.2.18 fix. Target with isOrphan=True must be PUT a
    second time after the source-side wire-up, with isOrphan=False so
    execution doesn't fail with 'Node is orphan'."""
    src = _block("src")
    tgt = _block("tgt", isOrphan=True, inputs=[])  # the broken case
    fake_put_node, captured = _capture_puts()

    with patch("nrev_wf_mcp.server.api.get_workflow",
               return_value={"id": "wf-1", "blocks": [src, tgt]}), \
         patch("nrev_wf_mcp.server.api.put_node", side_effect=fake_put_node), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        result = add_edge(
            workflow_id="wf-1",
            source_node_id="src",
            target_node_id="tgt",
        )

    assert result["edge_added"] is True
    assert result["target_isOrphan_refreshed"] is True
    # Two put_node calls: source-side wire + target-side orphan refresh
    assert len(captured["put_node_calls"]) == 2
    src_call, tgt_call = captured["put_node_calls"]
    assert src_call[0] == "src"
    assert tgt_call[0] == "tgt"
    assert tgt_call[1]["isOrphan"] is False
    # inputs populated with skeleton
    assert tgt_call[1]["inputs"] and tgt_call[1]["inputs"][0]["handle_condition"] == "_default"


def test_add_edge_skips_target_refresh_when_target_already_healthy():
    """Regression guard: when target is non-orphan AND has populated inputs,
    no second PUT. Keeps the common case at 1-PUT-per-edge."""
    src = _block("src")
    tgt = _block("tgt", isOrphan=False)  # has default inputs skeleton
    fake_put_node, captured = _capture_puts()

    with patch("nrev_wf_mcp.server.api.get_workflow",
               return_value={"id": "wf-1", "blocks": [src, tgt]}), \
         patch("nrev_wf_mcp.server.api.put_node", side_effect=fake_put_node), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        result = add_edge(
            workflow_id="wf-1",
            source_node_id="src",
            target_node_id="tgt",
        )

    assert result["target_isOrphan_refreshed"] is False
    assert len(captured["put_node_calls"]) == 1  # source only
    assert captured["put_node_calls"][0][0] == "src"


def test_add_edge_refreshes_target_with_empty_inputs_even_if_not_marked_orphan():
    """Some blocks have isOrphan=False but inputs=[] (paste_nodes quirk).
    The refresh should fire either way — the platform needs the inputs
    skeleton to route data correctly at execution."""
    src = _block("src")
    tgt = _block("tgt", isOrphan=False, inputs=[])  # the subtle case
    fake_put_node, captured = _capture_puts()

    with patch("nrev_wf_mcp.server.api.get_workflow",
               return_value={"id": "wf-1", "blocks": [src, tgt]}), \
         patch("nrev_wf_mcp.server.api.put_node", side_effect=fake_put_node), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        result = add_edge(workflow_id="wf-1", source_node_id="src", target_node_id="tgt")

    assert result["target_isOrphan_refreshed"] is True
    assert len(captured["put_node_calls"]) == 2


def test_add_edge_idempotent_no_puts_when_edge_already_exists():
    """If the exact same edge already exists, add_edge is a no-op. Don't
    accidentally trigger the orphan-refresh path on a no-op."""
    existing_edge = {
        "edgeId": "src-_default-tgt-_default",
        "edge_source_handle_condition": "_default",
        "edge_target_handle_condition": "_default",
        "toBlockId": "tgt",
    }
    src = _block("src", toBlocks=[existing_edge])
    # Target orphan — but since the edge already exists, refresh shouldn't run
    tgt = _block("tgt", isOrphan=True, inputs=[])
    fake_put_node, captured = _capture_puts()

    with patch("nrev_wf_mcp.server.api.get_workflow",
               return_value={"id": "wf-1", "blocks": [src, tgt]}), \
         patch("nrev_wf_mcp.server.api.put_node", side_effect=fake_put_node), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        result = add_edge(workflow_id="wf-1", source_node_id="src", target_node_id="tgt")

    assert result["edge_existed"] is True
    assert result["edge_added"] is False
    assert captured["put_node_calls"] == []  # no PUTs at all

