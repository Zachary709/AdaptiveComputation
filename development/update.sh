#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

git -C "$repo_root" pull --ff-only
"$repo_root/development/sync-local-config.sh"
