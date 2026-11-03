from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query, Request

from .common import find_node, latest_snapshot

router = APIRouter(tags=["nodes"])


@router.get("/nodes")
def list_nodes(request: Request) -> Dict[str, Any]:
    payload = latest_snapshot(request)
    return {
        "snapshot_id": payload.get("snapshot_id"),
        "generated_at": payload.get("generated_at"),
        "nodes": payload.get("nodes", []),
    }


@router.get("/nodes/{alias}")
def node_detail(alias: str, request: Request) -> Dict[str, Any]:
    payload = latest_snapshot(request)
    node = find_node(payload, alias)
    if node is None:
        raise HTTPException(status_code=404, detail=f"unknown node: {alias}")
    return {
        "snapshot_id": payload.get("snapshot_id"),
        "generated_at": payload.get("generated_at"),
        "node": node,
        "environment": request.app.state.db.get_environment(alias),
    }


@router.get("/nodes/{alias}/series")
def node_series(
    alias: str,
    request: Request,
    limit: int = Query(default=120, ge=1, le=2000),
) -> Dict[str, Any]:
    payload = latest_snapshot(request)
    if find_node(payload, alias) is None:
        raise HTTPException(status_code=404, detail=f"unknown node: {alias}")
    return {"alias": alias, "samples": request.app.state.db.gpu_series(alias, limit=limit)}
