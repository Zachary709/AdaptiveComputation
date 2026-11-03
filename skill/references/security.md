# Security Policy

## Reporting a vulnerability

Please do not open a public issue for a security vulnerability. Contact the
repository owner privately or use GitHub's private vulnerability reporting.

## Operational safety

AdaptiveComputation reads cluster inventory and can execute reviewed commands
over SSH. Treat the service host, the controller machine, and task manifests as
privileged operational inputs.

- Private-key contents are never read, copied, or transmitted.
- SSH metadata is obtained with `ssh -G`; connections use host-key checking
  and `BatchMode=yes`.
- The service performs only the fixed read-only inventory probe. It does not
  install packages, edit remote configuration, transfer files, or run a repair.
- Environment repair is a reviewed remote write executed by the agent through
  `adaptive-runner repair`. It uses uv project lockfiles and data-disk paths.
- Review inventory, placement, command expansion, and the plan before dispatch.
- Keep real server URLs, SSH proxy values, and node paths in the gitignored
  `local.json`. Never commit them, and never copy them into a manifest, a
  command example, or a report.
- Declared source code, model, and data artifacts use controller-local paths;
  models and datasets may also use `modelscope:<owner>/<name>` identifiers. The
  dispatcher never follows an arbitrary network URL or searches for another
  source.
- Model acquisition checks the declared target destination first. A missing
  model is downloaded directly on the target only through the documented
  `modelscope download` command and only for the declared identifier, revision,
  and patterns. The dispatcher removes every proxy variable from that process.
  A failed or invalid ModelScope result falls back to the declared
  controller-local source through rsync over the reviewed SSH connection.
  Without a declared local source, the task stops.
- ModelScope datasets are downloaded without a proxy into the controller cache
  and synchronized through the reviewed connection. Local-node synchronization
  uses local rsync.
- Do not install into `/`, `/root`, `/usr`, `/opt`, `~/.cache`, or `~/.local`.
- Keep proxy forwarding loopback-only, PID-tracked, bounded, and cleaned up.
- Treat `adaptive-runner run`, `adaptive-runner repair`, and explicit benchmark
  commands as remote code execution.

## Service access

- Publish the service with `tailscale serve`, which exposes it over HTTPS to
  tailnet devices only. The local uvicorn listener binds `127.0.0.1`.
- This project never enables Tailscale Funnel. Do not add a public proxy or a
  non-tailnet listener in front of the board.
- The service validates no bearer token. Restrict access with the tailnet and
  `tailscale serve`; never expose a public listener.
- Reported environment fields are validated before storage: the node must
  exist, the manager must be in the supported set, paths must start with `/` or
  `~/`, values must be single-line, and the check exit code must be zero.
- The service revalidates a reported environment with a read-only probe. A
  mismatch marks the record `conflict`, publishes a new snapshot version, and
  makes the planner treat the environment as unavailable until it is repaired
  and reported again.
- Every write action is appended to `audit_log` with the actor, action, target,
  detail, and server time.
