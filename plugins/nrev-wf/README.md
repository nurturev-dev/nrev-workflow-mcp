# nrev-wf — Claude Code plugin

40 tools for building, debugging, and operating nRev workflows from inside Claude. Internal NurtureV tool — auth is per-user JWT (paste once per Claude session, never stored).

Current version: **v0.2.9** — see release notes in the [repo README](https://github.com/nurturev-dev/nrev-workflow-mcp#release-notes).

## Install

In any Claude Code session:

```
/plugin marketplace add nurturev-dev/nrev-workflow-mcp
/plugin install nrev-wf@nrev
```

Restart Claude Code. Run `/mcp` — you should see `nrev-wf` connected with 40 tools.

### Prerequisites (one-time)

This plugin runs a small Python MCP server in the background. You need:

1. **Python 3.10 or later** — most macOS systems have this; check with `python3 --version`
2. **`uv`** (a fast Python package manager) — install with one command:
   ```
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
   Restart your terminal after installing.

`uv` handles all Python dependency installation automatically the first time the plugin runs. No `pip install` needed.

## First-session use

Once per Claude session, paste a fresh JWT:

1. Go to `app.nrev.ai` → DevTools → Network → copy the `Authorization` header from any request (the part after `Bearer `).
2. In Claude: *"Set my nrev workflow JWT to `eyJhbGc...`"*

JWTs last 12 hours. They live in the plugin's process memory only — never written to disk, never sent anywhere except the nRev API. Re-paste after Claude restart.

## What you can do

Some example prompts:

- *"Render workflow `<wf_id>` as a mermaid graph"* → `get_workflow_graph`
- *"What failed in the last execution of `<wf_id>`?"* → `get_execution`
- *"Show me the actual rows from node X in that run, just the `connection_note` column"* → `get_node_output(columns=...)`
- *"Clone the AI node, swap the prompt, run it in test mode"* → `clone_node` + `partial_execute`
- *"Cap all paid nodes at 5 rows before this run"* → `bulk_set_test_mode`

## Tool surface (40 tools)

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

## Update

```
/plugin update nrev-wf
```

Then **fully quit and reopen Claude Code** so the MCP server respawns under the new version. If `/plugin update` doesn't pick up the new version, force-refresh the marketplace cache first:

```
/plugin marketplace update nrev
/plugin update nrev-wf
```

## No `/plugin` slash command in your environment?

Older Claude Code builds, locked-down corporate installs, and several other MCP-capable clients (Cursor, Windsurf, Continue) don't expose `/plugin` or `/mcp`. The plugin still works — register the MCP server manually:

```bash
git clone https://github.com/nurturev-dev/nrev-workflow-mcp ~/Projects/nrev-workflow-mcp

# Then in a terminal (works even when /plugin doesn't):
claude mcp add nrev-wf --scope user -- \
  ~/Projects/nrev-workflow-mcp/plugins/nrev-wf/bin/run-mcp.sh
```

Full instructions including a JSON-config fallback (for non-Claude-Code clients): see [`Manual install`](https://github.com/nurturev-dev/nrev-workflow-mcp#manual-install-no-plugin-slash-command-available) in the repo README.

To update a manual install: `cd ~/Projects/nrev-workflow-mcp && git pull`, then restart Claude Code.

## Troubleshooting

**`/mcp` shows `nrev-wf` as failed** — almost always missing `uv`. Run `which uv` in a terminal:
- Empty output → install uv with `curl -LsSf https://astral.sh/uv/install.sh | sh`, restart Claude Code.
- Got a path → check `python3 --version` is 3.10+. If older, install a newer Python (e.g. via Homebrew: `brew install python@3.12`).

**Tool calls return "JWT not set"** — your previous JWT expired or this is a fresh Claude session. Paste a new one.

**Tool calls return HTTP 4xx** — JWT is invalid or expired. Get a fresh one from app.nrev.ai DevTools.

## Source

Repo (public): https://github.com/nurturev-dev/nrev-workflow-mcp
