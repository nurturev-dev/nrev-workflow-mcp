"""Static checks for code that will run inside the nRev Custom Code / Magic Node sandbox.

The sandbox has these well-known footguns (learned the hard way):

  E001  `from datetime import datetime`         — datetime is the pre-imported module
  E002  `next(...)`                             — not defined in the sandbox
  E003  Module-level constants used in run()    — assignments outside run() don't propagate
  E004  `import X as _Y` (underscore alias)     — sandbox strips underscore-prefixed names
  W005  `try/except: return []` swallow         — silent-empty failure pattern (warning only)

E000-E004 are blocking errors (attach_* tools refuse to PUT). W005 is a warning that
surfaces in the response but doesn't block — sometimes you genuinely want a defensive
swallow.

Imports DO propagate into run() (so `import pandas as pd` at module level is fine).
The footgun is specifically with module-level Assign statements AND underscore aliases.

Returns a list of LintIssue. Empty list = no issues found.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass


BLOCKING_CODES = frozenset({"E000", "E001", "E002", "E003", "E004"})


@dataclass
class LintIssue:
    line: int
    col: int
    code: str
    message: str

    @property
    def severity(self) -> str:
        return "error" if self.code in BLOCKING_CODES else "warning"

    @property
    def is_blocking(self) -> bool:
        return self.code in BLOCKING_CODES

    def format(self) -> str:
        return f"line {self.line}:{self.col} [{self.code}] {self.message}"


def _names_bound_in(node: ast.AST):
    """Yield all names bound (assigned, imported, looped, etc.) anywhere within node."""
    for n in ast.walk(node):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield n.name
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Name):
                        yield sub.id
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            yield n.target.id
        elif isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name):
            yield n.target.id
        elif isinstance(n, (ast.For, ast.AsyncFor)):
            for sub in ast.walk(n.target):
                if isinstance(sub, ast.Name):
                    yield sub.id
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for item in n.items:
                if item.optional_vars:
                    for sub in ast.walk(item.optional_vars):
                        if isinstance(sub, ast.Name):
                            yield sub.id
        elif isinstance(n, ast.comprehension):
            for sub in ast.walk(n.target):
                if isinstance(sub, ast.Name):
                    yield sub.id
        elif isinstance(n, ast.Import):
            for alias in n.names:
                yield (alias.asname or alias.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for alias in n.names:
                yield (alias.asname or alias.name)


def _is_silent_empty_return(handler: ast.ExceptHandler) -> bool:
    """Detect the dangerous `try/except [Exception]: return []` (or [], {}, None) pattern.

    Catches:
      except: return []           # bare
      except Exception: return [] # broad
      except: return {}           # any empty container
      except: return None         # bare None
      except: return              # bare return

    Does NOT catch:
      except ValueError: return [] # specific, presumed intentional
      except: print(...); return [] # has side effects, not silent
    """
    # Type must be None (bare) OR a Name "Exception" / "BaseException"
    is_broad = handler.type is None or (
        isinstance(handler.type, ast.Name)
        and handler.type.id in ("Exception", "BaseException")
    )
    if not is_broad:
        return False

    if len(handler.body) != 1 or not isinstance(handler.body[0], ast.Return):
        return False

    ret = handler.body[0]
    val = ret.value

    if val is None:
        return True  # bare `return`
    if isinstance(val, ast.Constant) and val.value is None:
        return True  # `return None`
    if isinstance(val, ast.List) and not val.elts:
        return True  # `return []`
    if isinstance(val, ast.Dict) and not val.keys:
        return True  # `return {}`
    if isinstance(val, ast.Tuple) and not val.elts:
        return True  # `return ()`
    return False


def lint(code: str) -> list[LintIssue]:
    issues: list[LintIssue] = []

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [LintIssue(e.lineno or 0, e.offset or 0, "E000", f"syntax error: {e.msg}")]

    # ── Module-level Assign / AnnAssign — these are the names that will NOT
    #    propagate into run(). Imports are excluded on purpose (they DO propagate).
    module_level_assigned: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Name):
                        module_level_assigned.add(sub.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            module_level_assigned.add(node.target.id)

    # ── E001 / E002 / E004 / W005 — anywhere in the tree
    for node in ast.walk(tree):
        # E001: from datetime import ...
        if isinstance(node, ast.ImportFrom) and node.module == "datetime":
            issues.append(LintIssue(
                node.lineno, node.col_offset, "E001",
                "Sandbox quirk: `from datetime import ...` doesn't work — `datetime` is "
                "pre-imported as the module. Use `pd.Timestamp.utcnow()` / `pd.Timedelta()` "
                "or `import datetime` + `datetime.datetime.utcnow()`."
            ))

        # E002: next()
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "next":
            issues.append(LintIssue(
                node.lineno, node.col_offset, "E002",
                "Sandbox quirk: `next()` is not defined. Replace with an explicit loop."
            ))

        # E004: import X as _Y (underscore alias)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname and alias.asname.startswith("_"):
                    issues.append(LintIssue(
                        node.lineno, node.col_offset, "E004",
                        f"Sandbox quirk: `import {alias.name} as {alias.asname}` — the sandbox "
                        f"strips underscore-prefixed names from the run() namespace at runtime "
                        f"(NameError on use). Drop the underscore alias: use "
                        f"`import {alias.name}` and reference as `{alias.name}` directly."
                    ))
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.asname and alias.asname.startswith("_"):
                    issues.append(LintIssue(
                        node.lineno, node.col_offset, "E004",
                        f"Sandbox quirk: `from {node.module} import {alias.name} as {alias.asname}` "
                        f"— the sandbox strips underscore-prefixed names from the run() namespace "
                        f"at runtime (NameError on use). Drop the underscore alias."
                    ))

        # W005: silent try/except: return [] / return {} / return None
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                if _is_silent_empty_return(handler):
                    issues.append(LintIssue(
                        handler.lineno, handler.col_offset, "W005",
                        "Silent failure pattern: `try / except [Exception]: return []/{}/None` "
                        "swallows ALL errors and returns empty data. Downstream sees no rows and "
                        "no error — the most dangerous bug shape (looks like 'no data found' "
                        "instead of 'parser crashed'). Either log the exception, return a "
                        "row with an error marker, or narrow the except to specific exceptions."
                    ))

    # ── E003 — module-level constants referenced inside run()
    run_func = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run"),
        None,
    )
    if run_func is None:
        return issues

    local_defined: set[str] = set()
    args = run_func.args
    for arg in args.args:
        local_defined.add(arg.arg)
    for arg in (args.posonlyargs or []):
        local_defined.add(arg.arg)
    for arg in args.kwonlyargs:
        local_defined.add(arg.arg)
    if args.vararg:
        local_defined.add(args.vararg.arg)
    if args.kwarg:
        local_defined.add(args.kwarg.arg)
    local_defined.update(_names_bound_in(run_func))

    flagged: set[tuple[int, str]] = set()
    for child in ast.walk(run_func):
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            name = child.id
            key = (child.lineno, name)
            if (
                name in module_level_assigned
                and name not in local_defined
                and key not in flagged
            ):
                flagged.add(key)
                issues.append(LintIssue(
                    child.lineno, child.col_offset, "E003",
                    f"Sandbox quirk: `{name}` is defined at module level (outside `run()`), "
                    f"but module-level assignments are NOT visible inside `run()`. "
                    f"Move the definition inside `run()`."
                ))

    return issues
