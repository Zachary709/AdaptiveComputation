from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional


class TailscaleError(RuntimeError):
    pass


@dataclass(frozen=True)
class TailscaleStatus:
    dns_name: str
    backend_state: str
    tailnet: str

    @property
    def https_url(self) -> str:
        return f"https://{self.dns_name}/"


def parse_status(payload: Mapping[str, Any]) -> TailscaleStatus:
    self_info = payload.get("Self") if isinstance(payload.get("Self"), Mapping) else {}
    dns_name = str(self_info.get("DNSName") or "").strip().rstrip(".")
    if not dns_name:
        raise TailscaleError("tailscale status does not contain Self.DNSName")
    current_tailnet = payload.get("CurrentTailnet")
    tailnet = ""
    if isinstance(current_tailnet, Mapping):
        tailnet = str(current_tailnet.get("Name") or "")
    return TailscaleStatus(
        dns_name=dns_name,
        backend_state=str(payload.get("BackendState") or ""),
        tailnet=tailnet,
    )


def parse_serve_status(payload: Mapping[str, Any]) -> Dict[int, Dict[str, Any]]:
    result: Dict[int, Dict[str, Any]] = {}
    web = payload.get("Web")
    if not isinstance(web, Mapping):
        return result
    for key, value in web.items():
        if not isinstance(value, Mapping):
            continue
        port_text = str(key).rsplit(":", 1)[-1]
        try:
            port = int(port_text)
        except ValueError:
            continue
        handlers = value.get("Handlers")
        proxy = ""
        if isinstance(handlers, Mapping):
            root = handlers.get("/")
            if isinstance(root, Mapping):
                proxy = str(root.get("Proxy") or "")
        result[port] = {"proxy": proxy, "https": True}
    return result


def serve_command(https_port: int, local_url: str) -> List[str]:
    return ["serve", "--bg", f"--https={https_port}", local_url]


def disable_command(https_port: int) -> List[str]:
    return ["serve", f"--https={https_port}", "off"]


class Tailscale:
    def __init__(self, binary: str = "tailscale", timeout_s: int = 15):
        self.binary = binary
        self.timeout_s = timeout_s

    def _run(self, args: List[str]) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                [self.binary, *args],
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise TailscaleError(f"cannot run {self.binary}: {exc}") from exc

    def _json(self, args: List[str]) -> Mapping[str, Any]:
        result = self._run(args)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise TailscaleError(f"tailscale {' '.join(args)} failed: {detail}")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise TailscaleError(f"tailscale {' '.join(args)} returned invalid JSON: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise TailscaleError(f"tailscale {' '.join(args)} returned an unexpected document")
        return payload

    def status(self) -> TailscaleStatus:
        return parse_status(self._json(["status", "--json"]))

    def ensure_running(self) -> TailscaleStatus:
        status = self.status()
        if status.backend_state != "Running":
            raise TailscaleError(
                f"tailscale backend is {status.backend_state or 'unknown'}; run 'tailscale up' first"
            )
        return status

    def serve_status(self) -> Dict[int, Dict[str, Any]]:
        return parse_serve_status(self._json(["serve", "status", "--json"]))

    def expose(self, https_port: int, local_url: str) -> TailscaleStatus:
        status = self.ensure_running()
        existing = self.serve_status()
        current = existing.get(https_port)
        if current is not None and current.get("proxy") and current.get("proxy") != local_url:
            raise TailscaleError(
                f"tailscale serve already maps https:{https_port} to {current.get('proxy')}; "
                f"disable it with 'tailscale serve --https={https_port} off' first"
            )
        if current is None or not current.get("proxy"):
            result = self._run(serve_command(https_port, local_url))
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                raise TailscaleError(_access_hint(detail))
        return status

    def disable(self, https_port: int) -> None:
        result = self._run(disable_command(https_port))
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise TailscaleError(_access_hint(detail))


def _access_hint(detail: str) -> str:
    text = detail or "tailscale serve failed"
    if "Access denied" in text or "serve config denied" in text:
        return (
            f"{text}\n"
            "The current user cannot change the serve configuration. Run "
            "'sudo tailscale set --operator=$USER' once, or start the service with sudo."
        )
    return text


def loopback_host(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}
