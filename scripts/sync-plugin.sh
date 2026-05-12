#!/usr/bin/env bash
# Sync src/nrev_wf_mcp/ → plugins/nrev-wf/mcp/nrev_wf_mcp/
#
# The repo serves two install paths:
#   1. Engineering team: pip install -e . from the repo root (uses src/)
#   2. Delivery team:    /plugin install nrev-wf@nrev (uses plugins/nrev-wf/mcp/)
#
# src/ is the source of truth. This script keeps the plugin copy fresh.
# Run before tagging a release. Pre-commit hook optional (see README dev section).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/src/nrev_wf_mcp"
DST="$ROOT/plugins/nrev-wf/mcp/nrev_wf_mcp"

[ -d "$SRC" ] || { echo "ERROR: source dir not found: $SRC" >&2; exit 1; }

mkdir -p "$DST"
# Wipe dst first to catch removed files; then copy fresh
rm -rf "$DST"
cp -R "$SRC" "$DST"

# Strip __pycache__ if any sneaked in
find "$DST" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

# Stamp the plugin pyproject.toml with the current package version
VER="$(grep -E '^__version__' "$SRC/__init__.py" | sed -E 's/.*"([^"]+)".*/\1/')"
[ -n "$VER" ] || { echo "ERROR: could not read __version__ from $SRC/__init__.py" >&2; exit 1; }

PYPROJECT="$ROOT/plugins/nrev-wf/mcp/pyproject.toml"
if [ -f "$PYPROJECT" ]; then
  # Update version line in-place (BSD sed compatible)
  sed -i.bak -E "s/^version = \".*\"/version = \"$VER\"/" "$PYPROJECT" && rm "$PYPROJECT.bak"
  echo "✓ Synced src/ → plugins/nrev-wf/mcp/nrev_wf_mcp/ (version $VER)"
else
  echo "✓ Synced src/ → plugins/nrev-wf/mcp/nrev_wf_mcp/ (no pyproject.toml found to stamp)"
fi
