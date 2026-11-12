from __future__ import annotations

import asyncio
import base64
import json
import re
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, Optional
import shlex

from adaptive_core.processes import RUNNER_SOURCE, run_managed

_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_GPU_IDLE = "idle"
_GPU_OCCUPIED = "occupied"
_TIMEOUT_S = 45
_REFRESH_S = 5.0
_SYNC_S = 15.0
_HISTORY_S = 5 * 60.0

_LOCATE_SCRIPT = r"""
NVITOP=$(command -v nvitop 2>/dev/null || true)
if [ -z "$NVITOP" ]; then
  for candidate in "$HOME/miniconda3/bin/nvitop" "$HOME/anaconda3/bin/nvitop" "$HOME/.local/bin/nvitop" /usr/local/bin/nvitop /usr/bin/nvitop; do
    if [ -x "$candidate" ]; then
      NVITOP="$candidate"
      break
    fi
  done
fi
if [ -z "$NVITOP" ]; then
  echo "nvitop is not installed on this node" >&2
  exit 127
fi
PYTHON="$(dirname "$NVITOP")/python"
if [ ! -x "$PYTHON" ]; then
  PYTHON="$(dirname "$NVITOP")/python3"
fi
if [ ! -x "$PYTHON" ]; then
  IFS= read -r SHEBANG < "$NVITOP" || true
  CANDIDATE="${SHEBANG:2}"
  CANDIDATE="${CANDIDATE%% *}"
  if [ "$CANDIDATE" != "/usr/bin/env" ] && [ -x "$CANDIDATE" ]; then
    PYTHON="$CANDIDATE"
  fi
fi
if [ ! -x "$PYTHON" ]; then
  echo "python interpreter for nvitop not found" >&2
  exit 127
fi
"""

_JSON_SCRIPT = r"""
import json
import math
from datetime import datetime

import psutil
from nvitop import Device


def finite(value, digits=2):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(value):
        return 0
    return round(value, digits)


def temperature(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return int(round(value))


def power(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return round(value / 1000.0, 1)


cpu_percent = finite(psutil.cpu_percent(interval=0.2), 1)
memory = psutil.virtual_memory()
memory_total_mib = finite(memory.total / 1024**2)
memory_used_mib = finite((memory.total - memory.available) / 1024**2)


devices = []
for device in Device.all():
    processes = []
    for pid, process in device.processes().items():
        memory = process.gpu_memory()
        processes.append(
            {
                "pid": pid,
                "name": process.name(),
                "user": process.username(),
                "memory_mib": finite(memory / 1024**2) if memory else 0,
                "command": process.command(),
            }
        )
    devices.append(
        {
            "index": device.index,
            "name": device.name(),
            "memory_total_mib": finite(device.memory_total() / 1024**2),
            "memory_used_mib": finite(device.memory_used() / 1024**2),
            "utilization_percent": finite(device.gpu_utilization(), 0),
            "temperature": temperature(device.temperature()),
            "power_w": power(device.power_usage()),
            "processes": processes,
        }
    )
print(
    json.dumps(
        {
            "observed_at": datetime.now().astimezone().isoformat(),
            "cpu_percent": cpu_percent,
            "memory_total_mib": memory_total_mib,
            "memory_used_mib": memory_used_mib,
            "devices": devices,
        },
        ensure_ascii=False,
    )
)
"""

_JSON_PAYLOAD = base64.b64encode(_JSON_SCRIPT.encode("utf-8")).decode("ascii")

_TEXT_SCRIPT = _LOCATE_SCRIPT + '\nCOLUMNS=160 "$NVITOP" --once\n'

_JSON_COMMAND = (
    _LOCATE_SCRIPT
    + f'\n"$PYTHON" -c "import base64; exec(base64.b64decode(\'{_JSON_PAYLOAD}\'))"\n'
)


def is_local(node: Dict[str, Any]) -> bool:
    connection = node.get("connection") or {}
    return str(node.get("channel") or "") == "local" or str(
        connection.get("channel") or ""
    ) == "local"


class NvitopManager:
    def __init__(self, state_dir: Path, refresh_s: float = _REFRESH_S) -> None:
        self._state_dir = Path(state_dir)
        self._refresh_s = refresh_s
        self._control_dir = self._state_dir / "ssh"
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._history: Dict[str, Deque[Dict[str, Any]]] = {}
        self._errors: Dict[str, str] = {}
        self._tasks: Dict[str, asyncio.Task[None]] = {}
        self._nodes: Dict[str, Dict[str, Any]] = {}
        self._supervisor: Optional[asyncio.Task[None]] = None

    def cached(self, alias: str) -> Optional[Dict[str, Any]]:
        payload = self._cache.get(alias)
        if payload is None:
            return None
        history = self._history.get(alias)
        return {**payload, "samples": list(history) if history else []}

    def error(self, alias: str) -> Optional[str]:
        return self._errors.get(alias)

    def states(
        self,
        alias: str,
        *,
        window_s: int,
        max_memory_mib: int,
    ) -> Dict[str, Any]:
        """Return ``idle`` or ``occupied`` for every GPU of a node.

        A GPU is idle only when every nvitop sample inside the window uses at
        most ``max_memory_mib``.  Before a full window exists, for example
        right after the service starts, the most recent sample decides.
        """
        if alias in self._errors:
            raise RuntimeError(self._errors[alias])
        history = self._history.get(alias)
        if not history:
            raise RuntimeError(f"{alias} has no nvitop samples")
        now = datetime.now().astimezone()
        cutoff = now.timestamp() - float(window_s)
        parsed = []
        for sample in history:
            try:
                observed = datetime.fromisoformat(str(sample.get("generated_at") or ""))
            except ValueError:
                continue
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=now.tzinfo)
            parsed.append((observed, sample))
        if not parsed:
            raise RuntimeError(f"{alias} has no nvitop samples")
        samples = [
            (observed, sample)
            for observed, sample in parsed
            if observed.timestamp() >= cutoff
        ]
        if not samples:
            samples = [max(parsed, key=lambda item: item[0])]
        used_by_index: Dict[int, float] = {}
        for _, sample in samples:
            try:
                index = int(sample.get("gpu_index"))
            except (TypeError, ValueError):
                continue
            total = float(sample.get("memory_total_mib") or 0)
            free = float(sample.get("memory_free_mib") or 0)
            used = max(0.0, total - free)
            used_by_index[index] = max(used_by_index.get(index, 0.0), used)
        if not used_by_index:
            raise RuntimeError(f"{alias} has no GPU samples in the last {window_s}s")
        return {
            "alias": alias,
            "observed_at": now.isoformat(),
            "window_s": int(window_s),
            "max_memory_mib": int(max_memory_mib),
            "gpus": [
                {
                    "index": index,
                    "state": _GPU_IDLE
                    if used <= float(max_memory_mib)
                    else _GPU_OCCUPIED,
                }
                for index, used in sorted(used_by_index.items())
            ],
        }

    async def start(self, database: Any) -> None:
        self._control_dir.mkdir(parents=True, exist_ok=True)
        self._supervisor = asyncio.create_task(self._supervise(database))

    async def stop(self) -> None:
        if self._supervisor is not None:
            self._supervisor.cancel()
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        pending = tasks + ([self._supervisor] if self._supervisor is not None else [])
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        self._supervisor = None

    async def _supervise(self, database: Any) -> None:
        while True:
            payload = database.latest_snapshot()
            for node in (payload or {}).get("nodes") or []:
                if node.get("reachable"):
                    self.ensure(node)
            await asyncio.sleep(_SYNC_S)

    def ensure(self, node: Dict[str, Any]) -> None:
        alias = str(node.get("alias") or "")
        if not _ALIAS_PATTERN.fullmatch(alias):
            return
        self._nodes[alias] = node
        current = self._tasks.get(alias)
        if current is not None and not current.done():
            return
        self._tasks[alias] = asyncio.create_task(self._loop(alias))

    async def retry(self, node: Dict[str, Any]) -> Dict[str, Any]:
        alias = str(node["alias"])
        payload = await self._fetch(node)
        self._cache[alias] = payload
        self._append_history(alias, payload)
        self._errors.pop(alias, None)
        self.ensure(node)
        return payload

    async def run_text(self, node: Dict[str, Any]) -> str:
        alias = str(node["alias"])
        return await self._run(alias, _TEXT_SCRIPT, is_local(node))

    async def run_script(
        self, node: Dict[str, Any], script: str, timeout_s: float = _TIMEOUT_S
    ) -> str:
        alias = str(node["alias"])
        return await self._run(alias, script, is_local(node), timeout_s=timeout_s)

    async def _loop(self, alias: str) -> None:
        while True:
            node = self._nodes.get(alias)
            if node is None:
                return
            try:
                payload = await self._fetch(node)
            except (OSError, RuntimeError, json.JSONDecodeError) as exc:
                self._errors[alias] = str(exc)
                self._cache.pop(alias, None)
            else:
                self._cache[alias] = payload
                self._append_history(alias, payload)
                self._errors.pop(alias, None)
            await asyncio.sleep(self._refresh_s)

    def _append_history(self, alias: str, payload: Dict[str, Any]) -> None:
        node_observed = str(payload.get("observed_at") or "")
        received = datetime.now().astimezone()
        history = self._history.setdefault(alias, deque())
        if history and history[-1].get("node_observed_at") == node_observed:
            return
        for device in payload.get("devices") or []:
            total = float(device.get("memory_total_mib") or 0)
            used = float(device.get("memory_used_mib") or 0)
            history.append(
                {
                    "generated_at": received.isoformat(),
                    "node_observed_at": node_observed,
                    "gpu_index": device.get("index"),
                    "name": device.get("name"),
                    "memory_total_mib": total,
                    "memory_free_mib": max(0.0, total - used),
                    "utilization_percent": float(device.get("utilization_percent") or 0),
                }
            )
        latest = received
        while history:
            try:
                oldest = datetime.fromisoformat(str(history[0].get("generated_at")))
            except ValueError:
                history.popleft()
                continue
            if (latest - oldest).total_seconds() <= _HISTORY_S:
                break
            history.popleft()

    async def _fetch(self, node: Dict[str, Any]) -> Dict[str, Any]:
        alias = str(node["alias"])
        raw = await self._run(alias, _JSON_COMMAND, is_local(node))
        payload = json.loads(raw)
        if not isinstance(payload.get("devices"), list):
            raise RuntimeError("nvitop 返回格式错误")
        return {"alias": alias, **payload}

    async def _run(
        self, alias: str, script: str, local: bool, timeout_s: float = _TIMEOUT_S
    ) -> str:
        if local:
            command = None
        else:
            control_path = self._control_dir / "cm-%C"
            command = [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=8",
                "-o",
                "ControlMaster=auto",
                "-o",
                f"ControlPath={control_path}",
                "-o",
                "ControlPersist=600",
                "-o",
                "ServerAliveInterval=5",
                "-o",
                "ServerAliveCountMax=2",
                alias,
                shlex.join(["python3", "-u", "-c", RUNNER_SOURCE]),
            ]
        try:
            rc, stdout, stderr = await run_managed(["bash", "-lc", script], timeout_s=timeout_s,
                                                  target="local" if local else f"ssh:{alias}", transport=command)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"命令在 {timeout_s} 秒内没有返回，进程已清理") from exc
        if rc != 0:
            raise RuntimeError(stderr.strip() or f"命令退出码 {rc}")
        return stdout
