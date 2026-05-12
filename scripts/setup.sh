#!/usr/bin/env bash
# nrev-wf-mcp setup script — one-command install for teammates.
#
# What this does:
#   1. Verifies Python 3.10+ is available
#   2. Installs the package in editable mode (pip install -e .)
#   3. Resolves the `nrev-wf-mcp` entrypoint path
#   4. Prints the Claude Code config snippet to paste into ~/.claude.json
#
# It does NOT modify ~/.claude.json — too much risk of clobbering existing
# config. Last step is copy-paste, but the snippet is generated with the
# correct path for your machine.

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
RESET='\033[0m'

say()  { printf "${GREEN}%s${RESET}\n" "$*"; }
warn() { printf "${YELLOW}%s${RESET}\n" "$*"; }
die()  { printf "${RED}%s${RESET}\n" "$*" >&2; exit 1; }

# ─── 1. Python check ───────────────────────────────────────────────────────
PY=$(command -v python3 || true)
[ -n "$PY" ] || die "python3 not found. Install Python 3.10+ first."

PY_VER=$("$PY" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_OK=$("$PY" -c "import sys; print(1 if sys.version_info >= (3,10) else 0)")
[ "$PY_OK" = "1" ] || die "Python 3.10+ required (you have $PY_VER)."
say "✓ Python $PY_VER"

# ─── 2. pip install editable ───────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

say "Installing nrev-wf-mcp in editable mode..."
"$PY" -m pip install -e . >/tmp/nrev-wf-mcp-install.log 2>&1 \
  || { cat /tmp/nrev-wf-mcp-install.log; die "pip install failed — see log above."; }
say "✓ Installed"

# ─── 3. Locate the entrypoint ──────────────────────────────────────────────
ENTRY=$(command -v nrev-wf-mcp || true)
if [ -z "$ENTRY" ]; then
  # Fallback to user-site bin
  USER_BIN="$("$PY" -c "import site; print(site.USER_BASE)" 2>/dev/null)/bin"
  [ -x "$USER_BIN/nrev-wf-mcp" ] && ENTRY="$USER_BIN/nrev-wf-mcp"
fi
[ -n "$ENTRY" ] || die "Could not locate the nrev-wf-mcp entrypoint after install. Check pip output above."
say "✓ Entrypoint: $ENTRY"

# ─── 4. Smoke test: server imports cleanly ─────────────────────────────────
"$PY" -c "from nrev_wf_mcp.server import mcp" 2>/dev/null \
  || die "Server module failed to import — install is broken."
say "✓ Server imports cleanly"

# ─── 5. Print the Claude config snippet ────────────────────────────────────
cat <<EOF

──────────────────────────────────────────────────────────────────────────
  Almost done. Add this to your Claude Code config to register the MCP.
──────────────────────────────────────────────────────────────────────────

The config lives at: ~/.claude.json

Open it, find the "mcpServers" block (if it doesn't exist, create one at
the top level of the JSON), and add this entry alongside any existing
servers:

  "nrev-wf": {
    "type": "stdio",
    "command": "$ENTRY",
    "args": [],
    "env": {}
  }

Example (combined with the typical nrev-lite entry many teammates already have):

  "mcpServers": {
    "nrev-lite": { ...your existing entry... },
    "nrev-wf": {
      "type": "stdio",
      "command": "$ENTRY",
      "args": [],
      "env": {}
    }
  }

Once saved:

  1. Quit Claude Code completely (Cmd+Q) and reopen
  2. Run /mcp inside Claude to confirm nrev-wf shows up with 31 tools
  3. First-session prompt: "set my nrev workflow JWT to <paste-fresh-jwt>"

Done. Happy building.
EOF
