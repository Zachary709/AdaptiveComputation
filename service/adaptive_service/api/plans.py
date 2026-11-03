from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(tags=["plans"])


class PlanDevice(BaseModel):
    alias: str
    gpu_index: int = Field(ge=0)


class PlanWriteRequest(BaseModel):
    name: str = ""
    owner: str = ""
    scope: str = "static"
    devices: List[PlanDevice] = Field(default_factory=list)


class PlanClaimRequest(BaseModel):
    target: str = ""
    ttl: str = "12h"
    task_id: Optional[str] = None


def _expiry(ttl: str) -> str:
    text = str(ttl or "12h").strip()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if len(text) < 2 or text[-1] not in units or not text[:-1].isdigit():
        raise HTTPException(status_code=422, detail=f"invalid ttl: {ttl}")
    seconds = int(text[:-1]) * units[text[-1]]
    expires = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return expires.isoformat().replace("+00:00", "Z")


def _expired(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed < datetime.now(timezone.utc)


def _record(request: Request, plan_id: str) -> Dict[str, Any]:
    record = request.app.state.db.get_plan(plan_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown plan: {plan_id}")
    return record


def _gpu_name(
    nodes: Mapping[str, Mapping[str, Any]], alias: str, index: int
) -> Optional[str]:
    node = nodes.get(alias)
    if not node:
        return None
    hardware = node.get("hardware") if isinstance(node.get("hardware"), Mapping) else {}
    gpu = hardware.get("gpu") if isinstance(hardware.get("gpu"), Mapping) else {}
    for device in gpu.get("devices", []) or []:
        if not isinstance(device, Mapping):
            continue
        try:
            if int(device.get("index")) == index:
                return device.get("name")
        except (TypeError, ValueError):
            continue
    return None


def _snapshot_nodes(request: Request) -> Dict[str, Mapping[str, Any]]:
    payload = request.app.state.db.latest_snapshot()
    if not payload:
        return {}
    return {
        str(node.get("alias") or ""): node
        for node in payload.get("nodes", [])
        if isinstance(node, Mapping)
    }


def expand_devices(
    record: Mapping[str, Any], request: Request
) -> List[Dict[str, Any]]:
    plan = record.get("plan") if isinstance(record.get("plan"), Mapping) else {}
    scope = str(plan.get("scope") or record.get("scope") or "static")
    nodes = _snapshot_nodes(request)
    devices: List[Dict[str, Any]] = []
    if scope == "all":
        for alias, node in nodes.items():
            if not alias or node.get("reachable") is False or node.get("stale"):
                continue
            hardware = node.get("hardware") if isinstance(node.get("hardware"), Mapping) else {}
            gpu = hardware.get("gpu") if isinstance(hardware.get("gpu"), Mapping) else {}
            for device in gpu.get("devices", []) or []:
                if not isinstance(device, Mapping):
                    continue
                try:
                    index = int(device.get("index"))
                except (TypeError, ValueError):
                    continue
                devices.append(
                    {
                        "alias": alias,
                        "gpu_index": index,
                        "gpu_name": device.get("name"),
                    }
                )
    else:
        for device in record.get("devices") or []:
            if not isinstance(device, Mapping):
                continue
            alias = str(device.get("alias") or "")
            try:
                index = int(device.get("gpu_index"))
            except (TypeError, ValueError):
                continue
            devices.append(
                {
                    "alias": alias,
                    "gpu_index": index,
                    "gpu_name": device.get("gpu_name") or _gpu_name(nodes, alias, index),
                }
            )
    devices.sort(key=lambda item: (item["alias"], item["gpu_index"]))
    return devices


def _plan_summary(record: Mapping[str, Any], request: Request) -> Dict[str, Any]:
    plan = record.get("plan") if isinstance(record.get("plan"), Mapping) else {}
    return {
        "plan_id": record.get("plan_id"),
        "name": record.get("name"),
        "owner": record.get("owner"),
        "scope": str(plan.get("scope") or "static"),
        "status": record.get("status"),
        "claimed_by": record.get("claimed_by"),
        "claimed_at": record.get("claimed_at"),
        "expires_at": record.get("expires_at"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "devices": expand_devices(record, request),
    }


def _plan_conflicts(
    devices: List[Mapping[str, Any]], request: Request, target: str
) -> List[Dict[str, Any]]:
    if not devices:
        return []
    nodes = _snapshot_nodes(request)
    if not nodes:
        raise HTTPException(status_code=503, detail="no inventory snapshot yet")
    settings = request.app.state.settings
    allocations: Dict[Any, str] = {}
    for item in request.app.state.db.list_allocations():
        if item.get("expires_at") and _expired(str(item.get("expires_at"))):
            continue
        try:
            key = (str(item.get("alias") or ""), int(item.get("gpu_index")))
        except (TypeError, ValueError):
            continue
        allocations[key] = str(item.get("target") or "")

    conflicts: List[Dict[str, Any]] = []
    states: Dict[str, Optional[Dict[int, str]]] = {}
    for device in devices:
        alias = str(device.get("alias") or "")
        try:
            index = int(device.get("gpu_index"))
        except (TypeError, ValueError):
            conflicts.append({"alias": alias, "reason": "invalid_gpu_index"})
            continue
        node = nodes.get(alias)
        if node is None:
            conflicts.append(
                {"alias": alias, "gpu_index": index, "reason": "missing_node"}
            )
            continue
        if node.get("reachable") is False:
            conflicts.append(
                {"alias": alias, "gpu_index": index, "reason": "unreachable"}
            )
            continue
        if node.get("stale"):
            conflicts.append(
                {"alias": alias, "gpu_index": index, "reason": "stale"}
            )
            continue

        if alias not in states:
            try:
                payload = request.app.state.nvitop.states(
                    alias,
                    window_s=settings.gpu_idle_window_s,
                    max_memory_mib=settings.gpu_idle_max_memory_mib,
                )
            except RuntimeError as exc:
                states[alias] = None
                conflicts.append(
                    {
                        "alias": alias,
                        "gpu_index": index,
                        "reason": "state_unavailable",
                        "detail": str(exc),
                    }
                )
            else:
                states[alias] = {
                    int(item.get("index")): str(item.get("state"))
                    for item in payload.get("gpus", [])
                    if isinstance(item, Mapping)
                }
        gpu_states = states.get(alias)
        if gpu_states is not None and gpu_states.get(index) != "idle":
            conflicts.append(
                {
                    "alias": alias,
                    "gpu_index": index,
                    "reason": "occupied",
                    "detail": f"gpu state {gpu_states.get(index) or 'unknown'}",
                }
            )

        holder = allocations.get((alias, index))
        if holder and holder not in {"*", target}:
            conflicts.append(
                {
                    "alias": alias,
                    "gpu_index": index,
                    "reason": "allocated",
                    "holder": holder,
                }
            )
    return conflicts


@router.post("/plans")
def create_plan(body: PlanWriteRequest, request: Request) -> Dict[str, Any]:
    scope = str(body.scope or "static")
    if scope not in {"static", "all"}:
        raise HTTPException(status_code=422, detail="scope must be static or all")
    devices = [device.model_dump() for device in body.devices]
    if scope == "static" and not devices:
        raise HTTPException(status_code=422, detail="a static plan needs at least one device")
    plan_id = uuid.uuid4().hex[:12]
    owner = str(body.owner or "board")
    name = str(body.name or plan_id)
    record = request.app.state.db.save_plan(
        {
            "plan_id": plan_id,
            "name": name,
            "owner": owner,
            "plan": {"scope": scope},
            "devices": devices,
            "status": "available",
        }
    )
    request.app.state.db.add_audit(
        owner, "plan.create", plan_id, {"name": name, "scope": scope, "devices": devices}
    )
    return _plan_summary(record, request)


@router.put("/plans/{plan_id}")
def update_plan(
    plan_id: str, body: PlanWriteRequest, request: Request
) -> Dict[str, Any]:
    record = _record(request, plan_id)
    scope = str(body.scope or "static")
    if scope not in {"static", "all"}:
        raise HTTPException(status_code=422, detail="scope must be static or all")
    devices = [device.model_dump() for device in body.devices]
    if scope == "static" and not devices:
        raise HTTPException(status_code=422, detail="a static plan needs at least one device")
    if str(record.get("status") or "") == "in_use":
        request.app.state.db.delete_allocations_for_plan(plan_id)
    updated = request.app.state.db.save_plan(
        {
            "plan_id": plan_id,
            "name": str(body.name or record.get("name") or plan_id),
            "owner": str(record.get("owner") or body.owner or "board"),
            "plan": {"scope": scope},
            "devices": devices,
            "status": "available",
            "created_at": record.get("created_at"),
        }
    )
    request.app.state.db.add_audit(
        str(record.get("owner") or "board"),
        "plan.update",
        plan_id,
        {"scope": scope, "devices": devices},
    )
    if record.get("claimed_by"):
        request.app.state.collector.publish_allocations(
            actor=str(record.get("claimed_by"))
        )
    return _plan_summary(updated, request)


@router.get("/plans")
def list_plans(
    request: Request,
    status: Optional[str] = None,
    owner: Optional[str] = None,
) -> Dict[str, Any]:
    records = request.app.state.db.list_plans(
        status=status, owner=owner, include_plan=True
    )
    return {"plans": [_plan_summary(record, request) for record in records]}


@router.get("/plans/{plan_id}")
def get_plan(plan_id: str, request: Request) -> Dict[str, Any]:
    record = _record(request, plan_id)
    return _plan_summary(record, request)


@router.post("/plans/{plan_id}/claim")
def claim_plan(
    plan_id: str, body: PlanClaimRequest, request: Request
) -> Dict[str, Any]:
    record = _record(request, plan_id)
    target = str(body.target or record.get("owner") or "anonymous").strip()
    if not target:
        target = "anonymous"
    status = str(record.get("status") or "")
    claimed_by = str(record.get("claimed_by") or "")
    if status == "paused":
        raise HTTPException(status_code=409, detail={"reason": "paused", "plan_id": plan_id})
    if status == "invalid":
        raise HTTPException(status_code=409, detail={"reason": "invalid", "plan_id": plan_id})
    if status == "in_use" and claimed_by and claimed_by != target:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "in_use",
                "plan_id": plan_id,
                "holder": claimed_by,
                "expires_at": record.get("expires_at"),
            },
        )

    devices = expand_devices(record, request)
    if not devices:
        raise HTTPException(
            status_code=409,
            detail={"reason": "no_devices", "plan_id": plan_id},
        )
    plan_body = record.get("plan") if isinstance(record.get("plan"), Mapping) else {}
    scope = str(plan_body.get("scope") or "static")
    conflicts = _plan_conflicts(devices, request, target)
    if conflicts and scope == "all":
        blocked = {
            (str(item.get("alias") or ""), item.get("gpu_index"))
            for item in conflicts
        }
        devices = [
            device
            for device in devices
            if (device["alias"], device["gpu_index"]) not in blocked
        ]
        if devices:
            conflicts = []
    if conflicts:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "conflict",
                "plan_id": plan_id,
                "conflicts": conflicts,
            },
        )

    expires_at = _expiry(body.ttl)
    for device in devices:
        request.app.state.db.upsert_allocation(
            {
                "alias": str(device.get("alias") or ""),
                "gpu_index": int(device.get("gpu_index") or 0),
                "target": target,
                "assigned_by": target,
                "expires_at": expires_at,
                "notes": f"plan:{plan_id}",
                "plan_id": plan_id,
            }
        )
    request.app.state.db.update_plan(
        plan_id,
        status="in_use",
        claimed_by=target,
        claimed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        expires_at=expires_at,
    )
    request.app.state.db.add_audit(
        target,
        "plan.claim",
        plan_id,
        {"devices": devices, "expires_at": expires_at},
    )
    request.app.state.collector.publish_allocations(actor=target)
    return {
        "ok": True,
        "plan_id": plan_id,
        "target": target,
        "expires_at": expires_at,
        "devices": devices,
    }


@router.post("/plans/{plan_id}/release")
def release_plan(plan_id: str, request: Request) -> Dict[str, Any]:
    record = _record(request, plan_id)
    released = request.app.state.db.delete_allocations_for_plan(plan_id)
    actor = str(record.get("claimed_by") or record.get("owner") or "anonymous")
    request.app.state.db.update_plan(
        plan_id,
        status="available",
        claimed_by="",
        claimed_at=None,
        expires_at=None,
    )
    request.app.state.db.add_audit(
        actor, "plan.release", plan_id, {"released": len(released)}
    )
    request.app.state.collector.publish_allocations(actor=actor)
    return {"ok": True, "plan_id": plan_id, "released": len(released)}


@router.post("/plans/{plan_id}/pause")
def pause_plan(plan_id: str, request: Request) -> Dict[str, Any]:
    record = _record(request, plan_id)
    actor = str(record.get("claimed_by") or record.get("owner") or "anonymous")
    request.app.state.db.delete_allocations_for_plan(plan_id)
    updated = request.app.state.db.update_plan(
        plan_id,
        status="paused",
        claimed_by="",
        claimed_at=None,
        expires_at=None,
    )
    request.app.state.db.add_audit(actor, "plan.pause", plan_id, {})
    request.app.state.collector.publish_allocations(actor=actor)
    return _plan_summary(updated or record, request)


@router.post("/plans/{plan_id}/resume")
def resume_plan(plan_id: str, request: Request) -> Dict[str, Any]:
    record = _record(request, plan_id)
    if str(record.get("status") or "") != "paused":
        raise HTTPException(status_code=409, detail="plan is not paused")
    actor = str(record.get("owner") or "anonymous")
    updated = request.app.state.db.update_plan(plan_id, status="available")
    request.app.state.db.add_audit(actor, "plan.resume", plan_id, {})
    return _plan_summary(updated or record, request)


@router.delete("/plans/{plan_id}")
def delete_plan(plan_id: str, request: Request) -> Dict[str, Any]:
    record = _record(request, plan_id)
    actor = str(record.get("claimed_by") or record.get("owner") or "anonymous")
    released = request.app.state.db.delete_allocations_for_plan(plan_id)
    request.app.state.db.delete_plan(plan_id)
    request.app.state.db.add_audit(
        actor, "plan.delete", plan_id, {"released": len(released)}
    )
    if released:
        request.app.state.collector.publish_allocations(actor=actor)
    return {"ok": True, "plan_id": plan_id, "released": len(released)}
