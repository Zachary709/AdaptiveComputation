from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .common import find_node, latest_snapshot

router = APIRouter(tags=["allocations"])


class AllocationRequest(BaseModel):
    alias: str
    gpu_index: int = Field(ge=0)
    target: str = "*"
    assigned_by: str = ""
    expires_at: Optional[str] = None
    notes: str = ""


@router.get("/allocations")
def list_allocations(request: Request) -> Dict[str, Any]:
    return {"allocations": request.app.state.db.list_allocations()}


@router.post("/allocations")
def upsert_allocation(
    allocation: AllocationRequest,
    request: Request,
) -> Dict[str, Any]:
    actor = "anonymous"
    payload = latest_snapshot(request)
    node = find_node(payload, allocation.alias)
    if node is None:
        raise HTTPException(status_code=404, detail=f"unknown node: {allocation.alias}")
    hardware = node.get("hardware") if isinstance(node.get("hardware"), dict) else {}
    gpu = hardware.get("gpu") if isinstance(hardware.get("gpu"), dict) else {}
    devices = gpu.get("devices") if isinstance(gpu.get("devices"), list) else []
    valid = {int(device.get("index")) for device in devices if isinstance(device, dict) and device.get("index") is not None}
    if valid and int(allocation.gpu_index) not in valid:
        raise HTTPException(status_code=422, detail=f"gpu {allocation.gpu_index} does not exist on {allocation.alias}")
    record = request.app.state.db.upsert_allocation(
        {
            "alias": allocation.alias,
            "gpu_index": allocation.gpu_index,
            "target": allocation.target or "*",
            "assigned_by": allocation.assigned_by or actor,
            "expires_at": allocation.expires_at,
            "notes": allocation.notes,
        }
    )
    request.app.state.db.add_audit(
        allocation.assigned_by or actor,
        "allocation.upsert",
        f"{allocation.alias}:{allocation.gpu_index}",
        {"target": allocation.target, "expires_at": allocation.expires_at},
    )
    request.app.state.collector.publish_allocations(actor=allocation.assigned_by or actor)
    return record


@router.delete("/allocations/{alias}/{gpu_index}")
def delete_allocation(
    alias: str,
    gpu_index: int,
    request: Request,
) -> Dict[str, Any]:
    actor = "anonymous"
    removed = request.app.state.db.delete_allocation(alias, gpu_index)
    if not removed:
        raise HTTPException(status_code=404, detail=f"no allocation for {alias}:{gpu_index}")
    request.app.state.db.add_audit(actor, "allocation.delete", f"{alias}:{gpu_index}", {})
    request.app.state.collector.publish_allocations(actor=actor)
    return {"removed": True, "alias": alias, "gpu_index": gpu_index}
