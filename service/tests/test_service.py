from __future__ import annotations

from pathlib import Path

from adaptive_service.collector.service import attach_environment_records
from adaptive_service.store.db import Database
from opensmi.planner import plan_tasks


def node_document(alias: str = "n1") -> dict:
    return {
        "alias": alias,
        "reachable": True,
        "stale": False,
        "ssh": {"address": "127.0.0.1", "user": "u", "port": 22},
        "configured": {},
        "hardware": {
            "cpu": {"logical_cores": 16},
            "memory": {"available_bytes": 64 * 1024**3},
            "disks": [{"path": "/data", "available_bytes": 500 * 1024**3}],
            "gpu": {
                "devices": [
                    {
                        "index": 0,
                        "uuid": f"{alias}-0",
                        "name": "A100",
                        "memory_total_mib": 81920,
                        "memory_free_mib": 81920,
                        "utilization_gpu_percent": 0,
                        "process_count": 0,
                        "available": True,
                        "processes": [],
                    }
                ]
            },
        },
        "software": {"uv": {"installed": True}, "environments": []},
    }


def test_snapshot_roundtrip_deduplicates_unchanged_nodes(tmp_path: Path):
    database = Database(tmp_path / "service.db")
    database.init_schema()
    document = {
        "schema_version": 1,
        "generated_at": "2026-10-26T15:00:00+09:00",
        "collector": {"name": "test"},
        "nodes": [node_document()],
        "environment_records": [],
        "allocations": [],
    }

    first = database.save_snapshot(document)
    second = database.save_snapshot(document)

    assert first["inserted"] is True
    assert second["inserted"] is False
    assert second["snapshot_id"] == first["snapshot_id"]
    latest = database.latest_snapshot()
    assert latest["snapshot_id"] == first["snapshot_id"]
    assert latest["nodes"][0]["alias"] == "n1"


def test_environment_report_creates_a_new_snapshot_version(tmp_path: Path):
    database = Database(tmp_path / "service.db")
    database.init_schema()
    first = database.save_snapshot(
        {
            "generated_at": "2026-10-26T15:00:00+09:00",
            "nodes": [node_document()],
            "environment_records": [],
            "allocations": [],
        }
    )
    database.upsert_environment(
        {
            "alias": "n1",
            "manager": "uv",
            "path": "/data/u/adaptive/uv",
            "version": "uv 0.5.4",
            "status": "reported",
            "source": "skill",
            "reporter": "session-1",
        }
    )
    records = database.list_environments()
    second = database.save_snapshot(
        {
            "generated_at": "2026-10-26T15:01:00+09:00",
            "nodes": attach_environment_records([node_document()], records),
            "environment_records": records,
            "allocations": [],
        }
    )

    assert second["inserted"] is True
    assert second["snapshot_id"] > first["snapshot_id"]
    latest = database.latest_snapshot()
    assert latest["nodes"][0]["environment_record"]["status"] == "reported"
    assert latest["nodes"][0]["software"]["environments"][0]["source"] == "skill"


def test_reported_environment_is_usable_by_planner(tmp_path: Path):
    database = Database(tmp_path / "service.db")
    database.init_schema()
    database.upsert_environment(
        {
            "alias": "n1",
            "manager": "uv",
            "path": "/data/u/adaptive/uv",
            "version": "uv 0.5.4",
            "status": "reported",
            "source": "skill",
        }
    )
    nodes = attach_environment_records([node_document()], database.list_environments())
    plan = plan_tasks(
        {"nodes": nodes},
        {"tasks": [{"id": "train", "command": "true", "gpus": 1, "uv_env": "uv"}]},
    )

    assert plan["tasks"][0]["assignments"][0]["environment_status"] == "recorded"


def test_conflict_removes_environment_from_planner_view(tmp_path: Path):
    database = Database(tmp_path / "service.db")
    database.init_schema()
    database.upsert_environment(
        {
            "alias": "n1",
            "manager": "uv",
            "path": "/data/u/adaptive/uv",
            "version": "uv 0.5.4",
            "status": "reported",
            "source": "skill",
        }
    )
    database.mark_environment_conflict("n1", {"field": "uv.version"})
    records = database.list_environments()
    nodes = attach_environment_records([node_document()], records)
    plan = plan_tasks(
        {"nodes": nodes},
        {
            "tasks": [
                {
                    "id": "train",
                    "command": "true",
                    "gpus": 1,
                    "uv_env": "uv",
                    "environment_check": "true",
                    "environment_repair_command": "uv sync",
                }
            ]
        },
    )

    assert nodes[0]["software"]["environments"] == []
    assert plan["tasks"][0]["assignments"][0]["environment_status"] == "unverified"


def test_allocations_are_stored_and_returned(tmp_path: Path):
    database = Database(tmp_path / "service.db")
    database.init_schema()
    record = database.upsert_allocation(
        {"alias": "n1", "gpu_index": 0, "target": "alice", "assigned_by": "board"}
    )

    assert record["target"] == "alice"
    assert database.list_allocations()[0]["alias"] == "n1"
    assert database.delete_allocation("n1", 0) is True
    assert database.list_allocations() == []


def test_gpu_series_returns_oldest_first(tmp_path: Path):
    database = Database(tmp_path / "service.db")
    database.init_schema()
    database.save_snapshot(
        {
            "generated_at": "2026-10-26T15:00:00+09:00",
            "nodes": [node_document()],
            "environment_records": [],
            "allocations": [],
        }
    )
    changed = node_document()
    changed["hardware"]["gpu"]["devices"][0]["memory_free_mib"] = 40960
    database.save_snapshot(
        {
            "generated_at": "2026-10-26T15:01:00+09:00",
            "nodes": [changed],
            "environment_records": [],
            "allocations": [],
        }
    )

    series = database.gpu_series("n1", limit=10)
    assert [sample["generated_at"] for sample in series] == [
        "2026-10-26T15:00:00+09:00",
        "2026-10-26T15:01:00+09:00",
    ]
    assert series[1]["memory_free_mib"] == 40960


def test_audit_records_actions(tmp_path: Path):
    database = Database(tmp_path / "service.db")
    database.init_schema()
    database.add_audit("session-1", "environment.report", "n1", {"manager": "uv"})

    events = database.list_audit()
    assert events[0]["action"] == "environment.report"
    assert events[0]["detail"]["manager"] == "uv"
