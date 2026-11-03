from __future__ import annotations

import subprocess

import pytest

from adaptive_service.tailscale import (
    Tailscale,
    TailscaleError,
    disable_command,
    parse_status,
    serve_command,
)


def status_payload(state: str = "Running") -> dict:
    return {
        "BackendState": state,
        "Self": {"DNSName": "gpu-host.tailnet.ts.net."},
        "CurrentTailnet": {"Name": "tailnet.ts.net"},
    }


def test_parse_status_strips_trailing_dot():
    status = parse_status(status_payload())

    assert status.dns_name == "gpu-host.tailnet.ts.net"
    assert status.backend_state == "Running"
    assert status.tailnet == "tailnet.ts.net"
    assert status.https_url == "https://gpu-host.tailnet.ts.net/"


def test_commands_use_https_only():
    assert serve_command(443, "http://127.0.0.1:8765") == [
        "serve",
        "--bg",
        "--https=443",
        "http://127.0.0.1:8765",
    ]
    assert disable_command(443) == [
        "serve",
        "--https=443",
        "off",
    ]


def test_expose_configures_missing_proxy(monkeypatch):
    client = Tailscale()

    def fake_json(args):
        if args[0] == "status":
            return status_payload()
        return {"Web": {}}

    calls = []

    def fake_run(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(client, "_json", fake_json)
    monkeypatch.setattr(client, "_run", fake_run)

    status = client.expose(443, "http://127.0.0.1:8765")

    assert status.https_url == "https://gpu-host.tailnet.ts.net/"
    assert calls == [serve_command(443, "http://127.0.0.1:8765")]


def test_expose_rejects_conflicting_proxy(monkeypatch):
    client = Tailscale()

    def fake_json(args):
        if args[0] == "status":
            return status_payload()
        return {
            "Web": {
                "gpu-host.tailnet.ts.net:443": {
                    "Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}}
                }
            }
        }

    monkeypatch.setattr(client, "_json", fake_json)

    with pytest.raises(TailscaleError):
        client.expose(443, "http://127.0.0.1:8765")


def test_cli_exposes_and_disables_tailscale(monkeypatch, tmp_path):
    from adaptive_service import cli
    from adaptive_service.tailscale import TailscaleStatus

    events = []

    class FakeTailscale:
        def __init__(self, binary="tailscale"):
            self.binary = binary

        def expose(self, https_port, local_url):
            events.append(("expose", https_port, local_url))
            return TailscaleStatus("board.tailnet.ts.net", "Running", "tailnet.ts.net")

        def disable(self, https_port):
            events.append(("disable", https_port))

    import uvicorn

    def fake_run(app, **kwargs):
        events.append(("run", kwargs.get("host"), kwargs.get("port")))
        callback = getattr(app.state, "on_shutdown", None)
        if callback is not None:
            callback()

    monkeypatch.setattr(cli, "Tailscale", FakeTailscale)
    monkeypatch.setattr(uvicorn, "run", fake_run)

    code = cli.main(
        [
            "--state-dir",
            str(tmp_path),
            "--ssh-config",
            str(tmp_path / "missing-ssh-config"),
            "--port",
            "8881",
            "--tailscale-port",
            "8443",
            "--interval",
            "0",
        ]
    )

    assert code == 0
    assert events == [
        ("expose", 8443, "http://127.0.0.1:8881"),
        ("run", "127.0.0.1", 8881),
        ("disable", 8443),
    ]


def test_cli_rejects_non_loopback_listener_with_tailscale(monkeypatch, tmp_path, capsys):
    from adaptive_service import cli

    code = cli.main(
        [
            "--state-dir",
            str(tmp_path),
            "--host",
            "0.0.0.0",
            "--tailscale-port",
            "8443",
        ]
    )

    assert code == 2
    assert "127.0.0.1" in capsys.readouterr().err
