from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


class SnapshotError(ValueError):
    pass


def load_snapshot(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"cannot read snapshot {path}: {exc}") from exc
    return validate_snapshot(payload)


def validate_snapshot(payload: Mapping[str, Any]) -> Dict[str, Any]:
    nodes = payload.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise SnapshotError("snapshot contains no nodes")
    for node in nodes:
        if not isinstance(node, Mapping) or not node.get("alias"):
            raise SnapshotError("every snapshot node must have an alias")
    document = dict(payload)
    document.setdefault("allocations", [])
    document.setdefault("environment_records", [])
    document.setdefault("policy", {})
    return document


def snapshot_age_seconds(payload: Mapping[str, Any]) -> Optional[float]:
    generated_at = str(payload.get("generated_at") or "").strip()
    if not generated_at:
        return None
    try:
        observed = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()


def require_fresh(payload: Mapping[str, Any], max_age_s: int) -> None:
    if max_age_s <= 0:
        return
    age = snapshot_age_seconds(payload)
    if age is None:
        raise SnapshotError("snapshot has no valid generated_at")
    if age > max_age_s:
        raise SnapshotError(
            f"snapshot is {int(age)}s old, which exceeds the {max_age_s}s limit; refresh inventory first"
        )


def policy_for_plan(payload: Mapping[str, Any]) -> Dict[str, Any]:
    policy = payload.get("policy") if isinstance(payload.get("policy"), Mapping) else {}
    return {
        "require_allocation": bool(policy.get("require_allocation", False)),
        "all_users_token": str(policy.get("all_users_token") or "*"),
    }


def allocations_for_plan(payload: Mapping[str, Any]) -> list:
    raw = payload.get("allocations")
    if not isinstance(raw, list):
        return []
    selected = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        selected.append(
            {
                "node_alias": str(item.get("alias") or item.get("node_alias") or ""),
                "gpu_index": item.get("gpu_index"),
                "target": item.get("target"),
                "expires_at": item.get("expires_at"),
            }
        )
    return selected
