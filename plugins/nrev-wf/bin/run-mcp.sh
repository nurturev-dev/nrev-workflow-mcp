#!/usr/bin/env bash
# nrev-wf MCP launcher.
#
# Works in two contexts:
#
#   1. Claude Code plugin loader — invoked from .mcp.json with
#      ${CLAUDE_PLUGIN_ROOT} set to the plugin's root. This is what
#      /plugin install does.
#
#   2. Manual MCP config — invoked directly by absolute path from
#      `claude mcp add nrev-wf -- /path/to/run-mcp.sh` (or hand-edited
#      ~/.claude.json). This is the fallback for environments where
#      /plugin isn't available (older Claude Code, locked-down installs,
#      other MCP-capable clients).
#
# Why a script at all (vs a bare `command: "uv"` in the config):
#   Claude Code launches MCP servers in a minimal env that often lacks
#   ~/.local/bin or /opt/homebrew/bin on PATH. A bare `uv` lookup fails
#   silently and the MCP listing just shows "nrev-wf: failed". This script:
#     1. Searches the common install locations for `uv` explicitly
#     2. Prints a useful error if `uv` is missing (instead of silent death)
#     3. Pins the cache dir to a stable location outside the plugin folder
#        (Claude Code can wipe per-plugin dirs on update)
#     4. Self-locates so it works whether or not ${CLAUDE_PLUGIN_ROOT} is set

set -euo pipefail

# Resolve the plugin root. In plugin context the env var is set; in manual
# context we derive it from the script's own location (bin/run-mcp.sh →
# plugin root is one level up).
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ -z "$PLUGIN_ROOT" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi

if [ ! -f "$PLUGIN_ROOT/mcp/pyproject.toml" ]; then
  cat >&2 <<EOF
nrev-wf MCP failed to start: plugin layout looks wrong.
Expected to find: $PLUGIN_ROOT/mcp/pyproject.toml

If you installed manually, point your MCP config at the run-mcp.sh
inside a clean clone of nurturev-dev/nrev-workflow-mcp:
  <repo>/plugins/nrev-wf/bin/run-mcp.sh
EOF
  exit 1
fi

UV="$(command -v uv 2>/dev/null || true)"
if [ -z "$UV" ]; then
  for c in "$HOME/.local/bin/uv" "/opt/homebrew/bin/uv" "/usr/local/bin/uv" "/usr/bin/uv"; do
    if [ -x "$c" ]; then
      UV="$c"
      break
    fi
  done
fi

if [ -z "$UV" ]; then
  cat >&2 <<EOF
nrev-wf MCP failed to start: \`uv\` not found.

Install it once:
  curl -LsSf https://astral.sh/uv/install.sh | sh

Then restart Claude Code.
EOF
  exit 127
fi

exec "$UV" run \
  --quiet \
  --project "$PLUGIN_ROOT/mcp" \
  --cache-dir "$HOME/.cache/nrev-wf-mcp/uv" \
  nrev-wf-mcp "$@"
