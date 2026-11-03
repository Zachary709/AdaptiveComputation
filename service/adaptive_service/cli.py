from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from .collector.service import CollectorSettings
from .local import ServiceLocalError, load_default_plans, local_path
from .tailscale import Tailscale, TailscaleError, loopback_host


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="adaptive-service",
        description="resource board and inventory service",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--ssh-config", default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument("--storage-path", action="append", default=None)
    parser.add_argument("--environment-path", action="append", default=None)
    parser.add_argument("--interval", default=None, type=int)
    parser.add_argument("--timeout", default=None, type=int)
    parser.add_argument("--benchmark", action="store_true")
    parser.add_argument("--reload", action="store_true")
    parser.add_argument(
        "--tailscale",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="publish the board over tailscale serve with HTTPS",
    )
    parser.add_argument("--tailscale-port", default=443, type=int)
    parser.add_argument("--tailscale-binary", default="tailscale")
    return parser


def _settings(args: argparse.Namespace) -> CollectorSettings:
    settings = CollectorSettings()
    if args.state_dir:
        settings.state_dir = Path(args.state_dir).expanduser().resolve()
    if args.ssh_config:
        settings.ssh_config = Path(args.ssh_config).expanduser().resolve()
    if args.config:
        settings.config_path = Path(args.config).expanduser().resolve()
    if args.storage_path:
        settings.storage_paths = list(args.storage_path)
    if args.environment_path:
        settings.environment_paths = list(args.environment_path)
    if args.interval is not None:
        settings.interval_s = int(args.interval)
    if args.timeout is not None:
        settings.timeout_s = int(args.timeout)
    if args.benchmark:
        settings.benchmark = True
    return settings


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        import uvicorn
    except ImportError:
        print("uvicorn is required to run the resource board", file=sys.stderr)
        return 2
    settings = _settings(args)

    try:
        local_plans = load_default_plans()
    except ServiceLocalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if local_plans is None:
        print(
            f"local plans: none at {local_path()}; using placeholder examples",
            file=sys.stderr,
        )
    else:
        print(f"local plans: {len(local_plans)} from {local_path()}")

    if args.reload and args.tailscale:
        print(
            "error: --reload cannot be combined with tailscale serve; pass --no-tailscale",
            file=sys.stderr,
        )
        return 2

    client: Optional[Tailscale] = None
    if args.tailscale:
        if not loopback_host(args.host):
            print(
                "error: tailscale serve requires the local listener on 127.0.0.1",
                file=sys.stderr,
            )
            return 2
        client = Tailscale(binary=args.tailscale_binary)
        local_url = f"http://127.0.0.1:{args.port}"
        try:
            status = client.expose(args.tailscale_port, local_url)
        except TailscaleError as exc:
            print(f"tailscale setup failed: {exc}", file=sys.stderr)
            return 2
        print(f"board: {status.https_url}")
        print(f"local listener: {local_url}")
        print("access: tailnet only; tailscale funnel is not enabled; HTTPS only")

    def shutdown() -> None:
        if client is None:
            return
        try:
            client.disable(args.tailscale_port)
        except TailscaleError as exc:
            print(f"warning: {exc}", file=sys.stderr)

    if args.reload:
        uvicorn.run(
            "adaptive_service.app:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            reload=True,
        )
    else:
        from .app import create_app

        app = create_app(settings, on_shutdown=shutdown if client is not None else None)
        uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
