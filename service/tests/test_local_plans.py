from __future__ import annotations

import json
from pathlib import Path

import pytest

from adaptive_service.local import ServiceLocalError, load_default_plans
from test_service_api import _client


def _write(path: Path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_missing_local_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("ADAPTIVE_SERVICE_LOCAL", str(tmp_path / "local.json"))
    assert load_default_plans() is None


def test_local_file_plans_override_the_builtin_defaults(tmp_path, monkeypatch):
    local = tmp_path / "local.json"
    _write(
        local,
        {
            "default_plans": [
                {
                    "plan_id": "local-only",
                    "name": "本地计划",
                    "scope": "static",
                    "devices": [{"alias": "GPU-01", "gpu_index": 0}],
                }
            ]
        },
    )
    monkeypatch.setenv("ADAPTIVE_SERVICE_LOCAL", str(local))

    with _client(tmp_path, monkeypatch) as client:
        ids = {item["plan_id"] for item in client.get("/api/v1/plans").json()["plans"]}

    assert "local-only" in ids
    assert "default-single" not in ids


def test_malformed_local_file_fails_fast(tmp_path, monkeypatch):
    local = tmp_path / "local.json"
    _write(local, {"default_plans": "not-a-list"})
    monkeypatch.setenv("ADAPTIVE_SERVICE_LOCAL", str(local))

    with pytest.raises(ServiceLocalError):
        load_default_plans()
