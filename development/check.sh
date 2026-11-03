#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

printf '%s\n' '== Repository hygiene =='
if git ls-files dist | grep -q .; then
  echo "ERROR: release artifacts under dist/ are tracked." >&2
  git ls-files dist >&2
  exit 2
fi

for forbidden in adaptive.json opensmi.json config.json; do
  if git ls-files --error-unmatch "$forbidden" >/dev/null 2>&1; then
    echo "ERROR: local cluster configuration is tracked: $forbidden" >&2
    exit 2
  fi
done

printf '%s\n' '== Python checks =='
UV_BIN=${UV_BIN:-$(command -v uv || true)}
if [ -z "$UV_BIN" ] && [ -x "$HOME/.local/bin/uv" ]; then
  UV_BIN="$HOME/.local/bin/uv"
fi
if [ -z "$UV_BIN" ]; then
  echo "ERROR: uv is required" >&2
  exit 2
fi
"$UV_BIN" run python -V
"$UV_BIN" run python -m compileall -q skill service
"$UV_BIN" run python -m pytest -q skill/tests service/tests

if [ -d service/frontend/node_modules ]; then
  printf '%s\n' '== Frontend build =='
  npm --prefix service/frontend run build
fi

printf '%s\n' 'All checks passed.'
