from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from test_service_api import _client


def _states(state: str):
    def fake(alias: str, *, window_s: int, max_memory_mib: int):
        return {
            "alias": alias,
            "gpus": [{"index": 0, "state": state}],
        }

    return fake


def _create(
    client: TestClient,
    name: str = "train-plan",
    *,
    devices: list | None = None,
    scope: str = "static",
) -> dict:
    response = client.post(
        "/api/v1/plans",
        json={
            "name": name,
            "owner": "tester",
            "scope": scope,
            "devices": devices if devices is not None else [{"alias": "n1", "gpu_index": 0}],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _find(client: TestClient, plan_id: str) -> dict:
    listing = client.get("/api/v1/plans").json()["plans"]
    return next(item for item in listing if item["plan_id"] == plan_id)


def test_create_plan_lists_devices(tmp_path: Path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        record = _create(client)

        assert record["status"] == "available"
        assert record["owner"] == "tester"
        assert record["scope"] == "static"
        assert record["devices"] == [
            {"alias": "n1", "gpu_index": 0, "gpu_name": "A100"}
        ]

        summary = _find(client, record["plan_id"])
        assert summary["devices"] == record["devices"]

        detail = client.get(f"/api/v1/plans/{record['plan_id']}").json()
        assert detail["scope"] == "static"


def test_scope_all_expands_to_available_devices(tmp_path: Path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        record = _create(client, name="all", scope="all", devices=[])

        assert record["scope"] == "all"
        assert record["devices"] == [
            {"alias": "n1", "gpu_index": 0, "gpu_name": "A100"}
        ]


def test_update_plan_replaces_devices(tmp_path: Path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        record = _create(client)
        updated = client.put(
            f"/api/v1/plans/{record['plan_id']}",
            json={
                "name": "renamed",
                "scope": "static",
                "devices": [{"alias": "n1", "gpu_index": 0}],
            },
        )

        assert updated.status_code == 200, updated.text
        assert updated.json()["name"] == "renamed"
        assert updated.json()["devices"] == record["devices"]

        empty = client.put(
            f"/api/v1/plans/{record['plan_id']}",
            json={"name": "renamed", "scope": "static", "devices": []},
        )
        assert empty.status_code == 422


def test_claim_rejects_occupied_gpu(tmp_path: Path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        monkeypatch.setattr(client.app.state.nvitop, "states", _states("occupied"))
        record = _create(client)

        response = client.post(
            f"/api/v1/plans/{record['plan_id']}/claim",
            json={"target": "tester", "ttl": "1h"},
        )

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["reason"] == "conflict"
        assert detail["conflicts"][0]["reason"] == "occupied"
        assert _find(client, record["plan_id"])["status"] == "available"


def test_claim_reserves_and_release_frees_gpus(tmp_path: Path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        monkeypatch.setattr(client.app.state.nvitop, "states", _states("idle"))
        record = _create(client)
        plan_id = record["plan_id"]

        response = client.post(
            f"/api/v1/plans/{plan_id}/claim",
            json={"target": "tester", "ttl": "1h"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True

        allocations = client.get("/api/v1/allocations").json()["allocations"]
        assert [
            (item["alias"], item["gpu_index"], item["target"]) for item in allocations
        ] == [("n1", 0, "tester")]
        assert _find(client, plan_id)["status"] == "in_use"

        released = client.post(f"/api/v1/plans/{plan_id}/release")
        assert released.status_code == 200
        assert client.get("/api/v1/allocations").json()["allocations"] == []
        assert _find(client, plan_id)["status"] == "available"


def test_claim_rejects_allocation_held_by_another_user(tmp_path: Path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        monkeypatch.setattr(client.app.state.nvitop, "states", _states("idle"))
        record = _create(client)
        client.post(
            "/api/v1/allocations",
            json={"alias": "n1", "gpu_index": 0, "target": "other", "assigned_by": "other"},
        )

        response = client.post(
            f"/api/v1/plans/{record['plan_id']}/claim",
            json={"target": "tester", "ttl": "1h"},
        )

        assert response.status_code == 409
        conflicts = response.json()["detail"]["conflicts"]
        assert conflicts[0]["reason"] == "allocated"
        assert conflicts[0]["holder"] == "other"


def test_pause_resume_and_delete_plan(tmp_path: Path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        monkeypatch.setattr(client.app.state.nvitop, "states", _states("idle"))
        record = _create(client)
        plan_id = record["plan_id"]
        client.post(f"/api/v1/plans/{plan_id}/claim", json={"target": "tester"})

        paused = client.post(f"/api/v1/plans/{plan_id}/pause")
        assert paused.status_code == 200
        assert paused.json()["status"] == "paused"
        assert client.get("/api/v1/allocations").json()["allocations"] == []

        blocked = client.post(
            f"/api/v1/plans/{plan_id}/claim", json={"target": "tester"}
        )
        assert blocked.status_code == 409

        resumed = client.post(f"/api/v1/plans/{plan_id}/resume")
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "available"

        deleted = client.delete(f"/api/v1/plans/{plan_id}")
        assert deleted.status_code == 200
        assert client.get(f"/api/v1/plans/{plan_id}").status_code == 404


def test_default_plans_are_seeded(tmp_path: Path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        listing = client.get("/api/v1/plans").json()["plans"]
        ids = {item["plan_id"] for item in listing}

        assert {
            "default-single",
            "default-large",
            "default-multi",
            "default-all",
        }.issubset(ids)
        all_plan = next(item for item in listing if item["plan_id"] == "default-all")
        assert all_plan["scope"] == "all"
