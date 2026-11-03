from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from adaptive_core.models import NodeConfig, NodeTarget, RemoteExecutionContext

from .executor import route_command_to_target


@dataclass
class RepairOutcome:
    node_alias: str
    command: str
    check: str
    command_exit_code: int
    check_exit_code: int
    command_stdout: str
    command_stderr: str
    check_stdout: str
    check_stderr: str

    @property
    def success(self) -> bool:
        return self.command_exit_code == 0 and self.check_exit_code == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_alias": self.node_alias,
            "command": self.command,
            "check": self.check,
            "command_exit_code": self.command_exit_code,
            "check_exit_code": self.check_exit_code,
            "command_stdout": self.command_stdout,
            "command_stderr": self.command_stderr,
            "check_stdout": self.check_stdout,
            "check_stderr": self.check_stderr,
            "success": self.success,
        }


def _context(node: NodeConfig, command: str, timeout_s: int) -> RemoteExecutionContext:
    return RemoteExecutionContext(
        target=NodeTarget(node.alias, [], node),
        command=command,
        execution_mode="direct",
        tmux_session=None,
        timeout_s=timeout_s,
    )


async def run_repair(
    node: NodeConfig,
    *,
    command: str,
    check: str = "",
    timeout_s: int = 300,
) -> RepairOutcome:
    command_result = await route_command_to_target(_context(node, command, timeout_s))
    command_exit = int(command_result.exit_code)
    command_stdout = str(command_result.stdout or "")[-8000:]
    command_stderr = str(command_result.stderr or "")[-8000:]

    check_exit = 0
    check_stdout = ""
    check_stderr = ""
    if check.strip():
        check_result = await route_command_to_target(_context(node, check, timeout_s))
        check_exit = int(check_result.exit_code)
        check_stdout = str(check_result.stdout or "")[-8000:]
        check_stderr = str(check_result.stderr or "")[-8000:]

    return RepairOutcome(
        node_alias=node.alias,
        command=command,
        check=check,
        command_exit_code=command_exit,
        check_exit_code=check_exit,
        command_stdout=command_stdout,
        command_stderr=command_stderr,
        check_stdout=check_stdout,
        check_stderr=check_stderr,
    )


def repair_local_record(outcome: RepairOutcome, reporter: str) -> Dict[str, Any]:
    return {
        "alias": outcome.node_alias,
        "command": outcome.command,
        "check": outcome.check,
        "success": outcome.success,
        "command_exit_code": outcome.command_exit_code,
        "check_exit_code": outcome.check_exit_code,
        "reporter": reporter,
    }
