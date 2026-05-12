"""Tests for settings-traversal helpers used by edit tools."""
from nrev_wf_mcp.server import (
    _walk_settings_set,
    _list_field_paths,
    _find_prompt_fields,
)


# Synthetic Magic Node settings, mirroring the platform shape
MAGIC_SETTINGS = [
    {
        "field_name": "data_manipulation-magic_node-instructions_and_ref",
        "field_value": [
            {
                "field_name": "data_manipulation-magic_node-instructions",
                "field_value": [
                    {"field_name": "data_manipulation-magic_node-instructions_text",
                     "field_value": "old instructions"},
                ],
            },
            {
                "field_name": "data_manipulation-magic_node-references",
                "field_value": ["edge1", "edge2"],
            },
        ],
    },
    {
        "field_name": "data_manipulation-magic_node-code_section",
        "field_value": [
            {"field_name": "data_manipulation-magic_node-code",
             "field_value": "def run(df1, df2):\n    return df1\nresult = run(df1, df2)"},
        ],
    },
]


# Synthetic AI node settings (single prompt field)
AI_SETTINGS_SINGLE = [
    {"field_name": "ai-node-model", "field_value": "claude-opus-4.5"},
    {"field_name": "ai-node-prompt", "field_value": "old prompt"},
    {"field_name": "ai-node-temperature", "field_value": 0.7},
]


# Synthetic AI node settings (multiple prompt-ish fields → ambiguous)
AI_SETTINGS_AMBIGUOUS = [
    {"field_name": "ai-system_message", "field_value": "you are helpful"},
    {"field_name": "ai-user_message", "field_value": "hi"},
]


# ── _walk_settings_set ────────────────────────────────────────────────────


def test_walk_set_top_level():
    settings = [{"field_name": "foo", "field_value": "old"}]
    assert _walk_settings_set(settings, ["foo"], "new") is True
    assert settings[0]["field_value"] == "new"


def test_walk_set_nested_two_levels():
    settings = [{"field_name": "outer", "field_value": [
        {"field_name": "inner", "field_value": "old"},
    ]}]
    assert _walk_settings_set(settings, ["outer", "inner"], "new") is True
    assert settings[0]["field_value"][0]["field_value"] == "new"


def test_walk_set_magic_code_three_deep():
    """Replace just the Magic Node code field — three levels deep."""
    new_code = "def run(df1):\n    return df1\nresult = run(df1)"
    ok = _walk_settings_set(
        MAGIC_SETTINGS,
        ["data_manipulation-magic_node-code_section", "data_manipulation-magic_node-code"],
        new_code,
    )
    assert ok is True
    code_section = next(
        s for s in MAGIC_SETTINGS
        if s["field_name"] == "data_manipulation-magic_node-code_section"
    )
    assert code_section["field_value"][0]["field_value"] == new_code


def test_walk_set_returns_false_for_missing_path():
    settings = [{"field_name": "foo", "field_value": "x"}]
    assert _walk_settings_set(settings, ["bar"], "new") is False


def test_walk_set_returns_false_for_too_deep_path():
    settings = [{"field_name": "foo", "field_value": "leaf"}]
    # Path goes one segment past the leaf
    assert _walk_settings_set(settings, ["foo", "nope"], "new") is False


# ── _list_field_paths ─────────────────────────────────────────────────────


def test_list_paths_flat():
    paths = _list_field_paths(AI_SETTINGS_SINGLE)
    assert "ai-node-model" in paths
    assert "ai-node-prompt" in paths
    assert "ai-node-temperature" in paths
    assert len(paths) == 3


def test_list_paths_nested_uses_slash():
    paths = _list_field_paths(MAGIC_SETTINGS)
    assert any(p.endswith("/data_manipulation-magic_node-code") for p in paths)
    assert any(p.endswith("/data_manipulation-magic_node-references") for p in paths)
    assert any("/data_manipulation-magic_node-instructions/" in p for p in paths)


# ── _find_prompt_fields + _score_prompt_field ─────────────────────────────


def test_find_prompt_single_match():
    matches = _find_prompt_fields(AI_SETTINGS_SINGLE)
    # New API: returns (path, score) tuples
    paths = [m[0] for m in matches]
    assert "ai-node-prompt" in paths


def test_find_prompt_multiple_matches():
    matches = _find_prompt_fields(AI_SETTINGS_AMBIGUOUS)
    paths = [m[0] for m in matches]
    assert "ai-system_message" in paths
    assert "ai-user_message" in paths


def test_find_prompt_in_magic_settings_finds_instructions_text():
    matches = _find_prompt_fields(MAGIC_SETTINGS)
    paths = [m[0] for m in matches]
    assert any("instructions_text" in p for p in paths)


def test_find_prompt_no_matches():
    settings = [
        {"field_name": "ai-temperature", "field_value": 0.7},
        {"field_name": "ai-model", "field_value": "x"},
    ]
    assert _find_prompt_fields(settings) == []


# ── The prompt vs prompt_file_urls fix (#1 issue from agent feedback) ─────


def test_prompt_vs_prompt_file_urls_clear_winner():
    """The bug we're fixing: `prompt` should win cleanly over `prompt_file_urls`
    because the latter contains the negative keyword 'url'.
    """
    from nrev_wf_mcp.server import _score_prompt_field
    assert _score_prompt_field("ai_toolkit-ask_ai-prompt") == 10
    assert _score_prompt_field("ai_toolkit-ask_ai-prompt_file_urls") == 0


def test_score_trailing_keyword_wins():
    from nrev_wf_mcp.server import _score_prompt_field
    assert _score_prompt_field("ai-prompt") == 10
    assert _score_prompt_field("ai-instruction") == 10
    assert _score_prompt_field("model-system_message") == 10
    assert _score_prompt_field("config-user_message") == 10


def test_score_substring_only_lower():
    from nrev_wf_mcp.server import _score_prompt_field
    # "prompt-helper" — keyword is somewhere, but not the trailing segment
    assert _score_prompt_field("prompt-helper") == 3


def test_score_negative_keywords_kill_match():
    from nrev_wf_mcp.server import _score_prompt_field
    for bad in [
        "ai-prompt_file_urls",
        "ai-prompt_attachment",
        "ai-prompt_ids",
        "ai-prompt_count",
        "ai-prompt_schema",
        "ai-prompt-type",
        "ai-prompt-model",
        "ai-prompt-image",
    ]:
        assert _score_prompt_field(bad) == 0, f"expected {bad} → 0"


def test_score_no_keyword_zero():
    from nrev_wf_mcp.server import _score_prompt_field
    assert _score_prompt_field("ai-temperature") == 0
    assert _score_prompt_field("ai-model") == 0
