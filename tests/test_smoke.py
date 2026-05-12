"""Smoke test — server module imports and registers tools."""


def test_server_imports():
    from nrev_wf_mcp import server
    assert server.mcp is not None
    assert server.mcp.name == "nrev-wf-mcp"


def test_block_types_present():
    from nrev_wf_mcp import block_types
    # Sanity check that constants exist and look like UUIDs.
    for name in ("CUSTOM_CODE", "MAGIC_NODE", "CSV_WRITE"):
        v = getattr(block_types, name)
        assert isinstance(v, str)
        assert len(v) == 36
        assert v.count("-") == 4


def test_looks_like_edge_id():
    from nrev_wf_mcp.server import _looks_like_edge_id
    # Real edge ID with _default handles
    assert _looks_like_edge_id("aaaa-bbbb-cccc-_default-dddd-eeee-ffff-_default")
    # Magic node edge using df1
    assert _looks_like_edge_id("aaaa-_default-magic-df1")
    # Raw UUID — NOT an edge id
    assert not _looks_like_edge_id("aaaa-bbbb-cccc-dddd-eeee")
    # Empty
    assert not _looks_like_edge_id("")
