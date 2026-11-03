import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from adaptive_core.models import NodeConfig
from adaptive_core.sshutil import (
    SSHRunError,
    SSHRetryExhausted,
    is_local_node,
    ssh_run_with_retry,
)

NODE = NodeConfig(alias="test-node", address="10.0.0.1", user="test")


def run(max_retries=3):
    return asyncio.run(
        ssh_run_with_retry(
            NODE, ["echo", "test"], timeout_s=10, max_retries=max_retries
        )
    )


def test_success_does_not_retry_and_non_retryable_error_stops():
    with patch("opensmi.sshutil.ssh_run", new_callable=AsyncMock) as mock_ssh:
        mock_ssh.return_value = (0, "success", "")
        assert run() == (0, "success", "")
        assert mock_ssh.call_count == 1

    with patch("opensmi.sshutil.ssh_run", new_callable=AsyncMock) as mock_ssh:
        mock_ssh.side_effect = SSHRunError("permission denied")
        with pytest.raises(SSHRunError, match="permission denied"):
            run()
        assert mock_ssh.call_count == 1


def test_retryable_errors_are_retried():
    errors = [
        "SSH timeout after 10s",
        "connection refused",
        "Connection timed out",
        "No route to host",
        "Network is unreachable",
        "Connection reset by peer",
        "Connection closed by remote host",
        "kex_exchange_identification: Connection closed by remote host",
    ]
    with patch("opensmi.sshutil.asyncio.sleep", new_callable=AsyncMock):
        for message in errors:
            with patch("opensmi.sshutil.ssh_run", new_callable=AsyncMock) as mock_ssh:
                mock_ssh.side_effect = [SSHRunError(message), (0, "success", "")]
                assert run(max_retries=2) == (0, "success", "")
                assert mock_ssh.call_count == 2


def test_exhausted_retries_raise():
    with patch("opensmi.sshutil.asyncio.sleep", new_callable=AsyncMock):
        with patch("opensmi.sshutil.ssh_run", new_callable=AsyncMock) as mock_ssh:
            mock_ssh.side_effect = [
                SSHRunError("SSH timeout after 10s"),
                SSHRunError("connection timed out"),
                SSHRunError("network is unreachable"),
            ]
            with pytest.raises(SSHRetryExhausted, match="after 3 attempts"):
                run()
            assert mock_ssh.call_count == 3


def test_local_node_detection():
    for address in ("localhost", "127.0.0.1"):
        assert is_local_node(NodeConfig(alias="n", address=address, user="u"))
    assert not is_local_node(NodeConfig(alias="n", address="10.0.0.99", user="u"))
