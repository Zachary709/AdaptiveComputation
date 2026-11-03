# Runtime packages

The repository contains two runtimes, each with its own `adaptive_core` copy.

- `skill/adaptive_core/` and `service/adaptive_core/` provide `NodeConfig`, other
  shared data models, `atomic_write_text`, `get_state_dir`, time helpers, and
  safe SSH command construction.
- `service/adaptive_service/` provides the resource board. It
  contains `collector/`, `store/`, `api/`, `app.py`, and `cli.py`.
- `skill/opensmi/` provides the scheduling runtime. The
  internal package name `opensmi` is retained for compatibility with existing
  tooling and tests.

Launchers:

- `skill/scripts/adaptive-runner` puts `skill` first on
  `sys.path`, then calls `opensmi.cli.main`.
- `service/scripts/adaptive-service` puts `service` first
  on `sys.path`, then calls `adaptive_service.cli.main`. It publishes the board
  with `tailscale serve` over HTTPS and keeps uvicorn on `127.0.0.1`.

The launchers resolve both packages from the repository directory. Neither
launcher installs anything on a remote node.

## Service modules

- `collector/probe.py` — SSH discovery, the read-only probe script, parsing,
  concurrency, stale merging, and snapshot serialisation.
- `collector/service.py` — node selection, refresh jobs, snapshot publishing,
  environment merging, and read-only revalidation.
- `store/db.py` — SQLite schema and queries for nodes, snapshots, GPU samples,
  environments, allocations, and audit events.
- `store/allocations.py` — allocation dataclass and JSON helpers retained for
  the allocation unit tests.
- `config.py` — cluster configuration parsing.
- `tailscale.py` — status parsing, serve status inspection, HTTPS serve
  configuration, and cleanup.
- `api/` — HTTP routers for nodes, inventory, environments, allocations, audit,
  and snapshot events.
- `app.py` — application factory, lifespan, static board hosting.

## Skill modules

- `planner.py` — task normalisation, artifact planning, placement, and resource
  reservations.
- `dispatcher.py` — plan execution order, environment checks, preflight, and
  result assembly.
- `artifacts.py` — target-local model reuse, ModelScope acquisition, controller
  transfer fallback, data caching, rsync synchronisation, and verification
  markers.
- `executor.py` — preflight checks including GPU idle validation, quoting, GPU
  binding, and direct or tmux execution.
- `repair.py` — runs one reviewed repair command and one check, then returns a
  structured outcome.
- `client.py` — HTTP client for snapshots, nodes, paths, directories, nvitop,
  environments, allocations, and audit.
- `snapshot.py` — snapshot validation, freshness checks, allocation conversion,
  and policy extraction.
- `cli.py` — `inventory pull|show`, `inspect`, `path`, `gpu`, `environment`,
  `plan`, `allocate`, `allocations`, `release`, `run`, and `repair`.
- `local.py` — reads the gitignored `local.json` for the service URL and the
  per-node `work`, `data`, and `model` paths.

When changing a module, run the matching test group from `skill/tests/` or
`service/tests/`.
