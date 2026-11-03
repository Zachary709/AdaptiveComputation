import asyncio
import time
from unittest.mock import patch

import pytest

from adaptive_core.models import NodeConfig, PreflightCheck, PreflightCheckType, PreflightResult
from opensmi.executor import run_preflight_checks

NODE = NodeConfig(alias="gpu01", address="10.0.0.1", user="testuser")


def test_empty_checks_list_raises():
    with pytest.raises(ValueError, match="cannot be empty"):
        asyncio.run(run_preflight_checks([]))


def test_checks_return_results_in_order_with_timestamps():
    async def fake_check(check):
        return PreflightResult(
            check=check,
            passed=True,
            timestamp="2025-01-01T00:00:00Z",
        )

    checks = [
        PreflightCheck(check_type=PreflightCheckType.TMUX_AVAILABLE, node_alias="gpu01"),
        PreflightCheck(
            check_type=PreflightCheckType.COMMAND_SYNTAX,
            node_alias="gpu01",
            command_to_validate="python train.py",
        ),
        PreflightCheck(
            check_type=PreflightCheckType.GPU_AVAILABILITY,
            node_alias="gpu01",
            target_gpu_indices=[0, 1, 2],
        ),
    ]
    with patch("opensmi.executor._run_single_preflight_check", new=fake_check):
        results = asyncio.run(run_preflight_checks(checks))
    assert [item.check.check_type for item in results] == [
        check.check_type for check in checks
    ]
    assert all(item.passed for item in results)
    assert all("T" in (item.timestamp or "") for item in results)


def test_checks_execute_in_parallel():
    async def slow_check(check):
        await asyncio.sleep(0.1)
        return PreflightResult(
            check=check, passed=True, timestamp="2025-01-01T00:00:00Z"
        )

    checks = [
        PreflightCheck(check_type=PreflightCheckType.TMUX_AVAILABLE, node_alias="gpu01"),
        PreflightCheck(
            check_type=PreflightCheckType.COMMAND_SYNTAX,
            node_alias="gpu01",
            command_to_validate="python train.py",
        ),
        PreflightCheck(
            check_type=PreflightCheckType.GPU_AVAILABILITY,
            node_alias="gpu01",
            target_gpu_indices=[0, 1, 2],
        ),
    ]
    with patch("opensmi.executor._run_single_preflight_check", new=slow_check):
        started = time.monotonic()
        results = asyncio.run(run_preflight_checks(checks))
        elapsed = time.monotonic() - started
    assert len(results) == 3
    assert elapsed < 0.3


def test_tmux_and_gpu_checks_report_failures():
    tmux_check = [
        PreflightCheck(
            check_type=PreflightCheckType.TMUX_AVAILABLE,
            node_alias="gpu01",
            node_config=NODE,
        )
    ]
    with patch("shutil.which", return_value="/usr/bin/tmux"):
        results = asyncio.run(run_preflight_checks(tmux_check))
    assert results[0].passed

    with patch("shutil.which", return_value=None), patch(
        "os.path.isfile", return_value=False
    ):
        results = asyncio.run(run_preflight_checks(tmux_check))
    assert not results[0].passed

    gpu_check = [
        PreflightCheck(
            check_type=PreflightCheckType.GPU_AVAILABILITY,
            node_alias="gpu01",
            target_gpu_indices=[0, 1, 2],
        )
    ]
    results = asyncio.run(run_preflight_checks(gpu_check))
    assert not results[0].passed
    assert "Node configuration missing" in (results[0].error_message or "")
