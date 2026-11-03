import json

import pytest

from opensmi.local import (
    LocalProfileError,
    resolve_node_path,
    resolve_server,
)


def _write_profile(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_resolve_server_prefers_flag_then_env_then_profile(tmp_path, monkeypatch):
    profile = tmp_path / "local.json"
    _write_profile(profile, {"server": "https://profile.example"})
    monkeypatch.setenv("ADAPTIVE_LOCAL_PROFILE", str(profile))
    monkeypatch.delenv("ADAPTIVE_SERVER", raising=False)
    assert resolve_server("https://flag.example") == "https://flag.example"
    monkeypatch.setenv("ADAPTIVE_SERVER", "https://env.example")
    assert resolve_server(None) == "https://env.example"
    monkeypatch.delenv("ADAPTIVE_SERVER")
    assert resolve_server(None) == "https://profile.example"


def test_resolve_node_path_uses_profile_and_rejects_missing_entry(tmp_path, monkeypatch):
    profile = tmp_path / "local.json"
    _write_profile(profile, {"nodes": {"GPU-01": {"work": "/data/user01/adaptive"}}})
    monkeypatch.setenv("ADAPTIVE_LOCAL_PROFILE", str(profile))
    assert resolve_node_path("work", "GPU-01", "/explicit/path") == "/explicit/path"
    assert resolve_node_path("work", "GPU-01", None) == "/data/user01/adaptive"
    with pytest.raises(LocalProfileError):
        resolve_node_path("data", "GPU-01", None)


def test_malformed_profile_fails_fast(tmp_path, monkeypatch):
    profile = tmp_path / "local.json"
    profile.write_text("{", encoding="utf-8")
    monkeypatch.setenv("ADAPTIVE_LOCAL_PROFILE", str(profile))
    with pytest.raises(LocalProfileError):
        resolve_server(None)
