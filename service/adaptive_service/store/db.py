from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from adaptive_core.state import now_kst_iso


SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    alias TEXT PRIMARY KEY,
    mac TEXT,
    channel TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    candidates_json TEXT NOT NULL DEFAULT '[]',
    ssh_json TEXT NOT NULL,
    configured_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_states (
    snapshot_id INTEGER NOT NULL,
    alias TEXT NOT NULL,
    reachable INTEGER NOT NULL,
    stale INTEGER NOT NULL,
    hardware_json TEXT NOT NULL,
    software_json TEXT NOT NULL,
    errors_json TEXT NOT NULL,
    observed_at TEXT,
    PRIMARY KEY (snapshot_id, alias)
);

CREATE TABLE IF NOT EXISTS gpu_samples (
    snapshot_id INTEGER NOT NULL,
    alias TEXT NOT NULL,
    gpu_index INTEGER NOT NULL,
    uuid TEXT,
    name TEXT,
    memory_total_mib INTEGER,
    memory_free_mib INTEGER,
    utilization_percent REAL,
    processes_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, alias, gpu_index)
);

CREATE INDEX IF NOT EXISTS gpu_samples_alias ON gpu_samples(alias, snapshot_id);

CREATE TABLE IF NOT EXISTS environment_records (
    alias TEXT PRIMARY KEY,
    manager TEXT NOT NULL,
    path TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '',
    project_path TEXT NOT NULL DEFAULT '',
    python_version TEXT NOT NULL DEFAULT '',
    uv_version TEXT NOT NULL DEFAULT '',
    lockfile TEXT NOT NULL DEFAULT '',
    lock_sha256 TEXT NOT NULL DEFAULT '',
    cache_hit INTEGER,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    reporter TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    reported_at TEXT NOT NULL,
    verified_at TEXT,
    conflict_detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS work_paths (
    alias TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    registered_by TEXT NOT NULL DEFAULT '',
    registered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS registered_paths (
    alias TEXT NOT NULL,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    registered_by TEXT NOT NULL DEFAULT '',
    registered_at TEXT NOT NULL,
    PRIMARY KEY (alias, kind)
);

CREATE TABLE IF NOT EXISTS allocations (
    alias TEXT NOT NULL,
    gpu_index INTEGER NOT NULL,
    target TEXT NOT NULL,
    assigned_by TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    expires_at TEXT,
    notes TEXT NOT NULL DEFAULT '',
    plan_id TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (alias, gpu_index)
);

CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL DEFAULT '',
    manifest_key TEXT NOT NULL DEFAULT '',
    manifest_json TEXT NOT NULL DEFAULT '{}',
    plan_json TEXT NOT NULL,
    devices_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'available',
    claimed_by TEXT NOT NULL DEFAULT '',
    claimed_at TEXT,
    expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS plans_status ON plans(status, created_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load(text: str, default: Any) -> Any:
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return default


def _work_path_row(row: Any) -> Dict[str, Any]:
    return {
        "alias": str(row["alias"]),
        "path": str(row["path"]),
        "registered_by": str(row["registered_by"]),
        "registered_at": str(row["registered_at"]),
    }


def _path_row(row: Any) -> Dict[str, Any]:
    return {
        "alias": str(row["alias"]),
        "kind": str(row["kind"]),
        "path": str(row["path"]),
        "registered_by": str(row["registered_by"]),
        "registered_at": str(row["registered_at"]),
    }


def content_digest(document: Mapping[str, Any]) -> str:
    digest = hashlib.sha256()
    nodes = [node for node in document.get("nodes", []) if isinstance(node, Mapping)]
    for node in sorted(nodes, key=lambda item: str(item.get("alias") or "")):
        digest.update(_dump(dict(node)).encode("utf-8"))
    digest.update(_dump(document.get("environment_records") or []).encode("utf-8"))
    digest.update(_dump(document.get("allocations") or []).encode("utf-8"))
    return "sha256:" + digest.hexdigest()


class Database:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve()

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def init_schema(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(nodes)")}
            for name, ddl in (
                ("mac", "ALTER TABLE nodes ADD COLUMN mac TEXT"),
                ("channel", "ALTER TABLE nodes ADD COLUMN channel TEXT NOT NULL DEFAULT ''"),
                ("address", "ALTER TABLE nodes ADD COLUMN address TEXT NOT NULL DEFAULT ''"),
                ("candidates_json", "ALTER TABLE nodes ADD COLUMN candidates_json TEXT NOT NULL DEFAULT '[]'"),
            ):
                if name not in columns:
                    connection.execute(ddl)
            environment_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(environment_records)")
            }
            for name, ddl in (
                ("uv_version", "ALTER TABLE environment_records ADD COLUMN uv_version TEXT NOT NULL DEFAULT ''"),
                ("lockfile", "ALTER TABLE environment_records ADD COLUMN lockfile TEXT NOT NULL DEFAULT ''"),
                ("lock_sha256", "ALTER TABLE environment_records ADD COLUMN lock_sha256 TEXT NOT NULL DEFAULT ''"),
                ("cache_hit", "ALTER TABLE environment_records ADD COLUMN cache_hit INTEGER"),
            ):
                if name not in environment_columns:
                    connection.execute(ddl)
            allocation_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(allocations)")
            }
            if "plan_id" not in allocation_columns:
                connection.execute(
                    "ALTER TABLE allocations ADD COLUMN plan_id TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS nodes_mac ON nodes(mac) "
                "WHERE mac IS NOT NULL AND mac != ''"
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO registered_paths(alias, kind, path, registered_by, registered_at)
                SELECT alias, 'work', path, registered_by, registered_at FROM work_paths
                """
            )

    def sync_nodes(self, nodes: Iterable[Mapping[str, Any]]) -> None:
        now = now_kst_iso()
        with self.connect() as connection:
            for node in nodes:
                alias = str(node.get("alias") or "").strip()
                if not alias:
                    continue
                mac = str(node.get("mac") or "").strip().lower() or None
                if mac:
                    connection.execute(
                        "DELETE FROM nodes WHERE mac = ? AND alias != ?",
                        (mac, alias),
                    )
                connection.execute(
                    """
                    INSERT INTO nodes(
                        alias, mac, channel, address, candidates_json,
                        ssh_json, configured_json, enabled, first_seen, last_seen
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(alias) DO UPDATE SET
                        mac=excluded.mac,
                        channel=excluded.channel,
                        address=excluded.address,
                        candidates_json=excluded.candidates_json,
                        ssh_json=excluded.ssh_json,
                        configured_json=excluded.configured_json,
                        last_seen=excluded.last_seen
                    """,
                    (
                        alias,
                        str(node.get("mac") or "").strip().lower() or None,
                        str(node.get("channel") or ""),
                        str((node.get("connection") or {}).get("address") or ""),
                        _dump(node.get("candidates") or []),
                        _dump(node.get("ssh") or {}),
                        _dump(node.get("configured") or {}),
                        now,
                        now,
                    ),
                )

    def known_nodes(self) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT alias, mac, channel, address, candidates_json, "
                "ssh_json, configured_json, enabled FROM nodes ORDER BY alias"
            ).fetchall()
        return [
            {
                "alias": row["alias"],
                "mac": row["mac"],
                "channel": row["channel"],
                "address": row["address"],
                "candidates": _load(row["candidates_json"], []),
                "ssh": _load(row["ssh_json"], {}),
                "configured": _load(row["configured_json"], {}),
                "enabled": bool(row["enabled"]),
            }
            for row in rows
        ]

    def save_snapshot(self, document: Mapping[str, Any]) -> Dict[str, Any]:
        nodes = [node for node in document.get("nodes", []) if isinstance(node, Mapping)]
        digest = content_digest(document)
        generated_at = str(document.get("generated_at") or now_kst_iso())
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id FROM snapshots WHERE content_hash = ?", (digest,)
            ).fetchone()
            if row is not None:
                snapshot_id = int(row["id"])
                connection.execute(
                    "UPDATE snapshots SET generated_at = ? WHERE id = ?",
                    (generated_at, snapshot_id),
                )
                return {"snapshot_id": snapshot_id, "inserted": False}

            payload = dict(document)
            payload.pop("snapshot_id", None)
            cursor = connection.execute(
                "INSERT INTO snapshots(generated_at, content_hash, payload_json) VALUES(?, ?, ?)",
                (generated_at, digest, _dump(payload)),
            )
            snapshot_id = int(cursor.lastrowid)
            for node in nodes:
                alias = str(node.get("alias") or "")
                if not alias:
                    continue
                connection.execute(
                    """
                    INSERT OR REPLACE INTO node_states(
                        snapshot_id, alias, reachable, stale, hardware_json,
                        software_json, errors_json, observed_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        alias,
                        1 if node.get("reachable") else 0,
                        1 if node.get("stale") else 0,
                        _dump(node.get("hardware") or {}),
                        _dump(node.get("software") or {}),
                        _dump(node.get("errors") or []),
                        node.get("observed_at"),
                    ),
                )
                hardware = node.get("hardware") if isinstance(node.get("hardware"), Mapping) else {}
                gpu = hardware.get("gpu") if isinstance(hardware.get("gpu"), Mapping) else {}
                for device in gpu.get("devices", []) or []:
                    if not isinstance(device, Mapping):
                        continue
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO gpu_samples(
                            snapshot_id, alias, gpu_index, uuid, name,
                            memory_total_mib, memory_free_mib, utilization_percent,
                            processes_json
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            snapshot_id,
                            alias,
                            int(device.get("index") or 0),
                            device.get("uuid"),
                            device.get("name"),
                            device.get("memory_total_mib"),
                            device.get("memory_free_mib"),
                            device.get("utilization_gpu_percent"),
                            _dump(device.get("processes") or []),
                        ),
                    )
        return {"snapshot_id": snapshot_id, "inserted": True}

    def latest_snapshot(self) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id, generated_at, payload_json FROM snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        payload = _load(row["payload_json"], {})
        payload["snapshot_id"] = int(row["id"])
        payload["generated_at"] = row["generated_at"]
        return payload

    def snapshot(self, snapshot_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id, generated_at, payload_json FROM snapshots WHERE id = ?",
                (int(snapshot_id),),
            ).fetchone()
        if row is None:
            return None
        payload = _load(row["payload_json"], {})
        payload["snapshot_id"] = int(row["id"])
        payload["generated_at"] = row["generated_at"]
        return payload

    def snapshot_history(self, limit: int = 60) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, generated_at FROM snapshots ORDER BY id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [{"snapshot_id": int(row["id"]), "generated_at": row["generated_at"]} for row in rows]

    def gpu_series(self, alias: str, limit: int = 120) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT s.generated_at, g.gpu_index, g.uuid, g.name,
                       g.memory_total_mib, g.memory_free_mib,
                       g.utilization_percent, g.processes_json
                FROM gpu_samples g
                JOIN snapshots s ON s.id = g.snapshot_id
                WHERE g.alias = ?
                ORDER BY s.id DESC
                LIMIT ?
                """,
                (str(alias), max(1, int(limit))),
            ).fetchall()
        return [
            {
                "generated_at": row["generated_at"],
                "gpu_index": int(row["gpu_index"]),
                "uuid": row["uuid"],
                "name": row["name"],
                "memory_total_mib": row["memory_total_mib"],
                "memory_free_mib": row["memory_free_mib"],
                "utilization_percent": row["utilization_percent"],
                "processes": _load(row["processes_json"], []),
            }
            for row in reversed(rows)
        ]

    def upsert_path(
        self, alias: str, kind: str, path: str, registered_by: str = ""
    ) -> Dict[str, Any]:
        now = now_kst_iso()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO registered_paths(alias, kind, path, registered_by, registered_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(alias, kind) DO UPDATE SET
                    path=excluded.path,
                    registered_by=excluded.registered_by,
                    registered_at=excluded.registered_at
                """,
                (str(alias), str(kind), str(path), str(registered_by), now),
            )
            row = connection.execute(
                "SELECT alias, kind, path, registered_by, registered_at FROM registered_paths "
                "WHERE alias = ? AND kind = ?",
                (str(alias), str(kind)),
            ).fetchone()
        return _path_row(row)

    def get_path(self, alias: str, kind: str) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT alias, kind, path, registered_by, registered_at FROM registered_paths "
                "WHERE alias = ? AND kind = ?",
                (str(alias), str(kind)),
            ).fetchone()
        return _path_row(row) if row is not None else None

    def list_paths(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT alias, kind, path, registered_by, registered_at FROM registered_paths "
                "ORDER BY alias, kind"
            ).fetchall()
        result: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for row in rows:
            record = _path_row(row)
            result.setdefault(record["alias"], {})[record["kind"]] = record
        return result

    def upsert_work_path(self, alias: str, path: str, registered_by: str = "") -> Dict[str, Any]:
        return self.upsert_path(alias, "work", path, registered_by)

    def get_work_path(self, alias: str) -> Optional[Dict[str, Any]]:
        return self.get_path(alias, "work")

    def list_work_paths(self) -> Dict[str, Dict[str, Any]]:
        return {
            alias: kinds["work"]
            for alias, kinds in self.list_paths().items()
            if "work" in kinds
        }

    def upsert_environment(self, record: Mapping[str, Any]) -> Dict[str, Any]:
        now = now_kst_iso()
        status = str(record.get("status") or "reported")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO environment_records(
                    alias, manager, path, version, project_path, python_version,
                    uv_version, lockfile, lock_sha256, cache_hit,
                    status, source, reporter, evidence, reported_at, verified_at,
                    conflict_detail_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(alias) DO UPDATE SET
                    manager=excluded.manager,
                    path=excluded.path,
                    version=excluded.version,
                    project_path=excluded.project_path,
                    python_version=excluded.python_version,
                    uv_version=excluded.uv_version,
                    lockfile=excluded.lockfile,
                    lock_sha256=excluded.lock_sha256,
                    cache_hit=excluded.cache_hit,
                    status=excluded.status,
                    source=excluded.source,
                    reporter=excluded.reporter,
                    evidence=excluded.evidence,
                    reported_at=excluded.reported_at,
                    verified_at=excluded.verified_at,
                    conflict_detail_json=excluded.conflict_detail_json
                """,
                (
                    str(record.get("alias") or ""),
                    str(record.get("manager") or ""),
                    str(record.get("path") or ""),
                    str(record.get("version") or ""),
                    str(record.get("project_path") or ""),
                    str(record.get("python_version") or ""),
                    str(record.get("uv_version") or ""),
                    str(record.get("lockfile") or ""),
                    str(record.get("lock_sha256") or ""),
                    record.get("cache_hit"),
                    status,
                    str(record.get("source") or "skill"),
                    str(record.get("reporter") or ""),
                    str(record.get("evidence") or ""),
                    str(record.get("reported_at") or now),
                    record.get("verified_at"),
                    _dump(record.get("conflict_detail") or {}),
                ),
            )
        return self.get_environment(str(record.get("alias") or "")) or {}

    def get_environment(self, alias: str) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM environment_records WHERE alias = ?", (str(alias),)
            ).fetchone()
        return _environment_row(row) if row is not None else None

    def list_environments(self) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM environment_records ORDER BY alias"
            ).fetchall()
        return [_environment_row(row) for row in rows]

    def mark_environment_conflict(self, alias: str, detail: Mapping[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE environment_records
                SET status = 'conflict', conflict_detail_json = ?
                WHERE alias = ?
                """,
                (_dump(dict(detail)), str(alias)),
            )

    def mark_environment_verified(self, alias: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE environment_records
                SET verified_at = ?, conflict_detail_json = '{}'
                WHERE alias = ?
                """,
                (now_kst_iso(), str(alias)),
            )

    def list_allocations(self) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM allocations ORDER BY alias, gpu_index"
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_allocation(self, record: Mapping[str, Any]) -> Dict[str, Any]:
        alias = str(record.get("alias") or "")
        gpu_index = int(record.get("gpu_index") or 0)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO allocations(
                    alias, gpu_index, target, assigned_by, assigned_at,
                    expires_at, notes, plan_id
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(alias, gpu_index) DO UPDATE SET
                    target=excluded.target,
                    assigned_by=excluded.assigned_by,
                    assigned_at=excluded.assigned_at,
                    expires_at=excluded.expires_at,
                    notes=excluded.notes,
                    plan_id=excluded.plan_id
                """,
                (
                    alias,
                    gpu_index,
                    str(record.get("target") or "*"),
                    str(record.get("assigned_by") or ""),
                    str(record.get("assigned_at") or now_kst_iso()),
                    record.get("expires_at"),
                    str(record.get("notes") or ""),
                    str(record.get("plan_id") or ""),
                ),
            )
            row = connection.execute(
                "SELECT * FROM allocations WHERE alias = ? AND gpu_index = ?",
                (alias, gpu_index),
            ).fetchone()
        return dict(row) if row is not None else {}

    def delete_allocation(self, alias: str, gpu_index: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM allocations WHERE alias = ? AND gpu_index = ?",
                (str(alias), int(gpu_index)),
            )
            return cursor.rowcount > 0

    def delete_allocations_for_plan(self, plan_id: str) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM allocations WHERE plan_id = ?", (str(plan_id),)
            ).fetchall()
            connection.execute(
                "DELETE FROM allocations WHERE plan_id = ?", (str(plan_id),)
            )
        return [dict(row) for row in rows]

    def save_plan(self, record: Mapping[str, Any]) -> Dict[str, Any]:
        now = now_kst_iso()
        plan_id = str(record.get("plan_id") or "")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO plans(
                    plan_id, name, owner, manifest_key, manifest_json,
                    plan_json, devices_json, status, claimed_by, claimed_at,
                    expires_at, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(plan_id) DO UPDATE SET
                    name=excluded.name,
                    owner=excluded.owner,
                    manifest_key=excluded.manifest_key,
                    manifest_json=excluded.manifest_json,
                    plan_json=excluded.plan_json,
                    devices_json=excluded.devices_json,
                    status=excluded.status,
                    claimed_by=excluded.claimed_by,
                    claimed_at=excluded.claimed_at,
                    expires_at=excluded.expires_at,
                    updated_at=excluded.updated_at
                """,
                (
                    plan_id,
                    str(record.get("name") or ""),
                    str(record.get("owner") or ""),
                    str(record.get("manifest_key") or ""),
                    _dump(record.get("manifest") or {}),
                    _dump(record.get("plan") or {}),
                    _dump(record.get("devices") or []),
                    str(record.get("status") or "available"),
                    str(record.get("claimed_by") or ""),
                    record.get("claimed_at"),
                    record.get("expires_at"),
                    str(record.get("created_at") or now),
                    now,
                ),
            )
        return self.get_plan(plan_id) or {}

    def get_plan(self, plan_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM plans WHERE plan_id = ?", (str(plan_id),)
            ).fetchone()
        return _plan_row(row) if row is not None else None

    def list_plans(
        self,
        *,
        status: Optional[str] = None,
        owner: Optional[str] = None,
        manifest_key: Optional[str] = None,
        include_plan: bool = False,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        clauses = []
        params: List[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(str(status))
        if owner:
            clauses.append("owner = ?")
            params.append(str(owner))
        if manifest_key:
            clauses.append("manifest_key = ?")
            params.append(str(manifest_key))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, int(limit)))
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM plans{where} ORDER BY created_at DESC, plan_id DESC LIMIT ?",
                params,
            ).fetchall()
        return [_plan_row(row, include_plan=include_plan) for row in rows]

    def update_plan(self, plan_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        allowed = {
            key: value
            for key, value in fields.items()
            if key in {"status", "claimed_by", "claimed_at", "expires_at"}
        }
        allowed["updated_at"] = now_kst_iso()
        assignments = ", ".join(f"{key} = ?" for key in allowed)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE plans SET {assignments} WHERE plan_id = ?",
                [*allowed.values(), str(plan_id)],
            )
        return self.get_plan(plan_id)

    def get_setting(self, key: str) -> Optional[str]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = ?", (str(key),)
            ).fetchone()
        return str(row["value"]) if row is not None else None

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(key), str(value)),
            )

    def delete_plan(self, plan_id: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM plans WHERE plan_id = ?", (str(plan_id),)
            )
            return cursor.rowcount > 0

    def add_audit(self, actor: str, action: str, target: str, detail: Mapping[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(actor, action, target, detail_json, created_at) VALUES(?, ?, ?, ?, ?)",
                (str(actor), str(action), str(target), _dump(dict(detail)), now_kst_iso()),
            )

    def list_audit(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "actor": row["actor"],
                "action": row["action"],
                "target": row["target"],
                "detail": _load(row["detail_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]


def _plan_row(row: sqlite3.Row, *, include_plan: bool = True) -> Dict[str, Any]:
    record = {
        "plan_id": row["plan_id"],
        "name": row["name"],
        "owner": row["owner"],
        "manifest_key": row["manifest_key"],
        "manifest": _load(row["manifest_json"], {}),
        "devices": _load(row["devices_json"], []),
        "status": row["status"],
        "claimed_by": row["claimed_by"],
        "claimed_at": row["claimed_at"],
        "expires_at": row["expires_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if include_plan:
        record["plan"] = _load(row["plan_json"], {})
    return record


def _environment_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "alias": row["alias"],
        "manager": row["manager"],
        "path": row["path"],
        "version": row["version"],
        "project_path": row["project_path"],
        "python_version": row["python_version"],
        "uv_version": row["uv_version"],
        "lockfile": row["lockfile"],
        "lock_sha256": row["lock_sha256"],
        "cache_hit": None if row["cache_hit"] is None else bool(row["cache_hit"]),
        "status": row["status"],
        "source": row["source"],
        "reporter": row["reporter"],
        "evidence": row["evidence"],
        "reported_at": row["reported_at"],
        "verified_at": row["verified_at"],
        "conflict_detail": _load(row["conflict_detail_json"], {}),
    }
