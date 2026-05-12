# Monitoring roadmap — parked for after real-world use

This is a deliberately-deferred slice. We discussed monitoring tools alongside v0.2.1, but agreed to ship v0.2.1's pain-fix tools first, monitor a few real workflows for a couple of weeks, then revisit which monitoring tools we actually need vs which we *thought* we'd need.

## Three shapes of monitoring that matter

| Shape | Question being asked | Frequency |
|---|---|---|
| **Health glance** | "Did this workflow run cleanly?" | Per-execution, on demand |
| **Anomaly catch** | "Why is this run different from the usual?" | When something feels off |
| **Cross-workflow watch** | "What's broken across all my customers right now?" | Continuous / scheduled |

## Candidate tools (v0.2.2 if greenlit later)

### Health glance

- **`assert_node_output(exec_id, node_id, assertions)`** — primitive. Runs a list of rules against any past execution's output: `row_count_min/max`, `no_error`, `column_present`, `column_no_nulls`, `duration_max_seconds`, `credits_max`. Returns pass/fail per assertion. Stateless.
- **`run_health_check(wf_id, exec_id?)`** — default health pass: all blocks completed, no node errors, last exec isn't stale, no row-count crashes (e.g. a block returning 0 rows when its predecessor had 100). Sensible defaults, no setup required.
- **`summary_of_node_outputs(exec_id, node_ids?)`** — per-block: row_count, column_count, sample first + last row, error if any. The "show me everything at a glance" tool.

### Anomaly catch

- **`compare_executions(exec_a, exec_b)`** — side-by-side: per-block credits diff, rowCount diff, duration diff, error appearance/disappearance. For "why is this run weird?"

### Cross-workflow watch (service team)

- **`list_recent_failures(since_hours=24, tenant_id?)`** — every failed execution across all the user's workflows in the last N hours. Needs a tenant-level endpoint; fallback is iterate via `GET /workflows` + per-wf scan.
- **`list_running_executions(tenant_id?)`** — what's in flight right now. Same fallback shape if no tenant-level endpoint.

## Checkpoint registry — open design questions

If we want named, persistent checkpoints (e.g. "after every run, assert Aggregate Contacts has >5 rows"), two questions:

**Q1 — Where do checkpoint definitions live?**
- **(A)** Local JSON file (`~/.nrev-wf-checkpoints.json`) — simple, local to one machine
- **(B)** Encoded in the workflow's `description` field as a JSON marker (e.g. `<!-- checkpoints: [...] -->`) — portable, survives PUT/GET, shows up wherever the workflow opens
- **(C)** A separate nRev dataset — cleanest but heavier setup

Current lean: **(B)**.

**Q2 — When do they fire?**
- **Pull-only**: explicit `run_workflow_checks(exec_id)` call after a run finishes
- **Auto-attach**: hook that runs them after every execution — requires platform support or our own polling

Current lean: **pull-only**. Auto-attach is premature.

## What we're explicitly NOT doing

| Idea | Why not |
|---|---|
| Per-block historical stats (avg credits, duration, rowCount over N days) | Needs many runs accumulated first. Build after a few months of data. |
| Auto-anomaly flagging (>2σ deviation) | Same — premature without baseline data. |
| Progress estimate / ETA | `tail_execution` covers the basic need. ETA is nice-to-have. |
| Slack / email alerts on failure | Out of scope for MCP — that's a scheduled task or cron. Separate concern. |

## Revisit trigger

Revisit this doc when **either**:
- We've operated 3+ workflows weekly for a month and have a concrete list of "I wish I knew earlier that..." moments
- A service-team request comes in for a specific monitoring capability

At that point, pick the 2-3 most-needed tools from above, ship as v0.2.2, monitor again, iterate.
