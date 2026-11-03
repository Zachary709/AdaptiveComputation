---
name: adaptive-computation
description: Plans and dispatches modular coding, training, evaluation, and data tasks across SSH-accessible lab GPU machines. Use when a task should be split into small modules, needs GPU/CPU/RAM/disk/uv-aware placement, or must run on more than one server. Inventory and environment records come from the resource board service.
compatibility: Requires Python 3.9+, OpenSSH, the bundled adaptive runner, and a reachable resource board service published with tailscale serve over HTTPS from a tailnet device. Remote nodes need a shell for command execution; rsync is required for source/data uploads and controller-to-node model fallback, the modelscope command line tool is required on a target node that downloads a missing model from ModelScope, and nvidia-smi is optional for CPU-only tasks.
---

# Adaptive computation

This directory is the skill package and includes its own `adaptive_core`
package. Set `SKILL_DIR` to this directory before invoking its scripts. Copy
this directory as a whole when registering the skill. Add this directory to the
agent's skill search path or pass it with `--skill "$SKILL_DIR"`.

The repository has two deployment units. The resource board service collects
read-only inventory, stores snapshots, environment records, and GPU
allocations, and serves the HTTP API and web board. This skill plans and
dispatches workloads. The service never runs a workload; the skill never
collects or writes inventory.

Use the service snapshot as the placement source of truth. Do not guess a
machine, and do not execute against a stale snapshot.

## Non-negotiable SSH safety

- Never read, print, copy, upload, or modify a private key. Do not run
  `cat ~/.ssh/id_*`, `ssh-add -L` as a substitute for discovery, or include key
  contents in a prompt, plan, log, or commit.
- The service obtains SSH metadata with `ssh -G` and passes the configured
  identity file to OpenSSH; it never opens the key.
- Use `BatchMode=yes` behavior. Stop and report authentication or network
  errors instead of falling back to passwords or disabling host verification.
- Treat `run` and `repair` as remote code execution. Ask for confirmation before
  executing a generated command unless the user explicitly authorized
  execution.
- Reach the service through its tailnet HTTPS URL. The service validates no
  bearer token. Never expose it through a public address or plain HTTP.

## Bundled runtime and service

`skill/scripts/adaptive-runner` loads `skill/adaptive_core` and
`skill/opensmi`. `service/scripts/adaptive-service` loads
`service/adaptive_core` and `service/adaptive_service`. Resolve both from this
repository instead of an ambient installation.

The service listens only on `127.0.0.1` and publishes itself through
`tailscale serve` with HTTPS. Only tailnet devices can reach the board URL,
which has the form `https://<machine>.<tailnet>.ts.net/`. Funnel is never
enabled. When port 443 already serves another application, the service uses
`--tailscale-port`.

The same publish rule applies to a service that a dispatched workload starts,
such as a demo, an inference endpoint, or another board. The task command binds
`127.0.0.1`, publishing happens from the controller through `tailscale serve`,
and the command never opens a public listener, a plain-HTTP listener, or a
second listener on an occupied port. Review a manifest against this rule before
dispatch.

The skill owns planning, artifact preparation, preflight, dispatch, execution,
and the repair command. The service owns inventory collection, snapshot
versions, environment records and revalidation, allocations, audit events, and
the board UI.

## Local private profile

Real server addresses, SSH proxy details, and per-node paths belong in
`local.json` next to this file. `local.json` is listed in `.gitignore` and must
never be committed; point `ADAPTIVE_LOCAL_PROFILE` at another file when it lives
elsewhere. Start from the committed template `assets/local.example.json`:

- `server` is the tailnet HTTPS URL used when `--server` and `ADAPTIVE_SERVER`
  are both unset.
- `ssh_proxy` holds the reviewed proxy alias, host, port, user, identity file,
  and forward ports. Read these values when a proxy tunnel is needed.
- `nodes` holds `work`, `data`, and `model` paths per node alias. `work-path`,
  `data-path`, and `model-path` use them when `--path` is omitted.

Do not copy these values into a task manifest, a command in this file, a commit,
or a report. Every example in this skill uses `<alias>`, `<user>`, and
`<data-disk>` as placeholders.

## Plan source

An executable plan must come from the resource board service.

- Create an executable plan only through `submit`. It claims a registered
  service plan and rebuilds the execution plan from that plan's devices.
- The runner refuses a plan that has no `server_plan_id`, whose registered plan
  is not claimed by the current user, or whose devices fall outside the claimed
  plan.
- Treat every service plan as a fixed device set, including a plan with scope
  `all`. The service expands its device list when the plan is claimed, and that
  expanded list must stay usable. If any device is unreachable, missing from
  the snapshot, or occupied, the whole plan is infeasible: `submit` releases
  the claim and tries another plan. Never let the planner drop an unusable
  device and execute on the remainder.
- Never hand-edit `plan.json`, `allocations.json`, or `plan-cache.json`, and
  never build a plan with another script, a template, or a copy of a previous
  plan.
- Do not execute the output of the local `plan` command. That command is only
  for offline review when the service is unreachable, and its output carries no
  service claim.
- When no registered plan can be claimed, stop and report the failure. Ask the
  user to create, edit, or free a plan on the board, or to repair the
  environment. Do not substitute a local placement.
- `--allow-snapshot-change`, `--ignore-allocations`, `--allow-unallocated`,
  `--gpu-state snapshot`, and `--no-reserve` require explicit user approval for
  the run.

## Mandatory execution boundary

Once a plan has been reviewed, execution is limited to submitting the reviewed
task and receiving its result:

- Use the bundled runner's `submit`, or `run` with the plan that `submit`
  printed. Prefer its local `--output` option to
  persist the JSON result; do not add a shell heredoc, Python parser, `jq`,
  `awk`, or other ad-hoc reporting script.
- Do not run a second SSH command, remote `ps`, `nvidia-smi`, `find`, `df`, or
  environment scan to explain a successful result. The dispatcher already
  performs GPU and command preflight, the short environment check, and result
  assembly.
- Do not re-run an environment setup task after every success. Tasks marked
  `environment_maintenance` are dormant candidates; the agent runs one
  explicitly through `adaptive-runner repair` only after a failed check.
- Do not refresh the service inventory just to produce a report. Refresh only
  when planning a new placement or recovering from a resource failure.
- Reuse a cached plan for repeated runs of the same manifest. Do not pull a
  snapshot or plan again only because another execution was requested. Plan
  again when the manifest or controller source root changes, the cache entry is
  marked invalid, or the user asks for `--refresh-plan`.
- Do not copy undeclared source code, rewrite manifests, install packages, or
  create unrelated files during dispatch. Declared source, model, and data
  artifact uploads must come from the reviewed plan.
- Report the runner result JSON directly. If a result is incomplete or failed,
  stop and report the node and task error. A retry requires a new plan claimed
  through `submit` or explicit user direction.

The only allowed local execution artifacts are the pulled snapshot, the
claimed plan, result JSON, repair records, the outbox, and the skill's ignored
artifact cache under `.adaptive-computation/`.

## Workflow

Reuse a plan when the task manifest and controller source root have not
changed. Run `submit` first: it reuses the cached plan, and pulls a fresh
snapshot and plans again only when the cache is missing, marked invalid, or
`--refresh-plan` is set. Review a newly created plan with `submit --dry-run`
before the first execution. A plan written by the local `plan` command is a
review artifact and is never executed.

1. **Decompose the request before coding.** Make small modules with one clear
   input and output contract, for example `data`, `train`, `evaluate`, and
   `report`. Add a focused test or runnable smoke check for each module. Record
   the dependency graph in a JSON task manifest. Avoid one giant script.

2. **Pull a fresh snapshot from the service.**

   ```bash
   mkdir -p .adaptive-computation
   "$SKILL_DIR/scripts/adaptive-runner" inventory pull \
     --server "$ADAPTIVE_SERVER" \
     --snapshot .adaptive-computation/snapshot.json
   ```

   `ADAPTIVE_SERVER` is the HTTPS tailnet URL printed by the service, for
example `https://<controller>.<tailnet>.ts.net`. When it is unset, the runner
reads `server` from `local.json`. Do not use a public address or plain HTTP.

   The snapshot contains nodes, GPU and disk state, environment records,
   allocations, policy, `snapshot_id`, and `generated_at`. Do not run a separate
   inventory collector from the skill. If the service is unreachable, stop and
   report the service address and error, or reuse a recent snapshot explicitly
   with `--snapshot` and a reviewed `--max-snapshot-age`.

3. **Write a manifest** such as
   `$SKILL_DIR/assets/examples/adaptive_tasks.json` to
   `.adaptive-computation/tasks.json`. Use `depends_on` for ordering and
   resource constraints such as `gpu_type`, `min_gpu_memory_gib`,
   `min_gpu_free_memory_gib`, `min_cpu_cores`, `min_memory`, `min_disk`,
   optional `disk_path`, `artifacts` for source code, models, and data,
   `uv_env`, `distribution` (`single` or `one-to-one`), `placement` (`pack`,
   `spread`, or `performance`), `prefer_local`, `cpu_only`, and `nodes` as a
   candidate pool. The planner skips busy candidates and selects eligible
   alternatives; use a narrow pool only for real node affinity.

   Environment-dependent tasks declare a short `environment_check`. They may
   also declare `environment_repair_task` or `environment_repair_command` as a
   reviewed repair candidate. The runner returns that candidate with an
   environment failure and never executes it automatically.

   For Python workloads, follow `references/python-environments.md`: use uv and
   keep its cache, runtimes, environments, and temporary files under the
   selected data-disk work root. Commands may use `{node_alias}`,
   `{gpu_index}`, `{gpu_indices}`, `{rank}`, `{world_size}`, `{work_dir}`,
   `{disk_path}`, `{data_path}`, and `{model_path}`.

   Each required source tree, model, or dataset must have an artifact. A
   `source` is a local controller path, or `modelscope:<owner>/<name>` for an
   open model or dataset. A ModelScope-only artifact also needs `size_bytes`. A
   model with a controller-local fallback uses that local path as `source` and
   declares its preferred ModelScope id in `modelscope`; it may also set
   `revision`, `allow_patterns`, `ignore_patterns`, and `required_files`.

   Plans are maintained by the service, not by this skill. The service ships
   default plans (single device, large-memory single device, two multi-GPU
   devices, all devices) and the board can create, edit, and delete them. Each
   plan defines a device set; task placement inside that set is still computed
   locally.

   Prefer `submit` when the manifest and controller source root have not
   changed. It asks the service for the right to use a plan before dispatch.
   The service checks node reachability, the 30-second GPU idle window, and
   allocations held by other users, and returns the conflicting devices when
   the plan cannot run.

   On the first run, `submit` tries the registered plans in ascending device
   count, claims the first one that fits the manifest, and caches only the
   chosen `plan_id` in `.adaptive-computation/plan-cache.json`. On later runs
   it claims that plan again and rebuilds the execution plan from its current
   devices, so edits made on the board take effect. When the cached plan cannot
   be claimed, `submit` clears the cache and tries the other plans. If no plan
   can be claimed, it prints each failure and exits with status 3; ask the user
   whether to repair the environment or choose another plan, then retry with
   `--plan-id ID`. Resource failures and failed tasks clear the cached plan
   choice; an environment mismatch keeps it so a repair can be retried.
   `--refresh-plan` ignores the cached choice.

   A claimed plan is all-or-nothing: `submit` rechecks every device in the
   plan before planning, and a single unusable device makes that plan
   infeasible instead of shrinking the device set. This includes a plan with
   scope `all`, whose device list the service expands when the plan is claimed.

   Manage plans with `plans list`, `plans show --id ID`, `plans claim --id ID`,
   `plans release --id ID`, `plans pause --id ID`, `plans resume --id ID`, and
   `plans delete --id ID`. The web board shows every plan with its devices and
   offers create, edit, delete, claim, release, pause, and resume.

   ```bash
   "$SKILL_DIR/scripts/adaptive-runner" submit .adaptive-computation/tasks.json \
     --server "$ADAPTIVE_SERVER" \
     --task train --mode tmux \
     --output .adaptive-computation/train-result.json
   ```

4. **Review the plan before the first execution.**

   ```bash
   "$SKILL_DIR/scripts/adaptive-runner" plans list
   "$SKILL_DIR/scripts/adaptive-runner" plans show --id ID
   "$SKILL_DIR/scripts/adaptive-runner" submit .adaptive-computation/tasks.json \
     --server "$ADAPTIVE_SERVER" --dry-run --json
   ```

   `plans show` prints the registered device set. `submit --dry-run` claims no
   plan, builds a local preview, performs no SSH work, and refuses to execute.

   The local `plan` command remains an offline review tool for a service
   outage:

   ```bash
   "$SKILL_DIR/scripts/adaptive-runner" plan .adaptive-computation/tasks.json \
     --snapshot .adaptive-computation/snapshot.json \
     --inspect \
     --output .adaptive-computation/plan.json
   ```

   Its output carries no service claim, and `run` refuses it.

   The planner reads each GPU's state from the service. The service reports
   `idle` only when every nvitop sample in the last 30 seconds used at most
   1 GiB of GPU memory, and `occupied` otherwise; graphical processes such as
   Xorg do not make a GPU busy. Planning fails when the service cannot return a
   state. Use `--gpu-state snapshot` only for offline review, which falls back
   to the snapshot's process fields. `--inspect` prints each candidate node's
   registered paths and the top-level data and model directories. Use
   `"$SKILL_DIR/scripts/adaptive-runner" gpu --node ALIAS` to inspect live
   devices separately. The plan records `gpu_source` as `service` or
   `snapshot`.

   Before dispatch, run
   `"$SKILL_DIR/scripts/adaptive-runner" inspect --node ALIAS --uv-cache` to
   compare each project lock with the node's uv cache. The output lists cached
   packages, Python versions, and how many lock packages are missing from the
   cache.

   Check every assignment, GPU model and memory, selected disk path,
   environment, environment status, dependency, and node count. The plan records
   `inventory_snapshot_id`, `inventory_generated_at`, and
   `inventory_max_age_s`. The planner excludes stale or unreachable nodes, busy
   GPUs, and insufficient RAM or disk. It reserves GPUs and requested CPU, RAM,
   and disk capacity across all tasks.

   The snapshot carries the allocation registry and policy. GPUs reserved for
   another user are excluded. When `require_allocation` is true, unallocated
   GPUs are excluded too; use `--allow-unallocated` only when shared policy
   permits it, and do not use `--ignore-allocations` without explicit approval.
   Use `--json` when the complete preview must be inspected.

5. **Register and use the fixed work path.** Before a node is used, register
   the fixed work directory that the skill uses on it:

   ```bash
   "$SKILL_DIR/scripts/adaptive-runner" work-path \
     --node <alias> \
     --reporter session-8f2c
   ```

   Use the alias from the snapshot. The path comes from `nodes.<alias>.work` in
   `local.json`. Pass `--path` instead when the node has no entry in that file.

   The path must be absolute and on a filesystem already mounted on that node.
   The skill checks over SSH that it exists, is a directory, and is writable
   before registering it. The service stores it with the node, and the board
   shows the filesystem that contains it. A node without a registered path
   shows no disk information.

   Register the dataset and model roots with the same command family. A data
   path must be `<work_path>/data` and a model path must be
   `<work_path>/models`; the skill rejects any other path:

   ```bash
   "$SKILL_DIR/scripts/adaptive-runner" data-path --node <alias>
   "$SKILL_DIR/scripts/adaptive-runner" model-path --node <alias>
   ```

   The paths come from `nodes.<alias>.data` and `nodes.<alias>.model` in
   `local.json`. Without a local entry, pass `--path '<work_path>/data'` or
   `--path '<work_path>/models'` on the command line.

   Before registering, the skill runs a read-only check on the node. The work
   path must exist, be a directory, and be writable. A data root must contain
   only directories with data files, and a model root must contain only
   directories with weight files whose shard names are complete. Registration
   stops with the offending entries listed when a check fails.

   The board lists the immediate child directories of a registered dataset or
   model path with their sizes. An unregistered path shows `未注册`.

   When a task omits `work_dir`, the planner uses the registered path first.
   Without one it falls back to
   `<storage_path>/data_<ssh-user>/adaptive` on the selected data disk. The
   dispatcher performs the idempotent directory check immediately before
   execution. Do not add a separate SSH `mkdir`, `findmnt`, write test,
   ownership change, disk format, or cleanup command. If the selected mount is
   not writable, report the dispatch failure and stop; do not silently fall
   back to `/`.

   `submit` claims the service plan and reserves its devices through the
   service allocation registry before dispatch, then releases the reservations
   it created. Do not run `allocate` against a plan written by the local `plan`
   command. Run `allocate` only when the user explicitly asks to reserve the
   devices of a registered plan, with `PLAN_PATH` set to the path that `submit`
   printed:

   ```bash
   "$SKILL_DIR/scripts/adaptive-runner" allocate \
     --plan "$PLAN_PATH" --target "$USER" --ttl 12h
   ```

   `allocate` reads the plan, checks the current registry, and writes the
   reservations to `allocations.json` next to the plan. It refuses a GPU that
   is reserved for another user. `run` reserves the plan GPUs by default and
   releases only the reservations it created; `--no-reserve` skips that step
   and requires explicit user approval. `release --plan` removes the
   reservations recorded in `allocations.json`. Use `allocations` to show the
   current registry.

6. **Handle an environment failure through repair and reporting.** A normal run
   prepares declared artifacts first, then runs the task's short
   `environment_check`. When that check fails, `run` stops with
   `reason: "environment_mismatch"` and returns the check output plus
   `suggested_repair`. The runner does not repair or retry.

   Diagnose the failure, review one repair command and one check, then run:

   ```bash
   "$SKILL_DIR/scripts/adaptive-runner" repair \
     --node <alias> \
     --command '/<data-disk>/<user>/adaptive/tools/uv sync --frozen' \
     --check '/<data-disk>/<user>/adaptive/tools/uv run python -c "import torch"' \
     --manager uv \
     --path /<data-disk>/<user>/adaptive/tools/uv \
     --version 'uv 0.5.4' \
     --uv-version 'uv 0.5.4' \
     --python-version 3.12.14 \
     --project-path /<data-disk>/<user>/adaptive/projects/demo \
     --lockfile /<data-disk>/<user>/adaptive/projects/demo/uv.lock \
     --lock-sha256 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
     --cache-hit \
     --reporter session-8f2c \
     --report
   ```

   `repair` runs the command and the check, writes a local record under
   `.adaptive-computation/repairs/`, and reports only structured fields to the
   service. A report that cannot be delivered is appended to
   `.adaptive-computation/outbox.jsonl` and retried on the next repair. The
   service revalidates the record with a read-only probe. A mismatch marks the
   record `conflict`; the planner then treats that environment as unavailable
   until it is repaired and reported again.

   Repair commands run through `bash -c`, which does not load a login shell. Use
   absolute paths or set `PATH` inside the command. Do not install into `/`,
   `/root`, `/usr`, `/opt`, `~/.cache`, or `~/.local`. A repair may download a
   pinned uv binary into `<storage_path>/data_<ssh-user>/adaptive/tools` using
   the storage and proxy rules in `references/python-environments.md`.

   For a proxy tunnel, retain the SSH child PID and install EXIT/INT/TERM
   cleanup that terminates and waits for that exact PID on success, failure,
   timeout, or cancellation; do not use an untracked `ssh -f` process. Use a
   run-specific forward port and verify the tunnel process and remote loopback
   listener are gone before another download. Set `UV_CACHE_DIR`,
   `UV_PYTHON_INSTALL_DIR`, `UV_TOOL_DIR`, `TMPDIR`, and
   `UV_PROJECT_ENVIRONMENT` below `{work_dir}/envir` for every setup or sync
   command. Use `uv sync --frozen` when a lockfile exists.

7. **Execute the claimed plan.**

   `submit` executes the claimed plan after its preview is reviewed. Run `run`
   separately only with the `PLAN_PATH` that `submit` printed, while that plan
   is still claimed by the current user:

   ```bash
   "$SKILL_DIR/scripts/adaptive-runner" run "$PLAN_PATH" \
     --mode tmux --task train-folds \
     --output .adaptive-computation/train-folds-result.json
   ```

   Omit `--task` to run all ordinary tasks in topological order with
   `--mode direct`; tasks marked `environment_maintenance` are skipped unless
   selected explicitly. For detached `tmux` runs, submit dependent modules one
   at a time with `--task` after confirming the predecessor finished. Use
   `--dry-run` to verify command expansion without SSH. `run` reserves the
   plan's GPUs through the service allocation registry by default and releases
   only the reservations it created, including after a failed dispatch. Pass
   `--no-reserve` when the GPUs are already reserved. A detached `tmux`
   session must remain available while its workload is running and must close
   automatically on every workload exit, including failure; its wrapper and
   configuration artifacts must be removed at the same time. Do not leave a
   completed session waiting for operator input, and do not terminate unrelated
   sessions.

   `one-to-one` starts one command per assigned GPU; `single` starts one command
   per assigned node and exposes that node's selected GPUs via
   `CUDA_VISIBLE_DEVICES`. For a true multi-node distributed program set
   `distributed: true` and make the command understand `MASTER_ADDR`,
   `MASTER_PORT`, `NNODES`, `NODE_RANK`, and `WORLD_SIZE`.

   Before dispatch the runner verifies that the plan carries a
   `server_plan_id`, that the service still claims that plan for the current
   user, and that every planned device belongs to the claimed plan. It then
   checks the plan against the current snapshot and live nvitop state. The node
   must still exist and be reachable, each planned
   GPU must still exist and have no active process, a foreign allocation blocks
   execution, and a changed registered work path blocks execution. A new
   `snapshot_id` alone does not invalidate the plan. `--allow-snapshot-change`
   skips the snapshot check only; it never skips the source check, and an
   unreachable service blocks execution.

8. **Report results.** A plan is reviewable but is not a cluster-wide lock;
   rely on service allocations for durable reservations. Preserve task ids,
   assignments, exit codes, and failed-node messages from the runner result. Do
   not run post-success diagnostics or silently move a task to a different GPU
   after it has started. A new placement or retry after a resource failure
   requires a fresh plan claimed through `submit`.

## Artifact protocol

Artifact preparation completes before the environment check and the workload.

- Source and data artifacts are synchronized from the controller with
  `rsync --checksum`.
- A model target first checks its destination and `required_files`. A missing
  model is downloaded directly from ModelScope after every proxy variable has
  been removed. When ModelScope is unavailable or fails validation, a declared
  controller-local `source` is transferred with `rsync --checksum` over the
  reviewed SSH connection.
- ModelScope-only datasets are downloaded once into the controller cache
  (`.adaptive-computation/modelscope`, changeable with `run --artifact-cache`)
  and then synchronized.
- Directory artifacts with a controller-local source are validated by file
  inventory, and trees up to 1 GiB additionally require per-file sha256
  digests. A directory model without a local source is validated by
  `size_bytes`.
- Completed verification is recorded under `<artifact-cache>/verified/` and
  reused after a metadata-only revalidation. Delete that directory to force a
  full re-verification.

Do not copy the bundled runtime or SSH material to remote nodes.

## What the snapshot means

The service snapshot records, per SSH host:

- GPU count, model, UUID, total and free memory, current utilization, process
  owners, clocks, compute capability, and `available` status;
- `compute_efficiency.observed_utilization_percent`, clock ratio, and an
  optional `benchmark_score`. Utilization is not peak TFLOPS. Only pass an
  explicit `--benchmark-command` to the service when a calibrated benchmark is
  wanted;
- CPU model, logical and physical cores, and 1-minute load, total and available
  RAM;
- mounted filesystem capacity and configured storage paths;
- lightweight `uv` installation and version, installed Python runtimes when
  supported, shallow `.venv`, `uv.lock`, and `pyproject.toml` discoveries, and
  configured conda or venv settings; these are hints that must be revalidated
  before environment setup or execution;
- SSH alias, address, user, port, and identity-file path only.

A failed refresh retains old hardware fields with `stale: true` and clears the
availability lists. Never schedule against it. Disk paths are remote paths;
ensure the command uses the same path on the selected node.

## Task-manifest conventions

Keep generated runtime files under `.adaptive-computation/` (it is local state
and should not be committed). A minimal manifest is:

```json
{
  "tasks": [
    {
      "id": "train",
      "command": "uv run python train.py --rank {rank}",
      "gpus": 4,
      "distribution": "one-to-one",
      "gpu_type": "A100",
      "min_gpu_memory_gib": 40,
      "min_memory": "32GiB",
      "uv_env": "uv",
      "depends_on": ["prepare"]
    }
  ]
}
```

Prefer explicit resource requirements over hard-coded node names. Use `nodes`
only when a task truly requires an affinity. A plan reserves GPUs and the
requested CPU, RAM, and disk capacity across all manifest tasks, so independent
tasks cannot accidentally overlap or overcommit a node.

## Recovery

- No matching GPU: explain which requirement failed and either wait, refresh the
  service inventory, or ask the user to relax that requirement; never steal a
  busy GPU.
- Service unreachable: report the address and error and stop. A recent snapshot
  may feed a review-only local plan, but nothing is executed. Repair reporting
  can be queued in the outbox.
- SSH failure: report the alias and effective target, then ask the user to fix
  `~/.ssh/config` or connectivity. Never request or expose a private key.
- Environment mismatch: report the check output and candidate, repair only the
  affected node, report the result, and re-plan before another run.
- Long jobs: use the reviewed `tmux` mode and keep the generated plan and
  runner result. Do not add unrelated monitoring commands.
