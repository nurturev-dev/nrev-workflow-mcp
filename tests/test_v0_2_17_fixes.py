"""Tests for v0.2.17 cleanup fixes (from the v0.2.16 stress-test campaign).

Covers:
  - Fix #2: duplicate_workflow defaults new_name to "Copy of <original>"
  - Fix #3: set_test_mode(on=False) sends the full node envelope (not partial)
  - Fix #4: orphan-trigger hint phrase matcher catches "has no Trigger nodes" /
    "has no start nodes" / "has no listener node"
  - Fix #5 (regression guard): sandbox_lint emits E000 on actual syntax errors
    (the v0.2.16 test agent's input "this is not python" is valid Python — a
    `this is_not python` comparison. Need a real syntax error to test E000.)
"""
from unittest.mock import patch

from nrev_wf_mcp.server import (
    duplicate_workflow,
    set_test_mode,
    partial_execute,
    _EXECUTE_GATE_PHRASES,
    _execute_error_response,
)
from nrev_wf_mcp.sandbox_lint import lint


# ════════════════════════════════════════════════════════════════════════
# Fix #2 — duplicate_workflow defaults new_name
# ════════════════════════════════════════════════════════════════════════


def test_duplicate_workflow_defaults_new_name_to_copy_of_source():
    """Pre-v0.2.17: omitting new_name caused HTTP 422 because the wrapper
    sent empty body. Now: read source name, send 'Copy of <name>'."""
    captured = {}

    def fake_get(wf_id):
        return {"id": wf_id, "name": "My Workflow", "description": ""}

    def fake_duplicate(wf_id, new_name=None):
        captured["new_name_sent"] = new_name
        return {"id": "new-wf-id", "name": new_name, "version": 1}

    with patch("nrev_wf_mcp.server.api.get_workflow", side_effect=fake_get), \
         patch("nrev_wf_mcp.server.api.duplicate_workflow", side_effect=fake_duplicate):
        result = duplicate_workflow("source-wf-id")  # new_name omitted

    assert captured["new_name_sent"] == "Copy of My Workflow"
    assert result["new_workflow_name"] == "Copy of My Workflow"


def test_duplicate_workflow_honors_explicit_new_name():
    """Caller-supplied new_name beats the default."""
    captured = {}

    def fake_duplicate(wf_id, new_name=None):
        captured["new_name_sent"] = new_name
        return {"id": "new-wf-id", "name": new_name, "version": 1}

    with patch("nrev_wf_mcp.server.api.get_workflow") as mock_get, \
         patch("nrev_wf_mcp.server.api.duplicate_workflow", side_effect=fake_duplicate):
        result = duplicate_workflow("source-wf-id", new_name="Fork for ACME")

    # When the caller supplies a name, we should NOT GET the source workflow
    # (no need to compute a default — saves an API call).
    mock_get.assert_not_called()
    assert captured["new_name_sent"] == "Fork for ACME"


def test_duplicate_workflow_handles_source_without_name():
    """Defensive — if the platform somehow returns a workflow without a name,
    fall back to 'Copy of untitled' rather than crashing."""
    captured = {}

    def fake_duplicate(wf_id, new_name=None):
        captured["new_name_sent"] = new_name
        return {"id": "new", "name": new_name}

    with patch("nrev_wf_mcp.server.api.get_workflow",
               return_value={"id": "src", "name": None}), \
         patch("nrev_wf_mcp.server.api.duplicate_workflow", side_effect=fake_duplicate):
        duplicate_workflow("src")

    assert captured["new_name_sent"] == "Copy of untitled"


# ════════════════════════════════════════════════════════════════════════
# Fix #3 — set_test_mode(on=False) sends full node envelope
# ════════════════════════════════════════════════════════════════════════


def _paid_node():
    """A node with non-zero credit cost — bypasses the free-node refusal."""
    return {
        "id": "ai-node-id", "typeId": "ai-typeid", "variableName": "AI Step",
        "settings_field_values": [{"field_name": "prompt", "field_value": "x"}],
        "isTrigger": False, "isListener": False, "isOrphan": False,
        "isPartOfActiveSwimlane": True, "isTestMode": True,
        "inputs": [], "outputs": [],
        "toBlocks": [],
        "position": {"x": 0, "y": 0},
        "creditCostPerItem": 1,  # paid → guard doesn't refuse
    }


def test_set_test_mode_off_sends_full_node_envelope():
    """The headline v0.2.17 fix. Pre-fix, the wrapper sent
    {"isTestMode": false} only → platform 422'd. Now it sends the full block
    with isTestMode flipped."""
    captured = {}

    def fake_put_node(wf_id, node_id, node_patch):
        captured["node_id"] = node_id
        captured["body"] = node_patch
        return {}

    with patch("nrev_wf_mcp.server.api.get_workflow",
               return_value={"blocks": [_paid_node()]}), \
         patch("nrev_wf_mcp.server.api.put_node", side_effect=fake_put_node), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        result = set_test_mode("wf-1", on=False, node_id="ai-node-id")

    assert result["ok"]
    body = captured["body"]
    # Must carry the fields the platform requires
    assert body["id"] == "ai-node-id"
    assert body["typeId"] == "ai-typeid"
    assert body["variableName"] == "AI Step"
    assert body["settings_field_values"] == [{"field_name": "prompt", "field_value": "x"}]
    assert body["isTrigger"] is False  # required field also present
    # And the actual mutation
    assert body["isTestMode"] is False


def test_set_test_mode_on_paid_node_also_sends_full_envelope():
    """on=True path uses the same code as on=False — both should send full block."""
    captured = {}

    def fake_put_node(wf_id, node_id, node_patch):
        captured["body"] = node_patch
        return {}

    paid = _paid_node()
    paid["isTestMode"] = False  # start off

    with patch("nrev_wf_mcp.server.api.get_workflow",
               return_value={"blocks": [paid]}), \
         patch("nrev_wf_mcp.server.api.put_node", side_effect=fake_put_node), \
         patch("nrev_wf_mcp.server._maybe_validate", return_value=None):
        set_test_mode("wf-1", on=True, node_id="ai-node-id")

    body = captured["body"]
    assert body["isTestMode"] is True
    assert body["id"] == "ai-node-id"
    assert body["typeId"] == "ai-typeid"


def test_set_test_mode_on_free_node_still_refuses():
    """Regression guard: the free-node refusal still fires before any PUT."""
    free_node = _paid_node()
    free_node["creditCostPerItem"] = 0

    with patch("nrev_wf_mcp.server.api.get_workflow",
               return_value={"blocks": [free_node]}), \
         patch("nrev_wf_mcp.server.api.put_node") as mock_put:
        result = set_test_mode("wf-1", on=True, node_id="ai-node-id")

    mock_put.assert_not_called()
    assert result["ok"] is False
    assert "free node" in result["message"]


# ════════════════════════════════════════════════════════════════════════
# Fix #4 — orphan-trigger hint phrase matcher
# ════════════════════════════════════════════════════════════════════════


def test_phrase_matcher_includes_v0_2_17_additions():
    """Pin the v0.2.17 phrase additions so they don't accidentally get
    dropped in a future refactor."""
    assert "has no trigger nodes" in _EXECUTE_GATE_PHRASES
    assert "has no start nodes" in _EXECUTE_GATE_PHRASES
    assert "has no listener node" in _EXECUTE_GATE_PHRASES
    # And the original three from v0.2.13 still present
    assert "workflow must be executable" in _EXECUTE_GATE_PHRASES
    assert "not in a valid state to execute" in _EXECUTE_GATE_PHRASES
    assert "must be in a valid state" in _EXECUTE_GATE_PHRASES


def test_partial_execute_hint_fires_on_has_no_trigger_nodes():
    """The v0.2.16 stress test surfaced 'Workflow has no Trigger nodes' as
    the most common platform message. Fix #4 makes the hint fire on it."""
    from nrev_wf_mcp.client import WorkflowAPIError

    err = WorkflowAPIError(
        status_code=400,
        body="{\"detail\":\"...{'message': 'Workflow has no Trigger nodes'}\"}",
        url="https://example/execute",
    )
    with patch("nrev_wf_mcp.server.api.execute_node", side_effect=err):
        result = partial_execute(workflow_id="wf-1", target_node_id="n-1")

    assert result["ok"] is False
    assert "hint" in result
    assert "orphan trigger" in result["hint"].lower()


def test_partial_execute_hint_fires_on_has_no_start_nodes():
    from nrev_wf_mcp.client import WorkflowAPIError
    err = WorkflowAPIError(400, "Workflow has no start nodes.", "url")
    with patch("nrev_wf_mcp.server.api.execute_node", side_effect=err):
        result = partial_execute("wf-1", "n-1")
    assert "hint" in result


def test_partial_execute_hint_fires_on_has_no_listener_node():
    from nrev_wf_mcp.client import WorkflowAPIError
    err = WorkflowAPIError(400, "Workflow has no listener node.", "url")
    with patch("nrev_wf_mcp.server.api.execute_node", side_effect=err):
        result = partial_execute("wf-1", "n-1")
    assert "hint" in result


def test_partial_execute_no_hint_when_message_doesnt_match():
    """Regression guard: don't add the hint for unrelated errors."""
    from nrev_wf_mcp.client import WorkflowAPIError
    err = WorkflowAPIError(500, "Something else", "url")
    with patch("nrev_wf_mcp.server.api.execute_node", side_effect=err):
        result = partial_execute("wf-1", "n-1")
    assert result["ok"] is False
    assert "hint" not in result


# ════════════════════════════════════════════════════════════════════════
# Fix #5 (regression guard) — sandbox lint emits E000 on real syntax errors
# ════════════════════════════════════════════════════════════════════════


def test_lint_emits_E000_on_unclosed_paren():
    """The v0.2.16 stress test agent's input 'this is not python' was actually
    valid Python (a comparison expression: `this is_not python`). Real syntax
    errors do trigger E000 — pin that with a definitely-broken input."""
    issues = lint("def foo(")
    codes = [i.code for i in issues]
    assert "E000" in codes


def test_lint_emits_E000_on_unclosed_string():
    issues = lint('x = "unterminated')
    codes = [i.code for i in issues]
    assert "E000" in codes


def test_lint_does_NOT_emit_E000_on_valid_but_unusual_python():
    """Regression guard: 'this is not python' looks broken but parses as
    `this is_not python` (a Compare(IsNot) expression). Must NOT emit E000."""
    issues = lint("this is not python")
    codes = [i.code for i in issues]
    assert "E000" not in codes, f"Got: {codes}"


def test_lint_e000_short_circuits_other_checks():
    """When E000 fires, the rest of the lint is skipped (can't walk a
    failed-to-parse tree). Pin that contract."""
    issues = lint("def foo(\nfrom datetime import x")
    codes = [i.code for i in issues]
    assert codes == ["E000"]  # E001 (from datetime) NOT emitted
