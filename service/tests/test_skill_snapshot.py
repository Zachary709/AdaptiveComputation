from __future__ import annotations

import json
from pathlib import Path

import pytest

from opensmi.cli import _append_outbox, _flush_outbox, main
from opensmi.client import ServiceClient, ServiceError
from opensmi.snapshot import (
    SnapshotError,
    allocations_for_plan,
    load_snapshot,
    policy_for_plan,
    require_fresh,
    validate_snapshot,
)


def snapshot_payload() -> dict:
    return {
        "snapshot_id": 7,
        "generated_at": "2026-10-26T15:00:00+09:00",
        "nodes": [
            {
                "alias": "n1",
                "reachable": True,
                "stale": False,
                "ssh": {"address": "127.0.0.1", "user": "u"},
                "configured": {},
                "hardware": {
                    "cpu": {"logical_cores": 8},
                    "memory": {"available_bytes": 32 * 1024**3},
                    "disks": [{"path": "/data", "available_bytes": 200 * 1024**3}],
                    "gpu": {
                        "devices": [
                            {
                                "index": 0,
                                "uuid": "n1-0",
                                "name": "A100",
                                "memory_total_mib": 81920,
                                "memory_free_mib": 81920,
                                "utilization_gpu_percent": 0,
                                "process_count": 0,
                                "available": True,
                            }
                        ]
                    },
                },
                "software": {"uv": {"installed": True}, "environments": []},
            }
        ],
        "allocations": [
            {"alias": "n1", "gpu_index": 0, "target": "alice", "expires_at": None}
        ],
        "policy": {"require_allocation": True, "all_users_token": "*"},
    }


def test_allocations_and_policy_are_converted_for_planner():
    payload = validate_snapshot(snapshot_payload())

    allocations = allocations_for_plan(payload)
    assert allocations == [
        {"node_alias": "n1", "gpu_index": 0, "target": "alice", "expires_at": None}
    ]
    assert policy_for_plan(payload) == {"require_allocation": True, "all_users_token": "*"}


def test_require_fresh_rejects_old_snapshot():
    payload = dict(snapshot_payload())
    payload["generated_at"] = "2020-01-01T00:00:00+00:00"

    with pytest.raises(SnapshotError):
        require_fresh(payload, 60)
    require_fresh(payload, 0)


def test_plan_from_snapshot_records_snapshot_version(tmp_path: Path):
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps(snapshot_payload()), encoding="utf-8")
    manifest = tmp_path / "tasks.json"
    manifest.write_text(
        json.dumps(
            {
                "tasks": [
                    {"id": "train", "command": "true", "gpus": 1, "uv_env": "uv"}
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "plan.json"

    code = main(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "plan",
            str(manifest),
            "--snapshot",
            str(snapshot),
            "--max-snapshot-age",
            "0",
            "--allow-unallocated",
            "--ignore-allocations",
            "--gpu-state",
            "snapshot",
            "--output",
            str(output),
        ]
    )

    assert code == 0
    plan = json.loads(output.read_text(encoding="utf-8"))
    assert plan["inventory_snapshot_id"] == 7
    assert plan["inventory_generated_at"] == "2026-10-26T15:00:00+09:00"


def test_outbox_keeps_unreported_records(tmp_path: Path):
    _append_outbox(tmp_path, {"alias": "n1", "manager": "uv"})
    client = ServiceClient(server="http://127.0.0.1:1")

    def fail(_record):
        raise ServiceError("offline")

    client.report_environment = fail  # type: ignore[assignment]
    assert _flush_outbox(client, tmp_path) == []
    assert (tmp_path / "outbox.jsonl").is_file()

    submitted = []

    def succeed(record):
        submitted.append(record["alias"])
        return {}

    client.report_environment = succeed  # type: ignore[assignment]
    assert _flush_outbox(client, tmp_path) == ["n1"]
    assert submitted == ["n1"]
    assert not (tmp_path / "outbox.jsonl").exists()
