# nrev-workflow-mcp

A Claude Code marketplace + plugin from NurtureV that exposes the nRev workflow API as **40 MCP tools** — build, debug, and operate workflows from inside any Claude session.

Internal tool. Auth is JWT-only, per-user, never stored.

Current version: **v0.2.8** ([release notes](#release-notes)).

---

## Install (for everyone — delivery team, ops, anyone using Claude Code)

In any Claude Code session:

```
/plugin marketplace add nurturev-dev/nrev-workflow-mcp
/plugin install nrev-wf@nrev
```

Restart Claude Code. Run `/mcp` and you should see `nrev-wf` with 40 tools.

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

Then **fully quit and reopen Claude Code** so the MCP server respawns under the new version. Verify with `/mcp` (tool count should be 40 on v0.2.6+).

If `/plugin update` doesn't see the new version, force-refresh the marketplace cache:

```
/plugin marketplace update nrev
/plugin update nrev-wf
```

Then restart.

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

## Tools (40)

| Group | Tools |
|---|---|
| **Auth** | `set_jwt`, `get_auth_status` |
| **Read / inspect** | `get_workflow`, `list_workflows`, `get_node`, `get_workflow_graph`, `list_node_settings`, `get_node_neighbors`, `trace_path` |
| **Discovery** | `list_node_definitions`, `get_node_definition`, `list_connections`, `list_connection_apps`, `list_field_options` |
| **Validate** | `validate_workflow`, `validate_custom_code` |
| **Build** | `create_workflow`, `attach_node`, `attach_magic_node`, `attach_python_block`, `paste_nodes`, `duplicate_workflow`, `clone_node` |
| **Edit** | `update_node_setting`, `update_magic_node`, `update_ai_prompt`, `set_node_output_schema` |
| **Wiring** | `add_edge`, `remove_edge`, `delete_node`, `splice_branch` |
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

Recent versions, newest first. Run `/plugin update nrev-wf` then restart Claude Code to pick up the latest.

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
