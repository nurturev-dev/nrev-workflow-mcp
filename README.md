# nrev-workflow-mcp

A Claude Code marketplace + plugin from NurtureV that exposes the nRev workflow API as **31 MCP tools** — build, debug, and operate workflows from inside any Claude session.

Internal tool. Auth is JWT-only, per-user, never stored.

---

## Install (for everyone — delivery team, ops, anyone using Claude Code)

In any Claude Code session:

```
/plugin marketplace add nurturev-dev/nrev-workflow-mcp
/plugin install nrev-wf@nrev
```

Restart Claude Code. Run `/mcp` and you should see `nrev-wf` with 31 tools.

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

### Update

```
/plugin update nrev-wf
```

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

## Tools (31)

| Group | Tools |
|---|---|
| **Auth** | `set_jwt`, `get_auth_status` |
| **Read** | `get_workflow`, `get_node`, `get_workflow_graph`, `list_node_settings`, `get_node_neighbors`, `trace_path` |
| **Validate** | `validate_workflow`, `validate_custom_code` |
| **Build** | `create_workflow`, `attach_magic_node`, `attach_python_block`, `clone_node` |
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

## Engineering / development

```bash
git clone https://github.com/nurturev-dev/nrev-workflow-mcp ~/Projects/nrev-workflow-mcp
cd ~/Projects/nrev-workflow-mcp
./scripts/setup.sh                                          # installs editable, prints MCP config snippet
pytest -q                                                   # run the test suite (81 tests)
./scripts/sync-plugin.sh                                    # mirror src/ → plugins/nrev-wf/mcp/ before release
```

When making changes, the workflow is:
1. Edit `src/nrev_wf_mcp/`
2. Run `pytest -q`
3. Bump `src/nrev_wf_mcp/__init__.py` version
4. Run `./scripts/sync-plugin.sh` (mirrors the source into the plugin)
5. Update `plugins/nrev-wf/.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` versions
6. Commit, tag (`git tag v0.2.x`), push (`git push && git push --tags`)

End users get the new version via `/plugin update nrev-wf`.

---

## License

[Apache 2.0](LICENSE)
