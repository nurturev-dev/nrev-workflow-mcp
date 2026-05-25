# Custom Code silent-failure bug — reproduction & root cause

**Date**: 2026-05-25
**Severity**: HIGH — silent data corruption; zero error percolated
**Status**: Reproduced live; root-cause identified; v0.2.24 fix proposed

## The bug in one sentence

When you attach a Custom Code node via `attach_python_block` (downstream of any node), **the platform silently ignores the CC code's return value and passes the parent's data through verbatim**, with `status: completed`, no error, no warning at any layer.

## Reproduction

**Setup**: workflow `b9f9b12f-24dd-4d45-8f2f-3e180c32c883` (`MNCC_test_bench_2026_05_25`) on prod. Duplicated from `99108cf8-...` (Weekly User Tracking — Top Credit Burner) which had a clean upstream: Find Messages (Slack) → Explode message list (Magic Node, outputs 105 rows of `[channel_id, message_ts, permalink]`).

**Test 1 — CC generates 4 rows from scratch**:

```python
import pandas as pd
def run(df):
    return pd.DataFrame([
        {"a": 1, "b": "alpha"},
        {"a": 2, "b": "beta"},
        {"a": 3, "b": "gamma"},
        {"a": 4, "b": "delta"},
    ])
result = run(df)
```

Attached with `attach_python_block(output_columns=["a", "b"])`. Validation passed. partial_execute completed with `status: completed`, `duration: 0.61s`, no error.

**Actual output**: 105 rows of `[channel_id, message_ts, permalink]` — **the parent's data verbatim**. The 4 rows of `[a, b]` the code returned were silently discarded.

**Test 2 — CC adds 4 columns to existing df**:

```python
import pandas as pd
def run(df):
    df = df.copy()
    df["new_a"] = "value_a"
    df["new_b"] = 100
    df["new_c"] = True
    df["new_d"] = df.index * 2
    return df
result = run(df)
```

Attached with `attach_python_block(output_columns=["channel_id", "message_ts", "permalink", "new_a", "new_b", "new_c", "new_d"])`. Same result — 105 rows × 3 cols passed through, **no `new_a/b/c/d` columns visible**.

**Test 3 — Magic Node, same code as Test 1**:

```python
import pandas as pd
def run(df1):
    return pd.DataFrame([
        {"a": 1, "b": "alpha"},
        {"a": 2, "b": "beta"},
        {"a": 3, "b": "gamma"},
        {"a": 4, "b": "delta"},
    ])
result = run(df1)
```

Attached with `attach_magic_node(parent_node_ids=[...], output_columns=["a", "b"])`. partial_execute completed. **Output: exactly 4 rows of `[a, b]` as declared. ✅ Works perfectly.**

## Workaround attempts that DID NOT fix it

1. **`set_node_output_schema(columns=[a, b])`** after attach. Tool returned `ok=true, column_count=2`. Re-ran partial_execute. **Output schema reverted to parent's during the execute.** Doesn't stick.
2. **`remove_edge` → `set_node_output_schema` → `add_edge`** (the v0.2.18 refresh path). After dance, `get_node` shows `inputs` correctly filled with parent's metadata, but `outputs` is STILL parent's schema. partial_execute output STILL parent's 105 rows.
3. Various code formulations (with/without `result =` line, with/without `def run()`, returning vs assigning). None changed behavior.

## Root cause analysis

### What `attach_python_block` sends (server.py:1166-1193)

```python
new_block = {
    "id": new_id,
    "typeId": CUSTOM_CODE,
    "settings_field_values": [
        _sf("data_manipulation-custom_code-code", code)   # ← ONLY field
    ],
    "inputs": [{
        "columns": [], "columns_metadata": None,           # ← empty skeleton
        "file": "", "handle_condition": "_default",
        "node_id": None,
    }],
    "outputs": [{
        "columns": output_columns,                          # ← user-declared
        "columns_metadata": <built from output_columns>,
        "file": "", "handle_condition": "_default",
        "node_id": new_id,
    }],
    ...
}
```

### What `attach_magic_node` sends (server.py:937-991)

```python
new_block = {
    "id": new_id,
    "typeId": MAGIC_NODE,
    "settings_field_values": [
        {
            "field_name": "data_manipulation-magic_node-instructions_and_ref",
            "field_value": [
                {... "data_manipulation-magic_node-instructions" ... text ...},
                {
                    "field_name": "data_manipulation-magic_node-references",
                    "field_value": [
                        f"{parent_id}-_default-{new_id}-df1",  # ← EXPLICIT WIRE
                        ...
                    ],
                },
            ],
        },
        {... "data_manipulation-magic_node-code_section" ... code ...},
    ],
    "inputs": [{empty skeleton, same as CC}],
    "outputs": [{filled with declared columns, same as CC}],
    ...
}
```

### The critical difference

MN sends an **explicit `references` list** in settings that names each parent edge (`<parent>-_default-<self>-df1`). This tells the platform exactly which inputs the user wants wired in and signals "user has authored this node intentionally — do not auto-clobber the schema."

CC sends **only** the `code` setting. No equivalent reference. The platform sees:
- Empty `inputs[0].columns_metadata`
- A `code` body that may or may not actually compile
- Declared `outputs` that don't match any production trace

…and conservatively decides to **pass through the parent's data**, ignoring the code entirely. The output schema also gets re-derived from "what actually came out" (which is the parent's), so the declared schema is silently overwritten.

This isn't a v0.2.23 Fix #3 issue (which was about `attach_node` empty inputs). This is a **deeper CC-specific platform behavior**: CC without proper output-schema declaration / settings flag falls into "passthrough" mode.

## Why the user hit this yesterday

The user said "the custom code couldn't generate 4 rows that we were trying to do, nor could it add 4 columns to the pandas df." Both symptoms match this bug **exactly**:

- "Couldn't generate 4 rows" → CC code returns 4 rows but platform passes through parent's N rows. User sees the wrong row count.
- "Couldn't add 4 columns" → CC code adds 4 columns to df but platform passes through parent's original columns. User sees no new columns.

In both cases the workflow ran `status: completed` with no error, so neither the platform UI nor the MCP surfaced anything. The user had to manually inspect the output to discover the discrepancy.

This is the **worst class of bug**: silent data corruption with zero diagnostic signal.

## The resolution path

### Immediate (v0.2.24 — must ship)

1. **`attach_python_block` should warn loudly** in its response: `{warning: "⚠️ Custom Code attach via MCP is currently broken — code is silently ignored at runtime. Use attach_magic_node instead with parent_node_ids=[parent_id]. See docs/CC_BUG_REPRO_2026_05_25.md."}`

2. **Add `require_magic_node_override=True` flag** to attach_python_block. If False (the default), raise an exception with the same message + a one-liner Python snippet showing how to convert to attach_magic_node.

3. **Update docstring** on attach_python_block: open with "⚠️ KNOWN BUG (v0.2.24): use attach_magic_node instead." Followed by the explanation.

4. **`update_ai_prompt`, `update_node_setting`, and `clone_node` callers that touch CC nodes** should also warn or refuse.

5. **`partial_execute` post-run schema-diff check**: after execute, compare declared output_columns with the actual output's columns. If mismatch, surface a warning in the response: `{output_schema_drift: {declared: [...], observed: [...], hint: "CC code may have been silently ignored — switch to Magic Node"}}`.

### Investigation (v0.2.25 or platform team)

6. **Find the platform-side trigger for CC passthrough mode.** Is there a settings field that turns it off (i.e., enables "use the user's return value")? File with platform team. The UI must do this when a user types code into a CC — otherwise CC would be entirely unusable.

7. **If the platform fix lands**, the v0.2.24 MCP warnings can be lifted. Until then, the MCP must steer to Magic Node.

### Standing recommendation

**Use `attach_magic_node` for all transforms.** Even single-input. The narrowed preference statement from earlier is now strengthened:

> Use Magic Node for any pandas transform — single OR multi-input. Custom Code is currently broken via the MCP (silent passthrough). Magic Node uses the same Python sandbox, runs the same `run(df1)` signature, returns the same way, and produces correct output. The only thing CC offers is the `df` variable name vs `df1`; that's not worth the silent-failure risk.

## Test artifacts

| Item | Value |
|---|---|
| Test workflow | `b9f9b12f-24dd-4d45-8f2f-3e180c32c883` (`MNCC_test_bench_2026_05_25`) on prod |
| Parent node (Magic Node, 105-row source) | `b0849431-0314-4da7-9434-e70764f93666` (Explode message list) |
| Failing CC node #1 (generate 4 rows) | `827eab32-8cac-4ac8-9c9c-88be99e75cf7` (`CC_test_generate_4_rows`) |
| Failing CC node #2 (add 4 columns) | `e323bd83-20e5-401f-a5b6-92ac315e4c5e` (`CC_test_add_4_columns`) |
| Working MN node (same code as failing CC #1) | `7ca25572-1beb-499b-b6c6-18d6bbed70f4` (`MN_test_generate_4_rows`) |
| Cost of this investigation | 0 credits (Slack reads free) |

## Open questions

- Is there a `settings_field_values` entry for CC that toggles passthrough vs run-user-code? The MN reference list seems to be that signal; CC needs equivalent. **File with platform team to discover.**
- Does the platform UI's CC editor send something the MCP doesn't? **Capture a cURL when a user types code into CC in the UI and compare to what the MCP PUTs.** This is the next investigation step if v0.2.24's warning route isn't enough.
- Are there OTHER nodes that have similar passthrough-on-attach behavior? (E.g., AI prompt nodes.) **Audit at v0.2.25.**
