from __future__ import annotations

import json
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from adaptive_service.app import create_app
from adaptive_service.collector.service import CollectorSettings


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


def _client(tmp_path: Path, monkeypatch) -> TestClient:
    settings = CollectorSettings(
        state_dir=tmp_path,
        interval_s=0,
        ssh_config=tmp_path / "ssh_config",
    )
    app = create_app(settings, auto_refresh_on_start=False)
    app.state.db.save_snapshot(
        {
            "generated_at": "2026-10-26T15:00:00+09:00",
            "nodes": [node_document()],
            "environment_records": [],
            "allocations": [],
        }
    )

    async def fake_verify(alias: str) -> dict:
        return {"alias": alias, "status": "verified"}

    monkeypatch.setattr(app.state.collector, "verify_environment", fake_verify)
    return TestClient(app)


def _headers(tmp_path: Path, *, write: bool = False) -> dict:
    return {}


def test_environment_report_updates_snapshot(tmp_path: Path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        before = client.get("/api/v1/snapshot", headers=_headers(tmp_path)).json()
        response = client.post(
            "/api/v1/environments",
            headers=_headers(tmp_path, write=True),
            json={
                "alias": "n1",
                "manager": "uv",
                "path": "/data/u/adaptive/uv",
                "version": "uv 0.5.4",
                "project_path": "/data/u/adaptive/projects/demo",
                "python_version": "3.11",
                "uv_version": "uv 0.5.4",
                "lockfile": "/data/u/adaptive/projects/demo/uv.lock",
                "lock_sha256": "0123456789abcdef" * 4,
                "cache_hit": True,
                "check_command": "uv run python -c 'import torch'",
                "check_exit_code": 0,
                "reporter": "session-1",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "reported"
        assert response.json()["uv_version"] == "uv 0.5.4"
        assert response.json()["lockfile"] == "/data/u/adaptive/projects/demo/uv.lock"
        assert response.json()["cache_hit"] is True
        after = client.get("/api/v1/snapshot", headers=_headers(tmp_path)).json()
        assert after["snapshot_id"] != before["snapshot_id"]
        assert after["nodes"][0]["environment_record"]["status"] == "reported"
        assert after["nodes"][0]["software"]["environments"][0]["manager"] == "uv"


def test_environment_report_rejects_failed_check_and_unknown_node(tmp_path: Path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        failed = client.post(
            "/api/v1/environments",
            headers=_headers(tmp_path, write=True),
            json={
                "alias": "n1",
                "manager": "uv",
                "path": "/data/u/adaptive/uv",
                "check_exit_code": 1,
            },
        )
        assert failed.status_code == 422
        unknown = client.post(
            "/api/v1/environments",
            headers=_headers(tmp_path, write=True),
            json={
                "alias": "missing",
                "manager": "uv",
                "path": "/data/u/adaptive/uv",
                "check_exit_code": 0,
            },
        )
        assert unknown.status_code == 404


def test_work_path_registration_updates_snapshot(tmp_path: Path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        before = client.get("/api/v1/snapshot", headers=_headers(tmp_path)).json()
        response = client.post(
            "/api/v1/nodes/n1/work-path",
            headers=_headers(tmp_path, write=True),
            json={"path": "/data/adaptive", "reporter": "session-1"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["path"] == "/data/adaptive"
        assert response.json()["registered_by"] == "session-1"

        detail = client.get(
            "/api/v1/nodes/n1/work-path", headers=_headers(tmp_path)
        ).json()
        assert detail["path"] == "/data/adaptive"

        after = client.get("/api/v1/snapshot", headers=_headers(tmp_path)).json()
        assert after["snapshot_id"] != before["snapshot_id"]
        assert after["nodes"][0]["work_path"] == "/data/adaptive"


def test_work_path_rejects_invalid_or_unmounted_path(tmp_path: Path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        relative = client.post(
            "/api/v1/nodes/n1/work-path",
            headers=_headers(tmp_path, write=True),
            json={"path": "data/adaptive"},
        )
        assert relative.status_code == 422

        unmounted = client.post(
            "/api/v1/nodes/n1/work-path",
            headers=_headers(tmp_path, write=True),
            json={"path": "/scratch/adaptive"},
        )
        assert unmounted.status_code == 422

        missing = client.post(
            "/api/v1/nodes/missing/work-path",
            headers=_headers(tmp_path, write=True),
            json={"path": "/data/adaptive"},
        )
        assert missing.status_code == 404


def test_refresh_job_completes(tmp_path: Path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        async def fake_refresh(actor: str = "service") -> dict:
            return {"snapshot_id": 99, "inserted": False}

        monkeypatch.setattr(client.app.state.collector, "refresh", fake_refresh)
        started = client.post(
            "/api/v1/inventory/refresh",
            headers=_headers(tmp_path, write=True),
        )
        assert started.status_code == 200
        job_id = started.json()["id"]
        deadline = time.time() + 5
        job = None
        while time.time() < deadline:
            job = client.get(f"/api/v1/jobs/{job_id}", headers=_headers(tmp_path)).json()
            if job["status"] != "running":
                break
            time.sleep(0.02)
        assert job is not None
        assert job["status"] == "succeeded"
        assert job["snapshot_id"] == 99


def test_nvitop_unknown_node_returns_404(tmp_path: Path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        response = client.get(
            "/api/v1/nodes/missing/nvitop",
            headers=_headers(tmp_path),
        )
        assert response.status_code == 404


def test_gpu_state_reports_idle_from_recent_samples(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    manager = client.app.state.nvitop
    now = datetime.now().astimezone()
    manager._history["n1"] = deque(
        {
            "generated_at": (now - timedelta(seconds=offset)).isoformat(),
            "node_observed_at": (now - timedelta(seconds=offset)).isoformat(),
            "gpu_index": 0,
            "name": "A100",
            "memory_total_mib": 81920.0,
            "memory_free_mib": 81920.0 - 1024,
            "utilization_percent": 0,
        }
        for offset in (31, 20, 10, 0)
    )

    response = client.get("/api/v1/nodes/n1/gpu-state")

    assert response.status_code == 200
    assert response.json()["gpus"] == [{"index": 0, "state": "idle"}]


def test_gpu_state_without_samples_returns_503(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.get("/api/v1/nodes/n1/gpu-state")

    assert response.status_code == 503
