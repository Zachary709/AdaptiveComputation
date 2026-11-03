"""Resource-aware task planning for the adaptive-computation skill.

Task manifests are JSON on purpose: the core project has no third-party
runtime dependencies.  A plan is a pure data file, so an LLM can inspect or
edit it before any remote command is executed.
"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

from .artifacts import (
    MODELSCOPE_DEFAULT_TIMEOUT_S,
    MODELSCOPE_ID_RE,
    MODELSCOPE_REPO_TYPES,
    MODELSCOPE_REVISION_RE,
    MODELSCOPE_SOURCE_PREFIX,
    artifact_source_size,
)
from adaptive_core.models import NodeConfig
from adaptive_core.sshutil import is_local_node
from adaptive_core.state import atomic_write_text


class PlanningError(ValueError):
    """Raised when a task manifest cannot be scheduled safely."""


_UNIT_FACTORS = {
    "b": 1,
    "k": 1024,
    "kb": 1000,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1000**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1000**3,
    "gib": 1024**3,
    "t": 1024**4,
    "tb": 1000**4,
    "tib": 1024**4,
}


def parse_size(value: Any, *, default_unit: str = "b") -> int:
    """Parse bytes or a human-readable size (``64GiB``, ``100 GB``)."""

    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        raise PlanningError(f"invalid size: {value!r}")
    if isinstance(value, (int, float)):
        number = float(value) * _UNIT_FACTORS.get(default_unit.lower(), 1)
        if not math.isfinite(number):
            raise PlanningError(f"invalid size: {value!r}")
        return max(0, int(number))
    text = str(value).strip().lower().replace(" ", "")
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([a-z]*)", text)
    if not match:
        raise PlanningError(f"invalid size: {value!r}")
    number = float(match.group(1))
    unit = match.group(2) or default_unit.lower()
    if unit not in _UNIT_FACTORS:
        raise PlanningError(f"unknown size unit in {value!r}")
    return max(0, int(number * _UNIT_FACTORS[unit]))


def _positive_int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError) as exc:
        raise PlanningError(f"invalid integer: {value!r}") from exc


def _as_list(value: Any) -> List[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _string_list(value: Any) -> List[str]:
    return [str(item) for item in _as_list(value) if str(item).strip()]


def _merge_defaults(defaults: Mapping[str, Any], raw: Mapping[str, Any]) -> Dict[str, Any]:
    out = dict(defaults)
    out.update(dict(raw))
    return out


def load_task_manifest(path: Path) -> Dict[str, Any]:
    """Load and minimally validate a JSON task manifest."""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PlanningError(f"task manifest not found: {path}")
    except json.JSONDecodeError as exc:
        raise PlanningError(f"invalid task manifest JSON: {exc}") from exc

    if isinstance(data, list):
        data = {"tasks": data}
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise PlanningError("task manifest must be an object with a 'tasks' list")
    return data


_ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ARTIFACT_TOKEN_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ARTIFACT_TOKENS = {"work_dir", "disk_path", "node_alias"}
_PRIVATE_SOURCE_NAMES = {
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "id_xmss",
}
_NETWORK_ARTIFACT_SCHEMES = {"ftp", "gs", "hf", "http", "https", "s3"}
_MODELSCOPE_ONLY_FIELDS = ("size_bytes", "repo_type")


def _artifact_name(raw_name: Any, default: str, task_id: str, index: int) -> str:
    name = str(raw_name or default).strip()
    if not _ARTIFACT_NAME_RE.fullmatch(name):
        raise PlanningError(f"task {task_id!r}: artifact {index + 1} has an invalid name {name!r}")
    return name


def _artifact_destination(raw_destination: Any, kind: str, name: str, task_id: str, index: int) -> str:
    destination = str(raw_destination or "").strip()
    categories = {"model": "models", "data": "data", "source": "projects"}
    if not destination:
        destination = f"{{work_dir}}/{categories[kind]}/{name}"
    if kind in {"data", "model"}:
        prefix = f"{{work_dir}}/{categories[kind]}/"
        if not destination.startswith(prefix) or ".." in destination.split("/"):
            raise PlanningError(
                f"task {task_id!r}: artifact {index + 1} {kind} destination must stay under {prefix}"
            )
    if destination.count("{") != destination.count("}"):
        raise PlanningError(
            f"task {task_id!r}: artifact {index + 1} has an invalid destination {destination!r}"
        )
    unknown_tokens = set(_ARTIFACT_TOKEN_RE.findall(destination)) - _ARTIFACT_TOKENS
    if unknown_tokens:
        names_text = ", ".join(sorted(unknown_tokens))
        raise PlanningError(
            f"task {task_id!r}: artifact {index + 1} has unsupported destination token(s): {names_text}"
        )
    if not destination.startswith(("/", "~/", "{work_dir}", "{disk_path}")):
        raise PlanningError(
            f"task {task_id!r}: artifact {index + 1} destination must use an absolute path or a placement path"
        )
    return destination


def _artifact_timeout(raw: Mapping[str, Any], task_id: str, index: int, default_s: int) -> int:
    timeout_s = _positive_int(raw.get("timeout_s"), default_s)
    if timeout_s <= 0:
        raise PlanningError(f"task {task_id!r}: artifact {index + 1} timeout_s must be positive")
    return timeout_s


def _modelscope_patterns(value: Any, task_id: str, index: int, field: str) -> List[str]:
    patterns: List[str] = []
    for item in _as_list(value):
        pattern = str(item).strip()
        if not pattern or pattern.startswith("-") or any(ord(char) < 32 for char in pattern):
            raise PlanningError(
                f"task {task_id!r}: artifact {index + 1} has an invalid {field} pattern {item!r}"
            )
        patterns.append(pattern)
    return patterns


def _required_model_files(
    raw: Mapping[str, Any], task_id: str, index: int, kind: str, mode: str
) -> List[str]:
    values: List[str] = []
    for item in _as_list(raw.get("required_files")):
        path = str(item).strip()
        parts = Path(path).parts
        if (
            not path
            or mode != "directory"
            or kind != "model"
            or path.startswith(("/", "~/"))
            or ".." in parts
            or any(ord(char) < 32 for char in path)
        ):
            raise PlanningError(
                f"task {task_id!r}: artifact {index + 1} has an invalid required model file {item!r}"
            )
        values.append(path)
    return values


def _local_artifact(
    raw: Mapping[str, Any],
    task_id: str,
    index: int,
    kind: str,
    source_text: str,
    root: Path,
) -> Dict[str, Any]:
    for field in _MODELSCOPE_ONLY_FIELDS:
        if raw.get(field) not in (None, "", [], {}):
            raise PlanningError(
                f"task {task_id!r}: artifact {index + 1} field {field!r} applies only to modelscope sources"
            )
    modelscope_id = str(raw.get("modelscope") or "").strip()
    if modelscope_id.startswith(MODELSCOPE_SOURCE_PREFIX):
        modelscope_id = modelscope_id[len(MODELSCOPE_SOURCE_PREFIX):].strip()
    if modelscope_id and kind != "model":
        raise PlanningError(
            f"task {task_id!r}: artifact {index + 1} modelscope fallback applies only to models"
        )
    if modelscope_id and not MODELSCOPE_ID_RE.fullmatch(modelscope_id):
        raise PlanningError(
            f"task {task_id!r}: artifact {index + 1} modelscope id must look like owner/name: {modelscope_id!r}"
        )
    revision = str(raw.get("revision") or "").strip()
    if revision and not modelscope_id:
        raise PlanningError(
            f"task {task_id!r}: artifact {index + 1} field 'revision' requires modelscope"
        )
    if revision and not MODELSCOPE_REVISION_RE.fullmatch(revision):
        raise PlanningError(
            f"task {task_id!r}: artifact {index + 1} has an invalid revision {revision!r}"
        )
    allow_patterns = _modelscope_patterns(
        raw.get("allow_patterns"), task_id, index, "allow_patterns"
    )
    ignore_patterns = _modelscope_patterns(
        raw.get("ignore_patterns"), task_id, index, "ignore_patterns"
    )
    if (allow_patterns or ignore_patterns) and not modelscope_id:
        raise PlanningError(
            f"task {task_id!r}: artifact {index + 1} ModelScope patterns require modelscope"
        )
    parsed_source = urlparse(source_text)
    if (
        source_text.startswith("//")
        or "://" in source_text
        or parsed_source.scheme.lower() in _NETWORK_ARTIFACT_SCHEMES
    ):
        raise PlanningError(
            f"task {task_id!r}: artifact sources must be local controller paths or modelscope: references"
        )
    source = Path(source_text).expanduser()
    if not source.is_absolute():
        source = root / source
    try:
        source = source.resolve(strict=True)
    except OSError as exc:
        raise PlanningError(
            f"task {task_id!r}: artifact source is unavailable: {source}"
        ) from exc
    if source == Path("/") or source == Path.home():
        raise PlanningError(f"task {task_id!r}: artifact source is too broad: {source}")
    if ".ssh" in source.parts or source.name.lower() in _PRIVATE_SOURCE_NAMES:
        raise PlanningError(f"task {task_id!r}: private-key paths cannot be artifact sources")
    if not source.is_file() and not source.is_dir():
        raise PlanningError(f"task {task_id!r}: artifact source is not a file or directory: {source}")
    actual_mode = "file" if source.is_file() else "directory"
    mode = str(raw.get("mode") or actual_mode).strip().lower()
    if mode in {"dir", "folder"}:
        mode = "directory"
    if mode not in {"file", "directory"} or mode != actual_mode:
        raise PlanningError(f"task {task_id!r}: artifact {index + 1} mode does not match its source")
    if modelscope_id and mode != "directory":
        raise PlanningError(
            f"task {task_id!r}: ModelScope model acquisition requires a directory fallback source"
        )
    name = _artifact_name(raw.get("name"), source.name, task_id, index)
    destination = _artifact_destination(
        raw.get("destination") or raw.get("target"), kind, name, task_id, index
    )
    checksum = str(raw.get("sha256") or "").strip().lower()
    if checksum and not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise PlanningError(
            f"task {task_id!r}: artifact {index + 1} sha256 must contain 64 hexadecimal characters"
        )
    if checksum and mode != "file":
        raise PlanningError(
            f"task {task_id!r}: artifact {index + 1} sha256 is supported for file artifacts; directory sync uses rsync checksums"
        )
    required_files = _required_model_files(raw, task_id, index, kind, mode)
    try:
        size_bytes = artifact_source_size(source)
    except OSError as exc:
        raise PlanningError(f"task {task_id!r}: cannot read artifact size: {exc}") from exc
    return {
        "name": name,
        "kind": kind,
        "source": str(source),
        "source_type": "local",
        "model_id": modelscope_id or None,
        "repo_type": "model" if modelscope_id else None,
        "revision": revision,
        "allow_patterns": allow_patterns,
        "ignore_patterns": ignore_patterns,
        "destination": destination,
        "mode": mode,
        "required_files": required_files,
        "size_bytes": size_bytes,
        "sha256": checksum or None,
        "timeout_s": _artifact_timeout(
            raw,
            task_id,
            index,
            MODELSCOPE_DEFAULT_TIMEOUT_S if modelscope_id else 3600,
        ),
    }


def _modelscope_artifact(
    raw: Mapping[str, Any],
    task_id: str,
    index: int,
    kind: str,
    source_text: str,
) -> Dict[str, Any]:
    model_id = source_text[len(MODELSCOPE_SOURCE_PREFIX):].strip()
    if not MODELSCOPE_ID_RE.fullmatch(model_id):
        raise PlanningError(
            f"task {task_id!r}: artifact {index + 1} modelscope id must look like owner/name: {model_id!r}"
        )
    default_repo_type = "model" if kind == "model" else "dataset"
    repo_type = str(raw.get("repo_type") or default_repo_type).strip().lower()
    if repo_type not in MODELSCOPE_REPO_TYPES:
        raise PlanningError(
            f"task {task_id!r}: artifact {index + 1} repo_type must be model or dataset"
        )
    revision = str(raw.get("revision") or "").strip()
    if revision and not MODELSCOPE_REVISION_RE.fullmatch(revision):
        raise PlanningError(
            f"task {task_id!r}: artifact {index + 1} has an invalid revision {revision!r}"
        )
    allow_patterns = _modelscope_patterns(raw.get("allow_patterns"), task_id, index, "allow_patterns")
    ignore_patterns = _modelscope_patterns(raw.get("ignore_patterns"), task_id, index, "ignore_patterns")
    if raw.get("sha256"):
        raise PlanningError(
            f"task {task_id!r}: artifact {index + 1} cannot pin sha256 for a modelscope snapshot"
        )
    mode = str(raw.get("mode") or "directory").strip().lower()
    if mode in {"dir", "folder"}:
        mode = "directory"
    if mode != "directory":
        raise PlanningError(
            f"task {task_id!r}: artifact {index + 1} is a modelscope directory snapshot"
        )
    size_bytes = parse_size(raw.get("size_bytes"), default_unit="b")
    if size_bytes <= 0:
        raise PlanningError(
            f"task {task_id!r}: artifact {index + 1} must declare a positive size_bytes for a modelscope source"
        )
    name = _artifact_name(raw.get("name"), model_id.rsplit("/", 1)[-1], task_id, index)
    required_files = _required_model_files(raw, task_id, index, kind, "directory")
    destination = _artifact_destination(
        raw.get("destination") or raw.get("target"), kind, name, task_id, index
    )
    return {
        "name": name,
        "kind": kind,
        "source": source_text,
        "source_type": "modelscope",
        "model_id": model_id,
        "repo_type": repo_type,
        "revision": revision,
        "allow_patterns": allow_patterns,
        "ignore_patterns": ignore_patterns,
        "destination": destination,
        "mode": "directory",
        "required_files": required_files,
        "size_bytes": size_bytes,
        "sha256": None,
        "timeout_s": _artifact_timeout(raw, task_id, index, MODELSCOPE_DEFAULT_TIMEOUT_S),
    }


def _normalise_artifacts(
    raw_artifacts: Any,
    task_id: str,
    source_root: Optional[Path],
) -> List[Dict[str, Any]]:
    if raw_artifacts is None:
        raw_artifacts = []
    if not isinstance(raw_artifacts, list):
        raise PlanningError(f"task {task_id!r}: artifacts must be a list")
    root = Path(source_root or Path.cwd()).expanduser().resolve()
    artifacts: List[Dict[str, Any]] = []
    names: Set[str] = set()
    destinations: Set[str] = set()
    for index, raw in enumerate(raw_artifacts):
        if not isinstance(raw, Mapping):
            raise PlanningError(f"task {task_id!r}: artifact {index + 1} must be an object")
        kind = str(raw.get("kind") or raw.get("type") or "").strip().lower()
        if kind not in {"model", "data", "source"}:
            raise PlanningError(
                f"task {task_id!r}: artifact {index + 1} kind must be model, data, or source"
            )
        source_text = str(raw.get("source") or "").strip()
        if not source_text:
            raise PlanningError(f"task {task_id!r}: artifact {index + 1} has no source")
        if source_text.startswith(MODELSCOPE_SOURCE_PREFIX):
            if kind == "source":
                raise PlanningError(
                    f"task {task_id!r}: source artifacts require a local controller path"
                )
            artifact = _modelscope_artifact(raw, task_id, index, kind, source_text)
        else:
            artifact = _local_artifact(raw, task_id, index, kind, source_text, root)
        if artifact["name"] in names:
            raise PlanningError(f"task {task_id!r}: duplicate artifact name {artifact['name']!r}")
        names.add(artifact["name"])
        if artifact["destination"] in destinations:
            raise PlanningError(
                f"task {task_id!r}: duplicate artifact destination {artifact['destination']!r}"
            )
        destinations.add(artifact["destination"])
        artifacts.append(artifact)
    return artifacts


def _normalise_task(
    raw: Mapping[str, Any],
    index: int,
    defaults: Mapping[str, Any],
    source_root: Optional[Path] = None,
) -> Dict[str, Any]:
    data = _merge_defaults(defaults, raw)
    task_id = str(data.get("id") or data.get("name") or f"task-{index + 1}").strip()
    if not task_id:
        raise PlanningError(f"task {index + 1} has an empty id")
    command = str(data.get("command") or "").strip()
    if not command:
        raise PlanningError(f"task {task_id!r} has no command")

    gpu_count_value = data.get("gpu_count", data.get("gpus", 1))
    # A list in gpus is treated as an explicit count/list of requested GPU
    # labels only for manifest readability; physical indices are selected from
    # the inventory and are never trusted from the input.
    if isinstance(gpu_count_value, list):
        gpu_count = len(gpu_count_value)
    else:
        try:
            gpu_count = int(gpu_count_value)
        except (TypeError, ValueError) as exc:
            raise PlanningError(f"task {task_id!r}: invalid GPU count {gpu_count_value!r}") from exc
    if gpu_count < 0:
        raise PlanningError(f"task {task_id!r} must not request a negative GPU count")
    if bool(data.get("cpu_only", False)):
        gpu_count = 0
    distribution = str(data.get("distribution", data.get("dist_mode", "single"))).lower()
    if distribution not in {"single", "one-to-one"}:
        raise PlanningError(f"task {task_id!r}: distribution must be single or one-to-one")
    if gpu_count == 0 and distribution == "one-to-one":
        raise PlanningError(f"task {task_id!r}: CPU-only tasks require distribution=single")

    if data.get("min_memory_bytes") is not None:
        memory_bytes = parse_size(data.get("min_memory_bytes"), default_unit="b")
    elif data.get("min_memory") is not None:
        memory_bytes = parse_size(data.get("min_memory"), default_unit="gib")
    else:
        memory_bytes = parse_size(data.get("memory_gib", 0), default_unit="gib")
    if data.get("min_disk_bytes") is not None:
        disk_bytes = parse_size(data.get("min_disk_bytes"), default_unit="b")
    elif data.get("min_disk") is not None:
        disk_bytes = parse_size(data.get("min_disk"), default_unit="gib")
    else:
        disk_bytes = parse_size(data.get("disk_gib", 0), default_unit="gib")
    gpu_memory_mib = _positive_int(data.get("min_gpu_memory_mib", 0))
    if data.get("min_gpu_memory_gib") is not None:
        gpu_memory_mib = max(gpu_memory_mib, int(parse_size(data["min_gpu_memory_gib"], default_unit="gib") / (1024**2)))
    gpu_free_memory_mib = _positive_int(data.get("min_gpu_free_memory_mib", 0))
    if data.get("min_gpu_free_memory_gib") is not None:
        gpu_free_memory_mib = max(
            gpu_free_memory_mib,
            int(parse_size(data["min_gpu_free_memory_gib"], default_unit="gib") / (1024**2)),
        )

    raw_env = data.get("env", {})
    if raw_env is None:
        raw_env = {}
    if not isinstance(raw_env, dict):
        raise PlanningError(f"task {task_id!r}: env must be an object")
    env = {str(k): str(v) for k, v in raw_env.items()}
    raw_environment = data.get("environment", "")
    # ``environment`` was historically an alias for ``uv_env``.  Keep that
    # compatibility while reserving mapping-shaped values for future metadata.
    legacy_environment = "" if isinstance(raw_environment, Mapping) else str(raw_environment or "")
    metadata = dict(data.get("metadata") or {}) if isinstance(data.get("metadata", {}), dict) else {}
    environment_maintenance = bool(data.get("environment_maintenance", False)) or str(
        metadata.get("role", "")
    ).strip().lower() in {"environment", "environment_maintenance", "env-maintenance"}
    artifacts = _normalise_artifacts(data.get("artifacts"), task_id, source_root)
    artifact_bytes = sum(_positive_int(item.get("size_bytes"), 0) for item in artifacts)

    depends_on = _string_list(data.get("depends_on", data.get("dependencies", [])))
    nodes = _string_list(data.get("nodes", data.get("node_selector", [])))
    try:
        max_gpu_utilization = float(
            data.get("max_gpu_utilization_percent", data.get("max_gpu_util", 5))
        )
    except (TypeError, ValueError) as exc:
        raise PlanningError(f"task {task_id!r}: invalid max GPU utilization") from exc
    task = {
        "id": task_id,
        "command": command,
        "commands": [str(x) for x in _as_list(data.get("commands"))],
        "distribution": distribution,
        "gpu_count": gpu_count,
        "replicas": max(1, _positive_int(data.get("replicas"), 1)),
        "gpu_type": str(data.get("gpu_type", data.get("gpu_model", "")) or "").strip(),
        "min_gpu_memory_mib": gpu_memory_mib,
        "min_gpu_free_memory_mib": gpu_free_memory_mib,
        "max_gpu_utilization_percent": max_gpu_utilization,
        "min_cpu_cores": _positive_int(data.get("min_cpu_cores", data.get("cpu_cores", 0))),
        "min_memory_bytes": memory_bytes,
        "min_disk_bytes": disk_bytes,
        "disk_path": str(data.get("disk_path", "") or "").strip(),
        "uv_env": str(data.get("uv_env", legacy_environment) or "").strip(),
        "env_manager": str(data.get("env_manager", "") or "").strip(),
        "environment_check": str(data.get("environment_check", "") or "").strip(),
        "environment_repair_task": str(
            data.get("environment_repair_task", data.get("environment_setup_task", "")) or ""
        ).strip(),
        "environment_repair_command": str(data.get("environment_repair_command", "") or "").strip(),
        "environment_maintenance": environment_maintenance,
        "user": str(data.get("user", data.get("run_as", "")) or "").strip(),
        "work_dir": str(data.get("work_dir", "") or "").strip(),
        "prefer_local": None if "prefer_local" not in data else bool(data.get("prefer_local")),
        "artifacts": artifacts,
        "artifact_bytes": artifact_bytes,
        "depends_on": depends_on,
        "nodes": nodes,
        "placement": str(data.get("placement", "pack") or "pack").lower(),
        "same_node": bool(data.get("same_node", False)),
        "min_nodes": max(0, _positive_int(data.get("min_nodes"), 0)),
        "max_nodes": max(0, _positive_int(data.get("max_nodes"), 0)),
        "distributed": bool(data.get("distributed", False)),
        "master_port": _positive_int(data.get("master_port"), 29500) or 29500,
        "env": env,
        "metadata": metadata,
    }
    if task["placement"] not in {"pack", "spread", "performance"}:
        raise PlanningError(f"task {task_id!r}: placement must be pack, spread, or performance")
    if not math.isfinite(task["max_gpu_utilization_percent"]) or task["max_gpu_utilization_percent"] < 0 or task["max_gpu_utilization_percent"] > 100:
        raise PlanningError(f"task {task_id!r}: max_gpu_utilization_percent must be 0..100")
    if task["master_port"] < 1 or task["master_port"] > 65535:
        raise PlanningError(f"task {task_id!r}: master_port must be 1..65535")
    if task["max_nodes"] and task["min_nodes"] > task["max_nodes"]:
        raise PlanningError(f"task {task_id!r}: min_nodes exceeds max_nodes")
    required_gpus = task["gpu_count"] * task["replicas"]
    if task["min_nodes"] > required_gpus:
        raise PlanningError(f"task {task_id!r}: min_nodes exceeds the number of requested GPUs")
    if task["same_node"] and task["min_nodes"] > 1:
        raise PlanningError(f"task {task_id!r}: same_node cannot be combined with min_nodes > 1")
    return task


def normalise_tasks(
    data: Mapping[str, Any],
    source_root: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        raise PlanningError("manifest defaults must be an object")
    tasks = [
        _normalise_task(raw, i, defaults, source_root)
        for i, raw in enumerate(data.get("tasks", []))
        if isinstance(raw, dict)
    ]
    if len(tasks) != len(data.get("tasks", [])):
        raise PlanningError("each task must be an object")
    ids = [task["id"] for task in tasks]
    if len(ids) != len(set(ids)):
        raise PlanningError("task ids must be unique")
    ordinary_tasks = [task for task in tasks if not is_environment_maintenance(task)]
    default_prefer_local = len(ordinary_tasks) == 1
    for task in tasks:
        if task["prefer_local"] is None:
            task["prefer_local"] = default_prefer_local
    return tasks


def topological_order(tasks: Sequence[Mapping[str, Any]]) -> List[str]:
    """Return a deterministic dependency order and reject missing/cyclic deps."""

    ids = {str(task["id"]) for task in tasks}
    incoming: Dict[str, Set[str]] = {task_id: set() for task_id in ids}
    outgoing: Dict[str, Set[str]] = defaultdict(set)
    for task in tasks:
        task_id = str(task["id"])
        for dep in _string_list(task.get("depends_on", [])):
            if dep not in ids:
                raise PlanningError(f"task {task_id!r} depends on unknown task {dep!r}")
            if dep == task_id:
                raise PlanningError(f"task {task_id!r} cannot depend on itself")
            incoming[task_id].add(dep)
            outgoing[dep].add(task_id)

    ready = deque(sorted(task_id for task_id, deps in incoming.items() if not deps))
    order: List[str] = []
    while ready:
        current = ready.popleft()
        order.append(current)
        for child in sorted(outgoing.get(current, set())):
            incoming[child].discard(current)
            if not incoming[child]:
                ready.append(child)
    if len(order) != len(ids):
        cyclic = sorted(task_id for task_id, deps in incoming.items() if deps)
        raise PlanningError(f"dependency cycle detected involving: {', '.join(cyclic)}")
    return order


def _node_hardware(node: Mapping[str, Any]) -> Mapping[str, Any]:
    value = node.get("hardware", {})
    return value if isinstance(value, Mapping) else {}


def _is_local_inventory_node(node: Mapping[str, Any]) -> bool:
    if node.get("local") is True:
        return True
    ssh = node.get("ssh", {}) if isinstance(node.get("ssh"), Mapping) else {}
    address = str(ssh.get("address", node.get("address", "")) or "").strip()
    if not address:
        return False
    return is_local_node(
        NodeConfig(
            alias=str(node.get("alias") or "local"),
            address=address,
            user=str(ssh.get("user", node.get("user", "")) or ""),
            port=_positive_int(ssh.get("port"), 22) or 22,
        )
    )


def _local_preference_rank(node: Mapping[str, Any], task: Mapping[str, Any]) -> int:
    return 0 if task.get("prefer_local") and _is_local_inventory_node(node) else 1


def _node_gpu_devices(
    node: Mapping[str, Any],
    gpu_states: Optional[Mapping[Tuple[str, int], str]] = None,
) -> List[Mapping[str, Any]]:
    gpu = _node_hardware(node).get("gpu", {})
    devices: List[Mapping[str, Any]] = []
    if isinstance(gpu, Mapping) and isinstance(gpu.get("devices"), list):
        devices = [item for item in gpu["devices"] if isinstance(item, Mapping)]
    elif isinstance(node.get("gpus"), list):
        devices = [item for item in node["gpus"] if isinstance(item, Mapping)]
    if gpu_states is None:
        return devices
    alias = str(node.get("alias") or "")
    annotated: List[Mapping[str, Any]] = []
    for device in devices:
        merged = dict(device)
        try:
            index = int(device.get("index"))
        except (TypeError, ValueError):
            merged["state"] = "occupied"
            annotated.append(merged)
            continue
        merged["state"] = str(gpu_states.get((alias, index)) or "occupied")
        annotated.append(merged)
    return annotated


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _memory_available(node: Mapping[str, Any]) -> int:
    memory = _node_hardware(node).get("memory", {})
    if not isinstance(memory, Mapping):
        return 0
    return _positive_int(memory.get("available_bytes", memory.get("total_bytes", 0)), 0)


def _disk_path(disk: Mapping[str, Any]) -> str:
    path = str(disk.get("path") or "").rstrip("/")
    return path or ("/" if disk.get("path") else "")


def _disk_candidates(node: Mapping[str, Any], requested_path: str = "") -> List[Mapping[str, Any]]:
    disks = _node_hardware(node).get("disks", [])
    if not isinstance(disks, list):
        return []
    out: List[Mapping[str, Any]] = []
    requested = requested_path.rstrip("/") or ("/" if requested_path else "")
    if requested:
        errors = node.get("errors", [])
        if isinstance(errors, list):
            for error in errors:
                error_path = str(error).split(":", 1)[0].rstrip("/") or "/"
                if error_path == requested or error_path.startswith(requested + "/") or requested.startswith(error_path + "/"):
                    return []
    for disk in disks:
        if not isinstance(disk, Mapping):
            continue
        path = _disk_path(disk)
        if not path:
            continue
        if requested and not (
            path == requested
            or (path == "/" and requested.startswith("/"))
            or requested.startswith(path + "/")
            or path.startswith(requested + "/")
        ):
            continue
        out.append(disk)
    if requested:
        exact = [disk for disk in out if _disk_path(disk) == requested]
        if exact:
            return exact
    return out


def _preferred_disk(disks: Sequence[Mapping[str, Any]], requested_path: str = "") -> Optional[Mapping[str, Any]]:
    """Choose a work filesystem, preferring the largest mounted data volume.

    With no explicit path, a non-root filesystem larger than the system root is
    treated as the node's data disk.  This makes the storage path follow the
    selected node instead of hard-coding one cluster-wide path.  An explicit
    path always wins; the root filesystem is the safe fallback when no larger
    data volume is mounted.
    """

    if not disks:
        return None
    requested = requested_path.rstrip("/") or ("/" if requested_path else "")
    pool = list(disks)
    if not requested:
        root_disks = [disk for disk in pool if _disk_path(disk) == "/"]
        root_total = max((_positive_int(disk.get("total_bytes"), 0) for disk in root_disks), default=0)
        non_root = [disk for disk in pool if _disk_path(disk) not in {"", "/"}]
        # A data filesystem should be materially larger than boot/system
        # mounts.  Comparing against root also avoids selecting /boot or /boot/efi
        # on nodes that have no mounted data disk.
        if non_root and (not root_disks or root_total <= 0):
            pool = non_root
        else:
            larger_data = [
                disk
                for disk in non_root
                if _positive_int(disk.get("total_bytes"), 0) > root_total
            ]
            if larger_data:
                pool = larger_data
    return max(
        pool,
        key=lambda disk: (
            _positive_int(disk.get("total_bytes"), 0),
            _positive_int(disk.get("available_bytes"), 0),
            _disk_path(disk),
        ),
    )


def _selected_environment(node: Mapping[str, Any], task: Mapping[str, Any]) -> Optional[Dict[str, str]]:
    """Return the best recorded environment to use on a selected node."""

    requested = str(task.get("uv_env") or "").strip().lower()
    manager = str(task.get("env_manager") or "").strip().lower()
    if not requested and not manager:
        return None

    configured = node.get("configured", {})
    if isinstance(configured, Mapping):
        configured_manager = str(configured.get("env_manager") or "").strip()
        configured_name = str(configured.get("env_name") or "").strip()
        if (manager and manager == configured_manager.lower()) and (
            not requested or requested == configured_name.lower()
        ):
            return {"manager": configured_manager, "name": configured_name}
        if requested and requested == configured_name.lower():
            return {"manager": configured_manager, "name": configured_name}

    software = node.get("software", {})
    if not isinstance(software, Mapping):
        software = {}
    uv = software.get("uv", {})
    if (
        (requested in {"uv", "uv-managed"} or (manager in {"uv", "uv-managed"} and not requested))
        and isinstance(uv, Mapping)
        and uv.get("installed")
    ):
        return {"manager": "uv", "name": ""}

    environments = software.get("environments", [])
    if not isinstance(environments, list):
        environments = []
    for env in environments:
        if not isinstance(env, Mapping):
            continue
        env_manager = str(env.get("manager") or "").strip()
        env_name = str(env.get("name") or "").strip()
        env_path = str(env.get("path") or "").strip()
        env_manager_lower = env_manager.lower()
        env_name_lower = env_name.lower()
        env_path_lower = env_path.lower()
        if manager and manager != env_manager_lower:
            continue
        if requested and requested not in {env_name_lower, env_path_lower, env_manager_lower} and requested not in env_path_lower:
            continue
        return {"manager": env_manager, "name": env_path or env_name}

    projects = software.get("projects", [])
    if requested and isinstance(projects, list):
        for project in projects:
            if not isinstance(project, Mapping):
                continue
            project_manager = str(project.get("manager") or "").strip()
            project_path = str(project.get("path") or "").strip()
            project_manager_lower = project_manager.lower()
            project_path_lower = project_path.lower()
            if manager and manager != "uv" and manager != project_manager_lower:
                continue
            if requested in project_path_lower or project_path_lower.endswith("/" + requested):
                return {"manager": project_manager or "uv", "name": project_path}
    if manager and manager not in {"uv", "uv-managed"}:
        raise PlanningError(
            f"environment manager {manager!r} is read-only legacy; "
            "new environments must use uv"
        )
    return None


def _environment_status(node: Mapping[str, Any], task: Mapping[str, Any]) -> str:
    requested = bool(str(task.get("uv_env") or "").strip() or str(task.get("env_manager") or "").strip())
    if not requested:
        return "not_required"
    declared = str(node.get("environment_status") or "").strip()
    if declared in {"conflict", "missing"}:
        if task.get("environment_check") and (
            task.get("environment_repair_task") or task.get("environment_repair_command")
        ):
            return "unverified"
        return "missing"
    if _selected_environment(node, task) is not None:
        return "recorded"
    if task.get("environment_check") and (
        task.get("environment_repair_task") or task.get("environment_repair_command")
    ):
        return "unverified"
    return "missing"


def _environment_matches(node: Mapping[str, Any], task: Mapping[str, Any]) -> bool:
    return _environment_status(node, task) in {"not_required", "recorded", "unverified"}


def is_environment_maintenance(task: Mapping[str, Any]) -> bool:
    """Return whether a task is a dormant repair action, not normal work."""

    if bool(task.get("environment_maintenance", False)):
        return True
    metadata = task.get("metadata", {})
    return isinstance(metadata, Mapping) and str(metadata.get("role", "")).strip().lower() in {
        "environment",
        "environment_maintenance",
        "env-maintenance",
    }


def _default_work_dir(
    node: Mapping[str, Any],
    task: Mapping[str, Any],
    disk: Optional[Mapping[str, Any]],
) -> str:
    """Resolve the default per-node work directory on the selected data disk."""

    explicit = str(task.get("work_dir") or "").strip()
    if explicit:
        return explicit
    registered = str(node.get("work_path") or "").strip()
    if registered:
        return registered
    configured = node.get("configured", {})
    if isinstance(configured, Mapping):
        configured_work_dir = str(configured.get("work_dir") or "").strip()
        if configured_work_dir:
            return configured_work_dir
    path = _disk_path(disk) if isinstance(disk, Mapping) else ""
    ssh = node.get("ssh", {})
    user = ""
    if isinstance(ssh, Mapping):
        user = str(ssh.get("user") or "").strip()
    user = user or str(node.get("user") or "").strip()
    if not path or not user:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", user):
        raise PlanningError(f"invalid SSH user for default work directory: {user!r}")
    return f"{path.rstrip('/')}/data_{user}/adaptive"


def _node_eligible(
    node: Mapping[str, Any],
    task: Mapping[str, Any],
    *,
    reserved_cpu: int = 0,
    reserved_memory: int = 0,
    reserved_disk: int = 0,
) -> Tuple[bool, str, Optional[Mapping[str, Any]]]:
    if node.get("reachable", True) is False or node.get("stale", False):
        return False, "node is unreachable or inventory is stale", None
    alias = str(node.get("alias") or "")
    selectors = [str(x) for x in task.get("nodes", [])]
    if selectors and alias not in selectors:
        return False, "not in node selector", None
    cpu = _node_hardware(node).get("cpu", {})
    logical = _positive_int(cpu.get("logical_cores", 0), 0) if isinstance(cpu, Mapping) else 0
    if logical < _positive_int(task.get("min_cpu_cores"), 0) + reserved_cpu:
        return False, "not enough CPU cores", None
    if _memory_available(node) < _positive_int(task.get("min_memory_bytes"), 0) + reserved_memory:
        return False, "not enough available memory", None
    requested_disk_path = str(task.get("disk_path") or "").strip()
    work_path = str(node.get("work_path") or "").strip()
    capacity_path = requested_disk_path or work_path
    disks = _disk_candidates(node, capacity_path)
    min_disk = _positive_int(task.get("min_disk_bytes"), 0)
    if capacity_path and not disks:
        return (
            False,
            "requested disk path is unavailable"
            if requested_disk_path
            else "work path is not on a mounted filesystem",
            None,
        )
    selected_disk = _preferred_disk(disks, capacity_path)
    artifact_bytes = _positive_int(task.get("artifact_bytes"), 0)
    required_disk = min_disk + artifact_bytes
    if required_disk and (
        selected_disk is None
        or _positive_int(selected_disk.get("available_bytes"), 0) < required_disk + reserved_disk
    ):
        return False, "not enough available disk space", None
    if not _environment_matches(node, task):
        return False, "requested uv/environment is unavailable", None
    return True, "", selected_disk


def _gpu_eligible(
    gpu: Mapping[str, Any],
    task: Mapping[str, Any],
    *,
    allocation_target: Optional[str] = None,
    current_user: str = "",
    require_allocation: bool = False,
    all_users_token: str = "*",
) -> Tuple[bool, str]:
    state = gpu.get("state")
    if state is not None:
        if state != "idle":
            return False, "GPU is occupied"
    else:
        if gpu.get("available") is False:
            return False, "GPU has an active process"
        if _positive_int(gpu.get("process_count"), 0) > 0:
            return False, "GPU has an active process"
        if isinstance(gpu.get("processes"), list) and gpu.get("processes"):
            return False, "GPU has an active process"
    target = str(
        allocation_target if allocation_target is not None else gpu.get("allocation_target") or ""
    ).strip()
    requested_user = str(current_user or task.get("user") or "").strip()
    if target and target not in {"*", all_users_token, requested_user}:
        return False, f"GPU is allocated to {target}"
    if require_allocation and not target:
        return False, "GPU has no allocation"
    util = _number(gpu.get("utilization_gpu_percent"), 0)
    if util > _number(task.get("max_gpu_utilization_percent"), 5):
        return False, "GPU utilization exceeds requested limit"
    wanted = str(task.get("gpu_type") or "").strip().lower()
    if wanted and wanted not in str(gpu.get("name") or "").lower():
        return False, "GPU model mismatch"
    minimum_mib = _positive_int(task.get("min_gpu_memory_mib"), 0)
    if _positive_int(gpu.get("memory_total_mib"), 0) < minimum_mib:
        return False, "GPU memory is below requested minimum"
    minimum_free_mib = _positive_int(task.get("min_gpu_free_memory_mib"), 0)
    if minimum_free_mib and _positive_int(gpu.get("memory_free_mib"), 0) < minimum_free_mib:
        return False, "GPU free memory is below requested minimum"
    return True, ""


def _gpu_score(gpu: Mapping[str, Any], node: Mapping[str, Any], task: Mapping[str, Any]) -> Tuple[Any, ...]:
    efficiency = gpu.get("compute_efficiency", {})
    if not isinstance(efficiency, Mapping):
        efficiency = {}
    benchmark = _number(efficiency.get("benchmark_score"), _number(gpu.get("benchmark_score"), 0))
    util = _number(gpu.get("utilization_gpu_percent"), 0)
    free_memory = _positive_int(gpu.get("memory_free_mib"), 0)
    placement = str(task.get("placement") or "pack")
    # Lower tuple components are preferred.  Performance placement prioritises
    # the measured benchmark; otherwise idleness is the first concern.
    if placement == "performance":
        return (-benchmark, util, -free_memory, str(node.get("alias")), _positive_int(gpu.get("index"), 0))
    return (util, -benchmark, -free_memory, str(node.get("alias")), _positive_int(gpu.get("index"), 0))


def _choose_gpus(
    nodes: Sequence[Mapping[str, Any]],
    task: Mapping[str, Any],
    reserved: Set[Tuple[str, int]],
    *,
    resource_reservations: Optional[Mapping[str, Mapping[str, int]]] = None,
    allocation_map: Optional[Mapping[Tuple[str, int], str]] = None,
    current_user: str = "",
    require_allocation: bool = False,
    all_users_token: str = "*",
    gpu_states: Optional[Mapping[Tuple[str, int], str]] = None,
) -> Tuple[List[Dict[str, Any]], Optional[Mapping[str, Any]], List[str]]:
    required = _positive_int(task.get("gpu_count"), 1) * max(1, _positive_int(task.get("replicas"), 1))
    eligible_nodes: List[Tuple[Mapping[str, Any], Optional[Mapping[str, Any]]]] = []
    reasons: List[str] = []
    resource_reservations = resource_reservations or {}
    allocation_map = allocation_map or {}
    for node in nodes:
        alias = str(node.get("alias") or "")
        reservation = resource_reservations.get(alias, {})
        ok, reason, disk = _node_eligible(
            node,
            task,
            reserved_cpu=_positive_int(reservation.get("cpu"), 0),
            reserved_memory=_positive_int(reservation.get("memory"), 0),
            reserved_disk=_positive_int(reservation.get("disk"), 0),
        )
        if ok:
            eligible_nodes.append((node, disk))
        elif reason and len(reasons) < 12:
            reasons.append(f"{node.get('alias', '?')}: {reason}")

    # A zero-GPU task is a useful CPU/data/preparation module.  It still gets
    # a node selected using the CPU/RAM/disk/environment constraints.
    if required == 0:
        if not eligible_nodes:
            return [], None, reasons
        node, disk = sorted(
            eligible_nodes,
            key=lambda item: (
                _local_preference_rank(item[0], task),
                str(item[0].get("alias") or ""),
            ),
        )[0]
        return [
            {
                "node_alias": str(node.get("alias") or ""),
                "gpu_indices": [],
                "gpus": [],
                "storage_path": disk.get("path") if isinstance(disk, Mapping) else None,
                "work_dir": _default_work_dir(node, task, disk),
                "data_path": node.get("data_path"),
                "model_path": node.get("model_path"),
                "environment": _selected_environment(node, task),
                "environment_status": _environment_status(node, task),
            }
        ], disk, reasons

    def candidate_score(
        item: Tuple[Mapping[str, Any], Mapping[str, Any], Optional[Mapping[str, Any]]]
    ) -> Tuple[Any, ...]:
        node, gpu, _disk = item
        alias = str(node.get("alias") or "")
        try:
            index = int(gpu.get("index"))
        except (TypeError, ValueError):
            index = -1
        target = str(
            allocation_map.get((alias, index))
            if (alias, index) in allocation_map
            else gpu.get("allocation_target") or ""
        ).strip()
        requested_user = str(current_user or task.get("user") or "").strip()
        if target and target == requested_user:
            allocation_rank = 0
        elif target in {"*", all_users_token}:
            allocation_rank = 1
        else:
            allocation_rank = 2
        return (allocation_rank, _local_preference_rank(node, task)) + _gpu_score(gpu, node, task)

    groups: Dict[str, List[Tuple[Mapping[str, Any], Mapping[str, Any], Optional[Mapping[str, Any]]]]] = defaultdict(list)
    for node, disk in eligible_nodes:
        alias = str(node.get("alias") or "")
        for gpu in _node_gpu_devices(node, gpu_states):
            try:
                index = int(gpu.get("index"))
            except (TypeError, ValueError):
                index = -1
            if index < 0 or (alias, index) in reserved:
                continue
            ok, reason = _gpu_eligible(
                gpu,
                task,
                allocation_target=allocation_map.get((alias, index)),
                current_user=current_user,
                require_allocation=require_allocation,
                all_users_token=all_users_token,
            )
            if ok:
                groups[alias].append((node, gpu, disk))
            elif reason and len(reasons) < 12:
                reasons.append(f"{alias}: GPU{index}: {reason}")

    for alias in groups:
        groups[alias].sort(key=candidate_score)

    if task.get("same_node"):
        possible = [items for items in groups.values() if len(items) >= required]
        if not possible:
            return [], None, reasons
        chosen_group = min(
            possible,
            key=lambda items: (candidate_score(items[0]), str(items[0][0].get("alias"))),
        )
        chosen = chosen_group[:required]
    else:
        placement = str(task.get("placement") or "pack")
        if placement == "pack":
            # Prefer a node with more capacity, then its best GPU.  A single
            # ordinary task opts into local capacity first and spills remotely
            # only when the local node cannot satisfy the request.
            if task.get("prefer_local"):
                groups_ordered = sorted(
                    groups.values(),
                    key=lambda items: (candidate_score(items[0]), -len(items)),
                )
            else:
                groups_ordered = sorted(
                    groups.values(),
                    key=lambda items: (-len(items), candidate_score(items[0])),
                )
            chosen = []
            for group in groups_ordered:
                remaining = required - len(chosen)
                chosen.extend(group[:remaining])
                if len(chosen) >= required:
                    break
        elif placement == "spread":
            groups_ordered = sorted(groups.values(), key=lambda items: (candidate_score(items[0]), str(items[0][0].get("alias"))))
            chosen = []
            positions = {id(group): 0 for group in groups_ordered}
            while len(chosen) < required:
                progressed = False
                for group in groups_ordered:
                    pos = positions[id(group)]
                    if pos < len(group):
                        chosen.append(group[pos])
                        positions[id(group)] = pos + 1
                        progressed = True
                        if len(chosen) >= required:
                            break
                if not progressed:
                    break
        else:
            all_items = [item for group in groups.values() for item in group]
            chosen = sorted(all_items, key=candidate_score)[:required]

    if len(chosen) < required:
        return [], None, reasons
    chosen_aliases = {str(item[0].get("alias")) for item in chosen}
    min_nodes = _positive_int(task.get("min_nodes"), 0)
    max_nodes = _positive_int(task.get("max_nodes"), 0)
    if min_nodes and len(chosen_aliases) < min_nodes:
        # Re-select round-robin from the best groups to meet min_nodes.
        available_groups = [group for group in sorted(groups.values(), key=lambda items: candidate_score(items[0])) if group]
        if len(available_groups) < min_nodes:
            return [], None, reasons + [f"requires at least {min_nodes} nodes"]
        chosen = []
        positions = {id(group): 0 for group in available_groups}
        for group in available_groups[:min_nodes]:
            chosen.append(group[0])
            positions[id(group)] = 1
        if len(chosen) < required:
            rest = [item for group in available_groups for item in group[positions[id(group)]:]]
            chosen.extend(sorted(rest, key=candidate_score)[: required - len(chosen)])
    if max_nodes and len({str(item[0].get("alias")) for item in chosen}) > max_nodes:
        return [], None, reasons + [f"requires at most {max_nodes} nodes"]

    selected_disk = None
    for _node, _gpu, disk in chosen:
        if disk is not None and (selected_disk is None or _positive_int(disk.get("available_bytes"), 0) > _positive_int(selected_disk.get("available_bytes"), 0)):
            selected_disk = disk
    assignment = [
        {
            "node_alias": str(node.get("alias") or ""),
            "gpu_index": _positive_int(gpu.get("index"), 0),
            "gpu_uuid": gpu.get("uuid"),
            "gpu_name": gpu.get("name"),
            "memory_total_mib": gpu.get("memory_total_mib"),
            "benchmark_score": (gpu.get("compute_efficiency", {}) or {}).get("benchmark_score") if isinstance(gpu.get("compute_efficiency", {}), Mapping) else None,
            "disk_path": disk.get("path") if isinstance(disk, Mapping) else None,
            "work_dir": _default_work_dir(node, task, disk),
            "data_path": node.get("data_path"),
            "model_path": node.get("model_path"),
            "allocation_target": (
                allocation_map.get((str(node.get("alias") or ""), _positive_int(gpu.get("index"), 0)))
                if (str(node.get("alias") or ""), _positive_int(gpu.get("index"), 0)) in allocation_map
                else gpu.get("allocation_target")
            ),
            "environment": _selected_environment(node, task),
            "environment_status": _environment_status(node, task),
        }
        for node, gpu, disk in chosen
    ]
    return assignment, selected_disk, reasons


def _node_descriptor(node: Mapping[str, Any]) -> Dict[str, Any]:
    """Copy only connection/configuration metadata needed by dispatch."""

    ssh = node.get("ssh", {}) if isinstance(node.get("ssh"), Mapping) else {}
    configured = node.get("configured", {}) if isinstance(node.get("configured"), Mapping) else {}
    return {
        "alias": str(node.get("alias") or ""),
        "ssh": {
            "address": ssh.get("address", node.get("address", "")),
            "user": ssh.get("user", ""),
            "port": ssh.get("port", 22),
            "connect_timeout_s": ssh.get("connect_timeout_s", 6),
            "identity_file": ssh.get("identity_file"),
            "proxy_jump": ssh.get("proxy_jump"),
        },
        "configured": {
            "env_manager": configured.get("env_manager"),
            "env_name": configured.get("env_name"),
            "work_dir": configured.get("work_dir"),
            "storage_paths": configured.get("storage_paths", []),
            "environment_paths": configured.get("environment_paths", []),
        },
        "work_path": node.get("work_path"),
        "data_path": node.get("data_path"),
        "model_path": node.get("model_path"),
    }


def _allocation_expired(raw_allocation: Any) -> bool:
    if isinstance(raw_allocation, Mapping):
        expires_raw = raw_allocation.get("expires_at")
    else:
        expires_raw = getattr(raw_allocation, "expires_at", None)
    if not expires_raw:
        return False
    try:
        value = str(expires_raw).strip().replace("Z", "+00:00")
        expires_at = datetime.fromisoformat(value)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at <= datetime.now(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        # A malformed expiry is safer when treated as active than silently
        # releasing a reservation that an administrator intended to keep.
        return False


def plan_tasks(
    inventory: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    allocations: Optional[Sequence[Any]] = None,
    current_user: str = "",
    require_allocation: bool = False,
    all_users_token: str = "*",
    source_root: Optional[Path] = None,
    gpu_states: Optional[Mapping[Tuple[str, int], str]] = None,
) -> Dict[str, Any]:
    """Allocate every task in a manifest against a fresh inventory snapshot.

    Explicit allocations are treated as reservations: a GPU assigned to another
    user is never selected, while unallocated GPUs remain available for shared
    scheduling unless ``require_allocation`` is enabled. Passing no
    ``allocations`` preserves standalone inventory planning for callers that do
    not use the allocation registry.
    """

    nodes = [node for node in inventory.get("nodes", []) if isinstance(node, Mapping)]
    if not nodes:
        raise PlanningError("inventory contains no nodes")
    aliases = [str(node.get("alias") or "") for node in nodes]
    if any(not alias for alias in aliases):
        raise PlanningError("every inventory node must have an alias")
    if len(aliases) != len(set(aliases)):
        raise PlanningError("inventory node aliases must be unique")
    tasks = normalise_tasks(manifest, source_root=source_root)
    all_users_token = str(all_users_token or "*").strip() or "*"
    order = topological_order(tasks)
    by_id = {str(task["id"]): task for task in tasks}
    reserved: Set[Tuple[str, int]] = set()
    resource_reservations: Dict[str, Dict[str, int]] = defaultdict(lambda: {"cpu": 0, "memory": 0, "disk": 0})
    allocation_map: Dict[Tuple[str, int], str] = {}
    for raw_allocation in allocations or []:
        if _allocation_expired(raw_allocation):
            continue
        if isinstance(raw_allocation, Mapping):
            alias_raw = raw_allocation.get("node_alias")
            index_raw = raw_allocation.get("gpu_index")
            target_raw = raw_allocation.get("target")
        else:
            alias_raw = getattr(raw_allocation, "node_alias", None)
            index_raw = getattr(raw_allocation, "gpu_index", None)
            target_raw = getattr(raw_allocation, "target", None)
        alias = str(alias_raw or "")
        try:
            index = int(index_raw)
        except (TypeError, ValueError):
            continue
        target = str(target_raw or "").strip()
        if target.lower() == "none":
            target = "*"
        if alias and index >= 0 and target:
            allocation_map[(alias, index)] = target
    planned_tasks: List[Dict[str, Any]] = []
    node_descriptors = {str(node.get("alias")): _node_descriptor(node) for node in nodes if node.get("alias")}

    for task_id in order:
        task = by_id[task_id]
        assignments, disk, reasons = _choose_gpus(
            nodes,
            task,
            reserved,
            resource_reservations=resource_reservations,
            allocation_map=allocation_map,
            current_user=current_user,
            require_allocation=require_allocation,
            all_users_token=all_users_token,
            gpu_states=gpu_states,
        )
        if not assignments:
            detail = "; ".join(reasons[:8])
            raise PlanningError(f"task {task_id!r} cannot be scheduled: insufficient matching resources" + (f" ({detail})" if detail else ""))
        # Environment-maintenance tasks are dormant repair recipes.  They keep
        # assignments in the plan so the dispatcher can target a repaired node,
        # but must not reserve GPUs from ordinary workload tasks.
        if not is_environment_maintenance(task):
            for item in assignments:
                if "gpu_index" in item:
                    reserved.add((str(item["node_alias"]), int(item["gpu_index"])))
        grouped: Dict[str, Dict[str, Any]] = {}
        for item in assignments:
            alias = str(item["node_alias"])
            grouped.setdefault(
                alias,
                {
                    "node_alias": alias,
                    "gpu_indices": [],
                    "gpus": [],
                    "storage_path": None,
                    "work_dir": None,
                    "data_path": None,
                    "model_path": None,
                    "environment": None,
                    "environment_status": None,
                },
            )
            if "gpu_index" in item:
                grouped[alias]["gpu_indices"].append(int(item["gpu_index"]))
                grouped[alias]["gpus"].append(item)
                if item.get("disk_path") and not grouped[alias].get("storage_path"):
                    grouped[alias]["storage_path"] = item.get("disk_path")
                if item.get("work_dir") and not grouped[alias].get("work_dir"):
                    grouped[alias]["work_dir"] = item.get("work_dir")
                if item.get("data_path") and not grouped[alias].get("data_path"):
                    grouped[alias]["data_path"] = item.get("data_path")
                if item.get("model_path") and not grouped[alias].get("model_path"):
                    grouped[alias]["model_path"] = item.get("model_path")
                if item.get("environment") and not grouped[alias].get("environment"):
                    grouped[alias]["environment"] = item.get("environment")
                if item.get("environment_status") and (
                    not grouped[alias].get("environment_status")
                    or item.get("environment_status") == "unverified"
                ):
                    grouped[alias]["environment_status"] = item.get("environment_status")
            else:
                grouped[alias]["gpu_indices"].extend(int(index) for index in item.get("gpu_indices", []))
                grouped[alias]["gpus"].extend(item.get("gpus", []))
                if item.get("storage_path") and not grouped[alias].get("storage_path"):
                    grouped[alias]["storage_path"] = item.get("storage_path")
                if item.get("work_dir") and not grouped[alias].get("work_dir"):
                    grouped[alias]["work_dir"] = item.get("work_dir")
                if item.get("data_path") and not grouped[alias].get("data_path"):
                    grouped[alias]["data_path"] = item.get("data_path")
                if item.get("model_path") and not grouped[alias].get("model_path"):
                    grouped[alias]["model_path"] = item.get("model_path")
                if item.get("environment") and not grouped[alias].get("environment"):
                    grouped[alias]["environment"] = item.get("environment")
                if item.get("environment_status") and (
                    not grouped[alias].get("environment_status")
                    or item.get("environment_status") == "unverified"
                ):
                    grouped[alias]["environment_status"] = item.get("environment_status")
        for group in grouped.values():
            group["gpus"].sort(key=lambda item: _positive_int(item.get("gpu_index"), 0))
            group["gpu_indices"] = [
                int(item.get("gpu_index")) for item in group["gpus"] if item.get("gpu_index") is not None
            ]
        task_aliases = {str(item["node_alias"]) for item in assignments}
        if not is_environment_maintenance(task):
            for alias in task_aliases:
                resource_reservations[alias]["cpu"] += _positive_int(task.get("min_cpu_cores"), 0)
                resource_reservations[alias]["memory"] += _positive_int(task.get("min_memory_bytes"), 0)
                resource_reservations[alias]["disk"] += (
                    _positive_int(task.get("min_disk_bytes"), 0)
                    + _positive_int(task.get("artifact_bytes"), 0)
                )
        planned = dict(task)
        planned["assignments"] = [grouped[alias] for alias in sorted(grouped)]
        planned["selected_disk"] = dict(disk) if isinstance(disk, Mapping) else None
        planned["status"] = "planned"
        planned_tasks.append(planned)

    total_gpus = sum(
        sum(len(group.get("gpu_indices", [])) for group in task.get("assignments", []))
        for task in planned_tasks
    )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "inventory_generated_at": inventory.get("generated_at"),
        "source_inventory": inventory.get("collector", {}).get("ssh_config") if isinstance(inventory.get("collector"), Mapping) else None,
        "allocation_policy": {
            "current_user": current_user or None,
            "registry_supplied": allocations is not None,
            "require_allocation": bool(require_allocation),
            "all_users_token": all_users_token,
        },
        "order": order,
        "nodes": node_descriptors,
        "gpu_source": "service" if gpu_states is not None else "snapshot",
        "tasks": planned_tasks,
        "summary": {
            "task_count": len(planned_tasks),
            "gpu_count": total_gpus,
            "node_count": len({assignment["node_alias"] for task in planned_tasks for assignment in task.get("assignments", [])}),
        },
    }


def save_plan(path: Path, plan: Mapping[str, Any]) -> None:
    # A plan contains host topology and identity-file paths.  Keep it private
    # by default, just like the hardware inventory.
    atomic_write_text(path, json.dumps(plan, indent=2, ensure_ascii=False) + "\n")


def load_plan(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise PlanningError(f"invalid plan: {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise PlanningError("plan must be an object with a tasks list")
    return data
