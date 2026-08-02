#!/bin/sh
# Install dropwatch as a standalone command.
#
# Builds an isolated virtual environment under ~/.local/share/dropwatch,
# copies the application into it, and links the launcher into ~/.local/bin.
# The copy is deliberate: once installed, this source directory is no longer
# needed to run the tool, and moving or deleting it changes nothing.
#
# Safe to re-run — it upgrades an existing install in place.
#
#   ./install.sh              install or upgrade
#   ./install.sh --uninstall  remove it completely

set -eu

APP_HOME="${DROPWATCH_HOME:-$HOME/.local/share/dropwatch}"
BIN_DIR="${DROPWATCH_BIN:-$HOME/.local/bin}"
LAUNCHER="$BIN_DIR/dropwatch"
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
MIN_PYTHON="3.10"

say()  { printf '%s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

# --- uninstall --------------------------------------------------------------

if [ "${1:-}" = "--uninstall" ]; then
    rm -f "$LAUNCHER"
    rm -rf "$APP_HOME"
    say "Removed the application and its launcher."
    say ""
    say "Your settings and cache were left alone. To remove those too:"
    say "  rm -rf ~/.config/dropwatch ~/.local/state/dropwatch"
    exit 0
fi

# --- find a suitable interpreter --------------------------------------------

find_python() {
    for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
        command -v "$candidate" >/dev/null 2>&1 || continue
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

PYTHON="$(find_python)" || fail "Python $MIN_PYTHON or newer is required.
  macOS ships an older one. Install a current version with:
      brew install python"

say "Installing dropwatch"
say "  python:  $PYTHON ($("$PYTHON" -c 'import platform; print(platform.python_version())'))"
say "  app:     $APP_HOME"
say "  command: $LAUNCHER"
say ""

# --- build the environment --------------------------------------------------

# Rebuilt from scratch so an upgrade cannot inherit a broken or stale state.
rm -rf "$APP_HOME"
mkdir -p "$APP_HOME" "$BIN_DIR"

"$PYTHON" -m venv "$APP_HOME/venv" \
    || fail "Could not create a virtual environment at $APP_HOME/venv"

"$APP_HOME/venv/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true

# A plain (non-editable) install copies the code in, so the tool no longer
# depends on this directory existing.
"$APP_HOME/venv/bin/python" -m pip install --quiet "$SOURCE_DIR" \
    || fail "Installation failed"

[ -x "$APP_HOME/venv/bin/dropwatch" ] \
    || fail "The launcher was not created; the package may be misconfigured"

ln -sf "$APP_HOME/venv/bin/dropwatch" "$LAUNCHER"

VERSION="$("$LAUNCHER" --version 2>/dev/null || echo "unknown")"
say "Installed $VERSION"

# --- PATH -------------------------------------------------------------------

case ":${PATH}:" in
    *":${BIN_DIR}:"*) ON_PATH=yes ;;
    *)                ON_PATH=no  ;;
esac

say ""
if [ "$ON_PATH" = yes ]; then
    say "Next:"
    say "  dropwatch setup     enter your Navidrome details"
    say "  dropwatch           list missing releases"
else
    say "$BIN_DIR is not on your PATH. Add it:"
    say ""
    say "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc"
    say "  exec zsh"
    say ""
    say "Then run:  dropwatch setup"
fi
