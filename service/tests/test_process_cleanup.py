import asyncio
import hashlib
import os
import shlex
import socket
import sys
from pathlib import Path

import psutil
import pytest

from adaptive_core import processes
from adaptive_core.models import NodeConfig
from adaptive_core.processes import run_managed
from adaptive_core.sshutil import SSHRunError, is_local_node
from adaptive_service.collector.probe import _run_inventory_script
from adaptive_service.collector.service import _default_local_node
from adaptive_service.collector.nvitop import NvitopManager


ROOT = Path(__file__).resolve().parents[2]


def test_managed_command_preserves_large_output_and_stdin():
    async def scenario():
        content = (ROOT / "uv.lock").read_bytes()
        rc, stdout, stderr = await run_managed(["sha256sum"], stdin_bytes=content, timeout_s=20)
        assert rc == 0 and not stderr
        assert stdout.split()[0] == hashlib.sha256(content).hexdigest()
        rc, stdout, stderr = await run_managed(
            [sys.executable, "-c", "import pathlib,sys; sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes())", str(ROOT / "uv.lock")],
            timeout_s=20,
        )
        assert rc == 0 and not stderr
        assert stdout.encode() == content
    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["inventory_timeout", "inventory_cancel", "nvitop_timeout", "nvitop_cancel"])
def test_real_sleep_children_are_reaped_on_timeout_and_cancellation(tmp_path, path):
    async def scenario():
        marker = tmp_path / "child.pid"
        script = f"sleep 120 & child=$!; printf '%s' \"$child\" > {shlex.quote(str(marker))}; wait \"$child\""
        timeout = 20 if path.endswith("cancel") else 2
        if path.startswith("inventory"):
            operation = _run_inventory_script(NodeConfig(alias=socket.gethostname(), address="127.0.0.1", user=""), script, timeout, 1)
            expected = SSHRunError
        else:
            operation = NvitopManager(tmp_path)._run(socket.gethostname(), script, True, timeout)
            expected = RuntimeError
        task = asyncio.create_task(operation)
        child = None
        try:
            deadline = asyncio.get_running_loop().time() + 10
            while not marker.exists() or not marker.read_text():
                assert asyncio.get_running_loop().time() < deadline
                if task.done():
                    await task
                await asyncio.sleep(0.02)
            child = psutil.Process(int(marker.read_text()))
            assert child.is_running()
            if path.endswith("cancel"):
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(expected):
                    await task
            assert not child.is_running(), f"遗留真实命令进程 {child.pid}"
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if child is not None and child.is_running():
                child.kill()
                child.wait(timeout=10)
    asyncio.run(scenario())


def test_cancellation_during_timeout_cleanup_is_preserved(tmp_path):
    async def scenario():
        marker = tmp_path / "command.pid"
        target = str(tmp_path)
        script = f"printf '%s' \"$$\" > {shlex.quote(str(marker))}; exec sleep 120"
        operation = asyncio.create_task(run_managed(["bash", "-c", script], timeout_s=2, target=target))
        child = supervisor = None
        try:
            deadline = asyncio.get_running_loop().time() + 10
            while not marker.exists() or not marker.read_text():
                assert asyncio.get_running_loop().time() < deadline
                if operation.done():
                    await operation
                await asyncio.sleep(0.02)
            child = psutil.Process(int(marker.read_text()))
            supervisor = child.parent()
            assert supervisor is not None
            supervisor.suspend()
            while target not in processes._CLEANING:
                assert asyncio.get_running_loop().time() < deadline
                if operation.done():
                    await operation
                await asyncio.sleep(0.02)
            operation.cancel()
            supervisor.resume()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(operation, timeout=10)
            assert not child.is_running()
            assert not supervisor.is_running()
        finally:
            if supervisor is not None and supervisor.is_running():
                supervisor.resume()
            if not operation.done():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
            if child is not None and child.is_running():
                child.kill()
                child.wait(timeout=10)
    asyncio.run(scenario())


def test_local_network_interfaces_are_recognized_before_collection():
    addresses = {link.address.split("%")[0] for links in psutil.net_if_addrs().values()
                 for link in links if link.family in (socket.AF_INET, socket.AF_INET6)}
    nodes = [NodeConfig(alias=address, address=address, user="") for address in sorted(addresses)]
    assert nodes
    assert all(is_local_node(node) for node in nodes)
    selected = _default_local_node(nodes)
    assert len(selected) == 1
    assert selected[0] in nodes
