from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple


logger = logging.getLogger(__name__)
_BLOCKED: Dict[str, str] = {}
_CLEANING: Dict[str, set] = {}
RUNNER_SOURCE = Path(__file__).with_name("managed_runner.py").read_text(encoding="utf-8")


class ProcessCleanupError(RuntimeError):
    pass


async def run_managed(
    argv: List[str], *, timeout_s: float, stdin_bytes: Optional[bytes] = None,
    target: str = "local", transport: Optional[List[str]] = None,
) -> Tuple[int, str, str]:
    if target in _BLOCKED:
        raise ProcessCleanupError(_BLOCKED[target])
    while _CLEANING.get(target):
        await asyncio.shield(asyncio.gather(*list(_CLEANING[target])))
    if target in _BLOCKED:
        raise ProcessCleanupError(_BLOCKED[target])
    command = transport or [sys.executable, "-u", "-c", RUNNER_SOURCE]
    proc = await asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True,
    )
    armed = False
    stderr_task = asyncio.create_task(proc.stderr.read())

    async def exchange() -> dict:
        nonlocal armed
        ready = await proc.stdout.readline()
        if not ready:
            await proc.wait()
            detail = (await stderr_task).decode("utf-8", errors="replace").strip()
            raise RuntimeError(detail or f"采集监督器退出，状态 {proc.returncode}")
        if json.loads(ready) != {"ready": True}:
            raise RuntimeError("采集监督器未确认就绪")
        request = {"argv": argv, "timeout": timeout_s,
                   "stdin": base64.b64encode(stdin_bytes).decode("ascii") if stdin_bytes is not None else None}
        armed = True
        proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
        await proc.stdin.drain()
        data = await proc.stdout.read()
        await proc.wait()
        detail = (await stderr_task).decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 or not data:
            raise RuntimeError(detail or f"采集监督器异常退出，状态 {proc.returncode}")
        result = json.loads(data)
        if result.get("cleaned") is not True:
            raise ProcessCleanupError("采集监督器未确认派生进程已退出")
        return result

    communication = asyncio.create_task(exchange())

    async def finish() -> None:
        proc.stdin.close()
        result = None
        try:
            result = await asyncio.wait_for(asyncio.shield(communication), timeout=timeout_s)
        except (asyncio.TimeoutError, RuntimeError, ValueError, OSError):
            pass
        if armed and (result is None or result.get("cleaned") is not True):
            message = f"{target} 采集进程清理未确认，已暂停该目标后续采集"
            _BLOCKED[target] = message
            logger.critical(message)
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        if not communication.done():
            communication.cancel()
        await asyncio.gather(communication, stderr_task, return_exceptions=True)
        if target in _BLOCKED:
            raise ProcessCleanupError(_BLOCKED[target])

    try:
        result = await asyncio.wait_for(asyncio.shield(communication), timeout=timeout_s)
    except BaseException:
        cleanup = asyncio.create_task(finish())
        cleaning = _CLEANING.setdefault(target, set())
        cleaning.add(cleanup)
        try:
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            cleanup.result()
        finally:
            cleaning.discard(cleanup)
            if not cleaning:
                _CLEANING.pop(target, None)
        raise
    finally:
        proc.stdin.close()
    if result["status"] == "timeout":
        raise asyncio.TimeoutError(f"采集命令在 {timeout_s} 秒内没有返回，进程已清理")
    if result["status"] == "cancelled":
        raise RuntimeError("采集命令已取消，进程已清理")
    rc = result.get("returncode")
    if not isinstance(rc, int):
        raise RuntimeError("采集监督器未返回命令退出码")
    return rc, base64.b64decode(result["stdout"], validate=True).decode("utf-8", errors="replace"), base64.b64decode(result["stderr"], validate=True).decode("utf-8", errors="replace")
