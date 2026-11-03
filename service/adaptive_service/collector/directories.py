from __future__ import annotations

import base64
import json
import time
from typing import Any, Dict, List

from .nvitop import NvitopManager

_LIST_SCRIPT = r"""
ROOT=$(printf '%s' '{encoded}' | base64 -d)
if [ ! -d "$ROOT" ]; then
  exit 0
fi
find "$ROOT" -mindepth 1 -maxdepth 1 -type d -print0 2>/dev/null | while IFS= read -r -d '' entry; do
  name=${entry##*/}
  size=$(du -sb -- "$entry" 2>/dev/null | cut -f1)
  encoded=$(printf '%s' "$name" | base64 -w0)
  printf '%s\t%s\n' "${size:-0}" "$encoded"
done
"""

_UV_CACHE_SCRIPT = r"""
WORK_PATH=$(printf '%s' '{encoded}' | base64 -d)
UV_BIN=$(command -v uv 2>/dev/null || true)
if [ -z "$UV_BIN" ] && [ -n "$WORK_PATH" ] && [ -x "$WORK_PATH/envir/tools/uv" ]; then
  UV_BIN="$WORK_PATH/envir/tools/uv"
fi
if [ -z "$UV_BIN" ]; then
  for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" "$HOME/.local/share/uv/uv" /usr/local/bin/uv /usr/bin/uv; do
    if [ -x "$candidate" ]; then
      UV_BIN="$candidate"
      break
    fi
  done
fi
if [ -z "$UV_BIN" ]; then
  exit 0
fi

CACHE_DIR=""
if [ -n "$WORK_PATH" ] && [ -d "$WORK_PATH/envir/cache/uv" ]; then
  CACHE_DIR="$WORK_PATH/envir/cache/uv"
fi
if [ -z "$CACHE_DIR" ]; then
  CACHE_DIR=$("$UV_BIN" cache dir 2>/dev/null)
fi
if [ -z "$CACHE_DIR" ] || [ ! -d "$CACHE_DIR" ]; then
  exit 0
fi
CACHE_SIZE=$(du -sb "$CACHE_DIR" 2>/dev/null | cut -f1)

UV_BIN="$UV_BIN" WORK_PATH="$WORK_PATH" CACHE_DIR="$CACHE_DIR" CACHE_SIZE="${CACHE_SIZE:-0}" "$(command -v python3 2>/dev/null || command -v python 2>/dev/null || echo /usr/bin/python3)" - <<'PY'
import glob
import json
import os
import subprocess
from pathlib import Path

uv_bin = os.environ.get("UV_BIN", "")
work_path = os.environ.get("WORK_PATH", "")
cache_dir = os.environ.get("CACHE_DIR", "")
cache_size = int(os.environ.get("CACHE_SIZE", "0") or 0)

packages = {}
for root in glob.glob(os.path.join(cache_dir, "wheels-v*", "pypi")):
    for name_dir in Path(root).iterdir():
        if not name_dir.is_dir():
            continue
        for version_dir in name_dir.iterdir():
            if version_dir.is_dir():
                version = version_dir.name.split("-", 1)[0]
                packages[(name_dir.name.lower(), version)] = {
                    "name": name_dir.name,
                    "version": version,
                }
for root in glob.glob(os.path.join(cache_dir, "archive-v0", "*")):
    for dist in Path(root).glob("*.dist-info"):
        stem = dist.name[: -len(".dist-info")]
        if "-" not in stem:
            continue
        name, version = stem.rsplit("-", 1)
        packages[(name.lower(), version)] = {"name": name, "version": version}

installed = {}
if work_path:
    patterns = (
        os.path.join(work_path, "envir", "envs", "*", "lib", "python*", "site-packages", "*.dist-info"),
        os.path.join(work_path, "projects", "*", ".venv", "lib", "python*", "site-packages", "*.dist-info"),
        os.path.join(work_path, "*", ".venv", "lib", "python*", "site-packages", "*.dist-info"),
    )
    for pattern in patterns:
        for dist in glob.glob(pattern):
            stem = Path(dist).name[: -len(".dist-info")]
            if "-" not in stem:
                continue
            name, version = stem.rsplit("-", 1)
            installed[(name.lower(), version)] = {"name": name, "version": version}

python_versions = []
if uv_bin:
    def list_pythons(extra_env):
        env = dict(os.environ)
        env["UV_CACHE_DIR"] = cache_dir
        env.update(extra_env)
        result = subprocess.run(
            [uv_bin, "python", "list", "--only-installed"],
            capture_output=True,
            text=True,
            env=env,
        )
        versions = []
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                parts = line.split()
                if not parts or not parts[0].startswith("cpython-"):
                    continue
                version = parts[0].split("-")[1]
                if version not in versions:
                    versions.append(version)
        return versions

    python_versions = list_pythons({})
    if work_path:
        for version in list_pythons(
            {"UV_PYTHON_INSTALL_DIR": os.path.join(work_path, "envir", "python")}
        ):
            if version not in python_versions:
                python_versions.append(version)

locks = []
if uv_bin and work_path:
    seen = set()
    patterns = ("uv.lock", "*/uv.lock", "*/*/uv.lock", "*/*/*/uv.lock")
    for pattern in patterns:
        for lock in sorted(Path(work_path).glob(pattern)):
            project = str(lock.parent)
            if project in seen or len(locks) >= 20:
                continue
            seen.add(project)
            entry = {
                "path": str(lock),
                "project": project,
                "packages": [],
            }
            result = subprocess.run(
                [
                    uv_bin,
                    "export",
                    "--project",
                    project,
                    "--format",
                    "requirements-txt",
                    "--no-hashes",
                    "--no-emit-project",
                ],
                capture_output=True,
                text=True,
                env={**os.environ, "UV_CACHE_DIR": cache_dir},
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    line = line.strip()
                    if not line or line.startswith(("#", "-e", "--")):
                        continue
                    if "==" not in line:
                        continue
                    name, version = line.split("==", 1)
                    entry["packages"].append(
                        {
                            "name": name.strip(),
                            "version": version.split(";")[0].strip(),
                        }
                    )
            else:
                entry["error"] = result.stderr.strip()
            locks.append(entry)

print(
    json.dumps(
        {
            "uv_path": uv_bin,
            "cache_dir": cache_dir,
            "cache_size_bytes": cache_size,
            "python_versions": python_versions,
            "packages": sorted(
                packages.values(),
                key=lambda item: (item["name"].lower(), item["version"]),
            ),
            "installed": sorted(
                installed.values(),
                key=lambda item: (item["name"].lower(), item["version"]),
            ),
            "locks": locks,
        },
        ensure_ascii=False,
    )
)
PY
"""


def _decode_name(value: str) -> str:
    try:
        return base64.b64decode(value).decode("utf-8", errors="replace")
    except ValueError:
        return ""


def parse_directory_output(text: str) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for line in text.splitlines():
        if "\t" not in line:
            continue
        raw_size, encoded = line.split("\t", 1)
        try:
            size_bytes = int(raw_size)
        except ValueError:
            continue
        name = _decode_name(encoded)
        if not name:
            continue
        entries.append({"name": name, "size_bytes": size_bytes})
    entries.sort(key=lambda item: item["name"])
    return entries


class DirectoryLister:
    def __init__(self, runner: NvitopManager, ttl_s: float = 120.0) -> None:
        self._runner = runner
        self._ttl_s = ttl_s
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._json_cache: Dict[str, Dict[str, Any]] = {}

    async def list_path(self, node: Dict[str, Any], path: str) -> List[Dict[str, Any]]:
        encoded = base64.b64encode(path.encode("utf-8")).decode("ascii")
        script = _LIST_SCRIPT.replace("{encoded}", encoded)
        return await self._cached(f"{node['alias']}:path:{path}", node, script)

    async def uv_cache(self, node: Dict[str, Any]) -> Dict[str, Any]:
        key = f"{node['alias']}:uv-cache"
        now = time.monotonic()
        cached = self._json_cache.get(key)
        if cached is not None and now - cached["at"] < self._ttl_s:
            return cached["value"]
        work_path = str(node.get("work_path") or "")
        encoded = base64.b64encode(work_path.encode("utf-8")).decode("ascii")
        script = _UV_CACHE_SCRIPT.replace("{encoded}", encoded)
        text = await self._runner.run_script(node, script, timeout_s=150)
        value = json.loads(text) if text.strip() else {}
        self._json_cache[key] = {"at": now, "value": value}
        return value

    async def _cached(
        self, key: str, node: Dict[str, Any], script: str
    ) -> List[Dict[str, Any]]:
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and now - cached["at"] < self._ttl_s:
            return cached["entries"]
        text = await self._runner.run_script(node, script, timeout_s=120)
        entries = parse_directory_output(text)
        self._cache[key] = {"at": now, "entries": entries}
        return entries
