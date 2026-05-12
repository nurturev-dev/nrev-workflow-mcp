"""Tests for the sandbox linter — one test per error code, plus false-positive guards."""
from nrev_wf_mcp.sandbox_lint import lint


def _codes(code: str) -> list[str]:
    return [i.code for i in lint(code)]


# ── Clean code ────────────────────────────────────────────────────────────


def test_clean_code_passes():
    code = '''
import pandas as pd

def run(df):
    return df.head()
'''
    assert lint(code) == []


def test_imports_visible_in_run_no_warning():
    """Imports DO propagate to run() in the sandbox — must not be flagged."""
    code = '''
import pandas as pd

def run(df):
    return pd.concat([df, df])
'''
    assert "E003" not in _codes(code)


# ── E000: syntax error ────────────────────────────────────────────────────


def test_syntax_error_returned():
    code = "def run(df:\n    return df"
    assert "E000" in _codes(code)


# ── E001: from datetime import ... ────────────────────────────────────────


def test_datetime_import_flagged():
    code = '''
from datetime import datetime

def run(df):
    return df
'''
    assert "E001" in _codes(code)


def test_datetime_timedelta_import_also_flagged():
    code = '''
from datetime import timedelta

def run(df):
    return df
'''
    assert "E001" in _codes(code)


# ── E002: next() ──────────────────────────────────────────────────────────


def test_next_call_flagged():
    code = '''
def run(df):
    row = next(df.iterrows())
    return row
'''
    assert "E002" in _codes(code)


# ── E003: module-level constants used in run() ────────────────────────────


def test_module_level_constant_used_in_run_flagged():
    code = '''
import pandas as pd

WEIGHTS = {1: 9, 2: 6}

def run(df):
    df['score'] = df['x'].map(WEIGHTS)
    return df
'''
    issues = lint(code)
    assert any(i.code == "E003" and "WEIGHTS" in i.message for i in issues)


def test_module_level_constant_redefined_inside_run_passes():
    code = '''
import pandas as pd

WEIGHTS = {1: 9, 2: 6}

def run(df):
    WEIGHTS = {1: 9, 2: 6}
    df['score'] = df['x'].map(WEIGHTS)
    return df
'''
    assert "E003" not in _codes(code)


def test_module_level_set_caught():
    code = '''
AGGREGATORS = {"virtualvocations.com", "jobleads.com"}

def run(df):
    return df[~df['domain'].isin(AGGREGATORS)]
'''
    assert "E003" in _codes(code)


def test_for_loop_target_not_flagged():
    """`for sid in [1,2,3]:` — sid is locally bound, not a module-level reference."""
    code = '''
def run(df):
    for sid in [1, 2, 3]:
        df[f"sig_{sid}"] = sid
    return df
'''
    assert "E003" not in _codes(code)


def test_tuple_unpacking_in_for_not_flagged():
    code = '''
def run(df):
    for k, v in [("a", 1), ("b", 2)]:
        df[k] = v
    return df
'''
    assert "E003" not in _codes(code)


# ── E004: underscore-aliased imports ──────────────────────────────────────


def test_e004_underscore_import_alias():
    code = '''
import pandas as pd
import json as _json

def run(df):
    return _json.dumps([])
'''
    issues = lint(code)
    e004 = [i for i in issues if i.code == "E004"]
    assert len(e004) == 1
    assert "_json" in e004[0].message


def test_e004_underscore_from_import():
    code = '''
from collections import defaultdict as _dd

def run(df):
    return _dd(list)
'''
    issues = lint(code)
    assert any(i.code == "E004" for i in issues)


def test_e004_normal_alias_not_flagged():
    """`import pandas as pd` is fine — no underscore prefix."""
    code = '''
import pandas as pd
import json

def run(df):
    return pd.DataFrame()
'''
    assert "E004" not in _codes(code)


def test_e004_is_blocking():
    """E004 should be a blocking error, not just a warning."""
    from nrev_wf_mcp.sandbox_lint import BLOCKING_CODES
    assert "E004" in BLOCKING_CODES


# ── W005: silent try/except: return [] swallow ────────────────────────────


def test_w005_silent_empty_list_return():
    code = '''
import json

def run(df):
    try:
        return json.loads(df["x"][0])
    except Exception:
        return []
'''
    issues = lint(code)
    w005 = [i for i in issues if i.code == "W005"]
    assert len(w005) == 1


def test_w005_silent_empty_dict_return():
    code = '''
def run(df):
    try:
        return {"x": 1}
    except Exception:
        return {}
'''
    assert "W005" in _codes(code)


def test_w005_silent_bare_return():
    code = '''
def run(df):
    try:
        return 1/0
    except Exception:
        return
'''
    assert "W005" in _codes(code)


def test_w005_silent_none_return():
    code = '''
def run(df):
    try:
        return df
    except Exception:
        return None
'''
    assert "W005" in _codes(code)


def test_w005_specific_exception_not_flagged():
    """Narrow exceptions are presumed intentional, not silently broad."""
    code = '''
def run(df):
    try:
        return df["x"]
    except KeyError:
        return []
'''
    assert "W005" not in _codes(code)


def test_w005_with_side_effects_not_flagged():
    """If the except body has a print/log alongside the return, it's not silent."""
    code = '''
def run(df):
    try:
        return df["x"]
    except Exception:
        print("oops")
        return []
'''
    assert "W005" not in _codes(code)


def test_w005_is_warning_not_blocking():
    """W005 should be a warning — surfaces but doesn't block attaches."""
    from nrev_wf_mcp.sandbox_lint import BLOCKING_CODES
    assert "W005" not in BLOCKING_CODES


def test_w005_severity_field():
    """LintIssue.severity should be 'warning' for W005, 'error' for E*."""
    from nrev_wf_mcp.sandbox_lint import LintIssue
    e = LintIssue(1, 0, "E004", "x")
    w = LintIssue(1, 0, "W005", "y")
    assert e.severity == "error"
    assert e.is_blocking is True
    assert w.severity == "warning"
    assert w.is_blocking is False
