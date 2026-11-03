from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from adaptive_service.collector.nvitop import NvitopManager


class _Database:
    def latest_snapshot(self):
        return {"nodes": [{"alias": "n1", "reachable": True}]}


def _record(generated_at: datetime, used_mib: float, index: int = 0) -> dict:
    return {
        "generated_at": generated_at.isoformat(),
        "node_observed_at": generated_at.isoformat(),
        "gpu_index": index,
        "name": "A100",
        "memory_total_mib": 81920.0,
        "memory_free_mib": 81920.0 - used_mib,
        "utilization_percent": 0,
    }


def _fill(manager: NvitopManager, used_mib: float, *, index: int = 0) -> None:
    now = datetime.now().astimezone()
    manager._history["n1"] = deque(
        _record(now - timedelta(seconds=offset), used_mib, index)
        for offset in (31, 25, 20, 15, 10, 5, 0)
    )


def test_nvitop_manager_keeps_polling_after_a_transient_failure(tmp_path: Path):
    async def scenario():
        manager = NvitopManager(tmp_path, refresh_s=0.01)
        calls = []

        async def fake_fetch(node):
            calls.append(node["alias"])
            if len(calls) == 1:
                raise OSError("temporary failure")
            return {"observed_at": "2026-01-01T00:00:00+08:00", "devices": []}

        manager._fetch = fake_fetch
        await manager.start(_Database())
        for _ in range(100):
            if manager.cached("n1") is not None:
                break
            await asyncio.sleep(0.01)
        assert len(calls) >= 2
        assert manager.cached("n1") is not None
        assert manager.error("n1") is None
        await manager.stop()

    asyncio.run(scenario())


def test_states_returns_idle_when_window_stays_below_limit(tmp_path: Path):
    manager = NvitopManager(tmp_path)
    _fill(manager, 1024)

    result = manager.states("n1", window_s=30, max_memory_mib=1024)

    assert result["gpus"] == [{"index": 0, "state": "idle"}]


def test_states_returns_occupied_when_one_sample_exceeds_limit(tmp_path: Path):
    manager = NvitopManager(tmp_path)
    now = datetime.now().astimezone()
    manager._history["n1"] = deque(
        [
            _record(now - timedelta(seconds=31), 100),
            _record(now - timedelta(seconds=20), 100),
            _record(now - timedelta(seconds=10), 2048),
            _record(now, 100),
        ]
    )

    result = manager.states("n1", window_s=30, max_memory_mib=1024)

    assert result["gpus"] == [{"index": 0, "state": "occupied"}]


def test_states_uses_latest_sample_before_a_full_window(tmp_path: Path):
    manager = NvitopManager(tmp_path)
    now = datetime.now().astimezone()
    manager._history["n1"] = deque(
        _record(now - timedelta(seconds=offset), 100) for offset in (10, 5, 0)
    )

    result = manager.states("n1", window_s=30, max_memory_mib=1024)

    assert result["gpus"] == [{"index": 0, "state": "idle"}]
