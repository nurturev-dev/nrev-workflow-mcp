"""Tests for graph rendering helpers (Mermaid + text + json with incoming edges)."""
from nrev_wf_mcp.server import (
    _to_mermaid,
    _to_text_graph,
    _with_incoming_edges,
    _short_id,
    _check_run_arity,
)


# Synthetic three-block linear chain: Scheduler → AI (8cr) → CSV
LINEAR = [
    {"id": "aaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "name": "Scheduler",
     "creditCostPerItem": 0, "isTestMode": False, "node_config_error": None,
     "toBlocks": [{"toBlockId": "bbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                   "src_handle": "_default", "tgt_handle": "_default"}]},
    {"id": "bbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "name": "AI Research",
     "creditCostPerItem": 8, "isTestMode": False, "node_config_error": None,
     "toBlocks": [{"toBlockId": "cccc-cccc-cccc-cccc-cccccccccccc",
                   "src_handle": "_default", "tgt_handle": "_default"}]},
    {"id": "cccc-cccc-cccc-cccc-cccccccccccc", "name": "CSV Write",
     "creditCostPerItem": 0, "isTestMode": False, "node_config_error": None,
     "toBlocks": []},
]


# Synthetic two-input Magic Node fan-in
MAGIC_FAN_IN = [
    {"id": "aaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "name": "Source A",
     "creditCostPerItem": 0, "isTestMode": False, "node_config_error": None,
     "toBlocks": [{"toBlockId": "mmmm-mmmm-mmmm-mmmm-mmmmmmmmmmmm",
                   "src_handle": "_default", "tgt_handle": "df1"}]},
    {"id": "bbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "name": "Source B",
     "creditCostPerItem": 0, "isTestMode": False, "node_config_error": None,
     "toBlocks": [{"toBlockId": "mmmm-mmmm-mmmm-mmmm-mmmmmmmmmmmm",
                   "src_handle": "_default", "tgt_handle": "df2"}]},
    {"id": "mmmm-mmmm-mmmm-mmmm-mmmmmmmmmmmm", "name": "Magic Merge",
     "creditCostPerItem": 0, "isTestMode": False, "node_config_error": None,
     "toBlocks": []},
]


def test_short_id_is_mermaid_safe():
    sid = _short_id("aaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert sid.startswith("n_")
    assert "-" not in sid


def test_mermaid_linear_chain():
    out = _to_mermaid(LINEAR, "test wf")
    # Title + flowchart header
    assert out.startswith("%% test wf\nflowchart LR")
    # All three nodes labelled
    assert "Scheduler" in out
    assert "AI Research" in out
    assert "8cr" in out  # cost badge on the paid node
    assert "CSV Write" in out
    # Default-handle edges have no label
    assert "Scheduler" in out and "-->" in out
    assert "_default → _default" not in out  # we collapse defaults to bare arrow


def test_mermaid_shows_handles_when_non_default():
    out = _to_mermaid(MAGIC_FAN_IN, "fan-in")
    # Magic node fan-in must show df1 / df2 handle labels
    assert "_default → df1" in out
    assert "_default → df2" in out


def test_mermaid_flags_errored_node():
    blocks = [
        {"id": "a-a-a-a-a", "name": "X", "creditCostPerItem": 0,
         "isTestMode": False, "node_config_error": "boom", "toBlocks": []},
    ]
    out = _to_mermaid(blocks, "x")
    assert "ERROR" in out


def test_text_graph_renders_arrows():
    out = _to_text_graph(LINEAR)
    assert "[Scheduler]" in out
    assert "[AI Research]" in out
    assert "(8cr)" in out
    # Default-handle edge uses simple arrow
    assert "→ [AI Research]" in out


def test_text_graph_shows_non_default_handles():
    out = _to_text_graph(MAGIC_FAN_IN)
    # Custom handles use the explicit notation
    assert "=_default/df1=>" in out
    assert "=_default/df2=>" in out


def test_with_incoming_edges_inverts_to_blocks():
    enriched = _with_incoming_edges(MAGIC_FAN_IN)
    # Magic Node should have 2 incoming edges (from Source A and B)
    magic = next(b for b in enriched if b["name"] == "Magic Merge")
    assert len(magic["incoming"]) == 2
    handles = sorted(e["tgt_handle"] for e in magic["incoming"])
    assert handles == ["df1", "df2"]
    # Source nodes should have 0 incoming
    source_a = next(b for b in enriched if b["name"] == "Source A")
    assert source_a["incoming"] == []


# ── attach_magic_node arity check ──


def test_check_run_arity_matches():
    code = '''
import pandas as pd
def run(df1, df2):
    return df1
result = run(df1, df2)
'''
    assert _check_run_arity(code, expected=2) is None


def test_check_run_arity_mismatch():
    code = '''
def run(df1):
    return df1
result = run(df1)
'''
    msg = _check_run_arity(code, expected=2)
    assert msg is not None
    assert "1" in msg and "2" in msg


def test_check_run_arity_missing_function():
    code = "x = 1"
    msg = _check_run_arity(code, expected=1)
    assert msg is not None
    assert "must define" in msg
