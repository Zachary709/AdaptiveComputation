# Task manifest reference

`submit` accepts a task manifest as a JSON object with a `tasks` array and an
optional `defaults` object. A task must have a unique `id` and shell `command`.
`gpus`/`gpu_count` defaults to one; set `cpu_only: true` or `gpu_count: 0`
for a CPU/data task.

## Placement fields

- `distribution`: `single` runs once per selected node; `one-to-one` runs one
  command per selected GPU.
- `replicas`: additional GPU/process copies; total requested GPUs are
  `gpu_count * replicas`.
- `placement`: `pack`, `spread`, or `performance`. Performance uses an
  explicitly collected benchmark score when available, otherwise utilization
  and free memory; it never invents TFLOPS.
- `prefer_local`: optional boolean. It defaults to `true` when the manifest
  contains one ordinary task and to `false` when it contains multiple ordinary
  tasks. Set it explicitly to override the default.
- `same_node`, `min_nodes`, and `max_nodes` express topology. `nodes` is a
  candidate pool: the planner selects a free subset from it and automatically
  skips stale, busy, or otherwise ineligible candidates. Use a narrowly scoped
  `nodes` list only when the task has a real affinity requirement; otherwise
  omit it so every inventoried node can be considered.
- `distributed: true` injects `MASTER_ADDR`, `MASTER_PORT`, `NNODES`,
  `NODE_RANK`, `LOCAL_RANK`, and `WORLD_SIZE`.

## Resource fields

- `gpu_type`, `min_gpu_memory_gib`, and `min_gpu_free_memory_gib`;
- `max_gpu_utilization_percent`;
- `min_cpu_cores`, `min_memory`, and `min_disk`;
- `disk_path` optionally selects a matching remote filesystem/work path. If
  omitted, the planner prefers the largest mounted non-root data filesystem
  on each selected node (for example `/home/data` on a controller or another
  data mount) and falls back to `/` only when no larger data volume is mounted;
- `artifacts` declares required source code, model, and data uploads. Each item
  has `name`, `kind` (`source`, `model`, or `data`), `source`, and an optional
  `destination`; `mode` is inferred for a local source and `sha256` can pin a
  local file. Relative local sources resolve from the directory where `submit`
  runs. The planner records the local size and includes it in each selected
  node's disk requirement. A source artifact requires a local controller path;
  `timeout_s` optionally sets the per-artifact acquisition limit. Source, data,
  and controller-fallback model transfers use `rsync --checksum`; the current
  machine uses local rsync. Undeclared network URLs are rejected;
- an open model or dataset can use `modelscope:<owner>/<name>` as its `source`.
  For a model, every target first checks its destination, then downloads the
  missing model directly from ModelScope with all proxy variables removed. For
  a dataset, the controller downloads one snapshot into its artifact cache and
  synchronizes it to the assignments. A ModelScope-only artifact must declare
  `size_bytes` because acquisition happens after planning, and `sha256` does
  not apply to it;
- artifact reuse validation is content-aware. A file artifact with `sha256` is
  accepted only when the destination digest matches. A directory artifact with
  a controller-local source is accepted only when its file inventory (names and
  byte sizes) matches that source, and trees up to 1 GiB additionally require
  an sha256 digest match for every file; larger trees are validated by
  inventory only. A directory model without a controller-local source falls
  back to its declared `size_bytes`: reuse is rejected when the destination
  exceeds it. A failed validation retransfers or re-downloads the artifact;
  directory transfers mirror the source with rsync `--delete` and are verified
  again after the transfer. Results record the strongest passed check in
  `verified` (`digest`, `metadata`, `size-cap`, or `existence`). Every
  completed verification is persisted as a marker under
  `<artifact-cache>/verified/`; later runs reuse it after a metadata-only
  revalidation and skip the digest comparison and the rsync transfer until the
  source fingerprint or the destination metadata changes. Delete that
  directory to force a full re-verification;
- a model that has both a preferred ModelScope version and a controller-local
  transfer fallback uses a local `source` plus `modelscope: "<owner>/<name>"`.
  `revision` selects a branch, tag, or commit; `allow_patterns` and
  `ignore_patterns` narrow the ModelScope download. `required_files` lists
  relative files that must exist in a directory model before it is considered
  usable. The target-local check and direct ModelScope attempt precede the
  controller transfer, so model bytes use SSH only for the final fallback;
- `uv_env` and/or `env_manager` select recorded uv, venv, conda, or
  micromamba environments;
- `environment_check` is an optional short, reviewed shell probe run inside
  the dispatch path for each assignment. It should return zero only when the
  requested runtime and locked dependencies are usable;
- `environment_repair_task` names a task in the same plan whose command is a
  reviewed repair candidate, while `environment_repair_command` provides an
  inline candidate command. The runner returns the candidate with an
  environment failure and does not execute it. A repair task should be marked
  `environment_maintenance: true` and is skipped during an ordinary full-plan
  run; the agent runs it explicitly through `adaptive-runner repair`;
- `depends_on` creates the execution dependency graph;
- `user` optionally identifies the allocation owner. The CLI `--user` takes
  precedence when selecting against the service allocation registry.

`min_memory` and `min_disk` default to GiB and accept values such as `32GiB`
or `100 GB`. Explicit `*_bytes` fields are interpreted as bytes unless a unit
is included in the string.

An artifact without `destination` uses `{work_dir}/projects/<name>` for source
code, `{work_dir}/models/<name>` for a model, and `{work_dir}/data/<name>` for
data. Data destinations must stay under `{work_dir}/data`, and model
destinations must stay under `{work_dir}/models`. Destination templates may use
`{work_dir}`, `{disk_path}`, and `{node_alias}`. Artifact acquisition runs before the environment check and
preflight; a failed final acquisition stops that task. For example:

```json
{
  "artifacts": [
    {
      "name": "training-project",
      "kind": "source",
      "source": ".",
      "destination": "{work_dir}/projects/training-project"
    },
    {
      "name": "checkpoint",
      "kind": "model",
      "source": "checkpoints/best.pt",
      "destination": "{work_dir}/models/best.pt"
    },
    {
      "name": "training-data",
      "kind": "data",
      "source": "data/train",
      "destination": "{work_dir}/data/train"
    },
    {
      "name": "qwen",
      "kind": "model",
      "source": "/data/models/Qwen2.5-7B-Instruct",
      "modelscope": "Qwen/Qwen2.5-7B-Instruct",
      "revision": "master",
      "allow_patterns": ["*.json", "*.safetensors"],
      "required_files": ["config.json"],
      "destination": "{work_dir}/models/qwen2.5-7b"
    }
  ]
}
```

If `work_dir` is omitted as well, the planner resolves it per assignment as
`<storage_path>/data_<ssh-user>/adaptive`; the dispatcher creates it with
`mkdir -p` immediately before entering it. Existing directories are reused.
Commands and environment values may use `{node_alias}`, `{gpu_index}`,
`{gpu_indices}`, `{rank}`, `{world_size}`, `{work_dir}`, `{disk_path}`,
`{data_path}`, and `{model_path}`.
`CUDA_VISIBLE_DEVICES` is controlled by the reviewed assignment and cannot be
overridden through task `env`.

The planner rejects unknown dependencies, cycles, stale/unreachable nodes,
busy GPUs, and resource shortfalls. A missing recorded environment is rejected
unless the task supplies both a short `environment_check` and an explicit
repair recipe; such assignments are marked `environment_status: "unverified"`.
Environment-maintenance tasks retain assignments for targeted repair but do not
reserve GPUs or CPU/RAM/disk capacity from ordinary workload tasks. It reserves
selected GPUs and requested CPU/RAM/disk capacity across the plan. Plans are
review artifacts, not cluster-wide locks; a retry or a new placement claims a
fresh plan through `submit` before execution.
