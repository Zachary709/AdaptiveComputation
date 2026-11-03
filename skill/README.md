# Scheduling skill

The skill plans tasks against the resource board snapshot, prepares artifacts,
dispatches commands over SSH, and runs one reviewed repair command.

The complete agent workflow and safety contract are in [`SKILL.md`](SKILL.md).
Background references are in [`references/`](references/).

仓库层面的三部分划分见 [`../docs/`](../docs/)：服务的代码、服务启动的约束、使用方法。本文件只保留 skill 自身的快速上手。

## Start

Set `SKILL_DIR` to this directory and run from the workload project. Use the
tailnet HTTPS URL printed by the service as the server address; it can also be
stored as `server` in the gitignored `local.json`.

Start with `submit` when the manifest and controller source root already ran:
it reuses the cached plan and skips separate snapshot, plan, and allocation
steps.

```bash
"$SKILL_DIR/scripts/adaptive-runner" submit .adaptive-computation/tasks.json \
  --task train --mode tmux \
  --output .adaptive-computation/train-result.json
```

Plans are maintained by the service. It ships default plans for a single
device, a large-memory single device, two multi-GPU devices, and all devices;
the board can create, edit, and delete them. `submit` asks the service for the
right to use a plan before dispatch. The service checks node reachability, the
30-second GPU idle window, and allocations held by other users.

On the first run, `submit` tries the registered plans in ascending device
count, claims the first one that fits, and caches only the chosen `plan_id` in
`.adaptive-computation/plan-cache.json`. On later runs it claims that plan and
rebuilds the execution plan from its current devices. When the cached plan
cannot be claimed, it clears the cache and tries the other plans. If none can
be claimed, it prints each failure and exits with status 3; use `--plan-id` to
retry a specific plan. A failed task clears the cached choice; an environment
mismatch keeps it. Use `--refresh-plan` to ignore the cached choice.

An executable plan must come from the service. `submit` claims a registered
plan and rebuilds the execution plan from that plan's devices. `run` refuses a
plan without a service claim, a plan claimed by another user, and devices
outside the claimed plan. A claimed plan is all-or-nothing: one device that is
unreachable, missing, or occupied makes the whole plan infeasible instead of
shrinking the device set. This includes a plan with scope `all`, whose device
list the service expands when the plan is claimed.

For a first review, inspect the registered plan and request a local preview:

```bash
"$SKILL_DIR/scripts/adaptive-runner" plans list
"$SKILL_DIR/scripts/adaptive-runner" plans show --id ID
"$SKILL_DIR/scripts/adaptive-runner" submit .adaptive-computation/tasks.json \
  --dry-run --json
```

The local `plan` command is an offline review tool for a service outage. Its
output has no service claim and is never executed.

A failed short environment check stops the run with `environment_mismatch` and
a reviewed repair candidate. The agent then executes, checks, and reports the
repair:

```bash
"$SKILL_DIR/scripts/adaptive-runner" repair \
  --node <alias> \
  --command '<work_path>/tools/uv sync --frozen' \
  --check '<work_path>/tools/uv run python -c "import torch"' \
  --manager uv \
  --path '<work_path>/tools/uv' \
  --version 'uv 0.5.4' \
  --reporter session-8f2c \
  --report
```

Host-specific values stay in `local.json`. The committed template is
`assets/local.example.json`; `local.json` itself is ignored by Git. It holds
the service URL, the reviewed SSH proxy entry, and the `work`, `data`, and
`model` paths for each node alias. After a `git pull`, run
`development/update.sh` from the repository root to sync the file from the
private config repository.

## Commands

- `inventory pull|show` — fetch or display a service snapshot
- `inspect` — show registered paths and node directories
- `path` — show registered work, data, and model paths
- `gpu` — show live nvitop device state
- `environment` — show environment records and conflicts
- `plan` — build a review-only plan from a snapshot (never executed)
- `submit` — claim a service plan and execute the manifest
- `plans` — list, show, claim, release, pause, resume, or delete plans
- `allocate` — reserve the GPUs selected by a claimed plan
- `allocations` — show the service allocation registry
- `release` — release reservations
- `run` — execute a plan claimed by `submit`
- `repair` — run one reviewed repair command and report the environment

## Vendored core

The skill includes `adaptive_core` as a vendored package. Copy the whole
`skill/` directory when registering it; no sibling directory is required. The
service has its own `service/adaptive_core`, and a test keeps the two copies
identical.

## Tests

```bash
uv run python -m pytest -q skill/tests
```
