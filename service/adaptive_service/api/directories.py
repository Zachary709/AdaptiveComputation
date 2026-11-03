from __future__ import annotations

import json
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from .common import find_node, latest_snapshot

router = APIRouter(tags=["directories"])

DIRECTORY_KINDS = frozenset({"data", "model"})


def _node(alias: str, request: Request) -> Dict[str, Any]:
    node = find_node(latest_snapshot(request), alias)
    if node is None:
        raise HTTPException(status_code=404, detail=f"unknown node: {alias}")
    if node.get("reachable") is False or node.get("stale"):
        raise HTTPException(status_code=502, detail=f"node is unreachable: {alias}")
    return node


@router.get("/nodes/{alias}/directories/{kind}")
async def list_directories(
    alias: str,
    kind: str,
    request: Request,
) -> Dict[str, Any]:
    if kind not in DIRECTORY_KINDS:
        raise HTTPException(status_code=404, detail=f"unknown directory kind: {kind}")
    node = _node(alias, request)
    path = str(node.get(f"{kind}_path") or "").strip()
    if not path:
        raise HTTPException(status_code=404, detail=f"no {kind} path registered: {alias}")
    entries = await request.app.state.directories.list_path(node, path)
    return {"alias": alias, "kind": kind, "path": path, "entries": entries}


@router.get("/nodes/{alias}/uv-cache")
async def uv_cache(
    alias: str,
    request: Request,
) -> Dict[str, Any]:
    node = _node(alias, request)
    try:
        info = await request.app.state.directories.uv_cache(node)
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"alias": alias, **info}
