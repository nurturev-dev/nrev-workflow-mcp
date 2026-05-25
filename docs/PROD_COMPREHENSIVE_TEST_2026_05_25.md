# Prod comprehensive test — 2026-05-25

User asked to verify nRev tables works on prod (just released) AND to build/test workflows on prod comprehensively.

## TL;DR

- ✅ **Prod tables service is live + identical to staging.** Same 15 endpoints, same v0.1.0, same auth, same bugs. Verified end-to-end CRUD on a fresh probe table.
- ✅ **nRev Tables workflow nodes work.** `Query Table → Magic Node → Slack` chain executes end-to-end on prod. Output schemas inferred correctly, Magic Node transforms run correctly.
- 🐛 **Three real MCP issues surfaced** during the build, two of which were already partially addressed and one new.

## Prod tables verification

| Check | Result |
|---|---|
| URL | `https://nrev-tables-service.public.prod.nurturev.com` (mirrors workflow service naming) |
| Health | `{"status":"ok"}` |
| Auth | Same Bearer JWT (Supabase) as workflow service. `Authorization: Bearer <jwt>` |
| Surface | Identical 15 endpoints to staging. Same v0.1.0. No M2 endpoints. No DELETE endpoints. |
| `sortBy` bug | Still present (same as staging) |
| `limit` enum bug | Still present (`[100, 500, 1000, 5000, 10000, 50000, 100000]`) |
| CRUD round-trip | ✅ Created `prod_mcp_probe_2026_05_25` (id `23ff8476-5dce-4892-9d19-af7f1e405424`); added a row (Alice, score 87.5, tags `{src,tags}`); listed; updated score to 99; all clean |
| Existing tenant tables | 3 (Untitled by Rajat, outreach by Shantanu, the one I just made) — fresh prod env |

**One new limit-enum value on prod**: in addition to staging's `[100, 500, 1000]`, the prod Query Table node accepts `5000`, `10000`, `50000`, `100000`. Worth checking whether this is a node-level setting or whether the raw API also accepts these.

## Workflow A — Query Table → Magic Node → Slack (built + ran on prod)

Built fully through the MCP and executed end-to-end:

```
[Query probe table] → [Format for Slack (Magic Node)] → [Slack: post to test channel]
```

| Stage | What happened |
|---|---|
| Query Table attach | Required field-name prefix `nrev_tables-query_table-*` (not just `table_id`). Filter passed inline via `settings={...}` arrived as a real list — not stringified |
| Output schema | Correctly inferred from the table: `[name, score, tags]` with `origin_node_type=nrev_tables.query_table`. ✓ |
| Magic Node attach | Clean — code stored, schema set to `[message]` with origin=self |
| Slack node attach | Wrong field names accepted but flagged: response included `pipedream_field_warnings` listing every valid `schema_field_names` value. **Recovery was trivial** — I copy-pasted the correct names from the warning |
| Execute | Whole chain ran in 8.4s. 0 credits |
| Output | Magic Node produced "MCP prod test A: - Alice score 99" correctly. Slack got HTTP 200. |
| **BUG** | Slack rejected the message body with `invalid_blocks`. Block status reported `completed, error: null` — **the row-level error did not bubble to block-level status**. Need `get_node_output` + inspect `error` / `error_1` columns to discover the failure. |

## Three issues surfaced (priority ordered)

### 🐛 Issue #1 (HIGH) — Pipedream row-level errors don't bubble to `tail_execution`

This is the v0.2.20 Fix F territory but **the fix doesn't apply to `tail_execution`**. The Slack node's `block_runs[].status` was `completed` and `error: null`. But the actual row had a 200 OK from Slack containing an `invalid_blocks` error in the body — Pipedream captured it in the row's `error` column without raising a block-level failure.

**Impact**: agents see "completed" and move on. The user finds out only when the Slack message never appears.

**Fix path**: extend the v0.2.20 Fix F row-error detection (currently in `partial_execute`) to also run in `tail_execution`. When the latest execution shows a row-level `error` column with content for a Pipedream-shaped node, surface it as a `row_errors[]` in the tail response.

### 🐛 Issue #2 (MEDIUM) — `delete_node(confirm=True)` rejects the param

Tried `delete_node(workflow_id, node_id, confirm=True)`. Got Pydantic validation error: `Unexpected keyword argument confirm`. Calling without `confirm` worked fine.

**Impact**: agents who learned the pattern from `delete_workflow(confirm=True)` will hit this and not know whether the delete actually fired. (It didn't — the old node was still there until I retried without `confirm`.)

**Fix path**: either remove `confirm` from `delete_node` if the platform doesn't require it (current state — silent acceptance), OR add `confirm` as a documented no-op for consistency with `delete_workflow`. **Recommend the latter** for consistency.

### 🐛 Issue #3 (LOW-MED) — Stale validation cache references deleted nodes

After the failed delete_node call, `validate_workflow` STILL reported errors for the deleted node_id (`3f135ecc-...`) for a while. The workflow had a duplicate node visible in the graph. Once I successfully deleted (without `confirm`), the validation cleared.

**Impact**: cosmetic confusion. Agent reading validation output sees a node_id they don't recognize and wastes time investigating. Self-heals on next mutation.

**Fix path**: not urgent. Document the behavior in `validate_workflow` docstring: "Validation echoes back the platform's cached state — if you just deleted a node, re-validate (or run any small mutation) to refresh."

## Field-name discovery pattern is broken (UX issue, not a bug)

Building Workflow A required me to **guess field names twice** (Query Table prefix; Slack node prefix). The MCP's `attach_node` does post-attach warning that lists the valid schema fields — which is great. But that's reactive. The proactive path (`get_node_dynamic_fields`) only works *after* the node is attached, which creates the chicken-and-egg of "I need to attach to get the field list, but my settings on attach are guessed."

**Improvement**: extend `get_node_definition(type_id)` to also return the field-name catalog (with prefix). Then callers can know the right names BEFORE attaching.

## What's NOT covered in this test (deferred)

- **Workflow B**: read-from-table-only → some other action. Already proved table read in A; B would be redundant.
- **Workflow C**: multi-input Magic Node (df1 + df2). Worth doing in a follow-up — tests whether v0.2.24's JSON-string fix in `update_node_setting` propagates to `attach_magic_node`'s references list. Hypothesis: probably fine since `attach_magic_node` builds the references server-side and passes a Python list.
- **Add Row / Update Row / Get Row nodes** — same pattern as Query Table; if Query Table works the rest probably do too. Worth a 1-row probe per node later.

## Confirmed working end-to-end on prod (this session)

- ✓ create_workflow, attach_node (with proper prefixed field names), attach_magic_node, add_edge (implicit via attach), update_node_setting (with structured values when passed via `settings` on attach), partial_execute, tail_execution, get_node_output (with `columns=[...]` projection)
- ✓ Query Table → Magic Node → Slack chain executes
- ✓ nRev Tables service (raw API)
- ✓ Magic Node output schema with origin metadata

## Probe artifacts on prod

| Artifact | Value |
|---|---|
| Probe nRev table | `23ff8476-5dce-4892-9d19-af7f1e405424` (`prod_mcp_probe_2026_05_25`) — 1 row, 3 user cols |
| Probe workflow | `4da9426f-c237-4597-9fa3-4cbabd7f0101` (`prod_test_A_tables_read_to_slack_2026_05_25`) — 3 nodes |
| Existing test workflow (cloned earlier today) | `b9f9b12f-24dd-4d45-8f2f-3e180c32c883` (`MNCC_test_bench_2026_05_25`) — has the failing CC reproductions for v0.2.24 |
