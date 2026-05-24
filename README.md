# nrev-workflow-mcp

A Claude Code marketplace + plugin from NurtureV that exposes the nRev workflow API as **49 MCP tools** — build, debug, and operate workflows from inside any Claude session.

Internal tool. Auth is JWT-only, per-user, never stored.

Current version: **v0.2.22** ([release notes](#release-notes)).

---

## Install (for everyone — delivery team, ops, anyone using Claude Code)

In any Claude Code session:

```
/plugin marketplace add nurturev-dev/nrev-workflow-mcp
/plugin install nrev-wf@nrev
```

Restart Claude Code. Run `/mcp` and you should see `nrev-wf` with 49 tools.

### Prerequisites (one-time)

The MCP server needs:

1. **Python 3.10+** — `python3 --version` to check
2. **`uv`** — fast Python package manager:
   ```
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
   Restart your terminal after installing.

`uv` handles all dependency installation the first time you use the plugin. No `pip install` required.

### First-session use

Once per Claude session:

1. Grab a JWT from `app.nrev.ai` (DevTools → Network → copy the `Authorization` header value after `Bearer `)
2. In Claude: *"Set my nrev workflow JWT to `eyJhbGc...`"*

JWTs last 12 hours and live in the plugin's process memory only.

### Update (already-installed users)

```
/plugin update nrev-wf
```

Then **fully quit and reopen Claude Code** so the MCP server respawns under the new version. Verify with `/mcp` (tool count should be 45 on v0.2.12+).

If `/plugin update` doesn't see the new version, force-refresh the marketplace cache:

```
/plugin marketplace update nrev
/plugin update nrev-wf
```

Then restart.

---

## Install without `/plugin` (one-line installer)

Some environments don't have the `/plugin` slash command — older Claude Code builds, locked-down corporate installs, and other MCP-capable clients (Cursor, Windsurf, Continue). And some colleagues don't have `git` set up at all. The plugin still works; use the one-line installer:

```bash
curl -sSL https://raw.githubusercontent.com/nurturev-dev/nrev-workflow-mcp/main/scripts/install.sh | bash
```

What it does:

1. Downloads the latest tagged release as a tarball (no `git` required — just `curl` or `wget`).
2. Extracts it to `~/.nrev-wf-mcp/` (override with `NREV_WF_INSTALL_DIR=/path`).
3. Installs `uv` if missing (the Python package manager the launcher uses).
4. Registers the MCP server via `claude mcp add nrev-wf --scope user`. If the `claude` CLI isn't on your PATH, prints a JSON snippet to hand-paste into `~/.claude.json`.

Then **fully quit and reopen Claude Code**. The 40 `nrev-wf` tools should be live. First prompt to verify: *"List my nrev workflows"* — it'll ask you to set a JWT.

### Pin to a specific version

```bash
curl -sSL https://raw.githubusercontent.com/nurturev-dev/nrev-workflow-mcp/main/scripts/install.sh | bash -s v0.2.9
```

### Upgrade later

Re-run the same one-liner. It wipes the install dir, fetches the new release, and re-registers — your MCP config entry stays consistent and your JWT is unaffected (JWTs live in process memory only, never on disk).

### Uninstall

```bash
claude mcp remove nrev-wf --scope user
rm -rf ~/.nrev-wf-mcp
```

---

## Manual install via `git clone` (if you prefer)

For colleagues who want the source tree on disk for inspection or local edits:

```bash
# 1. Clone (uses public HTTPS — no GitHub auth required)
git clone https://github.com/nurturev-dev/nrev-workflow-mcp ~/Projects/nrev-workflow-mcp

# 2. Install uv if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh

# 3a. Register via claude mcp add (works without /plugin)
claude mcp add nrev-wf --scope user -- \
  ~/Projects/nrev-workflow-mcp/plugins/nrev-wf/bin/run-mcp.sh

# 3b. Or hand-edit ~/.claude.json (for non-Claude-Code clients):
#   {
#     "mcpServers": {
#       "nrev-wf": {
#         "command": "/Users/you/Projects/nrev-workflow-mcp/plugins/nrev-wf/bin/run-mcp.sh"
#       }
#     }
#   }
# Use the absolute path.

# 4. Fully quit and reopen Claude Code
```

Update with `git pull` in the clone + restart. Pin to a version with `git checkout v0.2.9`.

---

## What's in this repo

This is both a **Claude Code marketplace** and an **engineering source package**:

```
nrev-workflow-mcp/
├── .claude-plugin/
│   └── marketplace.json          # marketplace declaration — declares nrev-wf plugin
├── plugins/
│   └── nrev-wf/                  # the installable plugin
│       ├── .claude-plugin/
│       │   └── plugin.json
│       ├── .mcp.json             # MCP server launcher config
│       ├── bin/run-mcp.sh        # uv-locator + cache-dir wrapper
│       ├── mcp/                  # bundled Python source (synced from src/)
│       │   ├── pyproject.toml
│       │   └── nrev_wf_mcp/
│       └── README.md
├── src/nrev_wf_mcp/              # source of truth (engineering pip-install)
├── tests/
├── scripts/
│   ├── setup.sh                  # engineering install: pip install -e .
│   └── sync-plugin.sh            # mirrors src/ → plugins/nrev-wf/mcp/
├── docs/
│   ├── TOOL_SPECS.md
│   └── MONITORING_ROADMAP.md
└── pyproject.toml                # engineering package config
```

**End users** install via the marketplace (top of this README).
**Engineering** can also `pip install -e .` for direct development against `src/`.

---

## Tools (49)

| Group | Tools |
|---|---|
| **Auth & billing** | `set_jwt`, `get_auth_status`, `get_credit_balance` |
| **Read / inspect** | `get_workflow`, `list_workflows`, `get_node`, `get_workflow_graph`, `list_node_settings`, `get_node_neighbors`, `trace_path` |
| **Discovery** | `list_node_definitions`, `get_node_definition`, `list_connections`, `list_connection_apps`, `list_field_options`, `get_node_dynamic_fields` |
| **Validate** | `validate_workflow`, `validate_custom_code` |
| **Build & lifecycle** | `create_workflow`, `attach_node`, `attach_magic_node`, `attach_python_block`, `paste_nodes`, `duplicate_workflow`, `clone_node`, `publish_workflow`, `get_publish_status`, `delete_workflow` |
| **Edit** | `update_node_setting`, `update_magic_node`, `update_ai_prompt`, `set_node_output_schema` |
| **Wiring** | `add_edge`, `remove_edge`, `delete_node`, `splice_branch` |
| **Sticky notes** | `list_sticky_notes`, `add_sticky_note`, `update_sticky_note`, `delete_sticky_note` |
| **Run / monitor** | `list_executions`, `get_execution`, `get_node_output`, `partial_execute`, `tail_execution`, `abort_execution` |
| **Test mode** | `set_test_mode`, `bulk_set_test_mode` |
| **Diagnostics** | `dry_run_cost` |

Full schemas: [`docs/TOOL_SPECS.md`](docs/TOOL_SPECS.md).
Deferred monitoring tools: [`docs/MONITORING_ROADMAP.md`](docs/MONITORING_ROADMAP.md).

---

## Common patterns

**Diagnose a failing workflow**
```
get_workflow(<wf_id>)
list_executions(<wf_id>)
get_execution(<wf_id>, <exec_id>)                         → which block failed and why
get_node_output(<wf_id>, <exec_id>, <node_id>,
                columns=["error", "rowCount"])              → projected, no context overflow
```

**Test a prompt change without touching production**
```
clone_node(<wf_id>, <ai_node_id>,
           set_settings={"ai_toolkit-ask_ai-prompt": new_prompt},
           set_test_mode=True)                              → parallel branch, capped at 5 rows
partial_execute(<wf_id>, <new_node_id>,
                prior_execution_id=<last_run>)              → free re-run with cached upstream
```

**Cost protection before a big run**
```
bulk_set_test_mode(<wf_id>, on=True)                       → cap all paid nodes at 5 rows
partial_execute(<wf_id>, <terminal_node>)                  → smoke-test the chain
bulk_set_test_mode(<wf_id>, on=False)                      → flip back when ready
```

---

## Troubleshooting

**`/mcp` shows `nrev-wf` as failed** — almost always missing `uv`. The launcher script prints a useful error to Claude's MCP logs. Quick check: `which uv` in a terminal. If empty, install (see Prerequisites) and restart Claude Code.

**Tool calls return "JWT not set"** — paste a fresh token. JWTs are per-session.

**Tool calls return HTTP 4xx** — JWT is invalid or expired. Get a fresh one.

---

## Release notes

Recent versions, newest first. Run `/plugin update nrev-wf` then restart Claude Code to pick up the latest. (Manual installs: re-run the [one-line installer](#install-without-plugin-one-line-installer), or `git pull` in the clone, then restart.)

### v0.2.22 — start-node-vs-trigger guards + `prepend_trigger` + sticky-note philosophy
**The "if the catalog says it can't be a root, don't let it be a root" release.** Closes the silent runtime "No input data provided" failure for Custom Code / Magic Node / any action-only node attached as a workflow root. Plus a fresh helper to convert one-off workflows into scheduled automations in one call.

**Two new attach_node guards**:

- **Refuse non-trigger-capable as root** — if the node-def catalog marks `is_trigger=False` for the typeId AND `parent_node_ids=[]`, attach now raises with a clear pointer at real data sources (Get Values in Range, CSV Upload, Search People, etc.). Escape hatch: `force_root=True` for catalog edge cases.
- **One-listener-per-workflow** — if you try to attach a listener-capable node as a 2nd root in a workflow that already has a block with `isListener=True`, attach raises (the platform enforces max-one-listener-per-workflow). Escape hatches: `force_demote_listener=True` (auto-flips the new block to `is_listener=False`, response surfaces `demoted_from_listener: True`) or pass `is_listener=False` explicitly.

**New tool — `prepend_trigger`**:

`prepend_trigger(workflow_id, existing_root_id, trigger_type_id, trigger_settings, trigger_name="Trigger")` — the common "convert this one-off workflow into a scheduled automation" pattern in one call. Two steps under the hood: attach the trigger as a new root → `add_edge(trigger → existing_root)`. The v0.2.21 `add_edge` fix automatically flips the existing root's `isTrigger=False` so the workflow ends up with exactly ONE start node. Uses `force_demote_listener=True` internally so it works even if the existing root is itself a listener.

**Live-validated** in v0.2.22 prep: Gmail Find Email (Pipedream trigger-flavored start-only node) ran identically standalone vs prepended-with-Scheduler — the action's own settings drive what it does; the trigger just provides cadence. The tool's response includes a note explaining this: *"The downstream node will use its OWN configured settings each time the trigger fires. To template values from the trigger into the action, manually edit the action's settings to reference `{{data.<field>}}`."*

**`list_node_definitions` filter params**:

- `only_trigger=True` — filter to nodes that CAN be workflow roots (catalog `is_trigger=True`). Includes both true listeners AND one-shot start nodes. ~157 of 464.
- `only_action=True` — the inverse — action-only nodes that REQUIRE a parent. ~307 of 464.
- `only_listener=True` — client-side filter to the TRUE automation triggers (`isListener=True`). Subset of `only_trigger`. ~127 of 464.

The platform's `onlyTrigger=true` / `onlyAction=true` query params are now plumbed through; `only_listener` is a client-side filter on top.

**Sticky-note docstring guidance** (`add_sticky_note` + `update_sticky_note`):

Per user direction — sticky notes are a planning aid, not decoration. Treat them like comments in code: good for intent of a swimlane, non-obvious decisions, TODOs, known limitations. Avoid restating block names, decorative section headers, one-per-block. The exact text is now baked into both tool docstrings.

**`add_edge` docstring** — pulls the start-node-vs-trigger semantics out of `create_workflow` and into `add_edge` (where callers actually look first when wiring), and points at `prepend_trigger` for the common conversion case.

Tool count: 53 → 54 (added `prepend_trigger`). Tests: 256 → 273 (+17 in `test_v0_2_22_fixes.py`).

### v0.2.21 — Sheets CRUD validated end-to-end (READ + WRITE + UPDATE work)
**The release where Sheets actually works.** v0.2.20 documented patterns but couldn't execute writes/updates reliably. v0.2.21 ran 9 raw-API probes + an end-to-end round-trip + an independent sub-agent verification + a UI-cURL capture to land the actual platform shapes. All three CRUD operations now validated to write real rows. See [`docs/v0_2_21_api_investigation.md`](docs/v0_2_21_api_investigation.md) — 474 lines of probe-by-probe findings.

The headline discovery: **`POST /nodes/reload-props`** (NOT `updated-config-and-status`) is the Pipedream dynamic-schema endpoint. Body uses key `settings` not `settingFieldValues`. Response includes `col_NNNN` per sheet column with `label` = the header name, plus an auto-issued `dynamic_props_id` token. Without this token + col_NNNN templates, Pipedream actions silently no-op (the v0.2.19/v0.2.20 silent-failure pattern).

**New tools (49 → 53)**:

- **`reload_pipedream_props(workflow_id, node_id)`** — wraps `/nodes/reload-props`. Returns `{dynamic_props_id, col_to_label, array_fields, fields, has_dynamic_props}`. Cache the token — endpoint is NOT idempotent (each call issues a fresh token).
- **`auto_map_pipedream_columns(workflow_id, node_id)`** — for an Add Single Row, auto-fills each `col_NNNN` with `{{<upstream_header>}}` template, derived from `col_NNNN.label`. Persists the `dynamic_props_id` too. One call wires up the entire Sheets write.
- **`configure_update_row(workflow_id, node_id, criteria, updates, add_if_not_present=False)`** — pythonic helper for Update Row. Pass `criteria=[{"header": "who", "value": "ana@..."}]` and `updates=[{"header": "status", "value": "replied"}]` — the helper resolves header names to `col_NNNN` slugs via reload-props AND builds the correct list-of-lists envelope shape that the platform validator requires. The earlier "Column 'X' not found in sheet" HTTP 400 was caused by sending the wrong shape; this helper sends the right one.
- **`save_and_execute(workflow_id, target_node_id)`** — wraps `POST /workflows/{wf}/nodes/{n}/update-workflow-and-execute`, the atomic save-then-execute endpoint the platform's "Run Workflow" button uses. Avoids the stale-state class of bugs from separate save + execute calls.

**Tool changes**:

- **`add_edge` also flips target `isTrigger=False`** when wiring downstream of an existing root. v0.2.20 left the workflow with two start nodes (user session: "the scheduler was still a start node not a trigger node"). Response now includes `target_isTrigger_flipped: bool`.
- **`attach_node` rejects Scheduler with `is_listener=False`** outright. Pre-v0.2.21 this was honored, producing a "start node that doesn't actually fire" — a documented footgun the v0.2.20 session burned ~10 min on. Error message points at the right pattern (real data source root for one-off; leave is_listener=True for cron).
- **`get_node_dynamic_fields` docstring updated** to clarify it returns only the STATIC schema (5 fields for Add Single Row). For DYNAMIC Pipedream fields (col_NNNN + dynamic_props_id + array fields), point at `reload_pipedream_props`.

**Validated end-to-end** (round-trip workflow `d9807852-…` against MCP Testing sheet):
- READ via Get Values in Range: 4 rows ingested with smart 5-column schema (post-execution inference)
- WRITE via Add Single Row + auto-mapped templates: 4 rows appended to Sheet1, `updatedCells: 5` per row
- UPDATE via Update Row + configure_update_row helper: 12 matching rows updated, `payload: {updated_rows_indices: [2,...,13]}`

**What v0.2.21 does NOT solve (deferred)**:
- The CC-output-schema-clobber when wired downstream of Pipedream parent — platform-side constraint we can't override via PUT. Workaround: use Sheets-read → Sheets-write directly (no CC in between).
- Pipedream node previews via `get_node_output` return 0 rows on the `_default` handle when the action wasn't invoked — Fix F detection coming in v0.2.22.

Tool count: 49 → 53. Tests: 244 → 256 (+12 in `test_v0_2_21_fixes.py` covering client wrappers + the three new MCP tools + add_edge's target-isTrigger flip + Scheduler guard).

### v0.2.20 — Pipedream silent-failure killers + Sheets CRUD documented
**The Pipedream-quirk cleanup release.** Live end-to-end probing of v0.2.19 against Sheets revealed that Pipedream-wrapped action nodes (Add Single Row, Send Message, etc.) have a class of footguns the agent kept walking into: row-data fields that never appear in the static schema, block-level "completed/error:null" hiding row-level Pipedream errors, and update-then-write paths that couldn't add fields the schema reveals progressively. v0.2.20 ships six fixes:

- **Fix F — `tail_execution` + `update_node_setting(verify=True)` auto-surface Pipedream row errors.** When a Pipedream node "completes" with `error: null` at the block level, that does NOT mean the action succeeded. The actual error frequently lives in row[0].error of the output — F2's "Sheets append succeeded" in v0.2.19 testing was actually a Pipedream "undefined is not an array" error that block-level reporting buried. The wrapper now detects Pipedream-shaped blocks and adds a `pipedream_row_error` field to the slim execution snapshot (with `has_pipedream_row_errors: True` at the top level). Status becomes `completed_with_pipedream_row_error` for verify runs.
- **Fix B — `update_node_setting(add_if_missing=True, field_label=...)` can ADD new field paths.** Pre-v0.2.20 it could only modify EXISTING entries — but Pipedream nodes start with only the connection bound and need fields ADDED as the schema progresses (drive, sheetId, hasHeaders, etc.). The default is now `add_if_missing=True`. Response carries `added_new_field: bool` so callers know whether they modified or appended. Nested paths still require the parent group to exist (safer).
- **Fix C — `attach_node` validates Pipedream field names against the action schema.** When the typeId is Pipedream-flavored, after attach the wrapper calls `updated-config-and-status` once and surfaces `pipedream_field_warnings: [{field_name, issue, schema_field_names}]` for any setting whose name isn't in the action's actual schema. Catches the F2-style silent no-op where a typo'd or invented field name (e.g. `myColumnData`) gets persisted by the platform but ignored by the Pipedream runtime.
- **Fix A — `attach_python_block` refuses empty `output_columns` when parent is Pipedream-shaped.** A Pipedream parent's outputs are the fixed `[error, summary, payload]` triple. Without explicit output_columns on a CC downstream, the platform's schema inference buries any new columns the CC produces. New guard returns `stage: "pipedream_parent_schema_guard"` with the parent's columns listed for diagnosis.
- **Fix D — cross-tenant `fieldLabel` resolution via `list_connections(connection_app_id=...)`.** Pre-v0.2.20, `attach_node`'s auto-label resolution called unfiltered `list_connections()` which only returns the JWT user's own — so binding a teammate's connection silently left `fieldLabel=null`. Now on miss the resolver extracts the app slug from the Pipedream field name, looks up the corresponding `connection_app_id`, and retries with the filter — picking up teammates' connections.
- **Fix E + Sheets CRUD docstrings.** Made the Pipedream connection-field naming inconsistency loud in `attach_node` (with worked examples showing it's NOT a formula — discover via `get_node_dynamic_fields`). Documented canonical Sheets patterns: **READ = Get Values in Range, WRITE = Add Single Row in a per-row loop (NEVER Add Multiple Rows), row data comes from upstream (NEVER `myColumnData` in settings), check `pipedream_row_error` after every Pipedream execute**. User feedback: *"we never use add multiple rows"* and *"the MCP should know exactly which nodes it should use"*.

Tool count unchanged at 49. Tests: 225 → 244 (+19 in `test_v0_2_20_fixes.py`).

**Upgrade impact**: agents configuring Pipedream actions get loud, actionable warnings instead of silent no-ops. Cross-tenant attach now resolves labels correctly. Execution post-mortems include the real Pipedream errors that v0.2.19 was missing.

### v0.2.19 — Cross-tenant Pipedream node configuration UNBLOCKED
**The big one for customer-tenant use cases.** Previously we couldn't fully configure Slack / Calendar / Sheets etc. when using a teammate's OAuth connection — cascading dropdowns (channels, calendars, worksheets) appeared to refuse cross-tenant context. v0.2.19 surfaces the platform's actual cross-tenant-friendly endpoint and makes everything work end-to-end.

- **New tool `get_node_dynamic_fields(workflow_id, node_id)`** wraps `POST /nodes/updated-config-and-status` — the same endpoint the platform UI calls when a Pipedream node's settings change to recompute the form schema. Returns the action's full field definitions including dynamic dependent fields (e.g. for Slack New Message, after the connection is bound the response includes `conversations` for the channel dropdown, `resolveNames`, `ignoreBots`, etc.). **CRITICAL — works cross-tenant**, unlike `/nodes/reload-props` which we found 400s on cross-tenant connections.
- **`list_field_options` confirmed cross-tenant** when called with the correct action-specific field name from `get_node_dynamic_fields`. Live-tested: returned 189 Slack channels for sayanta's cross-tenant connection from common.dev's JWT. The P2.5 stress-test agent's earlier blocker was using guessed field names (`channel`, `channelId`); the actual name varies per action and is only knowable from the dynamic-fields schema.
- **Recommended discovery flow for Pipedream nodes** (now documented in the `get_node_dynamic_fields` docstring):
  1. `attach_node` with a placeholder connection field (try `pipedream-<app>-<action>-<app>` or `-<app>_connection_id` — the latter is more common for trigger/listener actions)
  2. `get_node_dynamic_fields` → get the full schema + a `dropdown_field_names` list
  3. For each dropdown field, `list_field_options` with the correct field name → enumerate options
  4. `update_node_setting` to set the chosen values
- **Updates `list_field_options` docstring** to point at `get_node_dynamic_fields` as the discovery prerequisite.

Tool count: 48 → 49. Tests: 219 → 225.

**Customer-tenant impact**: agents acting on behalf of `support@yourco` who need to configure Slack/Sheets/Calendar/etc. against a customer's teammates' OAuth connections can now do it all end-to-end. No more "must each user OAuth their own" workaround.

### v0.2.18 — Pipedream stress-test cleanup (cross-user connections + edge orphan refresh + delete_node ok-flag)
Live Phase-2 testing against Gmail/Slack/Sheets/Calendar/Midbound/LinkedIn surfaced four bugs and several platform quirks worth documenting.

- **`list_connections(connection_app_id=<id>)` enables cross-user discovery.** Pre-v0.2.18 the unfiltered call returned ONLY the JWT user's own connections — useless in multi-user nRev tenants where teammates have OAuth'd apps but you haven't. Now you can pass an `app_id` (from `list_connection_apps`) and the wrapper returns ALL tenant connections for that app — matching what the platform's UI shows in its connection picker. The unfiltered call is unchanged.
- **`add_edge` refreshes the target's `isOrphan` + `inputs`** after wiring. Pre-v0.2.18, wiring an existing orphan block to a source updated the source's `toBlocks` but left the target as `isOrphan=True, inputs=[]` — execution then failed with `"Node is orphan"`. The wrapper now PUTs the target with `isOrphan=False` and a populated inputs skeleton when needed. Response includes `target_isOrphan_refreshed: bool` so callers can see when the second PUT fired.
- **`delete_node` `ok` flag now reflects delete success, not workflow validity post-delete.** Pre-v0.2.18 deleting the last block always returned `ok:false` because the empty workflow has `workflowConfigError: "Workflow has no start nodes"` — three stress-test agents independently misread this as a delete failure. `ok:true` now means "the block is gone"; workflow validity is surfaced separately in the validation slice.

**Docstring updates from real-world traps**:
- `attach_python_block`: Custom Code's single-input convention is `def run(df)` (NOT `df1`). Magic Node still uses `df1..dfN`.
- `attach_node`: `output_columns` is IMPORTANT for Custom Code transforms — without it, downstream blocks see upstream columns and your transform looks invisible. Prefer `attach_python_block` which always sets the output schema.
- `attach_node`: documented the Pipedream connection-field naming convention (`pipedream-<value_first_segment>-<action>-<trailing_segment>` where the trailing segment matches the catalog `value`'s first-after-`pipedream.` token, not the bare app name — confused multiple test agents).
- `attach_node`: documented the cross-tenant runtime gotcha (Gmail accepts cross-tenant connection_ids at runtime; Calendar's Pipedream component throws `oauth_access_token` errors). Recommendation: in multi-user tenants, each user OAuth their own connection.
- `attach_node`: documented the `list_node_settings` limitation for Pipedream nodes (only shows connection_id initially; action's full field list materializes later).

**Stress-test findings deferred** (not v0.2.18 scope):
- Block-level errors don't bubble to execution-level status in `get_execution` — agents have to read `get_node_output` per block to find errors. Worth a v0.2.19 feature to auto-surface.
- `list_node_settings` API-side fix would benefit from platform-side change.
- LinkedIn Automation per-action credit cost lives in the description, not `startingPrice` — catalog wrapper could extract.

Tool count unchanged at 48. Tests: 210 → 219 (+9 in `test_v0_2_18_cross_user_connections.py`).

### v0.2.17 — cleanup release (4 fixes + docstring corrections from v0.2.16 stress test)
Live stress-testing v0.2.16 against 47 of 48 tools surfaced four real defects and two doc inaccuracies. All fixed.

- **`duplicate_workflow` defaults `new_name` correctly.** Pre-v0.2.17: omitting the parameter sent empty body → HTTP 422. Now the wrapper reads the source workflow's name and substitutes `"Copy of <name>"` — matching what the docstring always promised. Caller-supplied names still win.
- **`set_test_mode(on=False)` works.** Pre-v0.2.17: the wrapper PATCHed with only `{"isTestMode": false}` → HTTP 422 because the platform requires the full node envelope (id, typeId, variableName, settings_field_values, isTrigger). Now sends the full block with isTestMode flipped, matching `bulk_set_test_mode`'s correct approach.
- **`partial_execute` orphan-trigger hint actually fires.** Pre-v0.2.17 the phrase matcher looked for `"Workflow must be executable"` / `"not in a valid state"` — but the platform actually returns `"Workflow has no Trigger nodes"` / `"Workflow has no start nodes"` / `"Workflow has no listener node"`. Hint was always absent for the most common failures. Added the three actual phrases.
- **`create_workflow` docstring: one-off pattern fix.** Previously suggested attaching a Custom Code as the root with `pd.DataFrame(...)` for one-off workflows. **This validates clean (`isRunable=true`) but fails at runtime with `"No input data provided"`** — the platform expects root nodes to receive runtime input from a real source. New guidance: use a real data-source root (Sheets "Get Values in Range", file reader, etc.) OR use a Magic Node with a dummy upstream.
- **`paste_nodes` docstring: required-fields list.** Investigation found the v0.2.16 stress test's HTTP 500 came from partially-malformed body shapes (the platform 500s on some malformed inputs and 422s on others). Wrapper itself is correct — beefed up the docstring with the canonical block shape and a "easiest way: `get_node` + mutate" pointer.
- **Sheets-read node naming corrected throughout** — the action is "Get Values in Range" (typeId `ce01c704-…`), not "Read Output Tab".

No new tools; surface count stays at 48. Tests: 195 → 210 (+15 in `test_v0_2_17_fixes.py`).

**Non-bug findings deferred:**
- `validate_custom_code` correctly emits E000 on actual syntax errors — the stress agent's test input `"this is not python"` is valid Python (a `this is_not python` comparison expression). Added regression tests with real syntax errors.
- Error-contract inconsistency across run/monitor tools (only `partial_execute` returns structured `ok:false`; the other 5 raise) — flagged for v0.2.18.
- `abort_execution` 404 — endpoint shape unknown until UI inspection lands. Docstring already warns.

### v0.2.16 — 6 edit tools converted to small-payload (big-workflow safe)
Six tools that previously sent the full workflow on every edit (and 413'd past ~50 blocks) now use per-block PUTs:

- `update_node_setting` — patch a single field
- `update_magic_node` — update code / instructions / output schema
- `update_ai_prompt` — change a prompt string
- `set_node_output_schema` — declare output columns
- `add_edge` — wire one source → one target
- `remove_edge` — drop one edge

All six use a new shared helper `_put_node_and_validate` that wraps `PUT /workflows/{id}/nodes/{node_id}` with the same {ok, node_config_error, workflowConfigError, isRunable, validation} response shape they had before. Backward-compatible — no caller changes required.

**Independent review** before shipping caught two issues:
1. Scope was off by one — original plan said 11 tools; the audit found `set_test_mode` already used per-node PUT. v0.2.16 ships 6 LOW-risk tools.
2. `delete_node` is structurally blocked — OpenAPI confirms there's **no DELETE node endpoint**. Until the platform team adds one, `delete_node` will continue to 413 on big workflows. Issue queued for platform escalation; no v0.2.x fix possible from the wrapper.

**Still on full-PUT (planned for v0.2.17+):**
- `splice_branch` — atomicity concession needed (current full-PUT is atomic; per-node version has a brief two-edge window). Will need add-then-remove ordering.
- `clone_node` — id-reassignment via paste-nodes is doable but needs careful integration with the existing helper.
- `bulk_set_test_mode` — deferred. N sequential put_node calls is materially slower than 1 big PUT when the workflow fits under 1 MB. Will branch on block count.
- `delete_node` — blocked on platform endpoint.

Tests: 184 → 195 (+11 in `test_v0_2_16_conversions.py`). Includes a regression guard that pins the count of remaining `_put_workflow_blocks` callers, so future early conversions trip a test.

### v0.2.15 — workflow lifecycle + slim-view audit fields + paste_nodes hardening
Four small wins shipped together. No behavioral changes to existing tools (except `paste_nodes` which gains a guard); all additive.

- **`get_workflow` slim view surfaces more fields** — per-block `isTrigger` / `isListener` (so the agent can audit trigger flags at a glance without per-block `get_node` calls); workflow-level `status` / `liveVersion` / `playVersion` (so the agent knows whether the workflow is draft or live). Closes a gap surfaced during v0.2.14 stress-testing.
- **New `publish_workflow(workflow_id, toggle_live=True)`** — wraps `POST /live/workflow/{id}/publish`. Promotes a draft to live (auto-fires the configured trigger). Pass `toggle_live=False` to take it off live without deleting.
- **New `get_publish_status(workflow_id)`** — companion to `publish_workflow`. Publishes are async on the platform side; poll this to know when the live version actually serves requests.
- **New `delete_workflow(workflow_id, confirm=True)`** — fills the obvious gap. Requires `confirm=True` to actually delete (default refuses with a clear message — guards against agents iterating). No more empty workflow shells piling up after testing.
- **`paste_nodes` single-input guard (the 5th leak the v0.2.13 reviewer flagged)** — pre-flight refuses to paste a node spec whose `toBlocks` would create a duplicate `_default` incoming edge into any target. Exempts `df1..df5` (Magic Node fan-in). Opt-in `allow_multi_input=True` escape hatch. Closes the back door that bypassed every other v0.2.13 guard.

Tool count: 45 → 48. Tests: 170 → 184.

### v0.2.14 — start-node vs trigger vocabulary fix (follow-up to v0.2.13)
Live probing against the platform during v0.2.13 stress-tests surfaced that I'd conflated two distinct flags. The platform models them separately:

- **Start node** = `isTrigger=true`. Marks a block as a swimlane entry point. Every workflow needs at least one (otherwise `"Workflow has no start nodes"`). MULTIPLE allowed — each begins its own swimlane.
- **Trigger** (the user-facing automation entry) = `isTrigger=true` AND `isListener=true`. Polls/subscribes so the workflow runs on its own. ONLY ONE per workflow (platform-enforced).

v0.2.13 auto-resolved both flags together from the catalog, which broke the one-off case: a plain Custom Code attached as root got both flags False, leaving the workflow with no start node. v0.2.14 separates the two:

- Parents present → both False (unchanged from v0.2.13)
- Parents empty (root block) → `isTrigger=True` ALWAYS (every root is a start node), `isListener` auto-detected from catalog (Scheduler/Sheets-read/Gmail listener types → True; Custom Code / plain transforms → False)
- Explicit overrides always win. Pass `is_listener=False` on a Scheduler root for a one-off run of an otherwise-pollable type.

Also rewrites `create_workflow` docstring with the correct vocabulary (start node vs trigger) and updates `attach_node`'s flag-defaults section to match. The agent now has the right mental model when building one-off vs scheduled vs event-driven workflows.

Tests: 166 → 170 (+4 in `test_v0_2_14_root_defaults.py` pinning the four new behavioral branches).

### v0.2.13 — orphan-trigger hotfix + multi-input guard everywhere
Bundle of six fixes addressing the regression pattern colleagues hit since v0.2.7. Plan was reviewed by an independent agent before shipping.

- **`attach_node` auto-trigger now respects parents.** Pre-fix: any trigger-capable type (Sheets Read Output Tab, Gmail New Email, etc.) attached as a downstream node got silently marked `isTrigger=True, isListener=True` because the catalog says it CAN be a trigger. Result: orphan trigger nodes and platform refusing to execute. Now: trigger flags only auto-resolve when `parent_node_ids=[]` (workflow root). With parents, both flags are forced False unless the caller explicitly overrides.
- **`add_edge` single-input guard.** Refuses to wire a second `_default` edge into a target that already has one. Exempts `df1..df5` (Magic Node fan-in). Opt-in `allow_multi_input=True` for the legacy Merge block. Closes the leak that bypassed v0.2.11's `attach_node` guard.
- **`splice_branch` reuses the same guard** when `replace_edge_from_node_id=None`. The typical splice pattern (with replace target) is unaffected — net incoming count stays at 1.
- **`clone_node` strips `isTrigger`/`isListener`** on the clone, regardless of source. A clone is structurally never the workflow's trigger; cloning a Scheduler used to silently produce a second trigger.
- **`create_workflow` docstring rewrite.** Three explicit patterns (one-off / scheduled / event-driven) instead of the previous "add a Scheduler trigger" bias — one-off workflows are now a first-class case the agent can recognise.
- **`partial_execute` surfaces non-2xx platform errors.** Pre-fix: silently raised. Now returns `ok=False` with the platform's exact message, plus an orphan-trigger hint when the error matches known execution-gate phrases. The agent gets a structured failure instead of a raw exception to misinterpret.

Tests: 151 → 166 (+15 in `test_v0_2_13_fixes.py` covering all five behavioural changes plus regression guards).

### v0.2.12 — credit balance + sticky notes
- New `get_credit_balance` tool — surfaces the active tenant's nRev credit balance as a plain integer. Tenant is resolved server-side from the JWT, so no parameter needed; useful as a sanity check before kicking off credit-heavy runs or when juggling multiple tenants (one Claude Code session per tenant, see [Multi-tenant pattern](#multi-tenant-pattern)).
- New sticky-note tools: `list_sticky_notes`, `add_sticky_note`, `update_sticky_note`, `delete_sticky_note`. Sticky notes are workflow-level annotations (the colored notes you drop on the canvas). Plain-text in, Tiptap JSON wrapping handled internally. Useful for documenting swimlanes, flagging review items, calling out known limitations — without touching node settings.
- Two API gotchas the OpenAPI spec doesn't warn about, encoded in the wrapper for free:
  - The PATCH body field is `stickyNotes` (camelCase), NOT the `sticky_notes` (snake_case) shown in the published schema. Live-confirmed both shapes; only camelCase is accepted.
  - `tenant_id` in the credit-balance path is *ignored* — server resolves tenant from the JWT. So passing 0 works for everyone.
- Tool count: 40 → 45.

### v0.2.11 — single-input guard in `attach_node`
- `attach_node` now refuses 2+ parents by default. Pre-v0.2.11 it silently created multiple `_default` edges into single-input nodes (HubSpot, Gmail, Sheets, Custom Code, AI — almost everything), producing workflows that looked correct in the UI but failed silently at execution.
- Error message points to `attach_magic_node` (1–5 inputs with `df1..dfN` handles — the right pattern for joins/merges) and special-cases the Magic Node typeId with a more specific hint.
- New `allow_multi_input=True` escape hatch for the legacy Merge block.
- Trigger nodes (0 parents) and the common single-parent case are unaffected.

### v0.2.10 — one-line installer (no git required)
- New `scripts/install.sh` downloads a release tarball via `curl`/`wget`, installs `uv` if missing, and registers the MCP server via `claude mcp add` — for colleagues who don't have `git` set up or who hit GitHub-auth friction.
- Idempotent: re-running the script upgrades in place (wipes install dir, re-fetches, re-registers; MCP config entry stays consistent; in-memory JWT is unaffected because JWTs aren't persisted by design).
- Resolves "latest tag" from GitHub's tags API by default; accepts an explicit `bash -s v0.2.10` arg to pin.
- Falls back gracefully when the `claude` CLI isn't on PATH (prints a JSON snippet to paste into `~/.claude.json` or other MCP-capable clients' configs).

### v0.2.9 — works without `/plugin`
- Launcher is now self-locating: works whether invoked by the Claude Code plugin loader (with `${CLAUDE_PLUGIN_ROOT}` set) OR directly by absolute path from `claude mcp add` / a hand-edited `~/.claude.json`. This unblocks colleagues on older Claude Code builds, locked-down corporate installs, and other MCP-capable clients (Cursor, Windsurf, Continue).
- Documents the [manual install path](#manual-install-no-plugin-slash-command-available) for those environments.
- Launcher also gained an upfront layout check that prints a helpful error if the script is run from a malformed clone (vs the previous silent uv-run failure).

### v0.2.8 — big-workflow support
- **Fixes HTTP 413 on workflows past ~50 blocks.** `attach_node`, `attach_magic_node`, `attach_python_block` no longer re-send the entire workflow on every PUT. New small-payload path: POST `/paste-nodes` for the new block (~2 KB), then per-parent `PUT /nodes/{parent_id}` with the new edge appended (~3 KB each). Live-tested on a 1.1 MB / 58-block workflow.
- Handles the platform's silent UUID-reassignment on paste (the new block's id changes server-side; the helper detects the new id via diff and rewrites all internal self-references before wiring edges).
- ⚠ Known limitation: 11 other mutating tools still use the giant-PUT path and will fail on big workflows: `delete_node`, `update_node_setting`, `update_magic_node`, `update_ai_prompt`, `add_edge`, `remove_edge`, `splice_branch`, `clone_node`, `set_test_mode`, `bulk_set_test_mode`, `set_node_output_schema`. Targeted for v0.2.9.

### v0.2.7 — Scheduler workflows can go live
- `attach_node` now auto-detects `isTrigger` AND `isListener` from the node-definition catalog. Pre-v0.2.7 callers had to remember `is_trigger=True` and there was no way to set `isListener` at all — which left Scheduler-rooted workflows with the misleading "Add a Trigger Node ⚡ to go live" tooltip blocking the live toggle.
- New `is_listener` parameter (defaults to None = auto-detect). Both flags can still be explicitly overridden when the caller knows better.
- Lookup is cached (`lru_cache`) so the catalog is paginated once per typeId per server lifetime.

### v0.2.6 — human-readable dropdown labels
- `attach_node(field_labels={...})` for explicit per-field human labels (e.g. `{"sheetId": "Competitive tracking"}`).
- `attach_node(auto_resolve_labels=True)` (default) automatically calls `/nodes/field-options` for known dropdown fields and resolves IDs to labels — Pipedream-wrapped nodes (Sheets, Gmail, Calendar, Slack) now show real names instead of opaque UUIDs in the UI.
- New `list_field_options(workflow_id, node_id, field_name)` tool for inspection / cascade discovery.
- `connection_id` fields are special-cased to `list_connections()` (the field-options endpoint can't resolve them; circular dependency).

### v0.2.5 — discovery + generic builder
- New generic `attach_node` builder works for ANY node type (Scheduler, Gmail, Calendar, AI, etc.) — not just Magic Node and Custom Code.
- Discovery tools: `list_node_definitions`, `get_node_definition`, `list_connections`, `list_connection_apps`, `list_workflows`, `paste_nodes`, `duplicate_workflow`.
- Fixed `columns_metadata` 422 bug (missing `origin_node_id` was breaking column-rename and downstream-validation paths).

### v0.2.4 — public release
- Restructured as Claude Code marketplace + plugin (this layout).
- Bulletproof launcher script (`bin/run-mcp.sh`) handles missing `uv` gracefully and pins the cache dir outside the per-plugin folder.
- 31 tools, all of the core build / edit / wire / run / test-mode primitives.

---

## Engineering / development

```bash
git clone https://github.com/nurturev-dev/nrev-workflow-mcp ~/Projects/nrev-workflow-mcp
cd ~/Projects/nrev-workflow-mcp
./scripts/setup.sh                                          # installs editable, prints MCP config snippet
pytest -q                                                   # run the test suite (128 tests as of v0.2.8)
./scripts/sync-plugin.sh                                    # mirror src/ → plugins/nrev-wf/mcp/ before release
```

When making changes, the workflow is:
1. Edit `src/nrev_wf_mcp/`
2. Run `pytest -q`
3. Bump version in **three places** (they must agree):
   - `src/nrev_wf_mcp/__init__.py`
   - `.claude-plugin/marketplace.json` (plugin entry's `version` field)
   - `plugins/nrev-wf/.claude-plugin/plugin.json`
4. Run `./scripts/sync-plugin.sh` (mirrors source into the plugin + stamps `plugins/nrev-wf/mcp/pyproject.toml`)
5. Commit, tag (`git tag -a v0.2.x -m "..."`), push (`git push && git push origin v0.2.x`)

End users get the new version via `/plugin update nrev-wf` followed by a Claude Code restart.

---

## License

[Apache 2.0](LICENSE)
