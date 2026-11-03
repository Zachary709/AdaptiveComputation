# Adaptive multi-node workflow

The repository provides a resource board service and a scheduling skill. The
service collects read-only inventory and maintains state; the skill plans and
dispatches workloads.

Start the service on the trusted controller host from the repository root. It
listens only on `127.0.0.1` and publishes an HTTPS endpoint through
`tailscale serve`:

```bash
service/scripts/adaptive-service \
  --state-dir ~/.adaptive-computation \
  --ssh-config ~/.ssh/config \
  --interval 60
```

Requirements: `tailscale up` has completed, and the current user may change the
serve configuration (`sudo tailscale set --operator=$USER` once). The startup
output prints the tailnet URL, for example
`https://<controller>.<tailnet>.ts.net/`. Only tailnet devices can reach that
URL; Funnel is never enabled. When port 443 already serves another application,
use `--tailscale-port 8443`. A conflicting target on the same port stops
startup. When the service exits, it removes its own serve rule.

The service creates no access token and validates no bearer token. Reach it
through its tailnet HTTPS URL. Clients may set `ADAPTIVE_SERVER` when the
default local listener is not the right address, or keep the URL in the
gitignored `local.json` next to `SKILL.md`.

Build and serve the board:

```bash
npm ci --prefix frontend
npm run build --prefix frontend
```

The build output is copied into the service package and served at `/`. Use the
API directly when the board is not needed:

```bash
curl https://<controller>.<tailnet>.ts.net/api/v1/snapshot
```

Use the same HTTPS URL as the server address for `adaptive-runner`.

## Skill workflow

Set `SKILL_DIR` to the repository directory and run from the workload project.
Use the tailnet HTTPS URL printed by the service as the server address.

Plans are maintained by the service. The board creates, edits, and deletes
them, and the service ships defaults for a single device, a large-memory single
device, two multi-GPU devices, and all devices.

Run `submit` first when the manifest and controller source root have not
changed. It claims a plan from the service, rebuilds the execution plan from
that plan's current devices, and skips the snapshot and planning steps. The
service checks node reachability, the 30-second GPU idle window, and
allocations held by other users. The chosen `plan_id` is cached; when it can no
longer be claimed, `submit` tries the other plans and exits with status 3 when
none fits, so the agent can ask the user whether to repair the environment or
choose another plan (`--plan-id`).

Plans can be inspected and changed with `plans list`, `plans show --id ID`,
`plans claim --id ID`, `plans release --id ID`, `plans pause --id ID`,
`plans resume --id ID`, and `plans delete --id ID`. Use the commands below for
the first review of a placement.

```bash
"$SKILL_DIR/scripts/adaptive-runner" plans list
"$SKILL_DIR/scripts/adaptive-runner" plans show --id ID
"$SKILL_DIR/scripts/adaptive-runner" submit .adaptive-computation/tasks.json \
  --dry-run --json
```

An executable plan must come from the service. `submit` claims a registered
plan and rebuilds the execution plan from its devices. `run` refuses a plan
without a service claim, a plan claimed by another user, and devices outside
the claimed plan. A claimed plan is all-or-nothing: one device that is
unreachable, missing, or occupied makes the whole plan infeasible instead of
shrinking the device set. This includes a plan with scope `all`, whose device
list the service expands when the plan is claimed. The local `plan` command
below is an offline review tool, and its output is never executed.

```bash
"$SKILL_DIR/scripts/adaptive-runner" plan .adaptive-computation/tasks.json \
  --snapshot .adaptive-computation/snapshot.json \
  --inspect \
  --output .adaptive-computation/plan.json
```

`plan` records the snapshot id and generation time. GPU placement reads each
GPU's `idle` or `occupied` state from the service; the service calls a GPU idle
only when every nvitop sample in the last 30 seconds stayed at or below 1 GiB
of used memory. Planning fails when that state is unavailable. Use
`--gpu-state snapshot` only for offline review. `run` rechecks the
same service state and verifies that each planned node and GPU is still
present, reachable, idle, and not reserved by another user. A new snapshot id
alone does not stop execution; `--allow-snapshot-change` skips the snapshot
check only and requires explicit user approval.

A manifest containing one ordinary task prefers eligible local resources by
default. Use `prefer_local: false` to disable that preference. The planner
skips stale, unreachable, busy, or insufficient nodes, and reserves GPUs plus
requested CPU, RAM, and disk capacity across all tasks.

## Environment failures

When a task's short `environment_check` fails, the runner returns:

```json
{
  "status": "failed",
  "reason": "environment_mismatch",
  "environment": {
    "checks": [{"node_alias": "<alias>", "status": "failed", "stderr": "..."}],
    "suggested_repair": "cd {work_dir}/projects/demo && uv sync --frozen"
  }
}
```

The agent diagnoses the failure, reviews a repair command, and runs it through
the skill:

```bash
"$SKILL_DIR/scripts/adaptive-runner" repair \
  --node <alias> \
  --command 'UV_INSTALL_DIR=<work_path>/tools sh install-uv.sh' \
  --check '<work_path>/tools/uv run python -c "import torch"' \
  --manager uv \
  --path '<work_path>/tools/uv' \
  --version 'uv 0.5.4' \
  --project-path '<work_path>/projects/demo' \
  --reporter session-8f2c \
  --report
```

`repair` executes the command, executes the check, writes a local record under
`.adaptive-computation/repairs/`, and reports only structured fields to the
service. A report that cannot be delivered is appended to
`.adaptive-computation/outbox.jsonl` and retried on the next repair. The service
revalidates the record with a read-only probe; a mismatch marks it `conflict`
and the planner treats the environment as unavailable until the agent repairs
and reports it again.

Repair commands run through `bash -c`, which does not load a login shell. Use
absolute paths or set `PATH` inside the command.

## Artifacts

The dispatcher prepares declared artifacts before the environment check and the
workload:

- source and data artifacts are synchronized from the controller with
  `rsync --checksum`;
- a model target first checks its destination, then downloads the declared
  ModelScope version directly without a proxy, and finally accepts a declared
  controller-local copy through rsync;
- ModelScope datasets are cached on the controller and synchronized;
- completed verification is recorded under `<artifact-cache>/verified/` and
  reused after a metadata-only revalidation.

## Planning inputs

Task requirements include `gpu_type`, `min_gpu_memory_gib`,
`min_gpu_free_memory_gib`, `prefer_local`, `min_cpu_cores`, `min_memory`,
`min_disk`, `disk_path`, `artifacts`, `uv_env`, `environment_check`,
`environment_repair_task`, `environment_repair_command`,
`nodes`, `placement`, `cpu_only`, and `distribution`. The full field reference
is in `references/task-manifest.md`.

If `disk_path` is omitted, the planner prefers the largest mounted non-root data
filesystem and records that node-local path. If `work_dir` is also omitted, it
resolves to `<storage_path>/data_<ssh_user>/adaptive`. Python caches, runtimes,
and environments stay below that root and are managed with uv.

`one-to-one` runs one command per GPU. `single` runs one command per selected
node with that node's GPU list in `CUDA_VISIBLE_DEVICES`. Commands support
`{node_alias}`, `{gpu_index}`, `{gpu_indices}`, `{rank}`, `{world_size}`,
`{work_dir}`, and `{disk_path}`. Use `distributed: true` only when the command
coordinates across nodes.

## Allocation policy

The service snapshot contains the allocation registry and policy. The planner
excludes GPUs reserved for another user. When `require_allocation` is true,
unallocated GPUs are also excluded; use `--allow-unallocated` only when shared
policy permits it. `--ignore-allocations` bypasses the registry only with
explicit user approval.

## Offline planning

`plan --snapshot <path>` produces a review-only plan when the service is
unreachable. `run` still requires a service claim, so nothing is executed
offline. Environment repair requires the service only for the final report; the
repair command itself uses the reviewed SSH connection.
