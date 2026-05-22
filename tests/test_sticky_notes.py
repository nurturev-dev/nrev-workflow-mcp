"""Tests for v0.2.12 sticky-note tools: list / add / update / delete.

Two things worth pinning behaviorally:
  - text → Tiptap content conversion (plain string in, doc with paragraphs out)
  - PATCH body uses camelCase `stickyNotes` (the OpenAPI body schema says
    snake_case but the server rejects that — discovered live, see client.py)
"""
from unittest.mock import patch

from nrev_wf_mcp.server import (
    _text_to_tiptap_content,
    _tiptap_content_to_text,
    list_sticky_notes,
    add_sticky_note,
    update_sticky_note,
    delete_sticky_note,
)


# ── content conversion ───────────────────────────────────────────────────


def test_text_to_tiptap_single_line():
    doc = _text_to_tiptap_content("Hello world")
    assert doc == {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Hello world"}]},
        ],
    }


def test_text_to_tiptap_multiline_splits_paragraphs():
    """Newlines in plain text become separate paragraph blocks in Tiptap."""
    doc = _text_to_tiptap_content("First\nSecond")
    assert len(doc["content"]) == 2
    assert doc["content"][0]["content"][0]["text"] == "First"
    assert doc["content"][1]["content"][0]["text"] == "Second"


def test_text_to_tiptap_preserves_blank_lines():
    """Blank lines become empty paragraphs — preserves the user's spacing intent."""
    doc = _text_to_tiptap_content("First\n\nThird")
    assert len(doc["content"]) == 3
    assert doc["content"][1].get("content") in (None, [])  # empty paragraph


def test_text_to_tiptap_empty():
    assert _text_to_tiptap_content("") == {"type": "doc", "content": []}


def test_tiptap_to_text_roundtrips():
    original = "Hello\nWorld\n\nThird line"
    doc = _text_to_tiptap_content(original)
    assert _tiptap_content_to_text(doc) == original


def test_tiptap_to_text_handles_legacy_simple_text_shape():
    """If a note was stored via a different client with {text: '...'}, we
    still surface its content rather than returning empty."""
    assert _tiptap_content_to_text({"text": "stored-by-other-client"}) == "stored-by-other-client"


def test_tiptap_to_text_returns_empty_for_unrecognized_shape():
    assert _tiptap_content_to_text(None) == ""
    assert _tiptap_content_to_text({}) == ""
    assert _tiptap_content_to_text("not a dict") == ""


# ── list_sticky_notes ────────────────────────────────────────────────────


def test_list_sticky_notes_returns_text_and_full_content():
    """Caller gets both the readable text AND the raw content for fidelity."""
    raw_note = {
        "id": "n1",
        "position": {"x": 10.0, "y": 20.0},
        "size": {"width": 240.0, "height": 160.0},
        "content": {"type": "doc", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Hi"}]},
        ]},
        "color": "#FFEB3B",
        "colorMode": "background",
        "zIndex": 0,
    }
    with patch("nrev_wf_mcp.server.api.get_workflow") as mock_get:
        mock_get.return_value = {"stickyNotes": [raw_note]}
        result = list_sticky_notes("wf-1")
    assert result["count"] == 1
    assert result["notes"][0]["text"] == "Hi"
    assert result["notes"][0]["content"] == raw_note["content"]
    assert result["notes"][0]["color"] == "#FFEB3B"


def test_list_sticky_notes_returns_empty_when_workflow_has_none():
    with patch("nrev_wf_mcp.server.api.get_workflow", return_value={}):
        result = list_sticky_notes("wf-1")
    assert result == {"count": 0, "notes": []}


# ── add_sticky_note ──────────────────────────────────────────────────────


def test_add_sticky_note_appends_to_existing_list():
    """The PATCH endpoint REPLACES the array, so we must include existing notes."""
    existing = [{
        "id": "old", "position": {"x": 0.0, "y": 0.0},
        "size": {"width": 240.0, "height": 160.0},
        "content": {"type": "doc", "content": []},
        "color": "#fff", "colorMode": "background", "zIndex": 0,
    }]
    captured = {}

    def fake_patch(wf_id, name=None, sticky_notes=None):
        captured["sticky_notes"] = sticky_notes
        return {}

    with patch("nrev_wf_mcp.server.api.get_workflow") as mock_get, \
         patch("nrev_wf_mcp.server.api.patch_workflow_no_validation", side_effect=fake_patch):
        mock_get.return_value = {"stickyNotes": existing}
        result = add_sticky_note(
            workflow_id="wf-1",
            text="New note",
            position_x=100, position_y=100,
        )

    assert result["ok"]
    assert result["count"] == 2  # one existing + one new
    sent = captured["sticky_notes"]
    assert len(sent) == 2
    assert sent[0]["id"] == "old"  # existing preserved
    new = sent[1]
    assert new["id"] == result["note_id"]
    # Text was Tiptap-encoded
    assert new["content"]["content"][0]["content"][0]["text"] == "New note"


def test_add_sticky_note_rejects_invalid_color_mode():
    import pytest
    with pytest.raises(ValueError) as exc:
        add_sticky_note(
            workflow_id="wf-1", text="x", color_mode="rainbow",
        )
    assert "background, transparent, border" in str(exc.value)


# ── update_sticky_note ───────────────────────────────────────────────────


def test_update_sticky_note_changes_only_passed_fields():
    """Caller passes text=... — position, color, size should stay as-is."""
    existing = [{
        "id": "n1",
        "position": {"x": 50.0, "y": 75.0},
        "size": {"width": 300.0, "height": 200.0},
        "content": {"type": "doc", "content": []},
        "color": "#abc", "colorMode": "background", "zIndex": 5,
    }]
    captured = {}

    def fake_patch(wf_id, name=None, sticky_notes=None):
        captured["sticky_notes"] = sticky_notes
        return {}

    with patch("nrev_wf_mcp.server.api.get_workflow") as mock_get, \
         patch("nrev_wf_mcp.server.api.patch_workflow_no_validation", side_effect=fake_patch):
        mock_get.return_value = {"stickyNotes": existing}
        update_sticky_note(workflow_id="wf-1", note_id="n1", text="changed")

    note = captured["sticky_notes"][0]
    assert note["position"] == {"x": 50.0, "y": 75.0}  # untouched
    assert note["size"] == {"width": 300.0, "height": 200.0}  # untouched
    assert note["color"] == "#abc"  # untouched
    assert note["zIndex"] == 5  # untouched
    # text was changed
    assert note["content"]["content"][0]["content"][0]["text"] == "changed"


def test_update_sticky_note_raises_on_unknown_id():
    import pytest
    with patch("nrev_wf_mcp.server.api.get_workflow") as mock_get:
        mock_get.return_value = {"stickyNotes": []}
        with pytest.raises(ValueError) as exc:
            update_sticky_note(workflow_id="wf-1", note_id="missing", text="x")
    assert "missing" in str(exc.value)
    assert "list_sticky_notes" in str(exc.value)


# ── delete_sticky_note ───────────────────────────────────────────────────


def test_delete_sticky_note_removes_and_keeps_others():
    existing = [
        {"id": "keep-1", "position": {}, "size": {}, "content": {}, "color": "#fff", "colorMode": "background", "zIndex": 0},
        {"id": "drop", "position": {}, "size": {}, "content": {}, "color": "#fff", "colorMode": "background", "zIndex": 0},
        {"id": "keep-2", "position": {}, "size": {}, "content": {}, "color": "#fff", "colorMode": "background", "zIndex": 0},
    ]
    captured = {}

    def fake_patch(wf_id, name=None, sticky_notes=None):
        captured["sticky_notes"] = sticky_notes
        return {}

    with patch("nrev_wf_mcp.server.api.get_workflow") as mock_get, \
         patch("nrev_wf_mcp.server.api.patch_workflow_no_validation", side_effect=fake_patch):
        mock_get.return_value = {"stickyNotes": existing}
        result = delete_sticky_note(workflow_id="wf-1", note_id="drop")

    assert result["ok"]
    assert result["remaining_count"] == 2
    remaining_ids = [n["id"] for n in captured["sticky_notes"]]
    assert remaining_ids == ["keep-1", "keep-2"]


def test_delete_sticky_note_raises_on_unknown_id():
    import pytest
    with patch("nrev_wf_mcp.server.api.get_workflow") as mock_get:
        mock_get.return_value = {"stickyNotes": []}
        with pytest.raises(ValueError):
            delete_sticky_note(workflow_id="wf-1", note_id="missing")
