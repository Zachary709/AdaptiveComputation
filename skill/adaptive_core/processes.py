from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
from importlib.resources import files
from typing import Dict, List, Optional, Tuple


logger = logging.getLogger(__name__)
_BLOCKED: Dict[str, str] = {}
_CLEANING: Dict[str, set] = {}
RUNNER_SOURCE = files(__name__.rpartition(".")[0]).joinpath("managed_runner.py").read_text(encoding="utf-8")


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

    async def wait_result() -> dict:
        # Python 3.9 的 wait_for 会吞掉与任务完成同时发生的取消。
        done, _ = await asyncio.wait([communication], timeout=timeout_s)
        if not done:
            raise asyncio.TimeoutError()
        return communication.result()

    async def finish() -> None:
        proc.stdin.close()
        result = None
        try:
            result = await wait_result()
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
        result = await wait_result()
    except BaseException as exc:
        cancellation = exc if isinstance(exc, asyncio.CancelledError) else None
        cleanup = asyncio.create_task(finish())
        completion = asyncio.gather(cleanup, return_exceptions=True)
        cleaning = _CLEANING.setdefault(target, set())
        cleaning.add(cleanup)
        try:
            while not completion.done():
                try:
                    await asyncio.shield(completion)
                except asyncio.CancelledError as cancelled:
                    cancellation = cancelled
            # 清理期间收到的取消必须继续向上传播，防止轮询任务重新进入循环。
            if cancellation is not None:
                cleanup_error = completion.result()[0]
                if isinstance(cleanup_error, BaseException):
                    raise cancellation from cleanup_error
                raise cancellation
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
