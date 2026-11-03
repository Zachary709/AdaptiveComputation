from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .api import (
    allocations,
    audit,
    directories,
    environments,
    events,
    inventory,
    nodes,
    nvitop,
    plans,
    work_paths,
)
from .collector.directories import DirectoryLister
from .collector.nvitop import NvitopManager
from .collector.service import CollectorSettings, InventoryCollector
from .store.db import Database


STATIC_ROOT = Path(__file__).resolve().parent / "static"

DEFAULT_PLANS = [
    {
        "plan_id": "default-single",
        "name": "单设备（两张卡）",
        "scope": "static",
        "devices": [
            {"alias": "local-device", "gpu_index": 0},
            {"alias": "local-device", "gpu_index": 1},
        ],
    },
    {
        "plan_id": "default-large",
        "name": "大显存单设备（单卡）",
        "scope": "static",
        "devices": [{"alias": "GPU-01", "gpu_index": 0}],
    },
    {
        "plan_id": "default-multi",
        "name": "两设备（单卡 + 双卡）",
        "scope": "static",
        "devices": [
            {"alias": "GPU-01", "gpu_index": 0},
            {"alias": "GPU-02", "gpu_index": 0},
            {"alias": "GPU-02", "gpu_index": 1},
        ],
    },
    {
        "plan_id": "default-all",
        "name": "全设备",
        "scope": "all",
        "devices": [],
    },
]


def seed_default_plans(database: Database) -> None:
    if database.get_setting("plans_seeded"):
        return
    for item in DEFAULT_PLANS:
        if database.get_plan(item["plan_id"]) is not None:
            continue
        database.save_plan(
            {
                "plan_id": item["plan_id"],
                "name": item["name"],
                "owner": "builtin",
                "plan": {"scope": item["scope"]},
                "devices": item["devices"],
                "status": "available",
            }
        )
    database.set_setting("plans_seeded", "1")


def create_app(
    settings: Optional[CollectorSettings] = None,
    *,
    auto_refresh_on_start: bool = True,
    on_shutdown: Optional[Callable[[], None]] = None,
) -> FastAPI:
    settings = settings or CollectorSettings()
    database = Database(settings.database_path)
    database.init_schema()
    seed_default_plans(database)
    collector = InventoryCollector(database, settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        collector.start_scheduler()
        if auto_refresh_on_start and database.latest_snapshot() is None:
            collector.start_refresh(actor="service")
        await app.state.nvitop.start(database)
        try:
            yield
        finally:
            await collector.stop()
            await app.state.nvitop.stop()
            callback = app.state.on_shutdown
            if callback is not None:
                callback()

    app = FastAPI(title="adaptive resource board", version="0.2.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = database
    app.state.collector = collector
    app.state.nvitop = NvitopManager(settings.state_dir)
    app.state.directories = DirectoryLister(app.state.nvitop)
    app.state.on_shutdown = on_shutdown

    for router in (
        inventory.router,
        nodes.router,
        environments.router,
        work_paths.router,
        allocations.router,
        plans.router,
        audit.router,
        events.router,
        nvitop.router,
        directories.router,
    ):
        app.include_router(router, prefix="/api/v1")

    @app.get("/healthz")
    def healthz() -> Dict[str, Any]:
        latest = database.latest_snapshot()
        return {
            "status": "ok",
            "snapshot_id": latest.get("snapshot_id") if latest else None,
            "generated_at": latest.get("generated_at") if latest else None,
        }

    @app.get("/nvitop/{alias}", response_class=HTMLResponse)
    def nvitop_view(alias: str) -> HTMLResponse:
        return HTMLResponse(nvitop.nvitop_page(alias))

    if STATIC_ROOT.is_dir() and (STATIC_ROOT / "index.html").is_file():
        app.mount("/assets", StaticFiles(directory=str(STATIC_ROOT / "assets")), name="assets")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(STATIC_ROOT / "index.html")

        @app.get("/{path:path}")
        def spa(path: str) -> FileResponse:
            candidate = STATIC_ROOT / path
            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(STATIC_ROOT / "index.html")

    return app
