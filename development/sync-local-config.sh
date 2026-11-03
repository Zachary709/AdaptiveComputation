#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
config_repo=${ADAPTIVE_CONFIG_REPO:-https://github.com/Zachary709/AdaptiveComputation-config.git}
cache_dir=$repo_root/.local-config

if [ -d "$cache_dir/.git" ]; then
    git -C "$cache_dir" pull --ff-only
else
    git clone "$config_repo" "$cache_dir"
fi

install -m 600 "$cache_dir/service/local.json" "$repo_root/service/local.json"
install -m 600 "$cache_dir/skill/local.json" "$repo_root/skill/local.json"
printf 'synced %s\n' "$cache_dir"
