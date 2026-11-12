from __future__ import annotations

import asyncio
import ipaddress
import re
import shlex
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import psutil

from .models import NodeConfig
from .processes import ProcessCleanupError, RUNNER_SOURCE, run_managed


def _local_addresses() -> Set[str]:
    """Return the set of addresses that resolve to the current machine."""
    addrs: Set[str] = {"localhost", "127.0.0.1", "::1"}

    addrs.add(socket.gethostname().lower())
    addrs.add(socket.getfqdn().lower())
    for links in psutil.net_if_addrs().values():
        for link in links:
            if link.family in (socket.AF_INET, socket.AF_INET6):
                addrs.add(link.address.split("%")[0].lower())

    return addrs


def is_local_node(node: "NodeConfig") -> bool:
    """Return True if *node* refers to the current machine.

    Checks against localhost aliases, the machine's hostname/FQDN, and all
    network interface IPs so that SSH can be skipped in favour of direct
    subprocess execution.
    """
    address = node.address.split("%")[0].lower()
    addresses = _local_addresses()
    if address in addresses:
        return True
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        try:
            return any(str(item[4][0]).split("%")[0].lower() in addresses for item in socket.getaddrinfo(address, None))
        except socket.gaierror:
            return False


_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_RETRYABLE_SSH_ERROR_PATTERNS = (
    "timeout",
    "connection refused",
    "connection timed out",
    "no route to host",
    "network is unreachable",
    "connection reset by peer",
    "connection closed by remote host",
    "kex_exchange_identification",
)


def _validate_env_vars(env_vars: Dict[str, str]) -> None:
    for k in env_vars.keys():
        if not _ENV_KEY_RE.match(str(k)):
            raise ValueError(f"Invalid env var key: {k!r}")


def _env_export_prefix(env_vars: Dict[str, str]) -> str:
    """Return a shell-safe prefix exporting variables for the whole command.

    A leading ``KEY=value`` only applies to the first simple command.  Remote
    workload commands commonly contain ``&&``/``;`` or ``set -e`` and would
    otherwise lose CUDA_VISIBLE_DEVICES after that first command.  Exporting
    in the wrapper shell preserves the variables for the complete command.
    """

    if not env_vars:
        return ""
    _validate_env_vars(env_vars)
    return " ".join(
        f"export {key}={shlex.quote(str(value))};"
        for key, value in env_vars.items()
    ) + " "


class SSHRunError(RuntimeError):
    pass


class SSHRetryExhausted(SSHRunError):
    """Raised when all SSH retry attempts have been exhausted."""

    pass


def _resolve_identityfile(identityfile: str) -> str:
    raw = str(identityfile or "").strip()
    if not raw:
        return ""
    return str(Path(raw).expanduser())


def _ssh_base_cmd(node: NodeConfig) -> List[str]:
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={int(node.connect_timeout_s)}",
    ]
    if node.port and int(node.port) != 22:
        cmd += ["-p", str(int(node.port))]
    identityfile = _resolve_identityfile(node.identityfile)
    if identityfile:
        cmd += ["-i", identityfile]
    proxyjump = str(node.proxyjump or "").strip()
    if proxyjump:
        cmd += ["-o", f"ProxyJump={proxyjump}"]
    return cmd


async def ssh_run(
    node: NodeConfig,
    remote_args: List[str],
    *,
    stdin_bytes: Optional[bytes] = None,
    timeout_s: int = 15,
    managed: bool = False,
) -> Tuple[int, str, str]:
    """Run a remote command over SSH.

    Returns (rc, stdout, stderr).
    """
    target = f"{node.user}@{node.address}"
    # OpenSSH receives the remote command as a shell string, not as a
    # preserved argv.  Passing each argument separately lets the remote shell
    # strip quoting before it reaches `bash -c`, so separators in a command can
    # execute outside the intended wrapper.  Quote the complete argv once.
    cmd = _ssh_base_cmd(node) + [target, shlex.join(remote_args)]
    if managed:
        transport = _ssh_base_cmd(node) + [f"{node.user}@{node.alias}", shlex.join(["python3", "-u", "-c", RUNNER_SOURCE])]
        try:
            return await run_managed(remote_args, timeout_s=timeout_s, stdin_bytes=stdin_bytes,
                                     target=f"ssh:{node.alias}", transport=transport)
        except asyncio.TimeoutError as exc:
            raise SSHRunError(f"SSH timeout after {timeout_s}s; remote processes cleaned") from exc
        except ProcessCleanupError:
            raise
        except RuntimeError as exc:
            raise SSHRunError(str(exc)) from exc

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(stdin_bytes), timeout=timeout_s
        )
    except BaseException as exc:
        if proc.returncode is None:
            proc.kill()
        await proc.communicate()
        await proc.wait()
        if isinstance(exc, asyncio.TimeoutError):
            raise SSHRunError(f"SSH timeout after {timeout_s}s") from exc
        raise

    rc = int(proc.returncode or 0)
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    return rc, stdout, stderr


async def ssh_run_with_retry(
    node: NodeConfig,
    remote_args: List[str],
    *,
    stdin_bytes: Optional[bytes] = None,
    timeout_s: int = 15,
    max_retries: int = 3,
    managed: bool = False,
) -> Tuple[int, str, str]:
    """Run SSH command with exponential backoff retry on connection failures.

    Retries on: connection timeouts, connection refused, network unreachable.
    Does NOT retry on: authentication failures, command errors (rc != 0).
    """
    attempt = 0
    last_error: Optional[Exception] = None

    while attempt < max_retries:
        attempt += 1
        try:
            return await ssh_run(
                node,
                remote_args,
                stdin_bytes=stdin_bytes,
                timeout_s=timeout_s,
                managed=managed,
            )
        except SSHRunError as e:
            last_error = e
            err_msg = str(e).lower()

            is_retryable = any(
                keyword in err_msg for keyword in _RETRYABLE_SSH_ERROR_PATTERNS
            )

            if not is_retryable:
                raise

            if attempt >= max_retries:
                raise SSHRetryExhausted(
                    f"SSH failed after {max_retries} attempts: {e}"
                ) from e

            delay_s = 2 ** (attempt - 1)
            print(
                f"[ssh-retry] {node.alias} attempt {attempt}/{max_retries} failed: {e}. "
                f"Retrying in {delay_s}s...",
                file=sys.stderr,
            )
            await asyncio.sleep(delay_s)

    if last_error:
        raise SSHRetryExhausted(
            f"SSH failed after {max_retries} attempts: {last_error}"
        ) from last_error

    raise SSHRetryExhausted(f"SSH failed after {max_retries} attempts")


async def ssh_bash_script(
    node: NodeConfig,
    script: str,
    *,
    timeout_s: int = 15,
    max_retries: int = 1,
    managed: bool = False,
) -> Tuple[int, str, str]:
    return await ssh_run_with_retry(
        node,
        ["bash", "-s"],
        stdin_bytes=script.encode("utf-8"),
        timeout_s=timeout_s,
        max_retries=max_retries,
        managed=managed,
    )


@dataclass
class RemoteExecResult:
    """Result of a remote command execution."""

    exit_code: int
    stdout: str
    stderr: str
    node_alias: str
    command: str
    success: bool = field(init=False)

    def __post_init__(self):
        self.success = self.exit_code == 0


async def ssh_exec_remote(
    node: NodeConfig,
    command: str,
    *,
    env_vars: Optional[Dict[str, str]] = None,
    timeout_s: int = 300,
    max_retries: int = 1,
) -> RemoteExecResult:
    """Execute a remote command with environment variable injection and result capture.

    Args:
        node: Target node configuration
        command: Shell command to execute
        env_vars: Optional environment variables to inject (e.g., {"CUDA_VISIBLE_DEVICES": "0,1"})
        timeout_s: Execution timeout in seconds (default: 300s for long-running commands)
        max_retries: Number of retry attempts on connection failures (default: 1)

    Returns:
        RemoteExecResult with exit_code, stdout, stderr, and success status

    Raises:
        SSHRunError: On SSH connection/execution failures
        SSHRetryExhausted: When all retry attempts are exhausted
    """
    # Build the command with environment variable injection
    if env_vars:
        _validate_env_vars(env_vars)

    # Export values in the wrapper shell so they survive command separators
    # and apply to the entire workload, not only its first simple command.
    env_prefix = _env_export_prefix(env_vars or {})
    full_command = f"{env_prefix}{command}"

    # Execute via bash to ensure proper shell expansion and environment handling
    rc, stdout, stderr = await ssh_run_with_retry(
        node,
        ["bash", "-c", full_command],
        timeout_s=timeout_s,
        max_retries=max_retries,
    )

    return RemoteExecResult(
        exit_code=rc,
        stdout=stdout,
        stderr=stderr,
        node_alias=node.alias,
        command=command,
    )
