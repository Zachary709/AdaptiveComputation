from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

router = APIRouter(tags=["events"])


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    async def stream():
        last = object()
        while True:
            if await request.is_disconnected():
                return
            payload = request.app.state.db.latest_snapshot()
            snapshot_id = payload.get("snapshot_id") if payload else None
            if snapshot_id != last:
                last = snapshot_id
                body = {
                    "snapshot_id": snapshot_id,
                    "generated_at": payload.get("generated_at") if payload else None,
                }
                yield f"event: snapshot\ndata: {json.dumps(body, ensure_ascii=False)}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
