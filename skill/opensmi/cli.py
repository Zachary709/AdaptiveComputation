from __future__ import annotations

import argparse
import asyncio
import copy
import getpass
import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from adaptive_core.models import node_config_from_inventory_node
from adaptive_core.sshutil import SSHRunError, is_local_node, ssh_bash_script
from adaptive_core.state import atomic_write_text, get_state_dir

from . import __version__
from .client import ServiceClient, ServiceError
from .dispatcher import DispatchError, dispatch_plan_sync
from .local import resolve_node_path, resolve_server
from .planner import (
    PlanningError,
    load_plan,
    load_task_manifest,
    plan_tasks,
    save_plan,
)
from .registration import (
    RegistrationError,
    build_probe_script,
    expected_kind_root,
    parse_probe_output,
    validate_directory,
    validate_work_path,
)
from .repair import repair_local_record, run_repair
from .snapshot import (
    SnapshotError,
    allocations_for_plan,
    load_snapshot,
    policy_for_plan,
    require_fresh,
    validate_snapshot,
)


def _fmt_bytes(value: Any) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return "-"
    if number <= 0:
        return "-"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if number < 1024 or unit == "PiB":
            return f"{number:.1f} {unit}"
        number /= 1024
    return "-"


def _inventory_summary(document: Mapping[str, Any]) -> str:
    rows: List[str] = []
    for node in document.get("nodes", []):
        if not isinstance(node, dict):
            continue
        hardware = node.get("hardware", {})
        gpu = hardware.get("gpu", {}) if isinstance(hardware, dict) else {}
        memory = hardware.get("memory", {}) if isinstance(hardware, dict) else {}
        state = "OK" if node.get("reachable") and not node.get("stale") else (
            "STALE" if node.get("stale") else "ERR"
        )
        rows.append(
            "  {alias:<24} {state:<5} GPU {free}/{total}  {model:<28} RAM {ram}".format(
                alias=str(node.get("alias", "?"))[:24],
                state=state,
                free=gpu.get("available_count", 0),
                total=gpu.get("count", 0),
                model=", ".join(str(x) for x in gpu.get("models", []))[:28] or "-",
                ram=f"{_fmt_bytes(memory.get('available_bytes'))}/{_fmt_bytes(memory.get('total_bytes'))}",
            )
        )
    header = "Alias                     State GPU free/total  GPU model                     RAM avail/total"
    return "\n".join([header, "-" * len(header)] + rows) if rows else "No nodes in snapshot."


def _client(args: argparse.Namespace, *, state_dir: Path) -> ServiceClient:
    return ServiceClient.from_environment(server=resolve_server(args.server))


def _snapshot_path(args: argparse.Namespace) -> Path:
    if args.snapshot:
        return Path(args.snapshot).expanduser().resolve()
    return Path(".adaptive-computation/snapshot.json").resolve()


def _resolve_snapshot(args: argparse.Namespace, *, state_dir: Path) -> Dict[str, Any]:
    if args.snapshot:
        payload = load_snapshot(Path(args.snapshot))
    else:
        payload = validate_snapshot(_client(args, state_dir=state_dir).snapshot())
    require_fresh(payload, int(args.max_snapshot_age))
    return payload


def _fetch_gpu_states(
    client: ServiceClient,
    aliases: Sequence[str],
) -> Dict[Any, str]:
    """Read the service's idle/occupied state for every GPU of every node."""
    states: Dict[Any, str] = {}
    for alias in sorted({str(alias) for alias in aliases if alias}):
        payload = client.gpu_state(alias)
        for device in payload.get("gpus") or []:
            if not isinstance(device, Mapping):
                continue
            try:
                index = int(device.get("index"))
            except (TypeError, ValueError):
                continue
            state = str(device.get("state") or "")
            if state not in {"idle", "occupied"}:
                raise ServiceError(
                    f"{alias} returned an unknown GPU state {state!r}"
                )
            states[(alias, index)] = state
    return states


def _plan_gpus(
    plan: Mapping[str, Any], task_id: Optional[str] = None
) -> List[Any]:
    selected: List[Any] = []
    seen: set[Any] = set()
    for task in plan.get("tasks", []):
        if not isinstance(task, Mapping):
            continue
        if task_id and str(task.get("id")) != str(task_id):
            continue
        for group in task.get("assignments", []):
            if not isinstance(group, Mapping):
                continue
            alias = str(group.get("node_alias") or "")
            for index in group.get("gpu_indices", []) or []:
                try:
                    key = (alias, int(index))
                except (TypeError, ValueError):
                    continue
                if alias and key not in seen:
                    seen.add(key)
                    selected.append(key)
    return selected


def _external_allocations(
    client: ServiceClient,
) -> Dict[Any, str]:
    payload = client.allocations()
    records: Dict[Any, str] = {}
    for item in payload.get("allocations", []) or []:
        if not isinstance(item, Mapping):
            continue
        alias = str(item.get("alias") or "")
        try:
            index = int(item.get("gpu_index"))
        except (TypeError, ValueError):
            continue
        target = str(item.get("target") or "").strip()
        if alias and target:
            records[(alias, index)] = target
    return records


def _parse_ttl(value: str) -> str:
    match = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", str(value or ""))
    if match is None:
        raise ValueError("--ttl must look like 30m, 12h, or 2d")
    amount = int(match.group(1))
    unit = match.group(2)
    seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit] * amount
    expires = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return expires.isoformat().replace("+00:00", "Z")


def _reserve_plan(
    client: ServiceClient,
    plan: Mapping[str, Any],
    *,
    target: str,
    ttl: str,
    task_id: Optional[str] = None,
) -> List[Any]:
    existing = _external_allocations(client)
    expires_at = _parse_ttl(ttl)
    created: List[Any] = []
    try:
        for alias, index in _plan_gpus(plan, task_id):
            current = existing.get((alias, index))
            if current and current not in {"*", target}:
                raise ServiceError(
                    f"GPU {alias}:{index} is allocated to {current}; release it or choose another plan"
                )
            client.upsert_allocation(
                {
                    "alias": alias,
                    "gpu_index": index,
                    "target": target,
                    "assigned_by": target,
                    "expires_at": expires_at,
                }
            )
            created.append((alias, index))
    except Exception:
        if created:
            _release_allocations(client, created)
        raise
    return created


def _release_allocations(
    client: ServiceClient, records: Sequence[Any]
) -> None:
    for alias, index in records:
        try:
            client.delete_allocation(str(alias), int(index))
        except ServiceError as exc:
            if "failed with 404" not in str(exc):
                print(
                    f"warning: could not release {alias}:{index}: {exc}",
                    file=sys.stderr,
                )


def _attach_environment_conflicts(
    client: ServiceClient, plan: Mapping[str, Any]
) -> None:
    for task in plan.get("tasks", []):
        if not isinstance(task, dict):
            continue
        for group in task.get("assignments", []):
            if not isinstance(group, dict):
                continue
            if group.get("environment_status") not in {"conflict", "missing"}:
                continue
            alias = str(group.get("node_alias") or "")
            try:
                group["environment_conflict"] = client.environment_conflict(alias)
            except ServiceError:
                continue


def _cmd_inventory(args: argparse.Namespace, *, state_dir: Path) -> int:
    if args.inventory_action == "pull":
        payload = validate_snapshot(_client(args, state_dir=state_dir).snapshot())
        output = _snapshot_path(args)
        atomic_write_text(output, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(_inventory_summary(payload))
            print(f"\nSnapshot written to: {output}")
        return 0

    payload = load_snapshot(_snapshot_path(args))
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(_inventory_summary(payload))
    return 0


def _build_plan(
    client: ServiceClient,
    snapshot: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    user: str,
    max_snapshot_age: int,
    ignore_allocations: bool,
    allow_unallocated: bool,
    gpu_state: str,
) -> Dict[str, Any]:
    policy = policy_for_plan(snapshot)
    allocations: Optional[List[Dict[str, Any]]] = allocations_for_plan(snapshot)
    require_allocation = bool(policy["require_allocation"])
    if ignore_allocations:
        allocations = None
        require_allocation = False
    if allow_unallocated:
        require_allocation = False

    gpu_states = None
    if gpu_state == "service":
        gpu_states = _fetch_gpu_states(
            client,
            [str(node.get("alias") or "") for node in snapshot.get("nodes", [])],
        )

    plan = plan_tasks(
        snapshot,
        manifest,
        allocations=allocations,
        current_user=user,
        require_allocation=require_allocation,
        all_users_token=policy["all_users_token"],
        source_root=Path.cwd().resolve(),
        gpu_states=gpu_states,
    )
    plan["inventory_snapshot_id"] = snapshot.get("snapshot_id")
    plan["inventory_generated_at"] = snapshot.get("generated_at")
    plan["inventory_max_age_s"] = int(max_snapshot_age)
    _attach_environment_conflicts(client, plan)
    return plan


def _restrict_snapshot(
    snapshot: Mapping[str, Any], devices: Sequence[Mapping[str, Any]]
) -> Dict[str, Any]:
    wanted: Dict[str, set] = {}
    for device in devices:
        alias = str(device.get("alias") or "")
        try:
            index = int(device.get("gpu_index"))
        except (TypeError, ValueError):
            continue
        if alias:
            wanted.setdefault(alias, set()).add(index)
    nodes = []
    for node in snapshot.get("nodes", []):
        if not isinstance(node, Mapping):
            continue
        alias = str(node.get("alias") or "")
        if alias not in wanted:
            continue
        clone = copy.deepcopy(dict(node))
        hardware = clone.get("hardware")
        gpu = hardware.get("gpu") if isinstance(hardware, Mapping) else None
        if isinstance(gpu, dict) and isinstance(gpu.get("devices"), list):
            kept = []
            for item in gpu["devices"]:
                if not isinstance(item, Mapping):
                    continue
                try:
                    if int(item.get("index")) in wanted[alias]:
                        kept.append(item)
                except (TypeError, ValueError):
                    continue
            gpu["devices"] = kept
            gpu["count"] = len(kept)
        nodes.append(clone)
    restricted = dict(snapshot)
    restricted["nodes"] = nodes
    return restricted


def _claimed_device_problems(
    client: ServiceClient,
    snapshot: Mapping[str, Any],
    devices: Sequence[Mapping[str, Any]],
    *,
    gpu_state: str,
) -> List[str]:
    nodes = {
        str(node.get("alias") or ""): node
        for node in snapshot.get("nodes", [])
        if isinstance(node, Mapping)
    }
    states: Dict[Any, str] = {}
    if gpu_state == "service":
        states = _fetch_gpu_states(
            client,
            [str(device.get("alias") or "") for device in devices],
        )
    problems: List[str] = []
    for device in devices:
        alias = str(device.get("alias") or "")
        try:
            index = int(device.get("gpu_index"))
        except (TypeError, ValueError):
            problems.append(f"{alias or '<unknown>'}: invalid gpu index")
            continue
        node = nodes.get(alias)
        if node is None:
            problems.append(f"{alias}:{index} is not in the snapshot")
            continue
        if node.get("reachable") is False or node.get("stale", False):
            problems.append(f"{alias}:{index} is on an unreachable or stale node")
            continue
        gpu = _find_node_gpu(node, index)
        if gpu is None:
            problems.append(f"{alias}:{index} is not in the snapshot")
            continue
        if gpu_state == "service":
            state = states.get((alias, index), "occupied")
            if state != "idle":
                problems.append(f"{alias}:{index} is {state}")
            continue
        try:
            process_count = int(gpu.get("process_count") or 0)
        except (TypeError, ValueError):
            process_count = 0
        processes = gpu.get("processes")
        if (
            gpu.get("available") is False
            or process_count > 0
            or (isinstance(processes, list) and processes)
        ):
            problems.append(f"{alias}:{index} is occupied")
    return problems


def _release_quiet(client: ServiceClient, plan_id: str) -> None:
    try:
        client.release_plan(plan_id)
    except ServiceError as exc:
        if exc.status != 404:
            print(
                f"warning: could not release plan {plan_id}: {exc}",
                file=sys.stderr,
            )


def _claim_text(exc: ServiceError) -> str:
    detail = exc.detail
    if isinstance(detail, Mapping):
        conflicts = detail.get("conflicts")
        if isinstance(conflicts, list) and conflicts:
            parts = []
            for item in conflicts:
                if not isinstance(item, Mapping):
                    continue
                alias = item.get("alias")
                index = item.get("gpu_index")
                reason = item.get("reason")
                holder = item.get("holder")
                label = f"{alias}:{index}" if index is not None else str(alias)
                parts.append(
                    f"{label} {reason}" + (f" (held by {holder})" if holder else "")
                )
            return "; ".join(parts)
        reason = detail.get("reason")
        if reason:
            holder = detail.get("holder")
            return str(reason) + (f" (held by {holder})" if holder else "")
    return str(exc)


def _result_reasons(path: Path) -> List[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    tasks = payload.get("tasks", []) if isinstance(payload, Mapping) else []
    reasons = []
    for task in tasks:
        if isinstance(task, Mapping) and task.get("reason"):
            reasons.append(str(task["reason"]))
    return reasons


def _cmd_plan(args: argparse.Namespace, *, state_dir: Path) -> int:
    client = _client(args, state_dir=state_dir)
    snapshot = _resolve_snapshot(args, state_dir=state_dir)
    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest = load_task_manifest(manifest_path)
    user = str(args.user or getpass.getuser())
    plan = _build_plan(
        client,
        snapshot,
        manifest,
        user=user,
        max_snapshot_age=int(args.max_snapshot_age),
        ignore_allocations=bool(args.ignore_allocations),
        allow_unallocated=bool(args.allow_unallocated),
        gpu_state=str(args.gpu_state),
    )
    output_path = Path(args.output).expanduser().resolve() if args.output else None
    if output_path:
        save_plan(output_path, plan)
    if args.json:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
    else:
        print("Task plan")
        for task in plan.get("tasks", []):
            placement = ", ".join(
                (
                    f"{group.get('node_alias')}:GPU{','.join(str(x) for x in group.get('gpu_indices', []))}"
                    if group.get("gpu_indices")
                    else f"{group.get('node_alias')}:CPU"
                )
                for group in task.get("assignments", [])
            )
            print(f"  {task.get('id')}: {placement}")
            for group in task.get("assignments", []):
                conflict = group.get("environment_conflict")
                if isinstance(conflict, Mapping) and conflict:
                    print(
                        f"    {group.get('node_alias')} environment conflict: {conflict}"
                    )
        print(f"  gpu source: {plan.get('gpu_source')}")
        if plan.get("server_plan_id"):
            print(f"  plan id: {plan.get('server_plan_id')}")
        print(f"  snapshot: {plan.get('inventory_snapshot_id')} ({plan.get('inventory_generated_at')})")
        if args.inspect:
            aliases = sorted(
                {
                    str(group.get("node_alias") or "")
                    for task in plan.get("tasks", [])
                    for group in task.get("assignments", [])
                    if group.get("node_alias")
                }
            )
            for alias in aliases:
                paths = client.paths(alias)
                registered = ", ".join(
                    f"{kind}={paths[kind]['path']}"
                    for kind in ("work", "data", "model")
                    if paths.get(kind)
                )
                print(f"  {alias} paths: {registered or '未注册'}")
                for kind in ("data", "model"):
                    if not paths.get(kind):
                        continue
                    try:
                        entries = client.directories(alias, kind).get("entries", [])
                    except ServiceError as exc:
                        print(f"    {kind}: {exc}")
                        continue
                    names = ", ".join(
                        str(entry.get("name"))
                        for entry in entries[:12]
                        if isinstance(entry, Mapping)
                    )
                    print(f"    {kind}: {names or '空'}")
        if output_path:
            print(f"Plan written to: {output_path}")
    return 0


def _find_node_gpu(
    node: Mapping[str, Any], index: int
) -> Optional[Mapping[str, Any]]:
    hardware = node.get("hardware")
    gpu = hardware.get("gpu") if isinstance(hardware, Mapping) else None
    devices = gpu.get("devices", []) if isinstance(gpu, Mapping) else []
    if not isinstance(devices, list):
        devices = []
    if not devices and isinstance(node.get("gpus"), list):
        devices = node["gpus"]
    for device in devices:
        if not isinstance(device, Mapping):
            continue
        try:
            if int(device.get("index")) == int(index):
                return device
        except (TypeError, ValueError):
            continue
    return None


def _verify_plan_source(
    plan: Mapping[str, Any],
    client: ServiceClient,
    *,
    target: str,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    plan_id = str(plan.get("server_plan_id") or "")
    if not plan_id:
        raise ServiceError(
            "plan was not created from a claimed service plan; use submit",
            status=400,
        )
    try:
        detail = client.plan(plan_id)
    except ServiceError as exc:
        raise ServiceError(
            f"plan {plan_id} could not be verified with the service: {exc}",
            status=exc.status,
            detail=exc.detail,
        ) from exc
    claimed_by = str(detail.get("claimed_by") or "")
    if claimed_by != target:
        raise ServiceError(
            f"plan {plan_id} is not claimed by {target!r}",
            status=409,
            detail={"claim_holder": claimed_by},
        )
    devices = set()
    for item in detail.get("devices", []) or []:
        if not isinstance(item, Mapping):
            continue
        try:
            devices.add((str(item.get("alias") or ""), int(item.get("gpu_index"))))
        except (TypeError, ValueError):
            continue
    if not devices:
        return
    planned = set()
    for task in plan.get("tasks", []) or []:
        if not isinstance(task, Mapping):
            continue
        for group in task.get("assignments", []) or []:
            if not isinstance(group, Mapping):
                continue
            alias = str(group.get("node_alias") or "")
            for raw_index in group.get("gpu_indices", []) or []:
                try:
                    planned.add((alias, int(raw_index)))
                except (TypeError, ValueError):
                    continue
    extra = sorted(planned - devices)
    if extra:
        rendered = ", ".join(f"{alias}:{index}" for alias, index in extra)
        raise ServiceError(
            f"plan uses devices outside the claimed service plan: {rendered}",
            status=409,
        )


def _verify_plan_still_valid(
    plan: Mapping[str, Any],
    client: ServiceClient,
    *,
    allow_change: bool,
    dry_run: bool,
) -> None:
    if allow_change or dry_run:
        return
    try:
        current = validate_snapshot(client.snapshot())
    except ServiceError as exc:
        print(f"warning: plan validity was not verified: {exc}", file=sys.stderr)
        return

    nodes = {
        str(node.get("alias") or ""): node
        for node in current.get("nodes", [])
        if isinstance(node, Mapping)
    }
    aliases = {
        str(group.get("node_alias") or "")
        for task in plan.get("tasks", [])
        for group in task.get("assignments", [])
        if isinstance(group, Mapping)
    }
    gpu_states = _fetch_gpu_states(client, aliases)
    try:
        allocations = _external_allocations(client)
    except ServiceError:
        allocations = {}
    policy = plan.get("allocation_policy")
    current_user = str(policy.get("current_user") or "") if isinstance(policy, Mapping) else ""

    for task in plan.get("tasks", []):
        if not isinstance(task, Mapping):
            continue
        for group in task.get("assignments", []):
            if not isinstance(group, Mapping):
                continue
            alias = str(group.get("node_alias") or "")
            node = nodes.get(alias)
            if node is None:
                raise SnapshotError(
                    f"plan node {alias!r} is no longer in the service snapshot"
                )
            if node.get("reachable") is False or node.get("stale", False):
                raise SnapshotError(f"plan node {alias!r} is now unreachable or stale")
            for raw_index in group.get("gpu_indices", []) or []:
                try:
                    index = int(raw_index)
                except (TypeError, ValueError):
                    continue
                device = _find_node_gpu(node, index)
                if device is None:
                    raise SnapshotError(
                        f"GPU {alias}:{index} is no longer present in the snapshot"
                    )
                state = gpu_states.get((alias, index))
                if state != "idle":
                    raise SnapshotError(
                        f"GPU {alias}:{index} is {state or 'not reported by the service'}; re-plan"
                    )
                target = allocations.get((alias, index))
                if target and target not in {"*", current_user}:
                    raise SnapshotError(
                        f"GPU {alias}:{index} is now allocated to {target}; re-plan"
                    )
            recorded = plan.get("nodes")
            recorded_node = (
                recorded.get(alias, {}) if isinstance(recorded, Mapping) else {}
            )
            work_path = str(recorded_node.get("work_path") or "")
            if work_path and str(node.get("work_path") or "") != work_path:
                raise SnapshotError(
                    f"registered work path for {alias!r} changed; re-plan"
                )


def _cmd_run(args: argparse.Namespace, *, state_dir: Path) -> int:
    plan_path = Path(args.plan).expanduser().resolve()
    plan = load_plan(plan_path)
    client = _client(args, state_dir=state_dir)
    _verify_plan_source(
        plan,
        client,
        target=str(args.reserve_target or getpass.getuser()),
        dry_run=bool(args.dry_run),
    )
    _verify_plan_still_valid(
        plan,
        client,
        allow_change=bool(args.allow_snapshot_change),
        dry_run=bool(args.dry_run),
    )
    created: List[Any] = []
    if args.reserve and not args.dry_run:
        created = _reserve_plan(
            client,
            plan,
            target=str(args.reserve_target or getpass.getuser()),
            ttl=str(args.allocation_ttl),
            task_id=args.task,
        )
    artifact_cache = (
        Path(args.artifact_cache).expanduser().resolve()
        if args.artifact_cache
        else plan_path.parent / "modelscope"
    )
    try:
        result = dispatch_plan_sync(
            plan,
            task_id=args.task,
            mode=str(args.mode),
            timeout_s=max(1, int(args.timeout)),
            dry_run=bool(args.dry_run),
            skip_preflight=bool(args.skip_preflight),
            artifact_cache_dir=str(artifact_cache),
        )
    finally:
        if created:
            _release_allocations(client, created)
    if args.output:
        atomic_write_text(
            Path(args.output).expanduser().resolve(),
            json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        for task in result.get("tasks", []):
            print(f"{task.get('task_id')}: {task.get('status')}")
            if task.get("reason"):
                print(f"  reason: {task.get('reason')}")
            for item in task.get("results", []):
                status = "DRY-RUN" if task.get("status") == "dry-run" else (
                    "OK" if item.get("success") else "FAIL"
                )
                gpus = ",".join(str(value) for value in item.get("gpu_indices", []))
                print(f"  {item.get('node_alias')} GPU{gpus}: {status}")
            for artifact in task.get("artifacts", []):
                status = "PLANNED" if task.get("status") == "dry-run" else (
                    "OK" if artifact.get("success") else "FAIL"
                )
                target = artifact.get("node_alias") or "controller"
                print(f"  artifact {artifact.get('name')} -> {target}: {status}")
            environment = task.get("environment")
            if isinstance(environment, dict):
                for check in environment.get("checks", []):
                    if check.get("status") != "not_configured":
                        print(f"  environment {check.get('node_alias')}: {check.get('status')}")
                if environment.get("suggested_repair"):
                    print(f"  suggested repair: {environment.get('suggested_repair')}")
    return 0 if result.get("ok") else 2


PLAN_CACHE_VERSION = 1
PLAN_CACHE_LIMIT = 20


def _submit_state_dir(
    args: argparse.Namespace, state_dir: Path, manifest_path: Path
) -> Path:
    """Keep the plan cache next to the manifest state unless a state dir was chosen."""

    if args.state_dir or os.environ.get("ADAPTIVE_STATE_DIR"):
        return state_dir
    parent = manifest_path.parent
    if parent.name == ".adaptive-computation":
        return parent
    return (parent / ".adaptive-computation").resolve()


def _plan_cache_path(state_dir: Path) -> Path:
    return state_dir / "plan-cache.json"


def _empty_plan_cache() -> Dict[str, Any]:
    return {"version": PLAN_CACHE_VERSION, "entries": {}}


def _load_plan_cache(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return _empty_plan_cache()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_plan_cache()
    if (
        not isinstance(payload, dict)
        or payload.get("version") != PLAN_CACHE_VERSION
        or not isinstance(payload.get("entries"), dict)
    ):
        return _empty_plan_cache()
    return payload


def _save_plan_cache(path: Path, cache: Mapping[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(cache, indent=2, ensure_ascii=False) + "\n",
    )


def _plan_cache_key(
    manifest: Mapping[str, Any], source_root: Path, user: str
) -> str:
    payload = {
        "version": PLAN_CACHE_VERSION,
        "manifest": manifest,
        "source_root": str(source_root),
        "user": user,
    }
    blob = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _invalidate_plan_cache(
    cache_path: Path, cache: Dict[str, Any], key: str, reason: str
) -> None:
    entry = cache.get("entries", {}).get(key)
    if not isinstance(entry, dict):
        return
    entry["status"] = "invalid"
    entry["invalidated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry["failure"] = reason
    _save_plan_cache(cache_path, cache)


def _prune_plan_cache(cache: Dict[str, Any]) -> None:
    entries = cache.get("entries")
    if not isinstance(entries, dict) or len(entries) <= PLAN_CACHE_LIMIT:
        return
    ordered = sorted(
        entries.items(),
        key=lambda item: (
            str(item[1].get("created_at") or "")
            if isinstance(item[1], Mapping)
            else ""
        ),
        reverse=True,
    )
    cache["entries"] = dict(ordered[:PLAN_CACHE_LIMIT])


def _run_claimed(
    client: ServiceClient,
    plan_id: str,
    plan_path: Path,
    output_path: Path,
    args: argparse.Namespace,
    *,
    target: str,
    state_dir: Path,
) -> int:
    run_args = argparse.Namespace(
        plan=str(plan_path),
        artifact_cache=args.artifact_cache,
        output=str(output_path),
        task=args.task,
        mode=str(args.mode),
        timeout=int(args.timeout),
        dry_run=bool(args.dry_run),
        skip_preflight=bool(args.skip_preflight),
        allow_snapshot_change=False,
        reserve=False,
        reserve_target=target,
        allocation_ttl=str(args.allocation_ttl),
        json=bool(args.json),
        server=args.server,
    )
    try:
        return _cmd_run(run_args, state_dir=state_dir)
    finally:
        try:
            client.release_plan(plan_id)
        except ServiceError as exc:
            if exc.status != 404:
                print(
                    f"warning: could not release plan {plan_id}: {exc}",
                    file=sys.stderr,
                )


def _remember_selected_plan(
    cache_path: Path,
    cache: Dict[str, Any],
    key: str,
    *,
    manifest_path: Path,
    plan_path: Path,
    plan_id: str,
) -> None:
    cache["entries"][key] = {
        "manifest_path": str(manifest_path),
        "plan_path": str(plan_path),
        "plan_id": plan_id,
        "status": "valid",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "invalidated_at": None,
        "failure": None,
    }
    _prune_plan_cache(cache)
    _save_plan_cache(cache_path, cache)


def _submit_dry_run(
    args: argparse.Namespace,
    *,
    client: ServiceClient,
    manifest: Mapping[str, Any],
    user: str,
    key: str,
    state_dir: Path,
    plan_path: Path,
    output_path: Path,
) -> int:
    if not args.refresh_plan and plan_path.is_file():
        plan = load_plan(plan_path)
        print(f"plan: {plan_path} (cached)")
    else:
        snapshot = _resolve_snapshot(args, state_dir=state_dir)
        plan = _build_plan(
            client,
            snapshot,
            manifest,
            user=user,
            max_snapshot_age=int(args.max_snapshot_age),
            ignore_allocations=bool(args.ignore_allocations),
            allow_unallocated=not bool(args.strict_allocations),
            gpu_state=str(args.gpu_state),
        )
        save_plan(plan_path, plan)
        print(f"plan: {plan_path} (new)")
    run_args = argparse.Namespace(
        plan=str(plan_path),
        artifact_cache=args.artifact_cache,
        output=str(output_path),
        task=args.task,
        mode=str(args.mode),
        timeout=int(args.timeout),
        dry_run=True,
        skip_preflight=bool(args.skip_preflight),
        allow_snapshot_change=False,
        reserve=False,
        reserve_target=args.reserve_target,
        allocation_ttl=str(args.allocation_ttl),
        json=bool(args.json),
        server=args.server,
    )
    return _cmd_run(run_args, state_dir=state_dir)


def _cmd_submit(args: argparse.Namespace, *, state_dir: Path) -> int:
    manifest_path = Path(args.manifest).expanduser().resolve()
    state_dir = _submit_state_dir(args, state_dir, manifest_path)
    manifest = load_task_manifest(manifest_path)
    user = str(args.user or getpass.getuser())
    key = _plan_cache_key(manifest, Path.cwd().resolve(), user)
    cache_path = _plan_cache_path(state_dir)
    cache = _load_plan_cache(cache_path)
    entry = cache["entries"].get(key)
    client = _client(args, state_dir=state_dir)
    target = str(args.reserve_target or user)
    ttl = str(args.allocation_ttl)
    plan_path = state_dir / "plans" / f"{key}.json"
    output_path = (
        Path(args.output).expanduser().resolve()
        if args.output
        else state_dir / f"result-{key}.json"
    )

    if args.dry_run:
        return _submit_dry_run(
            args,
            client=client,
            manifest=manifest,
            user=user,
            key=key,
            state_dir=state_dir,
            plan_path=plan_path,
            output_path=output_path,
        )

    failures: List[str] = []
    tried: set = set()

    def attempt(plan_id: str) -> Optional[int]:
        if plan_id in tried:
            return None
        tried.add(plan_id)
        try:
            detail = client.plan(plan_id)
        except ServiceError as exc:
            if exc.status == 404:
                failures.append(f"plan {plan_id} no longer exists")
                return None
            raise
        try:
            claim = client.claim_plan(
                plan_id, target=target, ttl=ttl, task_id=args.task
            )
        except ServiceError as exc:
            if exc.status != 409:
                raise
            failures.append(f"plan {plan_id}: {_claim_text(exc)}")
            return None
        devices = claim.get("devices") or detail.get("devices") or []
        if not devices:
            _release_quiet(client, plan_id)
            failures.append(f"plan {plan_id} has no devices")
            return None
        try:
            snapshot = _resolve_snapshot(args, state_dir=state_dir)
            problems = _claimed_device_problems(
                client, snapshot, devices, gpu_state=str(args.gpu_state)
            )
            if problems:
                raise PlanningError(
                    "claimed devices are no longer usable: "
                    + "; ".join(problems[:8])
                )
            plan = _build_plan(
                client,
                _restrict_snapshot(snapshot, devices),
                manifest,
                user=user,
                max_snapshot_age=int(args.max_snapshot_age),
                ignore_allocations=True,
                allow_unallocated=True,
                gpu_state=str(args.gpu_state),
            )
        except (SnapshotError, ServiceError, PlanningError) as exc:
            _release_quiet(client, plan_id)
            failures.append(f"plan {plan_id} is infeasible: {exc}")
            return None
        plan["server_plan_id"] = plan_id
        save_plan(plan_path, plan)
        _remember_selected_plan(
            cache_path,
            cache,
            key,
            manifest_path=manifest_path,
            plan_path=plan_path,
            plan_id=plan_id,
        )
        print(f"plan: {plan_path} ({plan_id})")
        try:
            code = _run_claimed(
                client,
                plan_id,
                plan_path,
                output_path,
                args,
                target=target,
                state_dir=state_dir,
            )
        except SnapshotError as exc:
            _invalidate_plan_cache(cache_path, cache, key, f"preflight: {exc}")
            print(f"plan invalidated: {exc}", file=sys.stderr)
            raise
        except Exception as exc:
            _invalidate_plan_cache(cache_path, cache, key, f"dispatch: {exc}")
            raise

        if code != 0:
            reasons = _result_reasons(output_path)
            environment_only = bool(reasons) and all(
                reason == "environment_mismatch" for reason in reasons
            )
            if environment_only:
                print("plan kept: environment mismatch", file=sys.stderr)
            else:
                _invalidate_plan_cache(cache_path, cache, key, "execution failed")
                print("plan invalidated: execution failed", file=sys.stderr)
        return code

    selected = str(getattr(args, "plan_id", "") or "")
    if selected:
        code = attempt(selected)
        if code is not None:
            return code
        for reason in failures:
            print(reason, file=sys.stderr)
        print(
            "Ask the user whether to repair the environment or choose another plan.",
            file=sys.stderr,
        )
        return 3

    if (
        not args.refresh_plan
        and isinstance(entry, Mapping)
        and entry.get("status") == "valid"
    ):
        cached_id = str(entry.get("plan_id") or "")
        if cached_id:
            code = attempt(cached_id)
            if code is not None:
                return code
            _invalidate_plan_cache(
                cache_path,
                cache,
                key,
                failures[-1] if failures else "cached plan unavailable",
            )

    try:
        listing = client.plans()
    except ServiceError as exc:
        print(f"cannot list plans: {exc}", file=sys.stderr)
        return 3

    plans = [
        item for item in listing.get("plans", []) or [] if isinstance(item, Mapping)
    ]
    plans.sort(
        key=lambda item: (
            len(item.get("devices") or []),
            str(item.get("plan_id") or ""),
        )
    )
    for summary in plans:
        plan_id = str(summary.get("plan_id") or "")
        if not plan_id:
            continue
        code = attempt(plan_id)
        if code is not None:
            return code

    print("no registered plan can be claimed for this manifest:", file=sys.stderr)
    for reason in failures:
        print(f"  {reason}", file=sys.stderr)
    print(
        "Ask the user whether to repair the environment or choose another plan. "
        "Use --plan-id to retry a specific plan.",
        file=sys.stderr,
    )
    return 3


def _outbox_path(state_dir: Path) -> Path:
    return state_dir / "outbox.jsonl"


def _append_outbox(state_dir: Path, record: Mapping[str, Any]) -> None:
    path = _outbox_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(record), ensure_ascii=False) + "\n")


def _flush_outbox(client: ServiceClient, state_dir: Path) -> List[str]:
    path = _outbox_path(state_dir)
    if not path.is_file():
        return []
    remaining: List[str] = []
    submitted: List[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            client.report_environment(record)
            submitted.append(str(record.get("alias") or ""))
        except ServiceError:
            remaining.append(line)
    if remaining:
        atomic_write_text(path, "\n".join(remaining) + "\n")
    elif path.exists():
        path.unlink()
    return submitted


def _cmd_repair(args: argparse.Namespace, *, state_dir: Path) -> int:
    snapshot = _resolve_snapshot(args, state_dir=state_dir)
    raw_node = None
    for node in snapshot.get("nodes", []):
        if isinstance(node, Mapping) and str(node.get("alias") or "") == args.node:
            raw_node = node
            break
    if raw_node is None:
        raise SnapshotError(f"unknown node: {args.node}")
    node_config = node_config_from_inventory_node(dict(raw_node))

    try:
        outcome = asyncio.run(
            run_repair(
                node_config,
                command=args.command,
                check=args.check or "",
                timeout_s=max(1, int(args.timeout)),
            )
        )
    except Exception as exc:
        print(f"repair failed to execute: {exc}", file=sys.stderr)
        return 2

    repairs_dir = state_dir / "repairs"
    repairs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    local_path = repairs_dir / f"{stamp}-{args.node}.json"
    atomic_write_text(
        local_path,
        json.dumps(repair_local_record(outcome, args.reporter), indent=2, ensure_ascii=False) + "\n",
    )

    if args.json:
        print(json.dumps(outcome.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(f"{outcome.node_alias}: {'done' if outcome.success else 'failed'}")
        if outcome.check:
            print(f"  check exit: {outcome.check_exit_code}")
        if outcome.command_stderr.strip():
            print(f"  stderr: {outcome.command_stderr.strip()[:2000]}")

    if not outcome.success:
        return 2
    if not args.report:
        return 0

    record = {
        "alias": args.node,
        "manager": args.manager,
        "path": args.path or "",
        "version": args.version or "",
        "project_path": args.project_path or "",
        "python_version": args.python_version or "",
        "uv_version": args.uv_version or "",
        "lockfile": args.lockfile or "",
        "lock_sha256": args.lock_sha256 or "",
        "cache_hit": args.cache_hit,
        "check_command": args.check or "",
        "check_exit_code": outcome.check_exit_code,
        "evidence": f"{args.check or args.command} exited with {outcome.check_exit_code}",
        "reporter": args.reporter,
    }
    client = _client(args, state_dir=state_dir)
    try:
        _flush_outbox(client, state_dir)
        client.report_environment(record)
        print(f"reported {args.node} environment to {client.server}")
    except ServiceError as exc:
        _append_outbox(state_dir, record)
        print(f"warning: report was queued in the outbox: {exc}", file=sys.stderr)
    return 0


def _probe_registration(
    node: Mapping[str, Any], kind: str, path: str
) -> Dict[str, Any]:
    config = node_config_from_inventory_node(dict(node))
    script = build_probe_script(kind, path)
    if is_local_node(config):
        try:
            completed = subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RegistrationError(f"本地注册校验失败: {exc}") from exc
        rc, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
    else:
        try:
            rc, stdout, stderr = asyncio.run(
                ssh_bash_script(config, script, timeout_s=120)
            )
        except SSHRunError as exc:
            raise RegistrationError(
                f"注册校验无法在 {node.get('alias')} 上执行: {exc}"
            ) from exc
    if rc != 0:
        detail = stderr.strip() or stdout.strip()
        raise RegistrationError(
            f"注册校验脚本在 {node.get('alias')} 上失败: {detail}"
        )
    return parse_probe_output(stdout, stderr)


def _cmd_register_path(args: argparse.Namespace, *, state_dir: Path) -> int:
    client = _client(args, state_dir=state_dir)
    snapshot = client.snapshot()
    node = next(
        (
            item
            for item in snapshot.get("nodes", [])
            if isinstance(item, Mapping) and item.get("alias") == args.node
        ),
        None,
    )
    if node is None:
        raise RegistrationError(f"节点不存在: {args.node}")
    if node.get("stale"):
        raise RegistrationError(f"节点快照已过期: {args.node}")
    path = resolve_node_path(args.kind, args.node, args.path).strip().rstrip("/")
    if not path.startswith("/"):
        raise RegistrationError(f"路径必须是绝对路径: {path}")
    if args.kind in ("data", "model"):
        expected = expected_kind_root(str(node.get("work_path") or ""), args.kind)
        if posixpath.normpath(path) != expected:
            raise RegistrationError(
                f"{args.kind} 路径必须是 {expected}，收到 {path}"
            )
    report = _probe_registration(node, args.kind, path)
    if args.kind == "work":
        validate_work_path(path, report)
    else:
        validate_directory(args.kind, path, report)
    record = client.register_path(args.node, args.kind, path, args.reporter)
    if args.json:
        print(json.dumps(record, indent=2, ensure_ascii=False))
    else:
        print(f"{record['alias']}: {record['kind']} path registered as {record['path']}")
    return 0


def _cmd_inspect(args: argparse.Namespace, *, state_dir: Path) -> int:
    client = _client(args, state_dir=state_dir)
    snapshot = client.snapshot()
    aliases = list(args.node or [str(node.get("alias") or "") for node in snapshot.get("nodes", [])])
    payload: Dict[str, Any] = {}
    for alias in aliases:
        if not alias:
            continue
        paths = client.paths(alias)
        record: Dict[str, Any] = {
            "alias": alias,
            "paths": {
                kind: (paths[kind] or {}).get("path")
                for kind in ("work", "data", "model")
            },
            "registered_at": {
                kind: (paths[kind] or {}).get("registered_at")
                for kind in ("work", "data", "model")
            },
        }
        for kind in ("data", "model"):
            if args.kind and kind != args.kind:
                continue
            if not paths.get(kind):
                continue
            try:
                record[kind] = client.directories(alias, kind).get("entries", [])
            except ServiceError as exc:
                record[kind] = {"error": str(exc)}
        if args.uv_cache:
            try:
                record["uv_cache"] = client.uv_cache(alias)
            except ServiceError as exc:
                record["uv_cache"] = {"error": str(exc)}
        payload[alias] = record
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    for alias, record in payload.items():
        print(alias)
        for kind in ("work", "data", "model"):
            print(f"  {kind}: {record['paths'].get(kind) or '未注册'}")
        for kind in ("data", "model"):
            entries = record.get(kind)
            if entries is None:
                continue
            if isinstance(entries, Mapping) and entries.get("error"):
                print(f"  {kind} entries: {entries['error']}")
                continue
            names = ", ".join(
                f"{entry.get('name')} ({_fmt_bytes(entry.get('size_bytes'))})"
                for entry in entries
                if isinstance(entry, Mapping)
            )
            print(f"  {kind} entries: {names or '空'}")
        if args.uv_cache:
            cache = record.get("uv_cache")
            if isinstance(cache, Mapping) and cache.get("error"):
                print(f"  uv cache: {cache['error']}")
            elif isinstance(cache, Mapping):
                versions = ", ".join(cache.get("python_versions") or []) or "-"
                cached = {
                    (str(item.get("name") or "").lower(), str(item.get("version") or ""))
                    for item in (cache.get("packages") or [])
                    + (cache.get("installed") or [])
                    if isinstance(item, Mapping)
                }
                print(
                    f"  uv: {cache.get('uv_path') or '-'}\n"
                    f"  uv cache: {len(cache.get('packages') or [])} cached, "
                    f"{len(cache.get('installed') or [])} installed, "
                    f"python {versions}, {len(cache.get('locks') or [])} locks"
                )
                for lock in cache.get("locks") or []:
                    if not isinstance(lock, Mapping):
                        continue
                    missing = [
                        item
                        for item in lock.get("packages") or []
                        if isinstance(item, Mapping)
                        and (
                            str(item.get("name") or "").lower(),
                            str(item.get("version") or ""),
                        )
                        not in cached
                    ]
                    print(
                        f"    lock {lock.get('project')}: "
                        f"{len(lock.get('packages') or [])} packages, "
                        f"{len(missing)} missing from cache"
                    )
            else:
                print("  uv cache: 空")
    return 0


def _cmd_gpu(args: argparse.Namespace, *, state_dir: Path) -> int:
    client = _client(args, state_dir=state_dir)
    aliases = list(args.node or [])
    if not aliases:
        snapshot = client.snapshot()
        aliases = [
            str(node.get("alias") or "")
            for node in snapshot.get("nodes", [])
            if node.get("reachable", True)
        ]
    payload: Dict[str, Any] = {}
    for alias in aliases:
        if not alias:
            continue
        try:
            payload[alias] = client.nvitop(alias, retry=bool(args.retry))
        except ServiceError as exc:
            payload[alias] = {"error": str(exc)}
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    for alias, record in payload.items():
        if record.get("error"):
            print(f"{alias}: {record['error']}")
            continue
        print(f"{alias} {record.get('observed_at')}")
        for device in record.get("devices", []):
            used = float(device.get("memory_used_mib") or 0)
            total = float(device.get("memory_total_mib") or 0)
            processes = device.get("processes") or []
            print(
                f"  GPU{device.get('index')} {device.get('name')}: "
                f"{used:.0f}/{total:.0f} MiB "
                f"util {device.get('utilization_percent')}% "
                f"processes {len(processes)}"
            )
    return 0


def _cmd_environment(args: argparse.Namespace, *, state_dir: Path) -> int:
    client = _client(args, state_dir=state_dir)
    payload: Dict[str, Any] = {}
    for alias in args.node:
        try:
            record = client.environment(alias)
        except ServiceError as exc:
            if "failed with 404" not in str(exc):
                raise
            record = None
        conflict = None
        try:
            conflict = client.environment_conflict(alias)
        except ServiceError as exc:
            if "failed with 404" not in str(exc):
                raise
        payload[alias] = {"record": record, "conflict": conflict}
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    for alias, item in payload.items():
        record = item.get("record") or {}
        if not record:
            print(f"{alias}: 未注册环境记录")
            continue
        print(
            f"{alias}: {record.get('status')} {record.get('manager')} "
            f"{record.get('path')} {record.get('version')}"
        )
        conflict = item.get("conflict")
        if conflict:
            print(
                f"  conflict: field={conflict.get('field')} "
                f"expected={conflict.get('expected')} observed={conflict.get('observed')}"
            )
    return 0


def _cmd_path(args: argparse.Namespace, *, state_dir: Path) -> int:
    client = _client(args, state_dir=state_dir)
    if args.kind:
        records = {args.kind: client.path(args.node, args.kind)}
    else:
        records = client.paths(args.node)
    if args.json:
        print(json.dumps(records, indent=2, ensure_ascii=False))
        return 0
    for kind, record in records.items():
        if record is None:
            print(f"{args.node}: {kind}: 未注册")
        else:
            print(
                f"{args.node}: {kind}: {record.get('path')} "
                f"({record.get('registered_at')})"
            )
    return 0


def _allocation_file(plan_path: Path) -> Path:
    return plan_path.parent / "allocations.json"


def _read_allocation_file(path: Path) -> List[Any]:
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    records: List[Any] = []
    for item in payload if isinstance(payload, list) else []:
        if isinstance(item, Mapping) and item.get("alias") is not None:
            try:
                records.append((str(item["alias"]), int(item.get("gpu_index"))))
            except (TypeError, ValueError):
                continue
    return records


def _write_allocation_file(path: Path, records: Sequence[Any]) -> None:
    payload = [
        {"alias": str(alias), "gpu_index": int(index)}
        for alias, index in sorted({(str(alias), int(index)) for alias, index in records})
    ]
    atomic_write_text(
        path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    )


def _cmd_allocate(args: argparse.Namespace, *, state_dir: Path) -> int:
    plan_path = Path(args.plan).expanduser().resolve()
    plan = load_plan(plan_path)
    client = _client(args, state_dir=state_dir)
    created = _reserve_plan(
        client,
        plan,
        target=str(args.target or getpass.getuser()),
        ttl=str(args.ttl),
        task_id=args.task,
    )
    path = _allocation_file(plan_path)
    existing = _read_allocation_file(path)
    _write_allocation_file(path, list(existing) + list(created))
    if args.json:
        print(json.dumps([{"alias": a, "gpu_index": i} for a, i in created], indent=2, ensure_ascii=False))
    else:
        for alias, index in created:
            print(f"reserved {alias}:{index}")
        print(f"allocation record: {path}")
    return 0


def _cmd_release(args: argparse.Namespace, *, state_dir: Path) -> int:
    client = _client(args, state_dir=state_dir)
    records: List[Any] = []
    path = None
    if args.plan:
        path = _allocation_file(Path(args.plan).expanduser().resolve())
        records = _read_allocation_file(path)
    elif args.node:
        for index in args.gpu or []:
            records.append((str(args.node), int(index)))
    else:
        raise ValueError("release requires --plan or --node with --gpu")
    _release_allocations(client, records)
    if path is not None and records:
        remaining = [
            item for item in _read_allocation_file(path) if item not in set(records)
        ]
        _write_allocation_file(path, remaining)
    if not args.json:
        for alias, index in records:
            print(f"released {alias}:{index}")
    return 0


def _cmd_plans(args: argparse.Namespace, *, state_dir: Path) -> int:
    client = _client(args, state_dir=state_dir)
    action = str(args.plans_action)
    if action == "list":
        payload = client.plans(
            status=args.status,
            owner=args.owner,
            manifest_key=args.manifest_key,
        )
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 0
        records = payload.get("plans", []) or []
        for item in records:
            if not isinstance(item, Mapping):
                continue
            print(
                f"{item.get('plan_id')} [{item.get('status')}] {item.get('name')} "
                f"owner={item.get('owner')} claimed_by={item.get('claimed_by') or '-'}"
            )
            for device in item.get("devices", []) or []:
                if not isinstance(device, Mapping):
                    continue
                gpu_name = device.get("gpu_name") or ""
                task_ids = [
                    str(item) for item in device.get("task_ids") or [] if item
                ] or [str(device.get("task_id") or "")]
                print(
                    f"  {', '.join(task_ids)}: {device.get('alias')}:GPU"
                    f"{device.get('gpu_index')} {gpu_name}".rstrip()
                )
        if not records:
            print("no plans")
        return 0

    plan_id = str(args.id or "")
    if not plan_id:
        raise ValueError(f"plans {action} requires --id")
    if action == "show":
        payload = client.plan(plan_id)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    if action == "claim":
        payload = client.claim_plan(
            plan_id,
            target=str(args.target or getpass.getuser()),
            ttl=str(args.ttl),
        )
    elif action == "release":
        payload = client.release_plan(plan_id)
    elif action == "pause":
        payload = client.pause_plan(plan_id)
    elif action == "resume":
        payload = client.resume_plan(plan_id)
    else:
        payload = client.delete_plan(plan_id)
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"{action}: {plan_id} -> {payload.get('status') or payload.get('ok')}")
        if payload.get("expires_at"):
            print(f"expires: {payload.get('expires_at')}")
    return 0


def _cmd_allocations(args: argparse.Namespace, *, state_dir: Path) -> int:
    client = _client(args, state_dir=state_dir)
    payload = client.allocations()
    records = payload.get("allocations", [])
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    for item in records:
        print(
            f"{item.get('alias')}:{item.get('gpu_index')} -> {item.get('target')} "
            f"expires {item.get('expires_at')}"
        )
    if not records:
        print("no allocations")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="adaptive-runner",
        description="Adaptive plan, dispatch, and environment repair",
    )
    parser.add_argument("--version", action="version", version=f"adaptive-runner {__version__}")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--server", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    inventory = sub.add_parser("inventory", help="pull or show an inventory snapshot")
    inventory.add_argument("inventory_action", nargs="?", choices=["pull", "show"], default="pull")
    inventory.add_argument("--snapshot", default=None)
    inventory.add_argument("--json", action="store_true")
    inventory.set_defaults(handler=_cmd_inventory)

    plan = sub.add_parser(
        "plan", help="build a review-only plan from a snapshot (never executed)"
    )
    plan.add_argument("manifest")
    plan.add_argument("--snapshot", default=None)
    plan.add_argument("--output", default=None)
    plan.add_argument("--user", default=None)
    plan.add_argument("--max-snapshot-age", default=600, type=int)
    plan.add_argument("--ignore-allocations", action="store_true")
    plan.add_argument("--allow-unallocated", action="store_true")
    plan.add_argument(
        "--gpu-state",
        choices=["service", "snapshot"],
        default="service",
        help="read GPU idle state from the service, or fall back to snapshot data",
    )
    plan.add_argument("--inspect", action="store_true")
    plan.add_argument("--json", action="store_true")
    plan.set_defaults(handler=_cmd_plan)

    run = sub.add_parser("run", aliases=["dispatch"], help="execute a plan claimed by submit")
    run.add_argument("plan")
    run.add_argument("--artifact-cache", default=None)
    run.add_argument("--output", default=None)
    run.add_argument("--task", default=None)
    run.add_argument("--mode", choices=["direct", "tmux"], default="tmux")
    run.add_argument("--timeout", default=300, type=int)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--skip-preflight", action="store_true")
    run.add_argument("--allow-snapshot-change", action="store_true")
    run.add_argument(
        "--reserve",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="reserve plan GPUs before dispatch and release them afterward",
    )
    run.add_argument("--reserve-target", default=None)
    run.add_argument("--allocation-ttl", default="12h")
    run.add_argument("--json", action="store_true")
    run.set_defaults(handler=_cmd_run)

    submit = sub.add_parser(
        "submit",
        help="claim a registered or cached plan and execute it",
    )
    submit.add_argument("manifest")
    submit.add_argument("--snapshot", default=None)
    submit.add_argument("--max-snapshot-age", default=600, type=int)
    submit.add_argument("--ignore-allocations", action="store_true")
    submit.add_argument(
        "--strict-allocations",
        action="store_true",
        help="exclude unallocated GPUs instead of planning them",
    )
    submit.add_argument(
        "--gpu-state",
        choices=["service", "snapshot"],
        default="service",
        help="read GPU idle state from the service, or fall back to snapshot data",
    )
    submit.add_argument("--user", default=None)
    submit.add_argument(
        "--plan-id",
        default=None,
        help="use a specific registered plan instead of choosing one",
    )
    submit.add_argument("--refresh-plan", action="store_true")
    submit.add_argument("--artifact-cache", default=None)
    submit.add_argument("--output", default=None)
    submit.add_argument("--task", default=None)
    submit.add_argument("--mode", choices=["direct", "tmux"], default="tmux")
    submit.add_argument("--timeout", default=300, type=int)
    submit.add_argument("--dry-run", action="store_true")
    submit.add_argument("--skip-preflight", action="store_true")
    submit.add_argument("--no-reserve", action="store_true")
    submit.add_argument("--reserve-target", default=None)
    submit.add_argument("--allocation-ttl", default="12h")
    submit.add_argument("--json", action="store_true")
    submit.set_defaults(handler=_cmd_submit)

    plans = sub.add_parser("plans", help="list and manage registered plans")
    plans.add_argument(
        "plans_action",
        choices=["list", "show", "claim", "release", "pause", "resume", "delete"],
    )
    plans.add_argument("--id", default=None)
    plans.add_argument("--status", default=None)
    plans.add_argument("--owner", default=None)
    plans.add_argument("--manifest-key", default=None)
    plans.add_argument("--target", default=None)
    plans.add_argument("--ttl", default="12h")
    plans.add_argument("--json", action="store_true")
    plans.set_defaults(handler=_cmd_plans)

    repair = sub.add_parser("repair", help="run a reviewed repair command and report the environment")
    repair.add_argument("--node", required=True)
    repair.add_argument("--command", required=True)
    repair.add_argument("--check", default="")
    repair.add_argument("--snapshot", default=None)
    repair.add_argument("--max-snapshot-age", default=600, type=int)
    repair.add_argument("--timeout", default=300, type=int)
    repair.add_argument("--manager", default="uv", choices=["uv"])
    repair.add_argument("--path", default="")
    repair.add_argument("--version", default="")
    repair.add_argument("--project-path", default="")
    repair.add_argument("--python-version", default="")
    repair.add_argument("--uv-version", default="")
    repair.add_argument("--lockfile", default="")
    repair.add_argument("--lock-sha256", default="")
    repair.add_argument("--cache-hit", action=argparse.BooleanOptionalAction, default=None)
    repair.add_argument("--reporter", default=getpass.getuser())
    repair.add_argument("--report", action="store_true")
    repair.add_argument("--json", action="store_true")
    repair.set_defaults(handler=_cmd_repair)

    for kind, description in (
        ("work", "register the fixed work directory used by the skill on a node"),
        ("data", "register the dataset directory shown on the board"),
        ("model", "register the model directory shown on the board"),
    ):
        path_parser = sub.add_parser(f"{kind}-path", help=description)
        path_parser.add_argument("--node", required=True)
        path_parser.add_argument(
            "--path",
            default=None,
            help="省略时读取本地配置 local.json 的 nodes.<alias>.<kind>",
        )
        path_parser.add_argument("--reporter", default=getpass.getuser())
        path_parser.add_argument("--json", action="store_true")
        path_parser.set_defaults(handler=_cmd_register_path, kind=kind)

    inspect = sub.add_parser("inspect", help="show registered paths and node directories")
    inspect.add_argument("--node", action="append", default=None)
    inspect.add_argument("--kind", choices=["data", "model"], default=None)
    inspect.add_argument("--uv-cache", action="store_true")
    inspect.add_argument("--json", action="store_true")
    inspect.set_defaults(handler=_cmd_inspect)

    gpu = sub.add_parser("gpu", help="show live nvitop device state")
    gpu.add_argument("--node", action="append", default=None)
    gpu.add_argument("--retry", action="store_true")
    gpu.add_argument("--json", action="store_true")
    gpu.set_defaults(handler=_cmd_gpu)

    environment = sub.add_parser("environment", help="show environment records and conflicts")
    environment.add_argument("--node", action="append", required=True)
    environment.add_argument("--json", action="store_true")
    environment.set_defaults(handler=_cmd_environment)

    path = sub.add_parser("path", help="show registered node paths")
    path.add_argument("--node", required=True)
    path.add_argument("--kind", choices=["work", "data", "model"], default=None)
    path.add_argument("--json", action="store_true")
    path.set_defaults(handler=_cmd_path)

    allocate = sub.add_parser("allocate", help="reserve the GPUs selected by a plan")
    allocate.add_argument("--plan", required=True)
    allocate.add_argument("--target", default=None)
    allocate.add_argument("--ttl", default="12h")
    allocate.add_argument("--task", default=None)
    allocate.add_argument("--json", action="store_true")
    allocate.set_defaults(handler=_cmd_allocate)

    release = sub.add_parser("release", help="release reserved GPUs")
    release.add_argument("--plan", default=None)
    release.add_argument("--node", default=None)
    release.add_argument("--gpu", action="append", default=None, type=int)
    release.add_argument("--json", action="store_true")
    release.set_defaults(handler=_cmd_release)

    allocations = sub.add_parser("allocations", help="show the service allocation registry")
    allocations.add_argument("--json", action="store_true")
    allocations.set_defaults(handler=_cmd_allocations)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    state_dir = get_state_dir(args.state_dir)
    try:
        return int(args.handler(args, state_dir=state_dir))
    except (OSError, ValueError, json.JSONDecodeError, DispatchError, SnapshotError, ServiceError) as exc:
        print(f"Execution failed: {exc}", file=sys.stderr)
        return 2
