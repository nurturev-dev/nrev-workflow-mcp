#!/usr/bin/env bash
# nrev-wf MCP launcher.
#
# Why a script (vs a bare `command: "uv"` in .mcp.json):
#   Claude Code launches MCP servers in a minimal env that often lacks
#   ~/.local/bin or /opt/homebrew/bin on PATH. A bare `uv` lookup fails
#   silently and /mcp just shows "nrev-wf: failed". This script:
#     1. Searches the common install locations for `uv` explicitly
#     2. Prints a useful error if `uv` is missing (instead of silent death)
#     3. Pins the cache dir to a stable location outside the plugin folder
#        (Claude Code can wipe per-plugin dirs on update)

set -euo pipefail

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
  --project "${CLAUDE_PLUGIN_ROOT}/mcp" \
  --cache-dir "$HOME/.cache/nrev-wf-mcp/uv" \
  nrev-wf-mcp "$@"
