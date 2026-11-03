# Inventory and plan schema

`inventory pull` copies the service snapshot into the workload project. The
snapshot is a JSON object with `schema_version`, `snapshot_id`, `generated_at`,
`collector`, `nodes`, `environment_records`, `allocations`, and `policy`.

Each node has `ssh`, `reachable`, `stale`, `errors`, `hardware`, `software`,
`configured`, `work_path`, `data_path`, `model_path`, `environment_record`, and
`environment_status`. The `ssh.identity_file` value is only a path; private-key
bytes never occur in this document. `work_path` is the fixed skill work
directory registered through `adaptive-runner work-path`; `data_path` and
`model_path` are the dataset and model directories registered through
`adaptive-runner data-path` and `adaptive-runner model-path`. Each is absent
until the skill registers one.

Important paths:

- `hardware.gpu.count`
- `hardware.gpu.models`
- `hardware.gpu.devices[].index/name/uuid`
- `hardware.gpu.devices[].memory_total_mib`
- `hardware.gpu.devices[].memory_free_mib` (used by `min_gpu_free_memory_gib`)
- `hardware.gpu.devices[].utilization_gpu_percent`
- `hardware.gpu.devices[].compute_efficiency`
- `hardware.cpu.model/logical_cores/physical_cores/load_1m`
- `hardware.memory.total_bytes/available_bytes`
- `hardware.disks[].path/total_bytes/available_bytes`
- `hardware.available_disk_paths`
- `work_path`, `data_path`, `model_path` and their `*_registered_at` fields
- `software.uv` (including discovered `python_runtimes`)
- `software.tmux.installed/version/path`
- `software.environments` and `software.projects`
- `availability.gpu_indices` and `availability.storage_paths`
- each GPU's `available`, `availability_reason`, and active `processes`

`environment_records` contains one record per node:

```json
{
  "alias": "GPU-01",
  "manager": "uv",
  "path": "/data/u/adaptive/uv",
  "version": "uv 0.5.4",
  "project_path": "/data/u/adaptive/projects/demo",
  "python_version": "3.11",
  "status": "reported",
  "source": "skill",
  "reporter": "session-8f2c",
  "evidence": "uv run python -c 'import torch' exited with 0",
  "reported_at": "2026-10-26T15:02:11+09:00",
  "verified_at": null,
  "conflict_detail": {}
}
```

`status` is one of `recorded`, `unverified`, `reported`, `conflict`, or
`missing`. Only `recorded` and `reported` are merged into
`software.environments`; `conflict` removes the matching entry and sets
`environment_status`, which makes the planner treat the node as unavailable for
that environment.

`allocations` contains `alias`, `gpu_index`, `target`, `assigned_by`,
`assigned_at`, `expires_at`, and `notes`.

`policy` contains `require_allocation` and `all_users_token`.

A plan produced by `adaptive-runner plan` adds `tasks`, topological `order`, an
`allocation_policy` audit record, `inventory_snapshot_id`,
`inventory_generated_at`, `inventory_max_age_s`, and a `nodes` map containing
only the connection and configuration metadata needed by `adaptive-runner run`.
Plans never contain private-key contents. Each task's `assignments` groups
physical GPU indices by node and records the selected remote `storage_path`,
environment, and `environment_status` for each node.
