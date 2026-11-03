from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Sequence

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from .common import find_node, latest_snapshot

router = APIRouter(tags=["paths"])

PATH_RE = re.compile(r"^/(?:[^\s/]+/)*[^\s/]+$")
PATH_KINDS = frozenset({"work", "data", "model"})


class PathReport(BaseModel):
    path: str = Field(min_length=2, max_length=512)
    reporter: str = ""

    @field_validator("path")
    @classmethod
    def absolute_path(cls, value: str) -> str:
        text = str(value or "").strip().rstrip("/")
        if not PATH_RE.match(text):
            raise ValueError("path must be an absolute directory path without whitespace")
        return text

    @field_validator("reporter")
    @classmethod
    def single_line(cls, value: str) -> str:
        text = str(value or "")
        if "\n" in text or "\r" in text:
            raise ValueError("reporter must be a single line")
        return text.strip()


WorkPathReport = PathReport


def _check_kind(kind: str) -> None:
    if kind not in PATH_KINDS:
        raise HTTPException(status_code=404, detail=f"unknown path kind: {kind}")


def _on_mounted_filesystem(disks: Sequence[Mapping[str, Any]], path: str) -> bool:
    if not disks:
        return True
    for disk in disks:
        mount = str(disk.get("path") or "").rstrip("/") or "/"
        if mount == "/" or path == mount or path.startswith(mount + "/"):
            return True
    return False


def _get_path(alias: str, kind: str, request: Request) -> Dict[str, Any]:
    record = request.app.state.db.get_path(alias, kind)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no {kind} path registered: {alias}")
    return record


def _register_path(
    alias: str,
    kind: str,
    report: PathReport,
    request: Request,
    actor: str,
) -> Dict[str, Any]:
    payload = latest_snapshot(request)
    node = find_node(payload, alias)
    if node is None:
        raise HTTPException(status_code=404, detail=f"unknown node: {alias}")
    hardware = node.get("hardware") if isinstance(node.get("hardware"), Mapping) else {}
    disks = hardware.get("disks") if isinstance(hardware.get("disks"), list) else []
    if not _on_mounted_filesystem(disks, report.path):
        raise HTTPException(
            status_code=422,
            detail=f"path is not on a mounted filesystem: {report.path}",
        )
    reporter = report.reporter or actor
    record = request.app.state.db.upsert_path(alias, kind, report.path, reporter)
    request.app.state.db.add_audit(
        reporter,
        f"path.{kind}.register",
        alias,
        {"path": report.path},
    )
    request.app.state.collector.publish_path(alias, kind, actor=reporter)
    return record


@router.get("/nodes/{alias}/paths/{kind}")
def get_registered_path(
    alias: str,
    kind: str,
    request: Request,
) -> Dict[str, Any]:
    _check_kind(kind)
    return _get_path(alias, kind, request)


@router.post("/nodes/{alias}/paths/{kind}")
def register_kind_path(
    alias: str,
    kind: str,
    report: PathReport,
    request: Request,
) -> Dict[str, Any]:
    _check_kind(kind)
    return _register_path(alias, kind, report, request, "anonymous")


@router.get("/nodes/{alias}/work-path")
def get_work_path(alias: str, request: Request) -> Dict[str, Any]:
    return _get_path(alias, "work", request)


@router.post("/nodes/{alias}/work-path")
def register_work_path(
    alias: str,
    report: PathReport,
    request: Request,
) -> Dict[str, Any]:
    return _register_path(alias, "work", report, request, "anonymous")
