#!/bin/bash
# One-line installer:
#   curl -fsSL https://raw.githubusercontent.com/tomtan2007/breathing/main/install.sh | bash
# Re-running it updates an existing install (your calibration is kept).
set -e

INSTALL_DIR="${BREATHING_DIR:-$HOME/Applications/Breathing}"
SOURCE_URL="${BREATHING_SOURCE:-https://github.com/tomtan2007/breathing/archive/refs/heads/main.tar.gz}"

if [ "$(uname)" != "Darwin" ]; then
  echo "Breathing only runs on macOS." >&2
  exit 1
fi

if ! python3 -c "import venv" >/dev/null 2>&1; then
  echo "Python 3 isn't set up yet. Run this, finish the install, then re-run the installer:" >&2
  echo "  xcode-select --install" >&2
  exit 1
fi

echo "Installing Breathing into $INSTALL_DIR …"
mkdir -p "$INSTALL_DIR"
curl -fsSL "$SOURCE_URL" | tar xz -C "$INSTALL_DIR" --strip-components 1

"$INSTALL_DIR/setup.sh"
"$INSTALL_DIR/run.sh"

echo
echo "To start Breathing again later, run:"
echo "  $INSTALL_DIR/run.sh"
