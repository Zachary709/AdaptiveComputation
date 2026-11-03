from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query, Request

from .common import latest_snapshot

router = APIRouter(tags=["inventory"])


@router.get("/snapshot")
def current_snapshot(request: Request) -> Dict[str, Any]:
    return latest_snapshot(request)


@router.get("/snapshots")
def snapshot_history(
    request: Request,
    limit: int = Query(default=60, ge=1, le=500),
) -> Dict[str, Any]:
    return {"snapshots": request.app.state.db.snapshot_history(limit=limit)}


@router.get("/snapshot/{snapshot_id}")
def snapshot_by_id(snapshot_id: int, request: Request) -> Dict[str, Any]:
    payload = request.app.state.db.snapshot(snapshot_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"unknown snapshot: {snapshot_id}")
    return payload


@router.post("/inventory/refresh")
async def refresh_inventory(request: Request) -> Dict[str, Any]:
    return request.app.state.collector.start_refresh(actor="anonymous")


@router.get("/jobs/{job_id}")
def job_status(job_id: str, request: Request) -> Dict[str, Any]:
    job = request.app.state.collector.job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
    return job
