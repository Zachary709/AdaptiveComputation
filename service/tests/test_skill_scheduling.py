from __future__ import annotations

import pytest

from opensmi.cli import _verify_plan_still_valid
from opensmi.planner import _gpu_eligible, _node_eligible, _node_gpu_devices
from opensmi.snapshot import SnapshotError


def _node(*, work_path: str = "", path: str = "/") -> dict:
    return {
        "alias": "n1",
        "reachable": True,
        "work_path": work_path,
        "ssh": {"user": "u"},
        "hardware": {
            "cpu": {"logical_cores": 8},
            "memory": {"available_bytes": 10**12},
            "disks": [
                {"path": "/", "total_bytes": 100, "available_bytes": 100},
                {"path": "/data", "total_bytes": 1000, "available_bytes": 500},
            ],
            "gpu": {
                "devices": [
                    {
                        "index": 0,
                        "uuid": "GPU-0",
                        "name": "A100",
                        "memory_total_mib": 40960,
                        "memory_free_mib": 40960,
                        "utilization_gpu_percent": 0,
                        "process_count": 0,
                        "processes": [],
                        "available": True,
                    }
                ]
            },
        },
    }


def _task(**values) -> dict:
    return {
        "disk_path": "",
        "min_disk_bytes": 0,
        "min_cpu_cores": 0,
        "min_memory_bytes": 0,
        **values,
    }


def test_gpu_states_mark_devices_idle_or_occupied():
    node = _node()

    idle = _node_gpu_devices(node, {("n1", 0): "idle"})
    occupied = _node_gpu_devices(node, {("n1", 0): "occupied"})
    missing = _node_gpu_devices(node, {})

    assert idle[0]["state"] == "idle"
    assert _gpu_eligible(idle[0], _task())[0]
    assert occupied[0]["state"] == "occupied"
    ok, reason = _gpu_eligible(occupied[0], _task())
    assert not ok
    assert reason == "GPU is occupied"
    assert missing[0]["state"] == "occupied"


def test_work_path_selects_its_mount_for_disk_capacity():
    node = _node(work_path="/data/u/adaptive")

    ok, reason, disk = _node_eligible(node, _task(min_disk_bytes=400))

    assert ok, reason
    assert disk is not None
    assert disk["path"] == "/data"


class _FakeClient:
    def __init__(self, snapshot: dict, allocations=None, states=None):
        self._snapshot = snapshot
        self._allocations = allocations or []
        self._states = states or {}

    def snapshot(self):
        return self._snapshot

    def gpu_state(self, alias):
        return {
            "alias": alias,
            "gpus": [
                {"index": index, "state": state}
                for index, state in sorted(self._states.get(alias, {}).items())
            ],
        }

    def allocations(self):
        return {"allocations": self._allocations}


def _plan() -> dict:
    return {
        "allocation_policy": {"current_user": "alice"},
        "nodes": {"n1": {"work_path": "/data/u/adaptive"}},
        "tasks": [
            {"id": "a", "assignments": [{"node_alias": "n1", "gpu_indices": [0]}]}
        ],
    }


def test_plan_validity_ignores_snapshot_id_change():
    snapshot = {"snapshot_id": 2, "nodes": [_node(work_path="/data/u/adaptive")]}
    client = _FakeClient(snapshot, states={"n1": {0: "idle"}})

    _verify_plan_still_valid(_plan(), client, allow_change=False, dry_run=False)


def test_plan_validity_rejects_busy_gpu():
    snapshot = {"snapshot_id": 2, "nodes": [_node(work_path="/data/u/adaptive")]}
    client = _FakeClient(snapshot, states={"n1": {0: "occupied"}})

    with pytest.raises(SnapshotError, match="occupied"):
        _verify_plan_still_valid(_plan(), client, allow_change=False, dry_run=False)


def test_plan_validity_rejects_foreign_allocation():
    snapshot = {"snapshot_id": 2, "nodes": [_node(work_path="/data/u/adaptive")]}
    client = _FakeClient(
        snapshot,
        allocations=[{"alias": "n1", "gpu_index": 0, "target": "bob"}],
        states={"n1": {0: "idle"}},
    )

    with pytest.raises(SnapshotError, match="allocated to bob"):
        _verify_plan_still_valid(_plan(), client, allow_change=False, dry_run=False)


def test_plan_validity_rejects_changed_work_path():
    snapshot = {"snapshot_id": 2, "nodes": [_node(work_path="/other/u/adaptive")]}
    client = _FakeClient(snapshot, states={"n1": {0: "idle"}})

    with pytest.raises(SnapshotError, match="work path"):
        _verify_plan_still_valid(_plan(), client, allow_change=False, dry_run=False)
