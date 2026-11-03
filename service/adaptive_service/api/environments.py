from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from .common import find_node, latest_snapshot

router = APIRouter(tags=["environments"])

MANAGERS = {"uv", "uv-managed", "venv", "conda", "micromamba"}
PATH_RE = re.compile(r"^(/|~/)")


class EnvironmentReport(BaseModel):
    alias: str
    manager: str
    path: str = ""
    version: str = Field(default="", max_length=64)
    project_path: str = ""
    python_version: str = Field(default="", max_length=32)
    uv_version: str = Field(default="", max_length=64)
    lockfile: str = ""
    lock_sha256: str = Field(default="", max_length=64)
    cache_hit: Optional[bool] = None
    check_command: str = ""
    check_exit_code: int
    evidence: str = ""
    reporter: str = ""

    @field_validator("alias", "manager", "path", "project_path", "evidence", "reporter", "lockfile")
    @classmethod
    def single_line(cls, value: str) -> str:
        text = str(value or "")
        if "\n" in text or "\r" in text:
            raise ValueError("value must be a single line")
        return text.strip()

    @field_validator("lock_sha256")
    @classmethod
    def lock_digest(cls, value: str) -> str:
        text = str(value or "").strip().lower()
        if text and not re.fullmatch(r"[0-9a-f]{64}", text):
            raise ValueError("lock_sha256 must be a 64-character hex digest")
        return text

    @field_validator("manager")
    @classmethod
    def known_manager(cls, value: str) -> str:
        if value.lower() not in MANAGERS:
            raise ValueError(f"unsupported manager: {value}")
        return value.lower()

    @field_validator("path", "project_path")
    @classmethod
    def absolute_path(cls, value: str) -> str:
        if value and not PATH_RE.match(value):
            raise ValueError("path must start with / or ~/")
        return value


@router.get("/environments")
def list_environments(request: Request) -> Dict[str, Any]:
    return {"environments": request.app.state.db.list_environments()}


@router.get("/environments/{alias}")
def environment_detail(alias: str, request: Request) -> Dict[str, Any]:
    record = request.app.state.db.get_environment(alias)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no environment record: {alias}")
    return record


@router.post("/environments")
async def report_environment(
    report: EnvironmentReport,
    request: Request,
) -> Dict[str, Any]:
    actor = "anonymous"
    if report.check_exit_code != 0:
        raise HTTPException(
            status_code=422,
            detail="environment can only be reported when the check exits with zero",
        )
    payload = latest_snapshot(request)
    if find_node(payload, report.alias) is None:
        raise HTTPException(status_code=404, detail=f"unknown node: {report.alias}")
    record = request.app.state.db.upsert_environment(
        {
            "alias": report.alias,
            "manager": report.manager,
            "path": report.path,
            "version": report.version,
            "project_path": report.project_path,
            "python_version": report.python_version,
            "uv_version": report.uv_version,
            "lockfile": report.lockfile,
            "lock_sha256": report.lock_sha256,
            "cache_hit": report.cache_hit,
            "status": "reported",
            "source": "skill",
            "reporter": report.reporter or actor,
            "evidence": report.evidence or report.check_command,
            "verified_at": None,
            "conflict_detail": {},
        }
    )
    request.app.state.db.add_audit(
        report.reporter or actor,
        "environment.report",
        report.alias,
        {
            "manager": report.manager,
            "path": report.path,
            "version": report.version,
            "check_command": report.check_command,
        },
    )
    request.app.state.collector.publish_environment(report.alias, actor=report.reporter or actor)
    asyncio.create_task(request.app.state.collector.verify_environment(report.alias))
    return record


@router.get("/environments/{alias}/conflict")
def environment_conflict(alias: str, request: Request) -> Dict[str, Any]:
    record = request.app.state.db.get_environment(alias)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no environment record: {alias}")
    return {"alias": alias, "status": record.get("status"), "detail": record.get("conflict_detail")}
