from __future__ import annotations

import asyncio
import getpass
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from adaptive_core.models import NodeConfig
from adaptive_core.sshutil import is_local_node
from adaptive_core.state import get_state_dir, now_kst_iso

from ..config import load_all_clusters
from ..store.db import Database
from .probe import (
    collect_inventory,
    collect_node_inventory,
    discover_ssh_nodes,
    node_config_from_inventory_node,
)


def _csv_env(name: str) -> List[str]:
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class CollectorSettings:
    state_dir: Path = field(default_factory=lambda: get_state_dir())
    ssh_config: Path = field(
        default_factory=lambda: Path(os.environ.get("ADAPTIVE_SSH_CONFIG", "~/.ssh/config")).expanduser()
    )
    config_path: Optional[Path] = None
    from_ssh_config: bool = True
    storage_paths: List[str] = field(default_factory=lambda: _csv_env("ADAPTIVE_STORAGE_PATHS"))
    environment_paths: List[str] = field(default_factory=lambda: _csv_env("ADAPTIVE_ENVIRONMENT_PATHS"))
    interval_s: int = field(default_factory=lambda: int(os.environ.get("ADAPTIVE_COLLECT_INTERVAL", "60")))
    timeout_s: int = field(default_factory=lambda: int(os.environ.get("ADAPTIVE_COLLECT_TIMEOUT", "20")))
    benchmark: bool = field(default_factory=lambda: os.environ.get("ADAPTIVE_BENCHMARK", "") == "1")
    gpu_idle_window_s: int = field(
        default_factory=lambda: int(os.environ.get("ADAPTIVE_GPU_IDLE_WINDOW", "30"))
    )
    gpu_idle_max_memory_mib: int = field(
        default_factory=lambda: int(os.environ.get("ADAPTIVE_GPU_IDLE_MAX_MEMORY_MIB", "1024"))
    )

    @property
    def database_path(self) -> Path:
        return self.state_dir / "service.db"

    @property
    def resolved_config_path(self) -> Path:
        if self.config_path:
            return Path(self.config_path).expanduser().resolve()
        return Path(os.environ.get("ADAPTIVE_CONFIG", self.state_dir / "adaptive.json")).expanduser().resolve()


def _default_local_node(nodes: Sequence[NodeConfig]) -> List[NodeConfig]:
    selected = list(nodes)
    if any(is_local_node(node) for node in selected):
        return selected
    aliases = {node.alias for node in selected}
    alias = "local"
    suffix = 2
    while alias in aliases:
        alias = f"local-{suffix}"
        suffix += 1
    selected.insert(0, NodeConfig(alias=alias, address="127.0.0.1", user=getpass.getuser()))
    return selected


def attach_environment_records(
    nodes: Sequence[Mapping[str, Any]],
    records: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    by_alias = {str(record.get("alias") or ""): record for record in records}
    attached: List[Dict[str, Any]] = []
    for raw_node in nodes:
        node = dict(raw_node)
        alias = str(node.get("alias") or "")
        record = by_alias.get(alias)
        software = dict(node.get("software") or {}) if isinstance(node.get("software"), Mapping) else {}
        environments = [
            dict(item)
            for item in software.get("environments", [])
            if isinstance(item, Mapping)
        ] if isinstance(software.get("environments"), list) else []
        if record is not None:
            manager = str(record.get("manager") or "").strip()
            environment_path = str(record.get("path") or "").strip()
            environments = [
                item
                for item in environments
                if not (
                    str(item.get("manager") or "").strip().lower() == manager.lower()
                    and (
                        manager.lower() in {"uv", "uv-managed"}
                        or str(item.get("path") or "").strip() == environment_path
                    )
                )
            ]
            if str(record.get("status") or "") in {"recorded", "reported"}:
                environments.append(
                    {
                        "manager": manager,
                        "path": environment_path,
                        "name": environment_path,
                        "version": str(record.get("version") or ""),
                        "project": str(record.get("project_path") or "") or None,
                        "source": str(record.get("source") or "skill"),
                        "status": str(record.get("status") or "reported"),
                    }
                )
        software["environments"] = environments
        node["software"] = software
        node["environment_record"] = dict(record) if record is not None else None
        node["environment_status"] = str(record.get("status") or "") if record is not None else None
        attached.append(node)
    return attached


class InventoryCollector:
    def __init__(self, db: Database, settings: CollectorSettings):
        self.db = db
        self.settings = settings
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._refresh_tasks: set = set()
        self._lock: Optional[asyncio.Lock] = None
        self._scheduler: Optional[asyncio.Task] = None
        self._stopping = False

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _nodes(self) -> List[NodeConfig]:
        config_path = self.settings.resolved_config_path
        if config_path.is_file():
            clusters = load_all_clusters(config_path)
            selected = list(clusters[0].nodes)
        else:
            selected = (
                discover_ssh_nodes(self.settings.ssh_config)
                if self.settings.ssh_config.is_file()
                else []
            )
        selected = _default_local_node(selected)
        for node in selected:
            if not node.storage_paths:
                node.storage_paths = list(self.settings.storage_paths)
            if not node.environment_paths:
                node.environment_paths = list(self.settings.environment_paths)
        return selected

    def nodes_from_snapshot(self, payload: Mapping[str, Any]) -> Dict[str, NodeConfig]:
        result: Dict[str, NodeConfig] = {}
        for raw in payload.get("nodes", []):
            if not isinstance(raw, Mapping) or not raw.get("alias"):
                continue
            node = node_config_from_inventory_node(dict(raw))
            if node.address:
                result[node.alias] = node
        return result

    def start_refresh(self, actor: str = "service") -> Dict[str, Any]:
        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "kind": "inventory_refresh",
            "status": "running",
            "started_at": now_kst_iso(),
            "finished_at": None,
            "snapshot_id": None,
            "error": None,
            "actor": actor,
        }
        self._jobs[job_id] = job
        task = asyncio.create_task(self._run_refresh(job))
        self._refresh_tasks.add(task)
        task.add_done_callback(self._refresh_tasks.discard)
        return dict(job)

    async def _run_refresh(self, job: Dict[str, Any]) -> None:
        try:
            async with self._get_lock():
                snapshot = await self.refresh(actor=str(job.get("actor") or "service"))
            job["status"] = "succeeded"
            job["snapshot_id"] = snapshot.get("snapshot_id")
        except Exception as exc:
            job["status"] = "failed"
            job["error"] = str(exc)
        finally:
            job["finished_at"] = now_kst_iso()

    def job(self, job_id: str) -> Optional[Dict[str, Any]]:
        job = self._jobs.get(str(job_id))
        return dict(job) if job is not None else None

    async def refresh(self, actor: str = "service") -> Dict[str, Any]:
        nodes = self._nodes()
        previous = self.db.latest_snapshot()
        known_aliases: Dict[str, str] = {}
        known_macs: Dict[str, str] = {}
        work_aliases = set(self.db.list_work_paths())
        for item in self.db.known_nodes():
            alias = str(item.get("alias") or "")
            mac = str(item.get("mac") or "").strip().lower()
            if not alias or not mac:
                continue
            known_macs[alias] = mac
            current = known_aliases.get(mac)
            if current is None:
                known_aliases[mac] = alias
            elif current.endswith("-frp") and not alias.endswith("-frp"):
                known_aliases[mac] = alias
            elif (
                alias in work_aliases
                and current not in work_aliases
                and not alias.endswith("-frp")
            ):
                known_aliases[mac] = alias
        document = await collect_inventory(
            nodes,
            timeout_s=self.settings.timeout_s,
            previous=previous,
            ssh_config=str(self.settings.ssh_config),
            benchmark=self.settings.benchmark,
            known_aliases=known_aliases,
            known_macs=known_macs,
            work_aliases=work_aliases,
        )
        self.db.sync_nodes(document.get("nodes", []))
        records = self.db.list_environments()
        nodes = self._attach_paths(
            attach_environment_records(document.get("nodes", []), records)
        )
        payload = {
            "schema_version": document.get("schema_version", 1),
            "generated_at": document.get("generated_at") or now_kst_iso(),
            "collector": document.get("collector") or {},
            "nodes": nodes,
            "environment_records": records,
            "allocations": self.db.list_allocations(),
            "policy": load_policy(self.settings),
        }
        saved = self.db.save_snapshot(payload)
        self.db.add_audit(
            actor,
            "inventory.refresh",
            "cluster",
            {
                "snapshot_id": saved["snapshot_id"],
                "inserted": saved["inserted"],
                "node_count": len(payload["nodes"]),
            },
        )
        return {"snapshot_id": saved["snapshot_id"], "inserted": saved["inserted"]}

    def _attach_paths(
        self, nodes: Sequence[Mapping[str, Any]]
    ) -> List[Dict[str, Any]]:
        registered = self.db.list_paths()
        attached: List[Dict[str, Any]] = []
        for raw_node in nodes:
            node = dict(raw_node)
            kinds = registered.get(str(node.get("alias") or ""), {})
            for kind in ("work", "data", "model"):
                record = kinds.get(kind)
                if record is not None:
                    node[f"{kind}_path"] = record["path"]
                    node[f"{kind}_path_registered_at"] = record["registered_at"]
                else:
                    node.pop(f"{kind}_path", None)
                    node.pop(f"{kind}_path_registered_at", None)
            attached.append(node)
        return attached

    def publish_environment(self, alias: str, actor: str = "skill") -> Dict[str, Any]:
        latest = self.db.latest_snapshot()
        if latest is None:
            raise ValueError("no snapshot available; refresh inventory first")
        records = self.db.list_environments()
        payload = dict(latest)
        payload.pop("snapshot_id", None)
        payload["generated_at"] = now_kst_iso()
        payload["nodes"] = self._attach_paths(
            attach_environment_records(latest.get("nodes", []), records)
        )
        payload["environment_records"] = records
        payload["allocations"] = self.db.list_allocations()
        saved = self.db.save_snapshot(payload)
        self.db.add_audit(actor, "environment.publish", alias, {"snapshot_id": saved["snapshot_id"]})
        return {"snapshot_id": saved["snapshot_id"], "inserted": saved["inserted"]}

    def publish_path(self, alias: str, kind: str, actor: str = "skill") -> Dict[str, Any]:
        latest = self.db.latest_snapshot()
        if latest is None:
            raise ValueError("no snapshot available; refresh inventory first")
        records = self.db.list_environments()
        payload = dict(latest)
        payload.pop("snapshot_id", None)
        payload["generated_at"] = now_kst_iso()
        payload["nodes"] = self._attach_paths(
            attach_environment_records(latest.get("nodes", []), records)
        )
        payload["environment_records"] = records
        payload["allocations"] = self.db.list_allocations()
        saved = self.db.save_snapshot(payload)
        self.db.add_audit(
            actor, f"path.{kind}.publish", alias, {"snapshot_id": saved["snapshot_id"]}
        )
        return {"snapshot_id": saved["snapshot_id"], "inserted": saved["inserted"]}

    def publish_work_path(self, alias: str, actor: str = "skill") -> Dict[str, Any]:
        return self.publish_path(alias, "work", actor)

    def publish_allocations(self, actor: str = "service") -> Dict[str, Any]:
        latest = self.db.latest_snapshot()
        if latest is None:
            raise ValueError("no snapshot available; refresh inventory first")
        records = self.db.list_environments()
        payload = dict(latest)
        payload.pop("snapshot_id", None)
        payload["generated_at"] = now_kst_iso()
        payload["nodes"] = self._attach_paths(
            attach_environment_records(latest.get("nodes", []), records)
        )
        payload["environment_records"] = records
        payload["allocations"] = self.db.list_allocations()
        payload["policy"] = load_policy(self.settings)
        saved = self.db.save_snapshot(payload)
        self.db.add_audit(actor, "allocation.publish", "cluster", {"snapshot_id": saved["snapshot_id"]})
        return {"snapshot_id": saved["snapshot_id"], "inserted": saved["inserted"]}

    async def verify_environment(self, alias: str) -> Dict[str, Any]:
        latest = self.db.latest_snapshot()
        record = self.db.get_environment(alias)
        if latest is None or record is None:
            return {"alias": alias, "status": "skipped", "reason": "no snapshot or record"}
        nodes = self.nodes_from_snapshot(latest)
        node = nodes.get(alias)
        if node is None:
            return {"alias": alias, "status": "skipped", "reason": "node is not in inventory"}
        observed = await collect_node_inventory(
            node,
            timeout_s=self.settings.timeout_s,
        )
        if not observed.get("reachable"):
            return {"alias": alias, "status": "pending", "reason": "node is unreachable"}
        conflict = _environment_conflict(record, observed)
        if conflict:
            self.db.mark_environment_conflict(alias, conflict)
            self.db.add_audit("service", "environment.conflict", alias, conflict)
            self.publish_environment(alias, actor="service")
            return {"alias": alias, "status": "conflict", "detail": conflict}
        self.db.mark_environment_verified(alias)
        self.db.add_audit("service", "environment.verified", alias, {"manager": record.get("manager")})
        self.publish_environment(alias, actor="service")
        return {"alias": alias, "status": "verified"}

    def start_scheduler(self) -> None:
        if self.settings.interval_s <= 0 or self._scheduler is not None:
            return
        self._scheduler = asyncio.create_task(self._scheduler_loop())

    async def _scheduler_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(self.settings.interval_s)
            except asyncio.CancelledError:
                return
            try:
                lock = self._get_lock()
                if lock.locked():
                    continue
                async with lock:
                    await self.refresh(actor="scheduler")
            except Exception:
                continue

    async def stop(self) -> None:
        self._stopping = True
        if self._scheduler is not None:
            self._scheduler.cancel()
            try:
                await self._scheduler
            except asyncio.CancelledError:
                pass
            self._scheduler = None
        pending = list(self._refresh_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._refresh_tasks.clear()


def load_policy(settings: CollectorSettings) -> Dict[str, Any]:
    config_path = settings.resolved_config_path
    policy: Dict[str, Any] = {"require_allocation": True, "all_users_token": "*"}
    if config_path.is_file():
        try:
            clusters = load_all_clusters(config_path)
        except (OSError, ValueError):
            return policy
        if clusters:
            policy.update(dict(clusters[0].policy))
    return policy


def _environment_conflict(record: Mapping[str, Any], observed: Mapping[str, Any]) -> Dict[str, Any]:
    manager = str(record.get("manager") or "").strip().lower()
    expected_version = str(record.get("version") or "").strip()
    software = observed.get("software") if isinstance(observed.get("software"), Mapping) else {}
    if manager in {"uv", "uv-managed"}:
        uv = software.get("uv") if isinstance(software.get("uv"), Mapping) else {}
        if not uv.get("installed"):
            return {"field": "uv.installed", "expected": True, "observed": False}
        observed_version = str(uv.get("version") or "").strip()
        if expected_version and observed_version and expected_version != observed_version:
            return {
                "field": "uv.version",
                "expected": expected_version,
                "observed": observed_version,
            }
        return {}
    expected_path = str(record.get("path") or "").strip()
    environments = software.get("environments") if isinstance(software.get("environments"), list) else []
    for item in environments:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("manager") or "").strip().lower() != manager:
            continue
        item_path = str(item.get("path") or "").strip()
        if expected_path and item_path and expected_path != item_path:
            continue
        item_version = str(item.get("version") or "").strip()
        if expected_version and item_version and expected_version != item_version:
            return {
                "field": "environment.version",
                "expected": expected_version,
                "observed": item_version,
            }
        return {}
    return {"field": "environment.path", "expected": expected_path, "observed": []}
