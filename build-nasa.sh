#!/usr/bin/env bash
# build-nasa.sh — build the nasa CLI as a standalone binary
# Usage: ./build-nasa.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "Building nasa binary..."
uv run --dev pyinstaller nasa.spec \
    --distpath dist \
    --workpath /tmp/nasa-build \
    --clean

echo ""
echo "Done: $(ls -lh dist/nasa | awk '{print $5, $9}')"
echo "To install: cp dist/nasa ~/bin/nasa"
