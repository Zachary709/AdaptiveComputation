from __future__ import annotations

import asyncio
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from adaptive_core.models import NodeConfig
from adaptive_core.sshutil import SSHRunError, _ssh_base_cmd, is_local_node, ssh_run
from adaptive_core.state import atomic_write_text


class ArtifactSyncError(ValueError):
    pass


MODELSCOPE_SOURCE_PREFIX = "modelscope:"
MODELSCOPE_ID_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)+$"
)
MODELSCOPE_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
MODELSCOPE_REPO_TYPES = ("model", "dataset")
MODELSCOPE_DEFAULT_TIMEOUT_S = 21600

_MODELSCOPE_BINARY = "modelscope"
_PROXY_ENV_VARS = (
    "ALL_PROXY",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "all_proxy",
    "https_proxy",
    "http_proxy",
)
# modelscope-hub 0.4.x logs per-repo download failures and still exits zero,
# so a successful exit code alone does not prove a complete snapshot.
_MODELSCOPE_FAILURE_MARKERS = (
    "file(s) failed to download",
    "file download failed",
)
_DOWNLOAD_LOCK_STALE_S = 24 * 60 * 60

_PRIVATE_SOURCE_NAMES = {
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "id_xmss",
}


def is_modelscope_source(source: str) -> bool:
    return str(source or "").strip().startswith(MODELSCOPE_SOURCE_PREFIX)


def is_modelscope_artifact(artifact: Mapping[str, Any]) -> bool:
    if str(artifact.get("source_type") or "") == "modelscope":
        return True
    return is_modelscope_source(str(artifact.get("source") or ""))


def artifact_source_size(source: Path) -> int:
    """Return the byte size of a file or directory tree without following symlinks."""

    if source.is_file():
        return max(0, int(source.stat().st_size))
    total = 0
    pending = [source]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += max(0, int(entry.stat(follow_symlinks=False).st_size))
    return total


def _directory_has_entries(path: Path) -> bool:
    if not path.is_dir():
        return False
    with os.scandir(path) as entries:
        return any(True for _ in entries)


def artifact_local_source(artifact: Mapping[str, Any]) -> Path:
    """Return the local path that a prepared artifact transfers from."""

    resolved = str(artifact.get("resolved_source") or "").strip()
    if resolved:
        return Path(resolved).expanduser()
    source = str(artifact.get("source") or "").strip()
    if not source:
        raise ArtifactSyncError(f"artifact {artifact.get('name')!r} has no local source")
    if is_modelscope_source(source):
        raise ArtifactSyncError(
            f"modelscope artifact {artifact.get('name')!r} has not been downloaded"
        )
    return Path(source).expanduser()


def modelscope_request(artifact: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate a modelscope artifact and return its resolved download request."""

    model_id = str(artifact.get("model_id") or "").strip()
    source = str(artifact.get("source") or "").strip()
    if not model_id and is_modelscope_source(source):
        model_id = source[len(MODELSCOPE_SOURCE_PREFIX):].strip()
    if not MODELSCOPE_ID_RE.fullmatch(model_id):
        raise ArtifactSyncError(f"invalid modelscope model id: {model_id!r}")
    repo_type = str(artifact.get("repo_type") or "").strip().lower()
    if not repo_type:
        repo_type = "model" if str(artifact.get("kind") or "") == "model" else "dataset"
    if repo_type not in MODELSCOPE_REPO_TYPES:
        raise ArtifactSyncError(f"invalid modelscope repo type: {repo_type!r}")
    revision = str(artifact.get("revision") or "").strip()
    if revision and not MODELSCOPE_REVISION_RE.fullmatch(revision):
        raise ArtifactSyncError(f"invalid modelscope revision: {revision!r}")
    return {
        "model_id": model_id,
        "repo_type": repo_type,
        "revision": revision,
        "allow_patterns": [str(value) for value in artifact.get("allow_patterns") or []],
        "ignore_patterns": [str(value) for value in artifact.get("ignore_patterns") or []],
    }


def modelscope_request_digest(request: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(request), sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def modelscope_snapshot_dir(cache_root: Path, artifact: Mapping[str, Any]) -> Path:
    request = modelscope_request(artifact)
    return (
        Path(cache_root).expanduser()
        / "snapshots"
        / request["model_id"]
        / modelscope_request_digest(request)
    )


def modelscope_marker_path(cache_root: Path, artifact: Mapping[str, Any]) -> Path:
    digest = modelscope_request_digest(modelscope_request(artifact))
    return Path(cache_root).expanduser() / "markers" / f"{digest}.json"


def modelscope_lock_path(cache_root: Path, artifact: Mapping[str, Any]) -> Path:
    digest = modelscope_request_digest(modelscope_request(artifact))
    return Path(cache_root).expanduser() / "locks" / f"{digest}.lock"


def _marker_matches(
    payload: Mapping[str, Any],
    request: Mapping[str, Any],
    snapshot: Path,
) -> bool:
    return (
        str(payload.get("model_id") or "") == request["model_id"]
        and str(payload.get("repo_type") or "") == request["repo_type"]
        and str(payload.get("revision") or "") == request["revision"]
        and [str(value) for value in payload.get("allow_patterns") or []]
        == request["allow_patterns"]
        and [str(value) for value in payload.get("ignore_patterns") or []]
        == request["ignore_patterns"]
        and str(payload.get("snapshot") or "") == str(snapshot)
    )


def modelscope_cache_hit(cache_root: Path, artifact: Mapping[str, Any]) -> Optional[Path]:
    """Return the cached snapshot path when a completed download matches the request."""

    request = modelscope_request(artifact)
    snapshot = modelscope_snapshot_dir(cache_root, artifact)
    marker = modelscope_marker_path(cache_root, artifact)
    if not marker.is_file():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # A damaged completion marker is equivalent to an incomplete download;
        # the next download rewrites it.
        return None
    if not isinstance(payload, Mapping) or not _marker_matches(payload, request, snapshot):
        return None
    if not _directory_has_entries(snapshot):
        return None
    return snapshot


def record_modelscope_snapshot(
    cache_root: Path,
    artifact: Mapping[str, Any],
    *,
    size_bytes: Optional[int] = None,
) -> Path:
    """Mark a snapshot directory as a complete download and return its path."""

    request = modelscope_request(artifact)
    snapshot = modelscope_snapshot_dir(cache_root, artifact)
    if not _directory_has_entries(snapshot):
        raise ArtifactSyncError(f"modelscope snapshot directory is empty: {snapshot}")
    payload = {
        "schema_version": 1,
        "model_id": request["model_id"],
        "repo_type": request["repo_type"],
        "revision": request["revision"],
        "allow_patterns": request["allow_patterns"],
        "ignore_patterns": request["ignore_patterns"],
        "snapshot": str(snapshot),
        "size_bytes": int(artifact_source_size(snapshot) if size_bytes is None else size_bytes),
        "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    marker = modelscope_marker_path(cache_root, artifact)
    marker.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(marker, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return snapshot


def verified_dir(cache_root: Path) -> Path:
    """Return the directory holding per-node artifact verification markers."""

    return Path(cache_root).expanduser() / "verified"


def _source_tree_fingerprint(root: Path, mode: str) -> Dict[str, Any]:
    """用 stat 元数据描述控制器本地源，捕获改名、增删与同大小的内容改写。"""

    root = root.resolve()
    if mode == "file":
        stat_result = root.stat()
        return {
            "type": "local-file",
            "path": str(root),
            "size": stat_result.st_size,
            "mtime_ns": stat_result.st_mtime_ns,
        }
    lines: List[str] = []
    total = 0
    pending = [root]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    stat_result = entry.stat(follow_symlinks=False)
                    size = max(0, stat_result.st_size)
                    lines.append(
                        f"{stat_result.st_mtime_ns}\t{size}\t"
                        f"{os.path.relpath(entry.path, root)}"
                    )
                    total += size
    lines.sort()
    tree_digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return {
        "type": "local-dir",
        "path": str(root),
        "tree_digest": tree_digest,
        "file_count": len(lines),
        "total_bytes": total,
    }


def _directory_inventory_digest(inventory: Mapping[str, int]) -> str:
    lines = [f"{size}\t{relative}" for relative, size in sorted(inventory.items())]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def verification_marker_key(
    node: NodeConfig,
    artifact: Mapping[str, Any],
    target: str,
) -> Optional[str]:
    """返回 (节点, 目标路径, 制品身份) 的标记键；源不可读或身份不完整时返回 None。"""

    mode = str(artifact.get("mode") or "").strip().lower()
    if mode not in {"file", "directory"}:
        return None
    resolved = str(artifact.get("resolved_source") or "").strip()
    try:
        if resolved:
            # 控制器侧已解析的快照（ModelScope 数据集）按目录内容参与身份。
            source_identity = _source_tree_fingerprint(Path(resolved), "directory")
        elif str(artifact.get("source_type") or "") == "modelscope":
            source_identity = {
                "type": "modelscope",
                "request": modelscope_request(artifact),
                "declared_size_bytes": _declared_size(artifact),
            }
        else:
            source_identity = _source_tree_fingerprint(
                Path(str(artifact.get("source") or "")), mode
            )
    except (ArtifactSyncError, OSError):
        return None
    payload = {
        "schema_version": 1,
        "node_alias": node.alias,
        "destination": target,
        "kind": str(artifact.get("kind") or ""),
        "mode": mode,
        "required_files": [str(value) for value in artifact.get("required_files") or []],
        "declared_sha256": str(artifact.get("sha256") or "").strip().lower(),
        "declared_model_id": str(artifact.get("model_id") or "").strip(),
        "source_identity": source_identity,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _read_verification_marker(
    verify_dir: Optional[Path],
    key: Optional[str],
) -> Optional[Dict[str, Any]]:
    if verify_dir is None or not key:
        return None
    marker_path = Path(verify_dir).expanduser() / f"{key}.json"
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # 损坏的标记等同于未命中；完整校验通过后会重写它。
        return None
    if not isinstance(payload, Mapping):
        return None
    if payload.get("schema_version") != 1 or str(payload.get("key") or "") != key:
        return None
    return dict(payload)


def record_verification_marker(
    verify_dir: Path,
    *,
    key: str,
    node_alias: str,
    destination: str,
    mode: str,
    kind: str,
    verified: str,
    inventory: Optional[Mapping[str, int]] = None,
    file_stat: Optional[Tuple[int, int]] = None,
) -> Path:
    """把一次成功的制品校验写成持久化标记，返回标记路径。"""

    payload: Dict[str, Any] = {
        "schema_version": 1,
        "key": key,
        "node_alias": node_alias,
        "destination": destination,
        "mode": mode,
        "kind": kind,
        "verified": verified,
        "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if mode == "directory":
        entries = dict(inventory or {})
        payload["destination_inventory_digest"] = _directory_inventory_digest(entries)
        payload["file_count"] = len(entries)
        payload["total_bytes"] = sum(entries.values())
    else:
        if file_stat is None:
            raise ArtifactSyncError("file verification marker requires a stat result")
        payload["file_stat"] = {"size": int(file_stat[0]), "mtime": int(file_stat[1])}
    marker_path = Path(verify_dir).expanduser() / f"{key}.json"
    atomic_write_text(marker_path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return marker_path


def modelscope_download_environment() -> Dict[str, str]:
    """Return a process environment for ModelScope downloads without any proxy.

    ModelScope is reachable directly. The proxy tunnel this skill uses for
    blocked package indexes must never carry model traffic, so every proxy
    variable is removed from the download process.
    """

    return {
        key: value
        for key, value in os.environ.items()
        if key not in _PROXY_ENV_VARS
    }


def build_modelscope_command(
    artifact: Mapping[str, Any],
    snapshot_dir: Path,
    *,
    binary: str = _MODELSCOPE_BINARY,
) -> List[str]:
    """Build the modelscope CLI command that downloads one snapshot.

    ``--model``/``--dataset`` with ``--local_dir`` are used because every
    modelscope release from the 1.x line accepts them, including the releases
    that accept the positional ``repo_id`` form as well.
    """

    request = modelscope_request(artifact)
    command = [binary, "download"]
    command.append("--model" if request["repo_type"] == "model" else "--dataset")
    command.append(request["model_id"])
    command += ["--local_dir", str(snapshot_dir)]
    if request["revision"]:
        command += ["--revision", request["revision"]]
    if request["allow_patterns"]:
        command.append("--include")
        command.extend(request["allow_patterns"])
    if request["ignore_patterns"]:
        command.append("--exclude")
        command.extend(request["ignore_patterns"])
    return command


def build_proxy_free_modelscope_command(
    artifact: Mapping[str, Any], destination: Path
) -> List[str]:
    command = ["env"]
    for name in _PROXY_ENV_VARS:
        command.extend(["-u", name])
    command.extend(build_modelscope_command(artifact, destination))
    return command


def _declared_size(artifact: Mapping[str, Any]) -> int:
    raw = artifact.get("size_bytes")
    if raw in (None, ""):
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError) as exc:
        raise ArtifactSyncError(
            f"artifact {artifact.get('name')!r} has an invalid size_bytes: {raw!r}"
        ) from exc


def _download_lock_expired(lock_path: Path) -> bool:
    try:
        age = time.time() - lock_path.stat().st_mtime
    except FileNotFoundError:
        return False
    return age > _DOWNLOAD_LOCK_STALE_S


async def _acquire_download_lock(lock_path: Path, timeout_s: int) -> int:
    deadline = time.monotonic() + max(1, int(timeout_s))
    while True:
        try:
            descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if _download_lock_expired(lock_path):
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise ArtifactSyncError(
                    f"timed out waiting for a concurrent modelscope download: {lock_path}"
                )
            await asyncio.sleep(2)
            continue
        os.write(descriptor, f"pid {os.getpid()}\n".encode("utf-8"))
        return descriptor


def _release_download_lock(descriptor: int, lock_path: Path) -> None:
    os.close(descriptor)
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


async def ensure_modelscope_snapshot(
    cache_root: Path,
    artifact: Mapping[str, Any],
) -> Tuple[Path, str]:
    """Return a complete local snapshot, downloading it once through modelscope."""

    cached = modelscope_cache_hit(cache_root, artifact)
    if cached is not None:
        return cached, "cached"

    binary = shutil.which(_MODELSCOPE_BINARY)
    if not binary:
        raise ArtifactSyncError(
            "the modelscope command line tool is required to download model artifacts; "
            "install it with 'pip install modelscope' or 'uv tool install modelscope' "
            "and keep it on PATH"
        )

    snapshot = modelscope_snapshot_dir(cache_root, artifact)
    lock_path = modelscope_lock_path(cache_root, artifact)
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    timeout_s = max(1, int(artifact.get("timeout_s") or MODELSCOPE_DEFAULT_TIMEOUT_S))

    descriptor = await _acquire_download_lock(lock_path, timeout_s)
    try:
        cached = modelscope_cache_hit(cache_root, artifact)
        if cached is not None:
            return cached, "cached"
        command = build_modelscope_command(artifact, snapshot, binary=binary)
        rc, stdout, stderr = await _run_command(
            command,
            timeout_s,
            missing_binary="the modelscope command line tool disappeared from PATH",
            env=modelscope_download_environment(),
        )
        output = f"{stdout}\n{stderr}".lower()
        detail = (stderr.strip() or stdout.strip())[-4000:]
        model_id = str(artifact.get("model_id") or "")
        if rc != 0:
            raise ArtifactSyncError(f"modelscope download exited {rc} for {model_id}: {detail}")
        failed_markers = [marker for marker in _MODELSCOPE_FAILURE_MARKERS if marker in output]
        if failed_markers:
            raise ArtifactSyncError(
                f"modelscope reported file download failures for {model_id}: {detail}"
            )
        if not _directory_has_entries(snapshot):
            raise ArtifactSyncError(f"modelscope produced an empty snapshot for {model_id}: {snapshot}")
        actual_size = artifact_source_size(snapshot)
        declared_size = _declared_size(artifact)
        if declared_size and actual_size > declared_size:
            raise ArtifactSyncError(
                f"downloaded snapshot of {model_id} uses {actual_size} bytes, above the declared "
                f"size_bytes {declared_size}; update the manifest"
            )
        record_modelscope_snapshot(cache_root, artifact, size_bytes=actual_size)
        return snapshot, "downloaded"
    finally:
        _release_download_lock(descriptor, lock_path)


def expand_artifact_destination(
    template: str,
    values: Mapping[str, Any],
    allowed_prefix: str = "",
) -> str:
    destination = str(template or "").strip()
    for name, value in values.items():
        token = "{" + str(name) + "}"
        if token in destination and not str(value or "").strip():
            raise ArtifactSyncError(f"artifact destination token has no value: {name}")
        destination = destination.replace(token, str(value))
    if "{" in destination or "}" in destination:
        raise ArtifactSyncError(f"artifact destination has an unresolved token: {destination!r}")
    if any(ord(char) < 32 for char in destination):
        raise ArtifactSyncError("artifact destination contains a control character")
    if not destination.startswith(("/", "~/")):
        raise ArtifactSyncError(
            f"artifact destination must be absolute or start with ~/; got {destination!r}"
        )
    normalized = _validate_destination(posixpath.normpath(destination))
    if allowed_prefix:
        prefix = posixpath.normpath(allowed_prefix)
        if normalized != prefix and not normalized.startswith(prefix.rstrip("/") + "/"):
            raise ArtifactSyncError(
                f"artifact destination must stay under {prefix!r}: {normalized!r}"
            )
    return normalized


def _validate_destination(destination: str, home: str = "") -> str:
    forbidden = ("/", "~", "/root", "/usr", "/opt", "~/.cache", "~/.local")
    roots = list(forbidden)
    if home:
        home_root = posixpath.normpath(home)
        roots.extend((f"{home_root}/.cache", f"{home_root}/.local"))
    for path in roots:
        if destination == path or (path not in {"/", "~"} and destination.startswith(path.rstrip("/") + "/")):
            raise ArtifactSyncError(f"artifact destination is outside the data-work policy: {destination!r}")
    return destination


def _source_contains_identityfile(source: Path, node: NodeConfig) -> bool:
    raw_identity = str(node.identityfile or "").strip()
    if not raw_identity:
        return False
    identity = Path(raw_identity).expanduser().resolve(strict=False)
    return identity == source or (source.is_dir() and source in identity.parents)


def _result(
    artifact: Mapping[str, Any],
    node: Optional[NodeConfig],
    destination: str,
    *,
    status: str,
    error: str = "",
    checksum: str = "",
    verified: str = "",
    verify_cache: str = "",
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "name": str(artifact.get("name") or ""),
        "kind": str(artifact.get("kind") or ""),
        "source": str(artifact.get("source") or ""),
        "destination": destination,
        "mode": str(artifact.get("mode") or ""),
        "status": status,
        "success": status in {"available", "downloaded", "uploaded", "skipped"},
    }
    if node is not None:
        result["node_alias"] = node.alias
    model_id = str(artifact.get("model_id") or "").strip()
    if model_id:
        result["modelscope"] = model_id
    if str(artifact.get("kind") or "") == "model":
        if status == "downloaded":
            result["transport"] = "modelscope-direct"
        elif status == "uploaded":
            result["transport"] = (
                "local-rsync"
                if node is not None and is_local_node(node)
                else "rsync-over-ssh"
            )
        elif status == "available":
            result["transport"] = "target-local"
    resolved = str(artifact.get("resolved_source") or "").strip()
    if resolved:
        result["resolved_source"] = resolved
    if checksum:
        result["sha256"] = checksum
    if verified:
        result["verified"] = verified
    if verify_cache:
        result["verify_cache"] = verify_cache
    if error:
        result["error"] = error
    return result


def artifact_failure_result(artifact: Mapping[str, Any], error: str) -> Dict[str, Any]:
    return _result(
        artifact,
        None,
        str(artifact.get("destination") or ""),
        status="failed",
        error=error,
    )


async def _run_command(
    command: List[str],
    timeout_s: int,
    *,
    missing_binary: str,
    env: Optional[Mapping[str, str]] = None,
) -> Tuple[int, str, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(env) if env is not None else None,
        )
    except FileNotFoundError as exc:
        raise ArtifactSyncError(missing_binary) from exc
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            process.communicate(), timeout=max(1, int(timeout_s))
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise ArtifactSyncError(f"artifact command timed out after {timeout_s}s: {command[0]}") from exc
    return (
        int(process.returncode or 0),
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )


async def _run_rsync(command: List[str], timeout_s: int) -> Tuple[int, str, str]:
    return await _run_command(
        command,
        timeout_s,
        missing_binary="rsync is required for source, model, and data synchronization",
    )


async def _artifact_ssh_run(
    node: NodeConfig,
    remote_args: List[str],
    timeout_s: int,
) -> Tuple[int, str, str]:
    try:
        return await ssh_run(node, remote_args, timeout_s=timeout_s)
    except SSHRunError as exc:
        raise ArtifactSyncError(f"artifact synchronization SSH failed on {node.alias}: {exc}") from exc


async def _remote_home(node: NodeConfig, timeout_s: int) -> str:
    rc, stdout, stderr = await _artifact_ssh_run(
        node,
        ["bash", "-c", 'printf "%s" "$HOME"'],
        min(max(1, int(timeout_s)), 30),
    )
    home = stdout.strip()
    if rc != 0 or not home.startswith("/") or any(ord(char) < 32 for char in home):
        detail = stderr.strip() or stdout.strip() or f"remote home lookup exited {rc}"
        raise ArtifactSyncError(f"could not resolve remote home on {node.alias}: {detail}")
    return home


async def _resolve_remote_destination(
    node: NodeConfig,
    destination: str,
    timeout_s: int,
) -> str:
    home = await _remote_home(node, timeout_s)
    if destination == "~" or destination.startswith("~/"):
        destination = home + destination[1:]
    return _validate_destination(posixpath.normpath(destination), home=home)


def _sha256_path(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


async def _sha256_file(path: Path) -> str:
    return await asyncio.to_thread(_sha256_path, path)


_DIRECTORY_DIGEST_VERIFY_MAX_BYTES = 1024 ** 3
_INVENTORY_TIMEOUT_CAP_S = 600


def _inventory_timeout(timeout_s: int) -> int:
    return min(max(1, int(timeout_s)), _INVENTORY_TIMEOUT_CAP_S)


def _local_file_inventory(root: Path) -> Dict[str, int]:
    """Return {relative path: byte size} for every regular file below root."""

    root = root.resolve()
    inventory: Dict[str, int] = {}
    pending = [root]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    inventory[os.path.relpath(entry.path, root)] = max(
                        0, entry.stat(follow_symlinks=False).st_size
                    )
    return inventory


def _local_sha256_inventory(root: Path) -> Dict[str, str]:
    """Return {relative path: sha256 hex digest} for every regular file below root."""

    root = root.resolve()
    inventory: Dict[str, str] = {}
    pending = [root]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    inventory[os.path.relpath(entry.path, root)] = _sha256_path(
                        Path(entry.path)
                    )
    return inventory


def _parse_sha256_inventory(text: str, node_alias: str) -> Dict[str, str]:
    inventory: Dict[str, str] = {}
    for line in text.splitlines():
        if not line:
            continue
        digest = line[:64]
        if len(line) < 66 or line[64] not in (" ", "*") or any(char not in "0123456789abcdef" for char in digest):
            raise ArtifactSyncError(
                f"unparsable sha256 inventory line from {node_alias}: {line!r}"
            )
        relative = line[66:]
        if relative.startswith("./"):
            relative = relative[2:]
        if not relative:
            raise ArtifactSyncError(
                f"unparsable sha256 inventory line from {node_alias}: {line!r}"
            )
        inventory[relative] = digest
    return inventory


async def _remote_file_inventory(
    node: NodeConfig,
    path: str,
    timeout_s: int,
) -> Dict[str, int]:
    """Return {relative path: byte size} for the remote directory in one SSH run."""

    script = 'cd "$1" || exit 9; find . -type f -printf "%s\\t%P\\n"'
    rc, stdout, stderr = await _artifact_ssh_run(
        node, ["bash", "-c", script, "_", path], _inventory_timeout(timeout_s)
    )
    if rc != 0:
        raise ArtifactSyncError(
            f"file inventory failed on {node.alias}: "
            f"{stderr.strip() or stdout.strip() or f'find exited {rc}'}"
        )
    inventory: Dict[str, int] = {}
    for line in stdout.splitlines():
        if not line:
            continue
        size_raw, separator, relative = line.partition("\t")
        if not separator or not size_raw.isdigit() or not relative or relative.startswith("/"):
            raise ArtifactSyncError(
                f"unparsable file inventory line from {node.alias}: {line!r}"
            )
        inventory[relative] = int(size_raw)
    return inventory


async def _remote_sha256_inventory(
    node: NodeConfig,
    path: str,
    timeout_s: int,
) -> Dict[str, str]:
    """Return {relative path: sha256 hex digest} for the remote directory in one SSH run."""

    script = 'cd "$1" || exit 9; find . -type f -print0 | sort -z | xargs -0 -r sha256sum'
    rc, stdout, stderr = await _artifact_ssh_run(
        node, ["bash", "-c", script, "_", path], _inventory_timeout(timeout_s)
    )
    if rc != 0:
        detail = stderr.strip() or stdout.strip() or f"sha256sum exited {rc}"
        raise ArtifactSyncError(f"file digest inventory failed on {node.alias}: {detail}")
    return _parse_sha256_inventory(stdout, node.alias)


async def _verify_directory_state(
    node: NodeConfig,
    target: str,
    source: Path,
    timeout_s: int,
) -> Tuple[bool, str, str, Dict[str, int]]:
    """Compare the target directory with the controller-local source directory.

    Returns (consistent, level, error, target_inventory). ``level`` is ``digest``
    when every file digest matched, and ``metadata`` when only names and byte
    sizes were compared because the tree exceeds
    _DIRECTORY_DIGEST_VERIFY_MAX_BYTES. The target inventory is returned so the
    caller can record a verification marker without walking the tree again.
    """

    if is_local_node(node):
        target_inventory = _local_file_inventory(Path(target))
    else:
        target_inventory = await _remote_file_inventory(node, target, timeout_s)
    source_inventory = _local_file_inventory(Path(source))
    if target_inventory != source_inventory:
        missing = sorted(set(source_inventory) - set(target_inventory))
        extra = sorted(set(target_inventory) - set(source_inventory))
        resized = sorted(
            relative
            for relative in set(source_inventory) & set(target_inventory)
            if source_inventory[relative] != target_inventory[relative]
        )
        details = []
        if missing:
            details.append(f"missing {len(missing)} file(s), first {missing[0]!r}")
        if extra:
            details.append(f"unexpected {len(extra)} file(s), first {extra[0]!r}")
        if resized:
            details.append(f"{len(resized)} file(s) changed size, first {resized[0]!r}")
        return (
            False,
            "metadata",
            "target directory does not match the source: " + "; ".join(details),
            target_inventory,
        )
    if sum(source_inventory.values()) > _DIRECTORY_DIGEST_VERIFY_MAX_BYTES:
        return True, "metadata", "", target_inventory
    source_digests = await asyncio.to_thread(_local_sha256_inventory, Path(source))
    if is_local_node(node):
        target_digests = await asyncio.to_thread(_local_sha256_inventory, Path(target))
    else:
        target_digests = await _remote_sha256_inventory(node, target, timeout_s)
    differing = sorted(
        relative
        for relative, digest in source_digests.items()
        if target_digests.get(relative) != digest
    )
    if differing:
        return (
            False,
            "digest",
            f"target file content differs from the source: first {differing[0]!r}",
            target_inventory,
        )
    return True, "digest", "", target_inventory


async def _verify_directory_against_source(
    node: NodeConfig,
    target: str,
    source: Path,
    timeout_s: int,
) -> Tuple[bool, str, str]:
    consistent, level, error, _inventory = await _verify_directory_state(
        node, target, source, timeout_s
    )
    return consistent, level, error


async def _collect_target_inventory(
    node: NodeConfig,
    target: str,
    timeout_s: int,
) -> Dict[str, int]:
    """Return the {relative path: byte size} inventory of a target directory."""

    if is_local_node(node):
        return _local_file_inventory(Path(target))
    return await _remote_file_inventory(node, target, _inventory_timeout(timeout_s))


async def _target_file_stat(
    node: NodeConfig,
    target: str,
    timeout_s: int,
) -> Optional[Tuple[int, int]]:
    """Return (size, mtime) of a target file, or None when it is missing."""

    if is_local_node(node):
        try:
            stat_result = Path(target).stat()
        except FileNotFoundError:
            return None
        return stat_result.st_size, int(stat_result.st_mtime)
    rc, stdout, _stderr = await _artifact_ssh_run(
        node, ["stat", "-c", "%s %Y", target], min(max(1, int(timeout_s)), 60)
    )
    if rc != 0:
        return None
    fields = stdout.split()
    if len(fields) < 2 or not fields[0].isdigit() or not fields[1].isdigit():
        raise ArtifactSyncError(f"unparsable stat output from {node.alias}: {stdout!r}")
    return int(fields[0]), int(fields[1])


async def _revalidate_with_marker(
    node: NodeConfig,
    target: str,
    marker: Mapping[str, Any],
    *,
    mode: str,
    declared_size: int = 0,
    timeout_s: int,
) -> bool:
    """复核验证标记描述的目标状态；不一致或目标缺失返回 False。

    复核阶段的错误按未命中处理，由完整校验路径给出确定性的修复或错误报告。
    """

    try:
        if mode == "file":
            current = await _target_file_stat(node, target, timeout_s)
            if current is None:
                return False
            expected = marker.get("file_stat") or {}
            return current == (
                int(expected.get("size") or -1),
                int(expected.get("mtime") or -1),
            )
        inventory = await _collect_target_inventory(node, target, timeout_s)
    except (ArtifactSyncError, OSError):
        return False
    if len(inventory) != int(marker.get("file_count") or -1):
        return False
    total = sum(inventory.values())
    if total != int(marker.get("total_bytes") or -1):
        return False
    if _directory_inventory_digest(inventory) != str(
        marker.get("destination_inventory_digest") or ""
    ):
        return False
    return not (declared_size and total > declared_size)


async def _record_verification(
    verify_dir: Optional[Path],
    key: Optional[str],
    node: NodeConfig,
    target: str,
    *,
    mode: str,
    kind: str,
    verified: str,
    timeout_s: int,
    inventory: Optional[Mapping[str, int]] = None,
) -> None:
    """把一次成功的完整校验写入持久化验证标记；未启用缓存时为空操作。"""

    if verify_dir is None or not key:
        return
    if mode == "file":
        file_stat = await _target_file_stat(node, target, timeout_s)
        if file_stat is None:
            raise ArtifactSyncError(
                f"verified artifact target disappeared before its marker was recorded: {target}"
            )
        record_verification_marker(
            verify_dir,
            key=key,
            node_alias=node.alias,
            destination=target,
            mode=mode,
            kind=kind,
            verified=verified or "existence",
            file_stat=file_stat,
        )
        return
    if inventory is None:
        inventory = await _collect_target_inventory(node, target, timeout_s)
    record_verification_marker(
        verify_dir,
        key=key,
        node_alias=node.alias,
        destination=target,
        mode=mode,
        kind=kind,
        verified=verified or "existence",
        inventory=inventory,
    )


async def _model_available(
    node: NodeConfig,
    destination: str,
    mode: str,
    required_files: List[str],
    checksum: str,
    timeout_s: int,
    source: Optional[Path] = None,
    declared_size: int = 0,
) -> Tuple[bool, str]:
    """Decide whether the destination already holds a usable artifact.

    Returns (available, verified). ``verified`` names the strongest check that
    passed: ``digest`` (full sha256 comparison), ``metadata`` (names and byte
    sizes against a controller-local source), ``size-cap`` (total bytes against
    the declared size_bytes), or ``existence`` (files exist and required_files
    are present). Any check that fails reports not available so the caller
    retransfers or re-downloads the artifact.
    """

    if mode == "file":
        if is_local_node(node):
            path = Path(destination).expanduser()
            if not path.is_file():
                return False, "existence"
            if not checksum:
                return True, "existence"
            if await _sha256_file(path) != checksum:
                return False, "digest"
            return True, "digest"

        rc, stdout, _stderr = await _artifact_ssh_run(
            node,
            ["sha256sum", destination] if checksum else ["test", "-f", destination],
            min(max(1, int(timeout_s)), 60),
        )
        if rc != 0:
            return False, "existence"
        if not checksum:
            return True, "existence"
        actual = stdout.strip().split(maxsplit=1)[0] if stdout.strip() else ""
        if actual != checksum:
            return False, "digest"
        return True, "digest"

    if is_local_node(node):
        path = Path(destination).expanduser()
        if not path.is_dir() or not _directory_has_entries(path):
            return False, "existence"
        if not all((path / relative).is_file() for relative in required_files):
            return False, "existence"
        if source is not None and source.is_dir():
            consistent, level, _error = await _verify_directory_against_source(
                node, str(path), source, timeout_s
            )
            if not consistent:
                return False, level
            return True, level
        if declared_size:
            if sum(_local_file_inventory(path).values()) > declared_size:
                return False, "size-cap"
            return True, "size-cap"
        return True, "existence"

    script = 'test -d "$1" && test -n "$(ls -A -- "$1")"'
    args = ["bash", "-c", script, "_", destination]
    rc, _stdout, _stderr = await _artifact_ssh_run(
        node, args, min(max(1, int(timeout_s)), 60)
    )
    if rc != 0:
        return False, "existence"
    for relative in required_files:
        rc, _stdout, _stderr = await _artifact_ssh_run(
            node,
            ["test", "-f", posixpath.join(destination, relative)],
            min(max(1, int(timeout_s)), 60),
        )
        if rc != 0:
            return False, "existence"
    if source is not None and source.is_dir():
        consistent, level, _error = await _verify_directory_against_source(
            node, destination, source, timeout_s
        )
        if not consistent:
            return False, level
        return True, level
    if declared_size:
        inventory = await _remote_file_inventory(node, destination, timeout_s)
        if sum(inventory.values()) > declared_size:
            return False, "size-cap"
        return True, "size-cap"
    return True, "existence"


async def _download_modelscope_to_node(
    node: NodeConfig,
    artifact: Mapping[str, Any],
    destination: str,
    timeout_s: int,
) -> Tuple[bool, str]:
    command = build_modelscope_command(artifact, Path(destination))
    if is_local_node(node):
        try:
            rc, stdout, stderr = await _run_command(
                command,
                timeout_s,
                missing_binary="the modelscope command line tool is required on the target node",
                env=modelscope_download_environment(),
            )
        except ArtifactSyncError as exc:
            return False, str(exc)
    else:
        parent = posixpath.dirname(destination) or "/"
        rc, stdout, stderr = await _artifact_ssh_run(
            node,
            ["mkdir", "-p", parent],
            min(max(1, int(timeout_s)), 60),
        )
        if rc != 0:
            return False, stderr.strip() or stdout.strip() or f"mkdir exited {rc}"
        remote_command = build_proxy_free_modelscope_command(
            artifact, Path(destination)
        )
        rc, stdout, stderr = await _artifact_ssh_run(node, remote_command, timeout_s)
    output = f"{stdout}\n{stderr}".lower()
    detail = (stderr.strip() or stdout.strip())[-4000:]
    if rc != 0:
        return False, f"ModelScope download exited {rc}: {detail}"
    if any(marker in output for marker in _MODELSCOPE_FAILURE_MARKERS):
        return False, f"ModelScope reported file download failures: {detail}"
    return True, ""


async def sync_artifact_to_node(
    node: NodeConfig,
    artifact: Mapping[str, Any],
    values: Mapping[str, Any],
    verify_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    mode = str(artifact.get("mode") or "").strip().lower()
    destination = expand_artifact_destination(str(artifact.get("destination") or ""), values)
    checksum = str(artifact.get("sha256") or "").strip().lower()
    timeout_s = max(1, int(artifact.get("timeout_s") or 3600))
    if mode not in {"file", "directory"}:
        raise ArtifactSyncError(f"invalid artifact mode for {artifact.get('name')!r}: {mode!r}")
    if checksum and mode != "file":
        raise ArtifactSyncError("sha256 verification requires a file artifact")

    kind = str(artifact.get("kind") or "").strip().lower()
    required_files = [str(value) for value in artifact.get("required_files") or []]
    for relative in required_files:
        if (
            not relative
            or posixpath.isabs(relative)
            or ".." in relative.split("/")
            or any(ord(char) < 32 for char in relative)
        ):
            raise ArtifactSyncError(f"invalid required model file: {relative!r}")
    if kind == "model":
        if is_local_node(node):
            target = str(Path(destination).expanduser())
        else:
            target = await _resolve_remote_destination(node, destination, timeout_s)
        # 命中持久化验证标记时，用一次元数据复核替代全量摘要比对与重新传输。
        marker_key = verification_marker_key(node, artifact, target) if verify_dir else None
        marker = _read_verification_marker(verify_dir, marker_key)
        if marker is not None and await _revalidate_with_marker(
            node, target, marker,
            mode=mode, declared_size=_declared_size(artifact), timeout_s=timeout_s,
        ):
            return _result(
                artifact, node, target,
                status="available", checksum=checksum,
                verified=str(marker.get("verified") or "existence"),
                verify_cache="hit",
            )
        resolved = str(artifact.get("resolved_source") or "").strip()
        source_anchor = Path(resolved).expanduser() if resolved else None
        if source_anchor is None and not is_modelscope_artifact(artifact):
            source_anchor = artifact_local_source(artifact)
        if source_anchor is not None and not source_anchor.is_dir():
            source_anchor = None
        model_check_arguments = {
            "source": source_anchor,
            "declared_size": _declared_size(artifact),
        }
        available, verified = await _model_available(
            node, target, mode, required_files, checksum, timeout_s,
            **model_check_arguments,
        )
        if available:
            await _record_verification(
                verify_dir, marker_key, node, target,
                mode=mode, kind=kind, verified=verified, timeout_s=timeout_s,
            )
            return _result(
                artifact, node, target,
                status="available", checksum=checksum, verified=verified,
            )
        model_id = str(artifact.get("model_id") or "").strip()
        if model_id:
            downloaded, download_error = await _download_modelscope_to_node(
                node, artifact, target, timeout_s
            )
            if downloaded:
                available, verified = await _model_available(
                    node, target, mode, required_files, checksum, timeout_s,
                    **model_check_arguments,
                )
                if available:
                    await _record_verification(
                        verify_dir, marker_key, node, target,
                        mode=mode, kind=kind, verified=verified, timeout_s=timeout_s,
                    )
                    return _result(
                        artifact, node, target,
                        status="downloaded", checksum=checksum, verified=verified,
                    )
            if str(artifact.get("source_type") or "") == "modelscope":
                return _result(
                    artifact,
                    node,
                    target,
                    status="failed",
                    error=download_error or "downloaded model failed validation",
                    checksum=checksum,
                )

    # source/data 制品：先解析目标并查询验证标记，命中时跳过整个传输。
    if kind != "model":
        if is_local_node(node):
            marker_target = str(Path(destination).expanduser())
        else:
            marker_target = await _resolve_remote_destination(node, destination, timeout_s)
        marker_key = (
            verification_marker_key(node, artifact, marker_target) if verify_dir else None
        )
        marker = _read_verification_marker(verify_dir, marker_key)
        if marker is not None and await _revalidate_with_marker(
            node, marker_target, marker, mode=mode, timeout_s=timeout_s,
        ):
            return _result(
                artifact, node, marker_target,
                status="available", checksum=checksum, verify_cache="hit",
            )

    source = artifact_local_source(artifact).resolve(strict=False)
    if source == Path("/") or source == Path.home():
        raise ArtifactSyncError(f"artifact source is too broad: {source}")
    if ".ssh" in source.parts or source.name.lower() in _PRIVATE_SOURCE_NAMES:
        return _result(
            artifact,
            node,
            destination,
            status="failed",
            error="private-key paths cannot be artifact sources",
            checksum=checksum,
        )
    if not source.is_file() and not source.is_dir():
        return _result(
            artifact,
            node,
            destination,
            status="failed",
            error=f"local artifact source does not exist: {source}",
            checksum=checksum,
        )
    if mode == "file" and not source.is_file():
        return _result(
            artifact,
            node,
            destination,
            status="failed",
            error=f"artifact source is not a file: {source}",
            checksum=checksum,
        )
    if mode == "directory" and not source.is_dir():
        return _result(
            artifact,
            node,
            destination,
            status="failed",
            error=f"artifact source is not a directory: {source}",
            checksum=checksum,
        )
    if _source_contains_identityfile(source, node):
        return _result(
            artifact,
            node,
            destination,
            status="failed",
            error="artifact source contains the configured SSH identity-file path",
            checksum=checksum,
        )

    if checksum:
        actual = await _sha256_file(source)
        if actual != checksum:
            return _result(
                artifact,
                node,
                destination,
                status="failed",
                error=f"local artifact checksum mismatch for {source}: expected {checksum}, got {actual}",
                checksum=checksum,
            )

    local_destination: Optional[Path] = None
    if is_local_node(node):
        local_destination = Path(destination).expanduser()
        resolved_destination = local_destination.resolve(strict=False)
        _validate_destination(str(resolved_destination), home=str(Path.home()))
        if resolved_destination == source.resolve(strict=False):
            await _record_verification(
                verify_dir, marker_key, node, str(local_destination),
                mode=mode, kind=kind, verified="metadata", timeout_s=timeout_s,
            )
            return _result(artifact, node, str(local_destination), status="skipped", checksum=checksum)
        if mode == "file" and local_destination.exists() and local_destination.is_dir():
            return _result(
                artifact,
                node,
                str(local_destination),
                status="failed",
                error=f"artifact destination is a directory, expected a file: {local_destination}",
                checksum=checksum,
            )
        if mode == "directory" and local_destination.exists() and not local_destination.is_dir():
            return _result(
                artifact,
                node,
                str(local_destination),
                status="failed",
                error=f"artifact destination is a file, expected a directory: {local_destination}",
                checksum=checksum,
            )
        if mode == "directory":
            local_destination.mkdir(parents=True, exist_ok=True)
        else:
            local_destination.parent.mkdir(parents=True, exist_ok=True)
        target = str(local_destination)
    else:
        target = await _resolve_remote_destination(node, destination, timeout_s)
        if mode == "file":
            rc, stdout, stderr = await _artifact_ssh_run(
                node,
                ["test", "-d", target],
                min(max(1, int(timeout_s)), 30),
            )
            if rc == 0:
                return _result(
                    artifact,
                    node,
                    target,
                    status="failed",
                    error=f"artifact destination is a directory, expected a file: {target}",
                    checksum=checksum,
                )
            if rc != 1:
                detail = stderr.strip() or stdout.strip() or f"destination type check exited {rc}"
                return _result(artifact, node, target, status="failed", error=detail, checksum=checksum)
            parent = posixpath.dirname(target) or "/"
            rc, stdout, stderr = await _artifact_ssh_run(
                node,
                ["mkdir", "-p", parent],
                min(max(1, int(timeout_s)), 30),
            )
        else:
            parent = target
            rc, stdout, stderr = await _artifact_ssh_run(
                node,
                ["mkdir", "-p", parent],
                min(max(1, int(timeout_s)), 30),
            )
        if rc != 0:
            detail = stderr.strip() or stdout.strip() or f"destination preparation exited {rc}"
            return _result(artifact, node, target, status="failed", error=detail, checksum=checksum)

    source_arg = str(source) + "/" if mode == "directory" else str(source)
    destination_arg = target.rstrip("/") + "/" if mode == "directory" else target
    command = [
        "rsync",
        "--recursive",
        "--links",
        "--perms",
        "--times",
        "--checksum",
        "--partial",
        "--protect-args",
    ]
    if is_local_node(node):
        command.extend(["--delete", source_arg, destination_arg])
    else:
        command.extend(
            [
                "--delete",
                "--rsh",
                shlex.join(_ssh_base_cmd(node)),
                source_arg,
                f"{node.user}@{node.address}:{destination_arg}",
            ]
        )
    rc, stdout, stderr = await _run_rsync(command, timeout_s)
    if rc != 0:
        detail = stderr.strip() or stdout.strip() or f"rsync exited {rc}"
        return _result(artifact, node, target, status="failed", error=detail, checksum=checksum)

    if checksum and mode == "file":
        if is_local_node(node):
            actual = await _sha256_file(Path(target))
        else:
            verify_rc, verify_stdout, verify_stderr = await _artifact_ssh_run(
                node,
                ["sha256sum", target],
                min(max(1, int(timeout_s)), 60),
            )
            remote_digest = verify_stdout.strip().split(maxsplit=1)[0] if verify_stdout.strip() else ""
            if verify_rc != 0 or remote_digest != checksum:
                detail = verify_stderr.strip() or verify_stdout.strip() or f"sha256sum exited {verify_rc}"
                if verify_rc == 0 and remote_digest != checksum:
                    detail = f"remote artifact checksum mismatch: expected {checksum}, got {remote_digest}"
                return _result(artifact, node, target, status="failed", error=detail, checksum=checksum)
            actual = checksum
        if actual != checksum:
            return _result(
                artifact,
                node,
                target,
                status="failed",
                error=f"destination checksum mismatch: expected {checksum}, got {actual}",
                checksum=checksum,
            )

    verified = ""
    target_inventory: Optional[Dict[str, int]] = None
    if mode == "directory":
        consistent, verified, verify_error, target_inventory = await _verify_directory_state(
            node, target, source, timeout_s
        )
        if not consistent:
            return _result(
                artifact, node, target,
                status="failed", error=verify_error, checksum=checksum,
            )
    elif checksum:
        verified = "digest"
    await _record_verification(
        verify_dir, marker_key, node, target,
        mode=mode, kind=kind, verified=verified, timeout_s=timeout_s,
        inventory=target_inventory,
    )
    result = _result(
        artifact, node, target,
        status="uploaded", checksum=checksum, verified=verified,
    )
    if source.is_file():
        result["bytes"] = source.stat().st_size
    return result
