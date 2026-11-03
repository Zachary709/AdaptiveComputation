#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
BUILD_DIR="$ROOT_DIR/.work/pyz-build"
OUTPUT="$ROOT_DIR/dist/adaptive-runner"

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR" "$ROOT_DIR/dist"
cp -R "$ROOT_DIR/skill/adaptive_core" "$BUILD_DIR/adaptive_core"
cp -R "$ROOT_DIR/skill/opensmi" "$BUILD_DIR/opensmi"
cp "$ROOT_DIR/development/zipapp_main.py" "$BUILD_DIR/__main__.py"
python3 -m zipapp "$BUILD_DIR" -o "$OUTPUT" -p "/usr/bin/env python3"
chmod +x "$OUTPUT"
echo "Built $OUTPUT"
