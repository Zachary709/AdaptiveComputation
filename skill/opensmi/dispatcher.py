"""Execute a resource plan on one or more SSH nodes.

The dispatcher is intentionally small and stateless. The service owns
inventory; the skill owns planning and execution. This lets the agent review
the exact node and GPU placement before sending commands to remote machines.
An environment check is a lightweight, declared probe. A failed check stops the
task and returns the reviewed repair candidate; the agent repairs and reports
it through the repair command.
"""

from __future__ import annotations

import asyncio
import posixpath
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .artifacts import (
    ArtifactSyncError,
    artifact_failure_result,
    ensure_modelscope_snapshot,
    expand_artifact_destination,
    is_modelscope_artifact,
    modelscope_snapshot_dir,
    sync_artifact_to_node,
    verified_dir,
)
from .executor import route_command_to_target, run_preflight_checks
from .planner import is_environment_maintenance
from adaptive_core.models import (
    NodeTarget,
    PreflightCheck,
    PreflightCheckType,
    RemoteExecutionContext,
    node_config_from_inventory_node,
)


class DispatchError(RuntimeError):
    """Raised when a plan cannot be executed safely."""


def _replace_tokens(command: str, values: Mapping[str, Any]) -> str:
    """Replace only documented ``{name}`` tokens, not arbitrary format braces.

    This matters for Python commands containing dictionaries or f-string source
    code.  ``str.format`` would make those commands unexpectedly fail.
    """

    result = str(command)
    for key, value in values.items():
        result = result.replace("{" + str(key) + "}", str(value))
    return result


def _safe_session(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", value)


def _plan_nodes(plan: Mapping[str, Any]) -> Dict[str, Any]:
    raw = plan.get("nodes", {})
    if isinstance(raw, list):
        return {str(item.get("alias")): item for item in raw if isinstance(item, Mapping) and item.get("alias")}
    if isinstance(raw, Mapping):
        return {str(alias): value for alias, value in raw.items() if isinstance(value, Mapping)}
    return {}


def _node_configs(plan: Mapping[str, Any]) -> Dict[str, Any]:
    configs: Dict[str, Any] = {}
    for alias, raw in _plan_nodes(plan).items():
        item = dict(raw)
        item.setdefault("alias", alias)
        node = node_config_from_inventory_node(item)
        if not node.address:
            raise DispatchError(f"plan node {alias!r} has no SSH address")
        configs[alias] = node
    return configs


def _flatten_assignments(task: Mapping[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for group in task.get("assignments", []):
        if not isinstance(group, Mapping):
            continue
        alias = str(group.get("node_alias") or "")
        indices = group.get("gpu_indices", [])
        if not isinstance(indices, list):
            indices = [indices]
        gpu_details = group.get("gpus", [])
        if not isinstance(gpu_details, list):
            gpu_details = []
        for position, index in enumerate(indices):
            try:
                gpu_index = int(index)
            except (TypeError, ValueError) as exc:
                raise DispatchError(f"invalid GPU index in task {task.get('id')!r}: {index!r}") from exc
            detail = gpu_details[position] if position < len(gpu_details) and isinstance(gpu_details[position], Mapping) else {}
            out.append({
                "node_alias": alias,
                "gpu_index": gpu_index,
                "disk_path": detail.get("disk_path") or group.get("storage_path") or "",
                "work_dir": detail.get("work_dir") or group.get("work_dir") or "",
                "environment": detail.get("environment") or group.get("environment"),
                "data_path": detail.get("data_path") or group.get("data_path") or "",
                "model_path": detail.get("model_path") or group.get("model_path") or "",
            })
    if not out:
        raise DispatchError(f"task {task.get('id')!r} has no GPU assignments")
    return out


def _task_context_values(
    task: Mapping[str, Any],
    node_alias: str,
    gpu_index: int,
    rank: int,
    world_size: int,
    disk_path: str = "",
    work_dir: str = "",
    gpu_indices: Optional[Sequence[int]] = None,
    data_path: str = "",
    model_path: str = "",
) -> Dict[str, Any]:
    selected_indices = list(gpu_indices) if gpu_indices is not None else [gpu_index]
    return {
        "node_alias": node_alias,
        "gpu_index": gpu_index,
        "gpu_indices": ",".join(str(index) for index in selected_indices),
        "rank": rank,
        "world_size": world_size,
        "work_dir": work_dir or str(task.get("work_dir") or ""),
        "disk_path": disk_path,
        "data_path": data_path,
        "model_path": model_path,
    }


def _task_node(
    node: Any,
    task: Mapping[str, Any],
    environment: Optional[Mapping[str, Any]] = None,
    work_dir: str = "",
) -> Any:
    """Apply task-local environment/workdir without mutating the plan."""

    changes: Dict[str, Any] = {}
    if task.get("work_dir"):
        changes["work_dir"] = str(task["work_dir"])
    elif work_dir:
        changes["work_dir"] = str(work_dir)
    if isinstance(environment, Mapping) and environment.get("manager"):
        changes["env_manager"] = str(environment["manager"])
        changes["env_name"] = str(environment.get("name") or "")
    elif task.get("env_manager"):
        changes["env_manager"] = str(task["env_manager"])
    elif task.get("uv_env"):
        changes["env_manager"] = "uv"
    # ``uv_env=uv`` means the command itself should use uv; a concrete name is
    # useful for a preconfigured venv/conda environment.  uv's project model
    # is not activated implicitly here, avoiding shell-hook side effects.
    if (
        not environment
        and task.get("uv_env")
        and str(task["uv_env"]).lower() not in {"uv", "uv-managed"}
        and not task.get("env_manager")
    ):
        changes["env_name"] = str(task["uv_env"])
    return replace(node, **changes) if changes else node


def _base_env(task: Mapping[str, Any], values: Optional[Mapping[str, Any]] = None) -> Dict[str, str]:
    raw = task.get("env", {})
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(key): _replace_tokens(str(value), values or {})
        for key, value in raw.items()
        if str(key) != "CUDA_VISIBLE_DEVICES"
    }


def _commands_for_one_to_one(task: Mapping[str, Any], count: int) -> List[str]:
    commands = task.get("commands", [])
    if isinstance(commands, list) and commands:
        if len(commands) != count:
            raise DispatchError(
                f"task {task.get('id')!r}: commands has {len(commands)} entries but {count} GPUs were assigned"
            )
        return [str(command) for command in commands]
    command = str(task.get("command") or "")
    return [command] * count


def _group_assignments(task: Mapping[str, Any]) -> Dict[str, List[int]]:
    grouped: Dict[str, List[int]] = {}
    for group in task.get("assignments", []):
        if not isinstance(group, Mapping):
            continue
        alias = str(group.get("node_alias") or "")
        values = group.get("gpu_indices", [])
        if not isinstance(values, list):
            values = [values]
        grouped[alias] = [int(value) for value in values]
    return grouped


def _group_storage_paths(task: Mapping[str, Any]) -> Dict[str, str]:
    paths: Dict[str, str] = {}
    for group in task.get("assignments", []):
        if isinstance(group, Mapping) and group.get("node_alias"):
            paths[str(group["node_alias"])] = str(group.get("storage_path") or "")
    return paths


def _group_work_dirs(task: Mapping[str, Any]) -> Dict[str, str]:
    work_dirs: Dict[str, str] = {}
    for group in task.get("assignments", []):
        if isinstance(group, Mapping) and group.get("node_alias"):
            work_dirs[str(group["node_alias"])] = str(group.get("work_dir") or "")
    return work_dirs


def _group_path_field(task: Mapping[str, Any], key: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for group in task.get("assignments", []):
        if isinstance(group, Mapping) and group.get("node_alias"):
            value = str(group.get(key) or "").strip()
            if value:
                values[str(group["node_alias"])] = value
    return values


def _group_environments(task: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    environments: Dict[str, Mapping[str, Any]] = {}
    for group in task.get("assignments", []):
        if not isinstance(group, Mapping) or not group.get("node_alias"):
            continue
        value = group.get("environment")
        if isinstance(value, Mapping):
            environments[str(group["node_alias"])] = value
    return environments


def _artifact_targets(
    task: Mapping[str, Any],
    contexts: Sequence[RemoteExecutionContext],
) -> List[tuple[Any, Dict[str, Any]]]:
    targets: List[tuple[Any, Dict[str, Any]]] = []
    seen: set[str] = set()
    for context in contexts:
        node = context.target.node_config
        if node is None:
            raise DispatchError(f"missing node config for {context.target.node_alias}")
        alias = str(context.target.node_alias)
        if alias in seen:
            continue
        seen.add(alias)
        indices = list(context.target.gpu_indices)
        values = _task_context_values(
            task,
            alias,
            indices[0] if indices else -1,
            0,
            max(1, len(contexts)),
            context.disk_path,
            str(getattr(node, "work_dir", "") or ""),
            indices,
            context.data_path,
            context.model_path,
        )
        targets.append((node, values))
    return targets


def _task_artifacts(task: Mapping[str, Any]) -> List[Any]:
    artifacts = task.get("artifacts", [])
    if artifacts is None:
        return []
    if not isinstance(artifacts, list):
        raise DispatchError(f"task {task.get('id')!r}: artifacts must be a list")
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            raise DispatchError(f"task {task.get('id')!r}: artifact must be an object")
    return artifacts


async def _prepare_task_artifacts(
    task: Mapping[str, Any],
    artifacts: Sequence[Mapping[str, Any]],
    artifact_cache_dir: Path,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Resolve controller-side artifact sources, downloading modelscope snapshots once."""

    prepared: List[Dict[str, Any]] = []
    for artifact in artifacts:
        if (
            not is_modelscope_artifact(artifact)
            or str(artifact.get("kind") or "") == "model"
        ):
            prepared.append(dict(artifact))
            continue
        model_id = str(artifact.get("model_id") or artifact.get("source") or "")
        try:
            snapshot, status = await ensure_modelscope_snapshot(artifact_cache_dir, artifact)
        except ArtifactSyncError as exc:
            return prepared, [artifact_failure_result(artifact, str(exc))]
        resolved = dict(artifact)
        resolved["resolved_source"] = str(snapshot)
        resolved["download"] = {"status": status, "model_id": model_id}
        prepared.append(resolved)
    return prepared, []


def _artifact_preview(
    task: Mapping[str, Any],
    artifacts: Sequence[Mapping[str, Any]],
    contexts: Sequence[RemoteExecutionContext],
    artifact_cache_dir: Path,
) -> List[Dict[str, Any]]:
    preview: List[Dict[str, Any]] = []
    for node, values in _artifact_targets(task, contexts):
        for artifact in artifacts:
            kind = str(artifact.get("kind") or "")
            allowed_prefix = ""
            if kind in {"data", "model"}:
                work_dir = str(values.get("work_dir") or "").strip()
                if work_dir:
                    allowed_prefix = posixpath.join(
                        work_dir, "data" if kind == "data" else "models"
                    )
            destination = expand_artifact_destination(
                str(artifact.get("destination") or ""),
                values,
                allowed_prefix=allowed_prefix,
            )
            resolved = str(artifact.get("resolved_source") or "").strip()
            if (
                not resolved
                and is_modelscope_artifact(artifact)
                and str(artifact.get("kind") or "") != "model"
            ):
                resolved = str(modelscope_snapshot_dir(artifact_cache_dir, artifact))
            item = {
                "name": str(artifact.get("name") or ""),
                "kind": str(artifact.get("kind") or ""),
                "node_alias": node.alias,
                "source": str(artifact.get("source") or ""),
                "resolved_source": resolved or None,
                "destination": destination,
                "mode": str(artifact.get("mode") or ""),
                "sha256": artifact.get("sha256"),
                "timeout_s": int(artifact.get("timeout_s") or 3600),
                "status": "planned",
                "success": True,
            }
            if str(artifact.get("kind") or "") == "model":
                order = ["target-local"]
                if artifact.get("model_id"):
                    order.append("modelscope-direct")
                if str(artifact.get("source_type") or "") == "local":
                    order.append("controller-rsync-fallback")
                item["acquisition_order"] = order
            preview.append(item)
    return preview


async def _sync_task_artifacts(
    task: Mapping[str, Any],
    artifacts: Sequence[Mapping[str, Any]],
    contexts: Sequence[RemoteExecutionContext],
    verify_dir: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    if not artifacts:
        return []

    async def sync_node(node: Any, values: Mapping[str, Any]) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for artifact in artifacts:
            try:
                result = await sync_artifact_to_node(node, artifact, values, verify_dir=verify_dir)
            except ArtifactSyncError as exc:
                result = artifact_failure_result(artifact, str(exc))
                result["node_alias"] = node.alias
            results.append(result)
            if not result.get("success"):
                break
        return results

    batches = await asyncio.gather(
        *(sync_node(node, values) for node, values in _artifact_targets(task, contexts)),
        return_exceptions=True,
    )
    for batch in batches:
        if isinstance(batch, Exception):
            raise batch
    return [result for batch in batches for result in batch]


async def _check_environment_context(context: RemoteExecutionContext) -> Dict[str, Any]:
    """Run a task's short environment check without starting the workload."""

    command = str(context.environment_check or "").strip()
    if not command:
        return {
            "node_alias": context.target.node_alias,
            "status": "not_configured",
            "passed": True,
        }
    check_context = replace(
        context,
        command=command,
        execution_mode="direct",
        tmux_session=None,
        timeout_s=min(max(1, int(context.timeout_s)), 60),
    )
    try:
        result = await route_command_to_target(check_context)
        return {
            "node_alias": context.target.node_alias,
            "status": "passed" if result.success else "failed",
            "passed": bool(result.success),
            "command": command,
            "exit_code": int(result.exit_code),
            "stdout": str(result.stdout or "")[-4000:],
            "stderr": str(result.stderr or "")[-4000:],
        }
    except Exception as exc:
        return {
            "node_alias": context.target.node_alias,
            "status": "failed",
            "passed": False,
            "command": command,
            "exit_code": 1,
            "stdout": "",
            "stderr": str(exc)[-4000:],
        }


async def _check_environments(
    contexts: Sequence[RemoteExecutionContext],
) -> List[Dict[str, Any]]:
    """Check only tasks that declare a lightweight environment probe."""

    return list(await asyncio.gather(*(_check_environment_context(context) for context in contexts)))


async def _route_contexts(
    contexts: Sequence[RemoteExecutionContext],
) -> List[Dict[str, Any]]:
    results = await asyncio.gather(
        *(route_command_to_target(context) for context in contexts),
        return_exceptions=True,
    )
    serialised: List[Dict[str, Any]] = []
    for context, result in zip(contexts, results):
        if isinstance(result, Exception):
            serialised.append(
                {
                    "node_alias": context.target.node_alias,
                    "gpu_indices": list(context.target.gpu_indices),
                    "command": context.command,
                    "success": False,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": str(result),
                }
            )
        else:
            serialised.append(
                {
                    "node_alias": result.node_alias,
                    "gpu_indices": list(context.target.gpu_indices),
                    "command": result.command,
                    "success": bool(result.success),
                    "exit_code": int(result.exit_code),
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "tmux_session": context.tmux_session,
                }
            )
    return serialised


async def _preflight_contexts(contexts: Sequence[RemoteExecutionContext], mode: str) -> List[Dict[str, Any]]:
    checks: List[PreflightCheck] = []
    for context in contexts:
        node = context.target.node_config
        if node is None:
            raise DispatchError(f"missing node config for {context.target.node_alias}")
        if mode == "tmux":
            checks.append(
                PreflightCheck(
                    check_type=PreflightCheckType.TMUX_AVAILABLE,
                    node_alias=context.target.node_alias,
                    node_config=node,
                )
            )
        checks.append(
            PreflightCheck(
                check_type=PreflightCheckType.COMMAND_SYNTAX,
                node_alias=context.target.node_alias,
                command_to_validate=context.command,
                node_config=node,
            )
        )
        if context.target.gpu_indices:
            checks.append(
                PreflightCheck(
                    check_type=PreflightCheckType.GPU_AVAILABILITY,
                    node_alias=context.target.node_alias,
                    target_gpu_indices=list(context.target.gpu_indices),
                    node_config=node,
                )
            )

    results = await run_preflight_checks(checks)
    serialised = [
        {
            "check_type": result.check.check_type.value,
            "node_alias": result.check.node_alias,
            "passed": bool(result.passed),
            "error_message": result.error_message,
            "metadata": result.metadata,
            "timestamp": result.timestamp,
        }
        for result in results
    ]
    if any(not result["passed"] for result in serialised):
        raise DispatchError("preflight failed; no task command was started")
    return serialised


async def _execute_task(
    task: Mapping[str, Any],
    node_configs: Mapping[str, Any],
    *,
    mode: str,
    timeout_s: int,
    skip_preflight: bool,
    dry_run: bool = False,
    artifact_cache_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    artifact_cache_dir = Path(artifact_cache_dir or Path.cwd() / ".adaptive-computation" / "modelscope")
    verify_dir = verified_dir(artifact_cache_dir)
    task_id = str(task.get("id") or "task")
    distribution = str(task.get("distribution") or "single")
    contexts: List[RemoteExecutionContext] = []

    if distribution == "one-to-one":
        flattened = _flatten_assignments(task)
        task_world_size = len(flattened)
        node_order = sorted({str(item["node_alias"]) for item in flattened})
        master_node = node_configs.get(node_order[0]) if node_order else None
        master_addr = getattr(master_node, "address", "")
        commands = _commands_for_one_to_one(task, len(flattened))
        local_ranks: Dict[str, int] = {}
        for rank, (item, command) in enumerate(zip(flattened, commands)):
            alias = str(item["node_alias"])
            gpu_index = int(item["gpu_index"])
            if alias not in node_configs:
                raise DispatchError(f"task {task_id!r} references unknown plan node {alias!r}")
            effective_node = _task_node(
                node_configs[alias],
                task,
                item.get("environment"),
                str(item.get("work_dir") or ""),
            )
            values = _task_context_values(
                task,
                alias,
                gpu_index,
                rank,
                task_world_size,
                str(item.get("disk_path") or ""),
                str(getattr(effective_node, "work_dir", "") or ""),
                [gpu_index],
                str(item.get("data_path") or ""),
                str(item.get("model_path") or ""),
            )
            env = _base_env(task, values)
            env["CUDA_VISIBLE_DEVICES"] = str(gpu_index)
            if bool(task.get("distributed")):
                node_rank = node_order.index(alias)
                local_rank = local_ranks.get(alias, 0)
                local_ranks[alias] = local_rank + 1
                env.setdefault("MASTER_ADDR", master_addr)
                env.setdefault("MASTER_PORT", str(int(task.get("master_port") or 29500)))
                env.setdefault("NNODES", str(len(node_order)))
                env.setdefault("NODE_RANK", str(node_rank))
                env.setdefault("LOCAL_RANK", str(local_rank))
                env.setdefault("WORLD_SIZE", str(task_world_size))
            session = _safe_session(f"adaptive-{task_id}-{alias}-gpu{gpu_index}") if mode == "tmux" else None
            contexts.append(
                RemoteExecutionContext(
                    target=NodeTarget(
                        alias,
                        [gpu_index],
                        effective_node,
                    ),
                    command=_replace_tokens(command, values),
                    env_vars=env,
                    execution_mode=mode,
                    tmux_session=session,
                    timeout_s=timeout_s,
                    environment_check=_replace_tokens(
                        str(task.get("environment_check") or ""), values
                    ) or None,
                    disk_path=str(item.get("disk_path") or ""),
                    data_path=str(item.get("data_path") or ""),
                    model_path=str(item.get("model_path") or ""),
                    max_gpu_utilization_percent=float(
                        task.get("max_gpu_utilization_percent") or 5
                    ),
                    min_gpu_free_memory_mib=int(
                        task.get("min_gpu_free_memory_mib") or 0
                    ),
                )
            )
    else:
        grouped = _group_assignments(task)
        aliases = sorted(grouped)
        if not aliases:
            raise DispatchError(f"task {task_id!r} has no node assignments")
        # ``single`` launches one process per selected node, even when that
        # process receives several GPUs through CUDA_VISIBLE_DEVICES.
        task_world_size = len(aliases)
        master_alias = aliases[0]
        storage_paths = _group_storage_paths(task)
        work_dirs = _group_work_dirs(task)
        environments = _group_environments(task)
        data_paths = _group_path_field(task, "data_path")
        model_paths = _group_path_field(task, "model_path")
        master_node = node_configs.get(master_alias)
        master_addr = getattr(master_node, "address", "")
        for node_rank, alias in enumerate(aliases):
            if alias not in node_configs:
                raise DispatchError(f"task {task_id!r} references unknown plan node {alias!r}")
            indices = grouped[alias]
            first_index = indices[0] if indices else -1
            effective_node = _task_node(
                node_configs[alias],
                task,
                environments.get(alias),
                work_dirs.get(alias, ""),
            )
            values = _task_context_values(
                task,
                alias,
                first_index,
                node_rank,
                task_world_size,
                storage_paths.get(alias, ""),
                str(getattr(effective_node, "work_dir", "") or ""),
                indices,
                data_paths.get(alias, ""),
                model_paths.get(alias, ""),
            )
            env = _base_env(task, values)
            if indices:
                env["CUDA_VISIBLE_DEVICES"] = values["gpu_indices"]
            if bool(task.get("distributed")):
                env.setdefault("MASTER_ADDR", master_addr)
                env.setdefault("MASTER_PORT", str(int(task.get("master_port") or 29500)))
                env.setdefault("NNODES", str(len(aliases)))
                env.setdefault("NODE_RANK", str(node_rank))
                env.setdefault("WORLD_SIZE", str(task_world_size))
            session = _safe_session(f"adaptive-{task_id}-{alias}") if mode == "tmux" else None
            contexts.append(
                RemoteExecutionContext(
                    target=NodeTarget(alias, indices, effective_node),
                    command=_replace_tokens(str(task.get("command") or ""), values),
                    env_vars=env,
                    execution_mode=mode,
                    tmux_session=session,
                    timeout_s=timeout_s,
                    environment_check=_replace_tokens(
                        str(task.get("environment_check") or ""), values
                    ) or None,
                    disk_path=storage_paths.get(alias, ""),
                    data_path=data_paths.get(alias, ""),
                    model_path=model_paths.get(alias, ""),
                    max_gpu_utilization_percent=float(
                        task.get("max_gpu_utilization_percent") or 5
                    ),
                    min_gpu_free_memory_mib=int(
                        task.get("min_gpu_free_memory_mib") or 0
                    ),
                )
            )

    if not contexts:
        raise DispatchError(f"task {task_id!r} has no executable assignments")
    artifacts = _task_artifacts(task)
    if dry_run:
        result = {
            "task_id": task_id,
            "status": "dry-run",
            "preflight": [],
            "results": [
                {
                    "node_alias": context.target.node_alias,
                    "gpu_indices": list(context.target.gpu_indices),
                    "command": context.command,
                    "env_vars": dict(context.env_vars),
                    "environment_check": context.environment_check,
                    "tmux_session": context.tmux_session,
                }
                for context in contexts
            ],
        }
        if artifacts:
            result["artifacts"] = _artifact_preview(task, artifacts, contexts, artifact_cache_dir)
        return result

    prepared_artifacts, download_failures = await _prepare_task_artifacts(
        task, artifacts, artifact_cache_dir
    )
    if download_failures:
        return {
            "task_id": task_id,
            "status": "failed",
            "preflight": [],
            "results": [],
            "artifacts": download_failures,
        }
    artifact_results = await _sync_task_artifacts(task, prepared_artifacts, contexts, verify_dir=verify_dir)
    if any(not item.get("success", False) for item in artifact_results):
        return {
            "task_id": task_id,
            "status": "failed",
            "preflight": [],
            "results": [],
            "artifacts": artifact_results,
        }

    environment_checks = await _check_environments(contexts)
    failed_checks = [check for check in environment_checks if not check.get("passed", False)]
    if failed_checks:
        return {
            "task_id": task_id,
            "status": "failed",
            "reason": "environment_mismatch",
            "preflight": [],
            "results": [],
            "artifacts": artifact_results,
            "environment": {
                "checks": environment_checks,
                "suggested_repair": str(task.get("environment_repair_command") or ""),
            },
        }

    preflight: List[Dict[str, Any]] = []
    if not skip_preflight:
        preflight = await _preflight_contexts(contexts, mode)

    serialised = await _route_contexts(contexts)

    successful = all(item.get("success", False) for item in serialised)
    result: Dict[str, Any] = {
        "task_id": task_id,
        "status": "running" if mode == "tmux" and successful else ("done" if successful else "failed"),
        "preflight": preflight,
        "results": serialised,
    }
    if artifact_results:
        result["artifacts"] = artifact_results
    if any(check.get("status") != "not_configured" for check in environment_checks):
        result["environment"] = {"checks": environment_checks}
    return result


async def dispatch_plan(
    plan: Mapping[str, Any],
    *,
    task_id: Optional[str] = None,
    mode: str = "tmux",
    timeout_s: int = 300,
    dry_run: bool = False,
    skip_preflight: bool = False,
    artifact_cache_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute selected/all tasks in plan order.

    Tasks are run sequentially in topological order.  Independent tasks are
    already assigned disjoint GPUs by the planner.  Direct mode can therefore
    execute a complete dependency chain safely; tmux mode refuses an
    unselected dependency chain because a detached session has no completion
    signal for the next task.
    """

    if mode not in {"direct", "tmux"}:
        raise DispatchError("mode must be direct or tmux")
    node_configs = _node_configs(plan)
    all_tasks = [task for task in plan.get("tasks", []) if isinstance(task, Mapping)]
    plan_order = [str(value) for value in plan.get("order", []) if value is not None]
    if plan_order:
        order_position = {value: position for position, value in enumerate(plan_order)}
        all_tasks.sort(key=lambda task: order_position.get(str(task.get("id")), len(order_position)))
    if task_id:
        tasks = [task for task in all_tasks if str(task.get("id")) == task_id]
        if not tasks:
            raise DispatchError(f"task not found in plan: {task_id}")
    else:
        tasks = [task for task in all_tasks if not is_environment_maintenance(task)]
        if not tasks:
            raise DispatchError(
                "plan contains only environment-maintenance tasks; select one with --task"
            )
    maintenance_ids = {
        str(task.get("id"))
        for task in all_tasks
        if is_environment_maintenance(task)
    }
    has_live_dependencies = any(
        str(dependency) not in maintenance_ids
        for task in tasks
        for dependency in (task.get("depends_on", []) or [])
    )
    if mode == "tmux" and not dry_run and not task_id and has_live_dependencies:
        raise DispatchError(
            "tmux cannot infer completion of dependent tasks; run each task with --task "
            "or use --mode direct for the full dependency chain"
        )
    results: List[Dict[str, Any]] = []
    resolved_cache_dir = Path(
        artifact_cache_dir or Path.cwd() / ".adaptive-computation" / "modelscope"
    ).expanduser()

    for task in tasks:
        result = await _execute_task(
            task,
            node_configs,
            mode=mode,
            timeout_s=timeout_s,
            skip_preflight=skip_preflight,
            artifact_cache_dir=resolved_cache_dir,
            dry_run=dry_run,
        )
        results.append(result)
        if result.get("status") == "failed":
            break

    return {
        "schema_version": 1,
        "plan_generated_at": plan.get("generated_at"),
        "dry_run": bool(dry_run),
        "tasks": results,
        "ok": all(result.get("status") in {"done", "running", "dry-run"} for result in results),
    }


def dispatch_plan_sync(plan: Mapping[str, Any], **kwargs: Any) -> Dict[str, Any]:
    return asyncio.run(dispatch_plan(plan, **kwargs))
