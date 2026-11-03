from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

ENV_PROFILE = "ADAPTIVE_LOCAL_PROFILE"
PROFILE_NAME = "local.json"
DEFAULT_SERVER = "http://127.0.0.1:8765"
PROFILE_ROOT = Path(__file__).resolve().parent.parent


class LocalProfileError(ValueError):
    """本地私有配置无法读取或缺少必需字段。"""


def profile_path() -> Path:
    raw = os.environ.get(ENV_PROFILE)
    if raw:
        return Path(raw).expanduser().resolve()
    return (PROFILE_ROOT / PROFILE_NAME).resolve()


def load_profile() -> Dict[str, Any]:
    path = profile_path()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LocalProfileError(f"{path} 不是有效的 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise LocalProfileError(f"{path} 的内容必须是 JSON 对象")
    return payload


def resolve_server(server: Optional[str]) -> str:
    if server:
        return server
    env = os.environ.get("ADAPTIVE_SERVER")
    if env:
        return env
    value = str(load_profile().get("server") or "").strip()
    return value or DEFAULT_SERVER


def resolve_node_path(kind: str, alias: str, path: Optional[str]) -> str:
    if path:
        return path
    nodes = load_profile().get("nodes") or {}
    if not isinstance(nodes, Mapping):
        raise LocalProfileError("nodes 必须是 JSON 对象")
    entry = nodes.get(alias) or {}
    if not isinstance(entry, Mapping):
        raise LocalProfileError(f"nodes.{alias} 必须是 JSON 对象")
    value = str(entry.get(kind) or "").strip()
    if not value:
        raise LocalProfileError(
            f"没有给出 --path，本地配置也没有 nodes.{alias}.{kind}"
        )
    return value
