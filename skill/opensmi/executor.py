"""Backend command executor for remote GPU workloads.

This module provides targeted command routing to selected nodes/GPUs
with support for environment variable injection and execution modes.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from adaptive_core.models import (
    NodeConfig,
    NodeTarget,
    PreflightCheck,
    PreflightCheckType,
    PreflightResult,
    RemoteExecutionContext,
)
from adaptive_core.state import atomic_write_text, get_state_dir
from adaptive_core.sshutil import (
    RemoteExecResult,
    _env_export_prefix,
    _ssh_base_cmd,
    is_local_node,
    ssh_exec_remote,
    ssh_run,
)


_TMUX_TMP_ARTIFACT_RE = re.compile(r"^tmux-(?P<session>.+)\.(?:sh|json)$")


def _active_tmux_sessions() -> set[str]:
    tmux_bin = _find_tmux_binary()
    if not tmux_bin:
        return set()
    try:
        out = subprocess.check_output(
            [tmux_bin, "list-sessions", "-F", "#S"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except Exception:
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}


def _cleanup_stale_tmux_artifacts(
    wrapper_dir: str,
    *,
    keep_sessions: set[str],
    max_age_s: int = 24 * 60 * 60,
) -> None:
    if not os.path.isdir(wrapper_dir):
        return

    active_sessions = _active_tmux_sessions()
    now = time.time()

    for entry in os.scandir(wrapper_dir):
        if not entry.is_file():
            continue
        m = _TMUX_TMP_ARTIFACT_RE.match(entry.name)
        if not m:
            continue

        session_name = m.group("session")
        if session_name in keep_sessions or session_name in active_sessions:
            continue

        try:
            if now - entry.stat().st_mtime < max_age_s:
                continue
            os.unlink(entry.path)
        except OSError:
            continue


def _build_env_setup(node: "NodeConfig") -> str:
    """Build shell commands to set up the virtual env and cd to work_dir.

    Instead of activating envs (which requires shell hooks / conda init
    that may not exist in non-login SSH sessions), we prepend the env's
    bin directory to PATH. Conda and micromamba envs store binaries at
    predictable paths:
      - conda:      ~/miniconda3/envs/{name}/bin  (or ~/anaconda3/envs/...)
      - micromamba:  ~/micromamba/envs/{name}/bin  (or ~/.local/share/micromamba/envs/...)

    Returns a string like 'export PATH=...:$PATH && cd ~/proj && ' or ''
    if no env config is set.
    """
    parts: list[str] = []

    # Change directory first so the project-local uv .venv candidate uses the
    # task's work directory rather than the SSH login directory.
    if node.work_dir:
        wd = node.work_dir.strip()
        if wd == "~":
            parts.append('mkdir -p "$HOME" && cd "$HOME"')
        elif wd.startswith("~/"):
            # Keep HOME expansion while quoting the user-controlled suffix.
            quoted_suffix = shlex.quote(wd[2:])
            parts.append(f'mkdir -p "$HOME"/{quoted_suffix} && cd "$HOME"/{quoted_suffix}')
        else:
            quoted_work_dir = shlex.quote(wd)
            parts.append(f"mkdir -p {quoted_work_dir} && cd {quoted_work_dir}")

    if node.env_manager and (node.env_name or node.env_manager.lower().strip() == "uv"):
        mgr = node.env_manager.lower().strip()
        name = node.env_name.strip()

        if mgr in ("conda", "miniconda"):
            # Search common conda/miniconda locations; first existing dir wins.
            if name.startswith("/"):
                candidates = [f"{shlex.quote(name)}/bin"]
            elif name.startswith("~/"):
                candidates = [f'$HOME/{shlex.quote(name[2:])}/bin']
            else:
                candidates = [
                    f"$HOME/miniconda3/envs/{shlex.quote(name)}/bin",
                    f"$HOME/miniconda/envs/{shlex.quote(name)}/bin",
                    f"$HOME/anaconda3/envs/{shlex.quote(name)}/bin",
                    f"$HOME/conda/envs/{shlex.quote(name)}/bin",
                    f"$HOME/miniforge3/envs/{shlex.quote(name)}/bin",
                ]
            search = " ".join(candidates)
            parts.append(
                f'for _d in {search}; do [ -d "$_d" ] && export PATH="$_d:$PATH" && break; done'
            )
        elif mgr == "micromamba":
            if name.startswith("/"):
                candidates = [f"{shlex.quote(name)}/bin"]
            elif name.startswith("~/"):
                candidates = [f'$HOME/{shlex.quote(name[2:])}/bin']
            else:
                candidates = [
                    f"$HOME/micromamba/envs/{shlex.quote(name)}/bin",
                    f"$HOME/.local/share/micromamba/envs/{shlex.quote(name)}/bin",
                ]
            search = " ".join(candidates)
            parts.append(
                f'for _d in {search}; do [ -d "$_d" ] && export PATH="$_d:$PATH" && break; done'
            )
        elif mgr == "venv":
            if name.startswith("~") or name.startswith("/"):
                parts.append(f"source {shlex.quote(name)}/bin/activate")
            else:
                parts.append(
                    f"source ~/{shlex.quote(name)}/bin/activate 2>/dev/null "
                    f"|| source ~/.venvs/{shlex.quote(name)}/bin/activate"
                )
        elif mgr == "uv":
            # uv project environments are ordinary .venv directories.  Prefer
            # an explicit path/name, then the project-local .venv; uv itself
            # remains available for commands such as `uv run`.
            candidates = []
            if name:
                if name.startswith("~") or name.startswith("/"):
                    candidates.append(f"{shlex.quote(name)}/bin")
                else:
                    candidates.extend(
                        [
                            f"$HOME/.venvs/{shlex.quote(name)}/bin",
                            f"$HOME/miniconda3/envs/{shlex.quote(name)}/bin",
                        ]
                    )
            candidates.append("$PWD/.venv/bin")
            search = " ".join(candidates)
            parts.append(
                f'for _d in {search}; do [ -d "$_d" ] && export PATH="$_d:$PATH" && break; done'
            )

    if not parts:
        return ""
    return " && ".join(parts) + " && "


async def validate_gpu_availability(
    target: NodeTarget,
) -> List[int]:
    """Validate GPU index availability on target node.

    Queries the remote node via nvidia-smi to get the list of available
    GPU indices and returns them. This should be called before GPU injection
    to ensure requested GPUs actually exist on the target node.

    Args:
        target: NodeTarget with node_config and gpu_indices to validate

    Returns:
        List of available GPU indices on the target node

    Raises:
        ValueError: If target.node_config is None
        RuntimeError: If nvidia-smi query fails or returns invalid data

    Example:
        >>> target = NodeTarget(
        ...     node_alias="gpu01",
        ...     gpu_indices=[0, 1, 2],
        ...     node_config=node_config,
        ... )
        >>> available = await validate_gpu_availability(target)
        >>> available
        [0, 1, 2, 3, 4, 5, 6, 7]
        >>> invalid = [idx for idx in target.gpu_indices if idx not in available]
        >>> if invalid:
        ...     raise ValueError(f"Invalid GPU indices: {invalid}")
    """
    if not target.node_config:
        raise ValueError(
            f"NodeTarget for {target.node_alias} missing node_config "
            "for GPU availability validation"
        )

    query_cmd = "nvidia-smi --query-gpu=index --format=csv,noheader,nounits"

    if is_local_node(target.node_config):
        # A local GPU node is a supported fast path for the controller;
        # preflight must not require an SSH daemon on the workstation either.
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            query_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError as exc:
            proc.kill()
            raise RuntimeError(f"Failed to query GPU availability on {target.node_alias}: local query timed out") from exc
        result = RemoteExecResult(
            exit_code=int(proc.returncode or 0),
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
            node_alias=target.node_alias,
            command=query_cmd,
        )
    else:
        result = await ssh_exec_remote(
            node=target.node_config,
            command=query_cmd,
            timeout_s=10,
        )

    if not result.success:
        raise RuntimeError(
            f"Failed to query GPU availability on {target.node_alias}: "
            f"{result.stderr or result.stdout}"
        )

    available_indices: List[int] = []
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            gpu_idx = int(line)
            available_indices.append(gpu_idx)
        except ValueError:
            continue

    if not available_indices:
        raise RuntimeError(
            f"No GPUs detected on {target.node_alias}. "
            f"nvidia-smi output: {result.stdout}"
        )

    return available_indices


def validate_gpu_indices_against_available(
    requested_indices: List[int],
    available_indices: List[int],
    node_alias: str,
) -> None:
    """Validate that requested GPU indices are available on the node.

    Args:
        requested_indices: GPU indices requested by user
        available_indices: GPU indices available on the node
        node_alias: Node alias for error messages

    Raises:
        ValueError: If any requested GPU index is not available
    """
    invalid_indices = [idx for idx in requested_indices if idx not in available_indices]
    if invalid_indices:
        raise ValueError(
            f"Invalid GPU indices {invalid_indices} for node {node_alias}. "
            f"Available indices: {available_indices}"
        )


async def route_command_to_target(
    context: RemoteExecutionContext,
) -> RemoteExecResult:
    """Route a command to a target node with GPU assignment.

    This function orchestrates the execution of a command on a remote node,
    handling environment variable injection (e.g., CUDA_VISIBLE_DEVICES)
    and respecting the specified execution mode (direct vs tmux).

    Args:
        context: RemoteExecutionContext specifying target, command, env vars,
                 execution mode, and timeout

    Returns:
        RemoteExecResult with execution status, stdout, stderr

    Raises:
        ValueError: If target node config is missing
        SSHRunError: On SSH connection/execution failures
        SSHRetryExhausted: When all retry attempts are exhausted

    Example:
        >>> from opensmi.models import NodeTarget, RemoteExecutionContext
        >>> target = NodeTarget(
        ...     node_alias="gpu01",
        ...     gpu_indices=[0, 1],
        ...     node_config=node_config,
        ... )
        >>> context = RemoteExecutionContext(
        ...     target=target,
        ...     command="python train.py",
        ...     env_vars={"CUDA_VISIBLE_DEVICES": "0,1"},
        ...     execution_mode="direct",
        ... )
        >>> result = await route_command_to_target(context)
        >>> print(result.success, result.stdout)
    """
    if not context.target.node_config:
        raise ValueError(
            f"NodeTarget for {context.target.node_alias} missing node_config"
        )

    node = context.target.node_config

    # For direct execution mode, simply execute the command with env vars
    if context.execution_mode == "direct":
        env_setup = _build_env_setup(node)
        command = f"{env_setup}{context.command}" if env_setup else context.command

        if is_local_node(node):
            # Local node: bypass SSH, run directly as a subprocess.
            env = dict(os.environ)
            env.update({k: str(v) for k, v in context.env_vars.items()})
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=context.timeout_s,
                )
            except asyncio.TimeoutError:
                proc.kill()
                return RemoteExecResult(
                    exit_code=1,
                    stdout="",
                    stderr=f"local exec timed out after {context.timeout_s}s",
                    node_alias=node.alias,
                    command=context.command,
                )
            return RemoteExecResult(
                exit_code=int(proc.returncode or 0),
                stdout=stdout_b.decode("utf-8", errors="replace"),
                stderr=stderr_b.decode("utf-8", errors="replace"),
                node_alias=node.alias,
                command=context.command,
            )

        return await ssh_exec_remote(
            node=node,
            command=command,
            env_vars=context.env_vars,
            timeout_s=context.timeout_s,
        )

    # For tmux execution mode, create a LOCAL tmux session that SSHes into
    # the remote node and runs the command there.  This lets the operator
    # attach with a simple `tmux attach -t <session>` on the controller
    # instead of having to SSH first and then attach on the remote side.
    #
    # To avoid shell quoting nightmares (tmux → bash → ssh → bash), we
    # write a small wrapper script and have tmux execute that directly.
    elif context.execution_mode == "tmux":
        if not context.tmux_session:
            raise ValueError("tmux_session name required for tmux execution mode")

        # Build the command: env_setup prefix + env vars + user command
        env_setup = _build_env_setup(node)
        if context.env_vars:
            # Export variables in the remote wrapper shell.  A leading
            # KEY=value would apply only to the first simple command when the
            # workload contains `;`/`&&`, which could drop CUDA_VISIBLE_DEVICES.
            env_prefix = _env_export_prefix(context.env_vars)
            remote_command = f"{env_setup}{env_prefix}{context.command}"
        else:
            remote_command = f"{env_setup}{context.command}"

        # Sanitize session name for safe use in file paths, tmux, and remote shell
        safe_session = re.sub(r"[^a-zA-Z0-9_\-]", "-", context.tmux_session)
        wrapper_dir = str(get_state_dir() / "tmp")
        os.makedirs(wrapper_dir, exist_ok=True)
        _cleanup_stale_tmux_artifacts(wrapper_dir, keep_sessions={safe_session})

        # Local node fast-path: skip SSH wrapper entirely, run directly in tmux.
        if is_local_node(node):
            pid_file = f"/tmp/adaptive-runner-{safe_session}.pid"
            wrapper_path = os.path.join(wrapper_dir, f"tmux-{safe_session}.sh")
            wrapper_content = (
                "#!/usr/bin/env bash\n"
                + "trap 'rm -f "
                + f"{shlex.quote(pid_file)} {shlex.quote(wrapper_path)}' EXIT\n"
                + f"echo $$ > {shlex.quote(pid_file)}; {remote_command}\n"
            )
            atomic_write_text(Path(wrapper_path), wrapper_content)
            os.chmod(wrapper_path, 0o755)

            tmux_bin = _find_tmux_binary() or "tmux"
            proc = await asyncio.create_subprocess_exec(
                tmux_bin,
                "new-session",
                "-d",
                "-s",
                safe_session,
                wrapper_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=context.timeout_s
                )
            except asyncio.TimeoutError:
                proc.kill()
                return RemoteExecResult(
                    exit_code=1,
                    stdout="",
                    stderr=f"local tmux creation timed out after {context.timeout_s}s",
                    node_alias=node.alias,
                    command=context.command,
                )
            return RemoteExecResult(
                exit_code=int(proc.returncode or 0),
                stdout=stdout_b.decode("utf-8", errors="replace"),
                stderr=stderr_b.decode("utf-8", errors="replace"),
                node_alias=node.alias,
                command=context.command,
            )

        # Remote path: Wrap remote command with PID tracking:
        # 1. Write bash PID to a known file on the remote node
        # 2. Run the actual command
        # 3. Clean up PID file on exit (normal or error)
        pid_file = f"/tmp/adaptive-runner-{safe_session}.pid"
        wrapped_command = (
            f'echo $$ > {pid_file}; trap "rm -f {pid_file}" EXIT; {remote_command}'
        )

        # Base64 encode the remote command to avoid nested quoting issues
        payload_b64 = base64.b64encode(wrapped_command.encode("utf-8")).decode("ascii")

        # Build the SSH target
        ssh_target = f"{node.user}@{node.address}"

        # Write a wrapper script that tmux will execute. The script closes the
        # session and removes its local artifacts as soon as SSH exits.
        #
        # DESIGN: To avoid ALL quoting issues, user input is NEVER embedded
        # in the shell script. Instead we write a JSON config file and a
        # generic launcher that reads it. The only dynamic shell content is
        # the path to the JSON file.
        wrapper_path = os.path.join(wrapper_dir, f"tmux-{safe_session}.sh")

        # 1) Write job config as JSON (no shell escaping needed)
        import json as _json

        job_config = {
            "node_alias": node.alias,
            "ssh_target": ssh_target,
            "ssh_args": _ssh_base_cmd(node),
            "payload_b64": payload_b64,
            "command_preview": context.command[:120],
            "remote_pid_file": pid_file,
        }
        config_path = os.path.join(wrapper_dir, f"tmux-{safe_session}.json")
        atomic_write_text(Path(config_path), _json.dumps(job_config))

        # 2) Launcher: a Python script that reads the JSON config.
        #    Zero shell quoting — Python handles everything natively.
        wrapper_script = f"""#!/usr/bin/env python3
import json, os, subprocess, sys, shlex

with open("{config_path}") as f:
    cfg = json.load(f)

print(f"adaptive-runner: connecting to '{{cfg['node_alias']}}' ({{cfg['ssh_target']}})...")
print(f"adaptive-runner: command → {{cfg['command_preview']}}")
print()

ssh_cmd = cfg["ssh_args"] + [
    "-tt", cfg["ssh_target"],
    f"echo {{cfg['payload_b64']}} | base64 --decode | bash",
]
try:
    rc = subprocess.call(ssh_cmd)
finally:
    for _p in ({config_path!r}, {wrapper_path!r}):
        try:
            os.unlink(_p)
        except OSError:
            pass
sys.exit(rc)
"""

        atomic_write_text(Path(wrapper_path), wrapper_script)
        os.chmod(wrapper_path, 0o755)

        # Create local tmux session running the wrapper script
        tmux_bin = _find_tmux_binary() or "tmux"
        tmux_argv = [
            tmux_bin,
            "new-session",
            "-d",
            "-s",
            safe_session,
            wrapper_path,
        ]

        proc = await asyncio.create_subprocess_exec(
            *tmux_argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=context.timeout_s,
            )
        except asyncio.TimeoutError:
            proc.kill()
            return RemoteExecResult(
                exit_code=1,
                stdout="",
                stderr=f"Local tmux creation timed out after {context.timeout_s}s",
                node_alias=node.alias,
                command=context.command,
            )

        rc = int(proc.returncode or 0)
        return RemoteExecResult(
            exit_code=rc,
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
            node_alias=node.alias,
            command=context.command,
        )

    else:
        raise ValueError(f"Unknown execution_mode: {context.execution_mode}")


async def run_preflight_checks(
    checks: List[PreflightCheck],
) -> List[PreflightResult]:
    """Run preflight checks before remote execution.

    Performs validation checks on target nodes to ensure they are ready for
    command execution. This includes checking tmux availability, command syntax
    validation, and GPU availability.

    Args:
        checks: List of PreflightCheck objects specifying which checks to run

    Returns:
        List of PreflightResult objects, one per check, in the same order as input

    Raises:
        ValueError: If checks list is empty or contains invalid check configurations

    Example:
        >>> checks = [
        ...     PreflightCheck(
        ...         check_type=PreflightCheckType.TMUX_AVAILABLE,
        ...         node_alias="gpu01",
        ...     ),
        ...     PreflightCheck(
        ...         check_type=PreflightCheckType.COMMAND_SYNTAX,
        ...         node_alias="gpu01",
        ...         command_to_validate="python train.py",
        ...     ),
        ...     PreflightCheck(
        ...         check_type=PreflightCheckType.GPU_AVAILABILITY,
        ...         node_alias="gpu01",
        ...         target_gpu_indices=[0, 1, 2],
        ...     ),
        ... ]
        >>> results = await run_preflight_checks(checks)
        >>> failed = [r for r in results if not r.passed]
        >>> if failed:
        ...     for result in failed:
        ...         print(f"Check failed: {result.error_message}")
    """
    if not checks:
        raise ValueError("checks list cannot be empty")

    # Execute all checks in parallel
    results = await asyncio.gather(
        *[_run_single_preflight_check(check) for check in checks],
        return_exceptions=False,
    )

    return results


async def _run_single_preflight_check(check: PreflightCheck) -> PreflightResult:
    """Run a single preflight check.

    Internal helper function that dispatches to the appropriate check handler
    based on check_type.

    Args:
        check: PreflightCheck specification

    Returns:
        PreflightResult with check status and metadata
    """
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    try:
        if check.check_type == PreflightCheckType.TMUX_AVAILABLE:
            return await _check_tmux_availability(check, timestamp)
        elif check.check_type == PreflightCheckType.COMMAND_SYNTAX:
            return await _check_command_syntax(check, timestamp)
        elif check.check_type == PreflightCheckType.GPU_AVAILABILITY:
            return await _check_gpu_availability(check, timestamp)
        else:
            return PreflightResult(
                check=check,
                passed=False,
                error_message=f"Unknown check type: {check.check_type}",
                timestamp=timestamp,
            )
    except Exception as e:
        return PreflightResult(
            check=check,
            passed=False,
            error_message=f"Check execution failed: {str(e)}",
            timestamp=timestamp,
        )


def _find_tmux_binary() -> Optional[str]:
    """Find the tmux binary, checking common paths if not in PATH."""
    import shutil

    tmux = shutil.which("tmux")
    if tmux:
        return tmux

    # Homebrew and common Linux paths (PATH may be stripped in subprocess chains)
    for candidate in [
        "/opt/homebrew/bin/tmux",
        "/usr/local/bin/tmux",
        "/usr/bin/tmux",
        "/bin/tmux",
    ]:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    return None


async def _check_tmux_availability(
    check: PreflightCheck, timestamp: str
) -> PreflightResult:
    """Check if tmux is available on the local controller machine.

    Tmux sessions are now created locally (not on the remote node), so we
    only need to verify that tmux is installed on this machine.

    Returns:
        PreflightResult with passed=True if tmux is available, False otherwise.
        On success, metadata['tmux_path'] contains the path to tmux binary.
    """
    try:
        tmux_path = _find_tmux_binary()
        if tmux_path:
            return PreflightResult(
                check=check,
                passed=True,
                metadata={"tmux_path": tmux_path},
                timestamp=timestamp,
            )
        else:
            return PreflightResult(
                check=check,
                passed=False,
                error_message="tmux not found on this machine. Install with: brew install tmux / apt install tmux",
                timestamp=timestamp,
            )

    except Exception as e:
        return PreflightResult(
            check=check,
            passed=False,
            error_message=f"Failed to check tmux availability: {str(e)}",
            timestamp=timestamp,
        )


async def _check_command_syntax(
    check: PreflightCheck, timestamp: str
) -> PreflightResult:
    """Validate command syntax using remote `bash -n`.

    Notes:
      - This is a syntax-only check (it does not execute the command).
      - We still run it on the target node because remote shells can differ.
    """
    if not check.node_config:
        return PreflightResult(
            check=check,
            passed=False,
            error_message=f"Node configuration missing for {check.node_alias}",
            timestamp=timestamp,
        )

    try:
        if is_local_node(check.node_config):
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-n",
                "-c",
                check.command_to_validate or "",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=10)
            except asyncio.TimeoutError as exc:
                proc.kill()
                raise RuntimeError("local command syntax check timed out") from exc
            rc = int(proc.returncode or 0)
            stderr = stderr_b.decode("utf-8", errors="replace")
        else:
            rc, _stdout, stderr = await ssh_run(
                node=check.node_config,
                remote_args=["bash", "-n", "-c", check.command_to_validate or ""],
                timeout_s=10,
            )

        if rc == 0:
            return PreflightResult(
                check=check,
                passed=True,
                metadata={},
                timestamp=timestamp,
            )

        err = (stderr or "").strip()
        return PreflightResult(
            check=check,
            passed=False,
            error_message=err or f"Command syntax check failed (rc={rc})",
            timestamp=timestamp,
        )
    except Exception as e:
        return PreflightResult(
            check=check,
            passed=False,
            error_message=f"Failed to check command syntax: {str(e)}",
            timestamp=timestamp,
        )


async def _run_gpu_query(
    node: NodeConfig, command: str, timeout_s: int = 10
) -> tuple[int, str, str]:
    if is_local_node(node):
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s
            )
        except asyncio.TimeoutError as exc:
            proc.kill()
            raise RuntimeError(
                f"GPU query timed out on {node.alias}"
            ) from exc
        return (
            int(proc.returncode or 0),
            stdout_b.decode("utf-8", errors="replace"),
            stderr_b.decode("utf-8", errors="replace"),
        )
    result = await ssh_exec_remote(
        node=node, command=command, timeout_s=timeout_s
    )
    return int(result.exit_code), result.stdout, result.stderr


async def _check_gpu_availability(
    check: PreflightCheck, timestamp: str
) -> PreflightResult:
    """Validate that requested GPU indices exist on the target node.

    This currently checks index existence (not 'free/idle').
    """
    if not check.node_config:
        return PreflightResult(
            check=check,
            passed=False,
            error_message=f"Node configuration missing for {check.node_alias}",
            timestamp=timestamp,
        )

    try:
        # Reuse existing helpers for consistency.
        target = NodeTarget(
            node_alias=check.node_alias,
            gpu_indices=list(check.target_gpu_indices or []),
            node_config=check.node_config,
        )
        available = await validate_gpu_availability(target)
        validate_gpu_indices_against_available(
            requested_indices=target.gpu_indices,
            available_indices=available,
            node_alias=check.node_alias,
        )

        return PreflightResult(
            check=check,
            passed=True,
            metadata={"available_indices": available},
            timestamp=timestamp,
        )
    except Exception as e:
        return PreflightResult(
            check=check,
            passed=False,
            error_message=str(e),
            timestamp=timestamp,
        )




