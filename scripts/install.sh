#!/usr/bin/env bash
# nrev-wf MCP — one-line installer.
#
# Targets colleagues who cannot use /plugin (older Claude Code, locked-down
# corporate installs, other MCP-capable clients) AND don't have git set up
# (or just don't want to deal with cloning). Downloads a tagged release
# tarball from GitHub via curl/wget, installs to ~/.nrev-wf-mcp, ensures uv
# is present, and registers the MCP server with `claude mcp add` if
# available — otherwise prints a JSON snippet to paste manually.
#
# Usage (typical):
#   curl -sSL https://raw.githubusercontent.com/nurturev-dev/nrev-workflow-mcp/main/scripts/install.sh | bash
#
# Pin to a specific version:
#   curl -sSL .../install.sh | bash -s v0.2.9
#
# Custom install dir (defaults to ~/.nrev-wf-mcp):
#   NREV_WF_INSTALL_DIR=/opt/nrev-wf curl -sSL .../install.sh | bash
#
# Re-running the script upgrades in place (wipes the install dir, fetches
# fresh, re-registers the MCP server).

set -euo pipefail

REPO="nurturev-dev/nrev-workflow-mcp"
INSTALL_DIR="${NREV_WF_INSTALL_DIR:-$HOME/.nrev-wf-mcp}"
REQUESTED_VERSION="${1:-}"

# ─── helpers ────────────────────────────────────────────────────────────

info() { printf '\033[36m• %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

fetch() {
  # fetch URL → stdout (writes the body, errors to stderr)
  local url="$1"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- "$url"
  else
    die "Need \`curl\` or \`wget\` installed."
  fi
}

# ─── 1. Resolve the version to install ──────────────────────────────────

if [ -n "$REQUESTED_VERSION" ]; then
  VERSION="$REQUESTED_VERSION"
  info "Using requested version: $VERSION"
else
  info "Looking up latest release tag on GitHub..."
  # Grab the first "name" entry from the tags API. Works without auth on
  # public repos. If the request fails we surface that and exit rather than
  # silently installing some unknown default.
  VERSION="$(
    fetch "https://api.github.com/repos/$REPO/tags" \
    | sed -n 's/.*"name": *"\([^"]*\)".*/\1/p' \
    | head -n1
  )"
  [ -n "$VERSION" ] || die "Could not determine latest tag. Pass a version explicitly: install.sh v0.2.9"
  ok "Latest: $VERSION"
fi

TARBALL_URL="https://github.com/$REPO/archive/refs/tags/$VERSION.tar.gz"

# ─── 2. Download + extract ──────────────────────────────────────────────

info "Installing to: $INSTALL_DIR"
if [ -d "$INSTALL_DIR" ]; then
  warn "Existing install found — replacing it (your MCP config and JWT are unaffected)."
  rm -rf "$INSTALL_DIR"
fi
mkdir -p "$INSTALL_DIR"

info "Downloading $VERSION from GitHub..."
# --strip-components=1 drops the top-level "nrev-workflow-mcp-VERSION/" dir
# so $INSTALL_DIR holds the repo contents directly.
if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$TARBALL_URL" | tar -xz -C "$INSTALL_DIR" --strip-components=1
else
  wget -qO- "$TARBALL_URL" | tar -xz -C "$INSTALL_DIR" --strip-components=1
fi

LAUNCHER="$INSTALL_DIR/plugins/nrev-wf/bin/run-mcp.sh"
[ -f "$LAUNCHER" ] || die "Download landed but launcher missing at: $LAUNCHER"
chmod +x "$LAUNCHER"
ok "Downloaded and extracted."

# ─── 3. Ensure uv is installed ──────────────────────────────────────────

if ! command -v uv >/dev/null 2>&1 \
   && [ ! -x "$HOME/.local/bin/uv" ] \
   && [ ! -x "/opt/homebrew/bin/uv" ] \
   && [ ! -x "/usr/local/bin/uv" ]; then
  info "Installing \`uv\` (Python package manager)..."
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  else
    wget -qO- https://astral.sh/uv/install.sh | sh
  fi
  ok "Installed uv."
else
  ok "uv is already installed."
fi

# ─── 4. Register the MCP server ─────────────────────────────────────────

if command -v claude >/dev/null 2>&1; then
  info "Registering nrev-wf with Claude Code..."
  # Remove any prior entry (idempotent — won't error if absent)
  claude mcp remove nrev-wf --scope user 2>/dev/null || true
  claude mcp add nrev-wf --scope user -- "$LAUNCHER"
  ok "Registered. Fully quit and reopen Claude Code to activate."
else
  warn "\`claude\` CLI not on PATH — auto-registration skipped."
  cat <<EOF

  Add this to your MCP client config (e.g. ~/.claude.json):

  {
    "mcpServers": {
      "nrev-wf": {
        "command": "$LAUNCHER"
      }
    }
  }

  Then fully quit and reopen Claude Code (or your MCP-capable client).
EOF
fi

cat <<EOF

────────────────────────────────────────────────────────────────────────
nrev-wf MCP $VERSION installed at $INSTALL_DIR

To upgrade later:
  curl -sSL https://raw.githubusercontent.com/$REPO/main/scripts/install.sh | bash

To uninstall:
  claude mcp remove nrev-wf --scope user
  rm -rf $INSTALL_DIR

First-session use: paste a JWT in Claude with
  "Set my nrev workflow JWT to eyJhbGc..."
(grab it from app.nrev.ai DevTools → Network → any request's Authorization header)
────────────────────────────────────────────────────────────────────────
EOF
