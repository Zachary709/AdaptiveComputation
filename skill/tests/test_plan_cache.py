from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from opensmi import cli
from opensmi.client import ServiceError
from opensmi.planner import PlanningError

SNAPSHOT = {
    "snapshot_id": 7,
    "generated_at": "2026-01-01T00:00:00Z",
    "nodes": [
        {
            "alias": "lab-a",
            "reachable": True,
            "stale": False,
            "work_path": "/w",
            "hardware": {
                "gpu": {
                    "count": 2,
                    "devices": [
                        {"index": 0, "name": "RTX 3090"},
                        {"index": 1, "name": "RTX 3090"},
                    ],
                }
            },
        }
    ],
    "policy": {"require_allocation": False, "all_users_token": "*"},
    "allocations": [],
    "environment_records": [],
}


def _plan() -> dict:
    return {
        "tasks": [
            {
                "id": "train",
                "assignments": [
                    {
                        "node_alias": "lab-a",
                        "gpu_indices": [0],
                        "gpus": [{"gpu_index": 0, "gpu_name": "RTX 3090"}],
                        "work_dir": "/w/projects/train",
                    }
                ],
            }
        ],
        "nodes": {"lab-a": {"work_path": "/w", "gpu_count": 1}},
        "inventory_snapshot_id": 7,
        "inventory_generated_at": "2026-01-01T00:00:00Z",
        "allocation_policy": {"current_user": "tester"},
    }


class FakeClient:
    def __init__(self) -> None:
        self.state_value = "idle"
        self.store: dict[str, dict] = {}
        self.claims: list[str] = []
        self.releases: list[str] = []
        self.deletes: list[str] = []
        self.plans_calls = 0

    def add(self, plan_id: str, devices: list, status: str = "available") -> None:
        self.store[plan_id] = {"status": status, "devices": devices}

    def snapshot(self):
        return SNAPSHOT

    def allocations(self):
        return {"allocations": []}

    def gpu_state(self, alias: str):
        return {
            "alias": alias,
            "gpus": [
                {"index": 0, "state": self.state_value},
                {"index": 1, "state": self.state_value},
            ],
        }

    def plans(self, *, status=None, owner=None, manifest_key=None):
        self.plans_calls += 1
        return {
            "plans": [
                {
                    "plan_id": plan_id,
                    "status": record["status"],
                    "devices": record["devices"],
                }
                for plan_id, record in self.store.items()
            ]
        }

    def plan(self, plan_id: str):
        if plan_id not in self.store:
            raise ServiceError("not found", status=404, detail="unknown plan")
        record = self.store[plan_id]
        return {
            "plan_id": plan_id,
            "status": record["status"],
            "devices": record["devices"],
        }

    def claim_plan(self, plan_id: str, *, target: str, ttl: str = "12h", task_id=None):
        if self.state_value != "idle":
            raise ServiceError(
                "conflict",
                status=409,
                detail={
                    "reason": "conflict",
                    "conflicts": [
                        {"alias": "lab-a", "gpu_index": 0, "reason": "occupied"}
                    ],
                },
            )
        self.claims.append(plan_id)
        return {"ok": True, "devices": self.store[plan_id]["devices"]}

    def release_plan(self, plan_id: str):
        self.releases.append(plan_id)
        return {"ok": True}

    def delete_plan(self, plan_id: str):
        self.deletes.append(plan_id)
        return {"ok": True}


def _manifest(tmp_path: Path) -> Path:
    path = tmp_path / "tasks.json"
    path.write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "id": "train",
                        "command": "python train.py",
                        "gpus": 1,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _args(
    manifest: Path,
    *,
    state_dir: Path,
    output: Path | None = None,
    plan_id: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        manifest=str(manifest),
        snapshot=None,
        max_snapshot_age=600,
        ignore_allocations=False,
        strict_allocations=False,
        gpu_state="service",
        user="tester",
        plan_id=plan_id,
        refresh_plan=False,
        artifact_cache=None,
        output=str(output) if output else None,
        task=None,
        mode="direct",
        timeout=10,
        dry_run=False,
        skip_preflight=False,
        no_reserve=True,
        reserve_target=None,
        allocation_ttl="12h",
        json=False,
        server="http://example",
        state_dir=str(state_dir),
    )


def _install(monkeypatch, *, run_code=0, run_reason=None, planning_error=False):
    client = FakeClient()
    builds: list[dict] = []
    runs: list[argparse.Namespace] = []

    monkeypatch.setattr(cli, "_client", lambda args, state_dir: client)
    monkeypatch.setattr(cli, "_resolve_snapshot", lambda args, state_dir: SNAPSHOT)

    def fake_build(client_arg, snapshot, manifest, **kwargs):
        if planning_error:
            raise PlanningError("no device fits")
        builds.append({"snapshot": snapshot, "kwargs": kwargs})
        return _plan()

    monkeypatch.setattr(cli, "_build_plan", fake_build)

    def fake_run(args, *, state_dir):
        runs.append(args)
        payload = {"ok": run_code == 0, "tasks": []}
        if run_reason:
            payload["tasks"] = [
                {"task_id": "train", "status": "failed", "reason": run_reason}
            ]
        Path(args.output).write_text(json.dumps(payload), encoding="utf-8")
        return run_code

    monkeypatch.setattr(cli, "_cmd_run", fake_run)
    return client, builds, runs


def _cache(state_dir: Path) -> dict:
    path = state_dir / "plan-cache.json"
    if not path.is_file():
        return {"entries": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _entry(state_dir: Path, manifest: Path) -> dict:
    key = cli._plan_cache_key(
        json.loads(manifest.read_text(encoding="utf-8")),
        Path.cwd().resolve(),
        "tester",
    )
    return _cache(state_dir)["entries"].get(key, {})


def test_submit_claims_registered_plan_and_caches_id(monkeypatch, tmp_path):
    manifest = _manifest(tmp_path)
    client, builds, runs = _install(monkeypatch)
    client.add("s1", [{"alias": "lab-a", "gpu_index": 0}])

    code = cli._cmd_submit(_args(manifest, state_dir=tmp_path), state_dir=tmp_path)

    assert code == 0
    assert client.claims == ["s1"]
    assert client.releases == ["s1"]
    assert len(runs) == 1
    entry = _entry(tmp_path, manifest)
    assert entry["plan_id"] == "s1"
    assert entry["status"] == "valid"
    assert "server_plan_id" not in entry
    saved = json.loads(Path(runs[0].plan).read_text(encoding="utf-8"))
    assert saved["server_plan_id"] == "s1"
    devices = builds[0]["snapshot"]["nodes"][0]["hardware"]["gpu"]["devices"]
    assert [item["index"] for item in devices] == [0]


def test_submit_reuses_cached_plan_id(monkeypatch, tmp_path):
    manifest = _manifest(tmp_path)
    client, _builds, runs = _install(monkeypatch)
    client.add("s1", [{"alias": "lab-a", "gpu_index": 0}])

    assert cli._cmd_submit(_args(manifest, state_dir=tmp_path), state_dir=tmp_path) == 0
    calls_after_first = client.plans_calls
    assert cli._cmd_submit(_args(manifest, state_dir=tmp_path), state_dir=tmp_path) == 0

    assert client.claims == ["s1", "s1"]
    assert len(runs) == 2
    assert client.plans_calls == calls_after_first


def test_plan_id_option_overrides_cache(monkeypatch, tmp_path):
    manifest = _manifest(tmp_path)
    client, _builds, _runs = _install(monkeypatch)
    client.add("s1", [{"alias": "lab-a", "gpu_index": 0}])
    client.add("s2", [{"alias": "lab-a", "gpu_index": 1}])

    assert cli._cmd_submit(_args(manifest, state_dir=tmp_path), state_dir=tmp_path) == 0
    assert (
        cli._cmd_submit(
            _args(manifest, state_dir=tmp_path, plan_id="s2"), state_dir=tmp_path
        )
        == 0
    )

    assert client.claims == ["s1", "s2"]
    assert _entry(tmp_path, manifest)["plan_id"] == "s2"


def test_busy_cached_plan_falls_back_to_next_plan(monkeypatch, tmp_path):
    manifest = _manifest(tmp_path)
    client, _builds, runs = _install(monkeypatch)
    client.add("s1", [{"alias": "lab-a", "gpu_index": 0}])

    assert cli._cmd_submit(_args(manifest, state_dir=tmp_path), state_dir=tmp_path) == 0
    client.state_value = "occupied"
    client.add("s2", [{"alias": "lab-a", "gpu_index": 1}])

    code = cli._cmd_submit(_args(manifest, state_dir=tmp_path), state_dir=tmp_path)

    assert code == 3
    assert client.claims == ["s1"]
    assert len(runs) == 1
    assert _entry(tmp_path, manifest)["status"] == "invalid"


def test_no_plan_fits_returns_three(monkeypatch, tmp_path):
    manifest = _manifest(tmp_path)
    client, _builds, runs = _install(monkeypatch, planning_error=True)
    client.add("s1", [{"alias": "lab-a", "gpu_index": 0}])

    code = cli._cmd_submit(_args(manifest, state_dir=tmp_path), state_dir=tmp_path)

    assert code == 3
    assert client.claims == ["s1"]
    assert client.releases == ["s1"]
    assert runs == []


def test_failed_execution_invalidates_cache_without_deleting_plan(monkeypatch, tmp_path):
    manifest = _manifest(tmp_path)
    client, _builds, _runs = _install(
        monkeypatch, run_code=2, run_reason="command_failed"
    )
    client.add("s1", [{"alias": "lab-a", "gpu_index": 0}])

    code = cli._cmd_submit(_args(manifest, state_dir=tmp_path), state_dir=tmp_path)

    assert code == 2
    assert client.deletes == []
    entry = _entry(tmp_path, manifest)
    assert entry["status"] == "invalid"
    assert entry["failure"] == "execution failed"


def test_environment_mismatch_keeps_plan_cache(monkeypatch, tmp_path):
    manifest = _manifest(tmp_path)
    client, _builds, _runs = _install(
        monkeypatch, run_code=2, run_reason="environment_mismatch"
    )
    client.add("s1", [{"alias": "lab-a", "gpu_index": 0}])

    code = cli._cmd_submit(_args(manifest, state_dir=tmp_path), state_dir=tmp_path)

    assert code == 2
    assert client.deletes == []
    assert _entry(tmp_path, manifest)["status"] == "valid"


def test_plan_cache_key_tracks_manifest_order_and_source_root(tmp_path):
    manifest = {"tasks": [{"id": "train", "command": "python train.py"}]}
    reordered = {"tasks": [{"command": "python train.py", "id": "train"}]}

    first = cli._plan_cache_key(manifest, tmp_path, "tester")

    assert first == cli._plan_cache_key(reordered, tmp_path, "tester")
    assert first != cli._plan_cache_key(manifest, tmp_path / "other", "tester")
    assert first != cli._plan_cache_key(manifest, tmp_path, "other")


def test_load_plan_cache_ignores_corrupt_file(tmp_path):
    path = tmp_path / "plan-cache.json"
    path.write_text("{not json", encoding="utf-8")

    assert cli._load_plan_cache(path) == {"version": 1, "entries": {}}


def test_submit_rejects_claimed_device_that_became_busy(monkeypatch, tmp_path):
    manifest = _manifest(tmp_path)
    client, builds, runs = _install(monkeypatch)
    client.add("s1", [{"alias": "lab-a", "gpu_index": 0}])
    client.state_value = "occupied"
    monkeypatch.setattr(
        client,
        "claim_plan",
        lambda plan_id, **kwargs: {
            "ok": True,
            "devices": client.store[plan_id]["devices"],
        },
    )

    code = cli._cmd_submit(_args(manifest, state_dir=tmp_path), state_dir=tmp_path)

    assert code == 3
    assert builds == []
    assert runs == []
    assert client.releases == ["s1"]


def test_submit_rejects_busy_device_in_scope_all_plan(monkeypatch, tmp_path):
    manifest = _manifest(tmp_path)
    client, builds, runs = _install(monkeypatch)
    client.add("s1", [{"alias": "lab-a", "gpu_index": 0}])
    client.state_value = "occupied"
    monkeypatch.setattr(
        client,
        "claim_plan",
        lambda plan_id, **kwargs: {
            "ok": True,
            "devices": client.store[plan_id]["devices"],
        },
    )
    monkeypatch.setattr(
        client,
        "plan",
        lambda plan_id: {
            "plan_id": plan_id,
            "scope": "all",
            "status": "available",
            "devices": client.store[plan_id]["devices"],
        },
    )

    code = cli._cmd_submit(_args(manifest, state_dir=tmp_path), state_dir=tmp_path)

    assert code == 3
    assert builds == []
    assert runs == []
    assert client.releases == ["s1"]


def test_submit_rejects_claimed_device_on_unreachable_node(monkeypatch, tmp_path):
    manifest = _manifest(tmp_path)
    client, builds, runs = _install(monkeypatch)
    client.add("s1", [{"alias": "lab-a", "gpu_index": 0}])
    down = {
        **SNAPSHOT,
        "nodes": [{**SNAPSHOT["nodes"][0], "reachable": False}],
    }
    monkeypatch.setattr(cli, "_resolve_snapshot", lambda args, state_dir: down)

    code = cli._cmd_submit(_args(manifest, state_dir=tmp_path), state_dir=tmp_path)

    assert code == 3
    assert builds == []
    assert runs == []
    assert client.releases == ["s1"]


class PlanDetailClient:
    def __init__(self, detail=None) -> None:
        self.detail = detail

    def plan(self, plan_id: str):
        if self.detail is None:
            raise ServiceError("service unreachable", status=503)
        return self.detail


def _claimed_plan(plan_id: str = "s1") -> dict:
    plan = _plan()
    plan["server_plan_id"] = plan_id
    return plan


def test_run_rejects_plan_without_service_claim():
    with pytest.raises(ServiceError):
        cli._verify_plan_source(
            _plan(), PlanDetailClient({}), target="tester", dry_run=False
        )


def test_run_rejects_plan_claimed_by_another_user():
    detail = {
        "plan_id": "s1",
        "claimed_by": "other",
        "devices": [{"alias": "lab-a", "gpu_index": 0}],
    }
    with pytest.raises(ServiceError):
        cli._verify_plan_source(
            _claimed_plan(), PlanDetailClient(detail), target="tester", dry_run=False
        )


def test_run_rejects_devices_outside_claimed_plan():
    detail = {
        "plan_id": "s1",
        "claimed_by": "tester",
        "devices": [{"alias": "lab-a", "gpu_index": 1}],
    }
    with pytest.raises(ServiceError):
        cli._verify_plan_source(
            _claimed_plan(), PlanDetailClient(detail), target="tester", dry_run=False
        )


def test_run_rejects_unreachable_service_for_claimed_plan():
    with pytest.raises(ServiceError):
        cli._verify_plan_source(
            _claimed_plan(), PlanDetailClient(None), target="tester", dry_run=False
        )


def test_run_accepts_plan_inside_claimed_devices():
    detail = {
        "plan_id": "s1",
        "claimed_by": "tester",
        "devices": [
            {"alias": "lab-a", "gpu_index": 0},
            {"alias": "lab-a", "gpu_index": 1},
        ],
    }
    cli._verify_plan_source(
        _claimed_plan(), PlanDetailClient(detail), target="tester", dry_run=False
    )


def test_run_command_rejects_plan_without_service_claim(monkeypatch, tmp_path):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan()), encoding="utf-8")
    monkeypatch.setattr(cli, "_client", lambda args, state_dir: PlanDetailClient({}))
    args = argparse.Namespace(
        plan=str(plan_path),
        artifact_cache=None,
        output=None,
        task=None,
        mode="direct",
        timeout=10,
        dry_run=False,
        skip_preflight=False,
        allow_snapshot_change=False,
        reserve=False,
        reserve_target=None,
        allocation_ttl="12h",
        json=False,
        server="http://example",
    )
    with pytest.raises(ServiceError):
        cli._cmd_run(args, state_dir=tmp_path)

