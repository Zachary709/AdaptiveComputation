from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Query, Request

router = APIRouter(tags=["audit"])


@router.get("/audit")
def list_audit(
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
) -> Dict[str, Any]:
    return {"events": request.app.state.db.list_audit(limit=limit)}
