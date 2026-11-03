from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import HTTPException, Request


def latest_snapshot(request: Request) -> Dict[str, Any]:
    payload = request.app.state.db.latest_snapshot()
    if payload is None:
        raise HTTPException(status_code=503, detail="no inventory snapshot yet")
    return payload


def find_node(payload: Dict[str, Any], alias: str) -> Optional[Dict[str, Any]]:
    for node in payload.get("nodes", []):
        if isinstance(node, dict) and str(node.get("alias") or "") == alias:
            return node
    return None
