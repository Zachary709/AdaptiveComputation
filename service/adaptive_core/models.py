from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


@dataclass
class NodeConfig:
    alias: str
    address: str
    user: str
    port: int = 22
    connect_timeout_s: int = 6
    env_manager: str = ""       # "uv" | "conda" | "micromamba" | "venv" | ""
    env_name: str = ""          # environment/project path or name
    work_dir: str = ""          # remote working directory (e.g. "~/projects")
    # Keep new SSH options after existing optional fields for positional-call compatibility.
    identityfile: str = ""
    proxyjump: str = ""
    # Optional paths used by the hardware inventory collector.  These fields
    # are appended to preserve compatibility with existing positional callers.
    storage_paths: List[str] = field(default_factory=list)
    environment_paths: List[str] = field(default_factory=list)
    benchmark_command: str = ""


@dataclass
class ClusterConfig:
    """Optional local configuration used to seed inventory discovery."""

    cluster_name: str
    nodes: List[NodeConfig]
    admins: Dict[str, object] = field(default_factory=dict)
    users: List[str] = field(default_factory=list)
    policy: Dict[str, object] = field(default_factory=dict)


@dataclass
class NodeTarget:
    """Specifies a target node and GPU(s) for remote execution."""

    node_alias: str
    gpu_indices: List[int]
    node_config: Optional[NodeConfig] = None


@dataclass
class RemoteExecutionContext:
    """Context for executing a command on a remote node with GPU assignment."""

    target: NodeTarget
    command: str
    env_vars: Dict[str, str] = field(default_factory=dict)
    execution_mode: str = "direct"  # "direct" or "tmux"
    tmux_session: Optional[str] = None
    timeout_s: int = 300
    # Optional lightweight environment check expanded by the dispatcher before
    # the workload starts.  It is deliberately separate from ``command`` so
    # the normal run command is not rewritten or duplicated.
    environment_check: Optional[str] = None
    disk_path: str = ""
    data_path: str = ""
    model_path: str = ""
    max_gpu_utilization_percent: float = 5.0
    min_gpu_free_memory_mib: int = 0


class PreflightCheckType(Enum):
    """Types of preflight checks for remote execution."""

    TMUX_AVAILABLE = "tmux_available"
    COMMAND_SYNTAX = "command_syntax"
    GPU_AVAILABILITY = "gpu_availability"


@dataclass
class PreflightCheck:
    """Specification for a preflight check to perform before remote execution.

    Preflight checks validate remote node readiness for command execution,
    including tmux availability, command syntax validation, and GPU state.
    """

    check_type: PreflightCheckType
    node_alias: str
    target_gpu_indices: Optional[List[int]] = None
    command_to_validate: Optional[str] = None
    node_config: Optional[NodeConfig] = None  # Required for actual SSH operations
    max_gpu_utilization_percent: float = 5.0
    min_gpu_free_memory_mib: int = 0

    def __post_init__(self):
        """Validate check configuration."""
        if self.check_type == PreflightCheckType.GPU_AVAILABILITY:
            if not self.target_gpu_indices:
                raise ValueError("GPU_AVAILABILITY check requires target_gpu_indices")
        elif self.check_type == PreflightCheckType.COMMAND_SYNTAX:
            if not self.command_to_validate:
                raise ValueError("COMMAND_SYNTAX check requires command_to_validate")


@dataclass
class PreflightResult:
    """Result of a single preflight check.

    Contains check status, error details if failed, and optional metadata
    (e.g., tmux version, GPU state snapshot, command validation output).
    """

    check: PreflightCheck
    passed: bool
    error_message: Optional[str] = None
    metadata: Dict[str, object] = field(default_factory=dict)
    timestamp: Optional[str] = None


def node_config_from_inventory_node(raw: Dict[str, Any]) -> NodeConfig:
    ssh = raw.get("ssh") if isinstance(raw.get("ssh"), dict) else {}
    configured = raw.get("configured") if isinstance(raw.get("configured"), dict) else {}

    def configured_paths(key: str) -> List[str]:
        value = configured.get(key, [])
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, (list, tuple, set)):
            values = value
        elif value:
            values = [value]
        else:
            values = []
        return [str(item) for item in values]

    return NodeConfig(
        alias=str(raw.get("alias") or "node"),
        address=str(ssh.get("address") or raw.get("address") or ""),
        user=str(ssh.get("user") or os.environ.get("USER") or "root"),
        port=int(ssh.get("port") or 22),
        connect_timeout_s=int(ssh.get("connect_timeout_s") or 6),
        identityfile=str(ssh.get("identity_file") or ""),
        proxyjump=str(ssh.get("proxy_jump") or ""),
        env_manager=str(configured.get("env_manager") or ""),
        env_name=str(configured.get("env_name") or ""),
        work_dir=str(configured.get("work_dir") or ""),
        storage_paths=configured_paths("storage_paths"),
        environment_paths=configured_paths("environment_paths"),
    )
