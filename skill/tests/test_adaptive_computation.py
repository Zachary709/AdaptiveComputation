import asyncio
import base64
import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from opensmi.artifacts import (
    _local_file_inventory,
    _local_sha256_inventory,
    build_modelscope_command,
    build_proxy_free_modelscope_command,
    ensure_modelscope_snapshot,
    modelscope_snapshot_dir,
    record_modelscope_snapshot,
    sync_artifact_to_node,
)
from adaptive_core.models import NodeConfig
from adaptive_core.sshutil import RemoteExecResult
from adaptive_service.collector.probe import parse_inventory_output
from adaptive_service.collector.service import _default_local_node
from opensmi.dispatcher import dispatch_plan_sync
from opensmi.planner import PlanningError, parse_size, plan_tasks, topological_order


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = Path(__file__).resolve().parents[1]


def b64(value):
    return base64.b64encode(value.encode()).decode()


def inventory_output():
    return "\n".join(
        [
            "__OPENSMI_INVENTORY_BEGIN__",
            f"__OPENSMI_INVENTORY__meta|hostname|{b64('gpu-a')}",
            f"__OPENSMI_INVENTORY__meta|os|{b64('Ubuntu 22.04')}",
            f"__OPENSMI_INVENTORY__cpu|{b64('AMD EPYC') }|32|16|2400",
            "__OPENSMI_INVENTORY__memory|137438953472|100000000000",
            f"__OPENSMI_INVENTORY__gpu|0|{b64('GPU-0')}|{b64('NVIDIA A100')}|81920|0|0|0|40|50",
            f"__OPENSMI_INVENTORY__gpu_extra|0|{b64('8.0')}|1200|1400|300|{b64('535.1')}",
            f"__OPENSMI_INVENTORY__disk|explicit|{b64('/scratch')}|1099511627776|0|1099511627776|0",
            f"__OPENSMI_INVENTORY__env|uv|{b64('/home/u/.local/bin/uv')}|{b64('/home/u')}|{b64('uv 0.5')}|{b64('')}",
            f"__OPENSMI_INVENTORY__uv_project|{b64('/home/u/project')}|{b64('/home/u/project/uv.lock')}",
            "__OPENSMI_INVENTORY_END__",
        ]
    )


def test_inventory_parser_collects_requested_hardware_fields():
    node = NodeConfig("gpu-a", "10.0.0.1", "u")
    data = parse_inventory_output(node, inventory_output())

    assert data["reachable"] is True
    assert data["hostname"] == "gpu-a"
    assert data["hardware"]["gpu"]["count"] == 1
    gpu = data["hardware"]["gpu"]["devices"][0]
    assert gpu["name"] == "NVIDIA A100"
    assert gpu["available"] is True
    assert gpu["compute_efficiency"]["sm_clock_ratio_percent"] == pytest.approx(1200 / 1400 * 100)
    assert data["hardware"]["available_disk_paths"] == ["/scratch"]
    assert data["software"]["uv"]["installed"] is True
    assert data["software"]["projects"][0]["manager"] == "uv"


def node_doc(alias, address, indices, *, stale=False):
    return {
        "alias": alias,
        "reachable": not stale,
        "stale": stale,
        "errors": [],
        "ssh": {"address": address, "user": "u", "port": 22},
        "configured": {},
        "hardware": {
            "cpu": {"logical_cores": 32},
            "memory": {"available_bytes": 128 * 1024**3},
            "disks": [{"path": "/scratch", "available_bytes": 500 * 1024**3}],
            "gpu": {
                "devices": [
                    {
                        "index": i,
                        "uuid": f"{alias}-{i}",
                        "name": "A100",
                        "memory_total_mib": 81920,
                        "memory_free_mib": 81920,
                        "utilization_gpu_percent": 0,
                        "process_count": 0,
                        "available": True,
                    }
                    for i in indices
                ]
            },
        },
        "software": {"uv": {"installed": True}, "environments": []},
    }


def test_collector_adds_local_machine_without_ssh_config():
    nodes = _default_local_node([])

    assert nodes[0].alias == "local"
    assert nodes[0].address == "127.0.0.1"


def test_planner_prefers_local_machine_for_one_task():
    remote = node_doc("remote", "10.0.0.2", [0, 1, 2, 3])
    local = node_doc("local", "127.0.0.1", [0])

    plan = plan_tasks(
        {"nodes": [remote, local]},
        {"tasks": [{"id": "train", "command": "true", "gpus": 1}]},
    )

    assert plan["tasks"][0]["assignments"][0]["node_alias"] == "local"


def test_planner_records_local_source_model_and_data_artifacts():
    source_tree = SKILL_ROOT / "assets/examples/cnn_smoke"
    model_source = SKILL_ROOT / "assets/examples/cnn_smoke/train.py"
    data_source = SKILL_ROOT / "assets/examples/cnn_smoke"
    node = node_doc("local", "127.0.0.1", [0])

    plan = plan_tasks(
        {"nodes": [node]},
        {
            "tasks": [
                {
                    "id": "train",
                    "command": "true",
                    "gpus": 1,
                    "artifacts": [
                        {
                            "name": "project",
                            "kind": "source",
                            "source": "assets/examples/cnn_smoke",
                        },
                        {
                            "name": "weights",
                            "kind": "model",
                            "source": "assets/examples/cnn_smoke/train.py",
                        },
                        {
                            "name": "dataset",
                            "kind": "data",
                            "source": "assets/examples/cnn_smoke",
                        },
                    ],
                }
            ]
        },
        source_root=REPOSITORY_ROOT,
    )

    artifacts = plan["tasks"][0]["artifacts"]
    assert artifacts[0]["source"] == str(source_tree.resolve())
    assert artifacts[0]["destination"] == "{work_dir}/projects/project"
    assert artifacts[0]["mode"] == "directory"
    assert artifacts[1]["source"] == str(model_source.resolve())
    assert artifacts[1]["destination"] == "{work_dir}/models/weights"
    assert artifacts[1]["mode"] == "file"
    assert artifacts[1]["size_bytes"] == model_source.stat().st_size
    assert artifacts[2]["source"] == str(data_source.resolve())
    assert artifacts[2]["mode"] == "directory"
    assert plan["tasks"][0]["artifact_bytes"] >= artifacts[1]["size_bytes"]

    dry_run = dispatch_plan_sync(plan, dry_run=True)
    assert dry_run["tasks"][0]["artifacts"][0]["status"] == "planned"
    assert dry_run["tasks"][0]["artifacts"][0]["node_alias"] == "local"

    small_node = node_doc("local", "127.0.0.1", [0])
    small_node["hardware"]["disks"][0]["available_bytes"] = artifacts[1]["size_bytes"] - 1
    with pytest.raises(PlanningError, match="disk"):
        plan_tasks(
            {"nodes": [small_node]},
            {
                "tasks": [
                    {
                        "id": "train",
                        "command": "true",
                        "artifacts": [
                            {
                                "name": "weights",
                                "kind": "model",
                                "source": "assets/examples/cnn_smoke/train.py",
                            }
                        ],
                    }
                ]
            },
            source_root=REPOSITORY_ROOT,
        )


def test_data_and_model_destinations_must_stay_in_work_dir():
    source = SKILL_ROOT / "assets/examples/cnn_smoke/train.py"
    node = node_doc("local", "127.0.0.1", [0])
    for kind in ("data", "model"):
        with pytest.raises(PlanningError, match="must stay under"):
            plan_tasks(
                {"nodes": [node]},
                {
                    "tasks": [
                        {
                            "id": "train",
                            "command": "true",
                            "work_dir": "/tmp/adaptive-work",
                            "artifacts": [
                                {
                                    "name": "payload",
                                    "kind": kind,
                                    "source": str(source),
                                    "destination": "/tmp/outside",
                                }
                            ],
                        }
                    ]
                },
                source_root=REPOSITORY_ROOT,
            )


def test_planner_records_modelscope_model_artifact():
    node = node_doc("local", "127.0.0.1", [0])
    plan = plan_tasks(
        {"nodes": [node]},
        {
            "tasks": [
                {
                    "id": "train",
                    "command": "true",
                    "gpus": 1,
                    "artifacts": [
                        {
                            "name": "qwen",
                            "kind": "model",
                            "source": "modelscope:Qwen/Qwen2.5-0.5B-Instruct",
                            "revision": "master",
                            "allow_patterns": ["*.json", "*.safetensors"],
                            "size_bytes": "2GiB",
                        }
                    ],
                }
            ]
        },
    )

    artifact = plan["tasks"][0]["artifacts"][0]
    assert artifact["source_type"] == "modelscope"
    assert artifact["model_id"] == "Qwen/Qwen2.5-0.5B-Instruct"
    assert artifact["repo_type"] == "model"
    assert artifact["revision"] == "master"
    assert artifact["allow_patterns"] == ["*.json", "*.safetensors"]
    assert artifact["ignore_patterns"] == []
    assert artifact["mode"] == "directory"
    assert artifact["destination"] == "{work_dir}/models/qwen"
    assert artifact["size_bytes"] == 2 * 1024**3
    assert artifact["timeout_s"] == 21600
    assert plan["tasks"][0]["artifact_bytes"] == 2 * 1024**3


def test_planner_rejects_modelscope_source_artifacts():
    with pytest.raises(
        PlanningError, match="source artifacts require a local controller path"
    ):
        plan_tasks(
            {"nodes": [node_doc("local", "127.0.0.1", [0])]},
            {
                "tasks": [
                    {
                        "id": "train",
                        "command": "true",
                        "artifacts": [
                            {
                                "name": "project",
                                "kind": "source",
                                "source": "modelscope:owner/project",
                                "size_bytes": "1GiB",
                            }
                        ],
                    }
                ]
            },
        )


def test_planner_requires_explicit_size_for_modelscope_artifacts():
    with pytest.raises(PlanningError, match="size_bytes"):
        plan_tasks(
            {"nodes": [node_doc("local", "127.0.0.1", [0])]},
            {
                "tasks": [
                    {
                        "id": "train",
                        "command": "true",
                        "artifacts": [
                            {
                                "name": "qwen",
                                "kind": "model",
                                "source": "modelscope:Qwen/Qwen2.5-0.5B-Instruct",
                            }
                        ],
                    }
                ]
            },
        )


def test_planner_reserves_disk_for_modelscope_artifacts():
    node = node_doc("local", "127.0.0.1", [0])
    node["hardware"]["disks"][0]["available_bytes"] = 1024**3
    with pytest.raises(PlanningError, match="disk"):
        plan_tasks(
            {"nodes": [node]},
            {
                "tasks": [
                    {
                        "id": "train",
                        "command": "true",
                        "artifacts": [
                            {
                                "name": "qwen",
                                "kind": "model",
                                "source": "modelscope:Qwen/Qwen2.5-7B-Instruct",
                                "size_bytes": "40GiB",
                            }
                        ],
                    }
                ]
            },
        )


def test_modelscope_command_covers_models_datasets_and_patterns():
    model = {
        "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
        "repo_type": "model",
        "revision": "master",
        "allow_patterns": ["*.json"],
        "ignore_patterns": ["*.bin"],
    }
    assert build_modelscope_command(model, Path("/cache/snap")) == [
        "modelscope",
        "download",
        "--model",
        "Qwen/Qwen2.5-0.5B-Instruct",
        "--local_dir",
        "/cache/snap",
        "--revision",
        "master",
        "--include",
        "*.json",
        "--exclude",
        "*.bin",
    ]
    dataset = {
        "model_id": "modelscope/clue",
        "repo_type": "dataset",
        "revision": "",
        "allow_patterns": [],
        "ignore_patterns": [],
    }
    assert build_modelscope_command(dataset, Path("/cache/snap")) == [
        "modelscope",
        "download",
        "--dataset",
        "modelscope/clue",
        "--local_dir",
        "/cache/snap",
    ]


def test_remote_modelscope_command_removes_every_proxy_variable():
    command = build_proxy_free_modelscope_command(
        {
            "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
            "repo_type": "model",
            "revision": "",
            "allow_patterns": [],
            "ignore_patterns": [],
        },
        Path("/data/models/qwen"),
    )

    assert command[:2] == ["env", "-u"]
    assert command[-5:] == [
        "download",
        "--model",
        "Qwen/Qwen2.5-0.5B-Instruct",
        "--local_dir",
        "/data/models/qwen",
    ]
    for name in (
        "ALL_PROXY",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "all_proxy",
        "https_proxy",
        "http_proxy",
    ):
        index = command.index(name)
        assert command[index - 1] == "-u"


def test_modelscope_cache_hit_skips_the_download(tmp_path, monkeypatch):
    artifact = {
        "name": "qwen",
        "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
        "repo_type": "model",
        "revision": "master",
        "allow_patterns": [],
        "ignore_patterns": [],
        "size_bytes": 1024**3,
    }
    snapshot = modelscope_snapshot_dir(tmp_path, artifact)
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}")
    record_modelscope_snapshot(tmp_path, artifact)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))

    path, status = asyncio.run(ensure_modelscope_snapshot(tmp_path, artifact))

    assert path == snapshot
    assert status == "cached"


def test_dispatch_reuses_model_already_available_on_target():
    work_dir = REPOSITORY_ROOT / ".adaptive-computation" / f"model-reuse-test-{uuid.uuid4().hex}"
    node = node_doc("local", "127.0.0.1", [0])
    try:
        destination = work_dir / "models" / "qwen"
        destination.mkdir(parents=True)
        (destination / "config.json").write_text("{}")
        plan = plan_tasks(
            {"nodes": [node]},
            {
                "tasks": [
                    {
                        "id": "train",
                        "command": "test -f {work_dir}/models/qwen/config.json",
                        "work_dir": str(work_dir),
                        "artifacts": [
                            {
                                "name": "qwen",
                                "kind": "model",
                                "source": "modelscope:Qwen/Qwen2.5-0.5B-Instruct",
                                "required_files": ["config.json"],
                                "size_bytes": "1GiB",
                            }
                        ],
                    }
                ]
            },
        )

        result = dispatch_plan_sync(plan, mode="direct", skip_preflight=True)

        assert result["ok"] is True
        acquisition = result["tasks"][0]["artifacts"][0]
        assert acquisition["status"] == "available"
        assert acquisition["transport"] == "target-local"
        assert acquisition["verified"] == "size-cap"
        assert acquisition["source"] == "modelscope:Qwen/Qwen2.5-0.5B-Instruct"
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def test_dispatch_repairs_corrupted_preexisting_model_directory(tmp_path):
    source_dir = tmp_path / "qwen"
    source_dir.mkdir()
    (source_dir / "config.json").write_text("{}")
    work_dir = tmp_path / "work"
    destination = work_dir / "models" / "qwen"
    destination.mkdir(parents=True)
    (destination / "config.json").write_text("{}")
    (destination / "extra.bin").write_text("stale")
    plan = plan_tasks(
        {"nodes": [node_doc("local", "127.0.0.1", [0])]},
        {
            "tasks": [
                {
                    "id": "train",
                    "command": "true",
                    "work_dir": str(work_dir),
                    "artifacts": [
                        {
                            "name": "qwen",
                            "kind": "model",
                            "source": str(source_dir),
                            "mode": "directory",
                        }
                    ],
                }
            ]
        },
    )

    result = dispatch_plan_sync(plan, mode="direct", skip_preflight=True)

    assert result["ok"] is True
    acquisition = result["tasks"][0]["artifacts"][0]
    assert acquisition["status"] == "uploaded"
    assert not (destination / "extra.bin").exists()
    assert (destination / "config.json").read_text() == "{}"


def test_file_inventories_skip_symlinks_and_match_digests(tmp_path):
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("hello")
    (root / "sub" / "b.bin").write_bytes(b"\x00" * 11)
    (root / "link.txt").symlink_to(root / "a.txt")

    assert _local_file_inventory(root) == {
        "a.txt": 5,
        str(Path("sub") / "b.bin"): 11,
    }
    digests = _local_sha256_inventory(root)
    assert digests["a.txt"] == hashlib.sha256(b"hello").hexdigest()
    assert set(digests) == {"a.txt", str(Path("sub")) + "/b.bin"}
    assert "link.txt" not in digests


def test_dry_run_previews_target_model_acquisition_order(tmp_path):
    plan = plan_tasks(
        {"nodes": [node_doc("local", "127.0.0.1", [0])]},
        {
            "tasks": [
                {
                    "id": "train",
                    "command": "true",
                    "artifacts": [
                        {
                            "name": "qwen",
                            "kind": "model",
                            "source": "modelscope:Qwen/Qwen2.5-0.5B-Instruct",
                            "size_bytes": "1GiB",
                        }
                    ],
                }
            ]
        },
    )
    result = dispatch_plan_sync(plan, dry_run=True, artifact_cache_dir=str(tmp_path))

    preview = result["tasks"][0]["artifacts"][0]
    assert preview["status"] == "planned"
    assert preview["resolved_source"] is None
    assert preview["acquisition_order"] == ["target-local", "modelscope-direct"]


def test_dispatch_reports_modelscope_download_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    plan = plan_tasks(
        {"nodes": [node_doc("local", "127.0.0.1", [0])]},
        {
            "tasks": [
                {
                    "id": "train",
                    "command": "true",
                    "artifacts": [
                        {
                            "name": "qwen",
                            "kind": "model",
                            "source": "modelscope:Qwen/Qwen2.5-0.5B-Instruct",
                            "size_bytes": "1GiB",
                        }
                    ],
                }
            ]
        },
    )

    result = dispatch_plan_sync(
        plan,
        mode="direct",
        skip_preflight=True,
        artifact_cache_dir=str(tmp_path / "cache"),
    )

    task = result["tasks"][0]
    assert task["status"] == "failed"
    assert task["results"] == []
    assert task["artifacts"][0]["success"] is False
    assert task["artifacts"][0]["node_alias"] == "local"
    assert "modelscope" in task["artifacts"][0]["error"]


def test_local_artifact_sync_copies_model_and_data():
    model_source = SKILL_ROOT / "assets/examples/cnn_smoke/train.py"
    data_source = SKILL_ROOT / "assets/examples/cnn_smoke"
    destination_root = REPOSITORY_ROOT / ".adaptive-computation" / f"artifact-sync-test-{uuid.uuid4().hex}"
    if destination_root.exists():
        shutil.rmtree(destination_root)
    node = NodeConfig(alias="local", address="127.0.0.1", user="u")
    try:
        model_result = asyncio.run(
            sync_artifact_to_node(
                node,
                {
                    "name": "weights",
                    "kind": "model",
                    "source": str(model_source),
                    "destination": str(destination_root / "weights.py"),
                    "mode": "file",
                    "sha256": hashlib.sha256(model_source.read_bytes()).hexdigest(),
                    "timeout_s": 60,
                },
                {},
            )
        )
        data_result = asyncio.run(
            sync_artifact_to_node(
                node,
                {
                    "name": "dataset",
                    "kind": "data",
                    "source": str(data_source),
                    "destination": str(destination_root / "dataset"),
                    "mode": "directory",
                    "timeout_s": 60,
                },
                {},
            )
        )
        assert model_result["success"] is True
        assert data_result["success"] is True
        assert (destination_root / "weights.py").read_bytes() == model_source.read_bytes()
        assert (destination_root / "dataset" / "train.py").is_file()
    finally:
        if destination_root.exists():
            shutil.rmtree(destination_root)


def test_dispatch_syncs_source_tree_before_local_workload():
    source = SKILL_ROOT / "assets/examples/cnn_smoke"
    work_dir = (
        REPOSITORY_ROOT
        / ".adaptive-computation"
        / f"dispatch-source-test-{uuid.uuid4().hex}"
    )
    node = node_doc("local", "127.0.0.1", [0])
    try:
        plan = plan_tasks(
            {"nodes": [node]},
            {
                "tasks": [
                    {
                        "id": "train",
                        "command": (
                            "test -f {work_dir}/projects/cnn_smoke/train.py && "
                            "test -f {work_dir}/projects/cnn_smoke/uv.lock"
                        ),
                        "work_dir": str(work_dir),
                        "artifacts": [
                            {
                                "name": "cnn_smoke",
                                "kind": "source",
                                "source": str(source),
                            }
                        ],
                    }
                ]
            },
            source_root=REPOSITORY_ROOT,
        )
        result = dispatch_plan_sync(plan, mode="direct", skip_preflight=True)

        assert result["ok"] is True
        upload = result["tasks"][0]["artifacts"][0]
        assert upload["kind"] == "source"
        assert upload["status"] == "uploaded"
        assert (work_dir / "projects" / "cnn_smoke" / "train.py").is_file()
    finally:
        if work_dir.exists():
            shutil.rmtree(work_dir)


def test_dispatch_syncs_artifact_before_local_workload():
    source = SKILL_ROOT / "assets/examples/cnn_smoke/train.py"
    work_dir = REPOSITORY_ROOT / ".adaptive-computation" / f"dispatch-artifact-test-{uuid.uuid4().hex}"
    node = node_doc("local", "127.0.0.1", [0])
    try:
        plan = plan_tasks(
            {"nodes": [node]},
            {
                "tasks": [
                    {
                        "id": "train",
                        "command": "test -f {work_dir}/models/model.py",
                        "work_dir": str(work_dir),
                        "artifacts": [
                            {
                                "name": "model",
                                "kind": "model",
                                "source": str(source),
                                "destination": "{work_dir}/models/model.py",
                            }
                        ],
                    }
                ]
            },
            source_root=REPOSITORY_ROOT,
        )
        result = dispatch_plan_sync(plan, mode="direct", skip_preflight=True)

        assert result["ok"] is True
        assert result["tasks"][0]["artifacts"][0]["success"] is True
    finally:
        if work_dir.exists():
            shutil.rmtree(work_dir)


def test_dry_run_expands_per_assignment_tokens_without_ssh():
    plan = {
        "generated_at": "now",
        "nodes": {
            "n": {
                "alias": "n",
                "ssh": {"address": "localhost", "user": "u"},
                "configured": {},
            }
        },
        "tasks": [
            {
                "id": "x",
                "command": "echo {node_alias} {gpu_index} {disk_path} {rank}/{world_size}",
                "distribution": "one-to-one",
                "assignments": [
                    {
                        "node_alias": "n",
                        "gpu_indices": [2],
                        "storage_path": "/scratch",
                        "gpus": [{"disk_path": "/scratch"}],
                    }
                ],
            }
        ],
    }
    result = dispatch_plan_sync(plan, dry_run=True)
    assert result["tasks"][0]["results"][0]["command"] == "echo n 2 /scratch 0/1"


def test_planner_defaults_to_largest_non_root_data_disk():
    node = node_doc("n", "10.0.0.1", [0])
    node["hardware"]["disks"] = [
        {"path": "/", "total_bytes": 500 * 1024**3, "available_bytes": 400 * 1024**3},
        {"path": "/home/data", "total_bytes": 4 * 1024**4, "available_bytes": 100 * 1024**3},
    ]
    plan = plan_tasks({"nodes": [node]}, {"tasks": [{"id": "x", "command": "true"}]})
    assert plan["tasks"][0]["assignments"][0]["storage_path"] == "/home/data"


def test_planner_assigns_per_node_adaptive_work_dir():
    node = node_doc("n", "10.0.0.1", [0])
    node["ssh"]["user"] = "user01"
    node["hardware"]["disks"] = [
        {"path": "/", "total_bytes": 500 * 1024**3, "available_bytes": 400 * 1024**3},
        {"path": "/mnt/data", "total_bytes": 4 * 1024**4, "available_bytes": 3 * 1024**4},
    ]
    plan = plan_tasks({"nodes": [node]}, {"tasks": [{"id": "x", "command": "pwd"}]})
    group = plan["tasks"][0]["assignments"][0]
    assert group["storage_path"] == "/mnt/data"
    assert group["work_dir"] == "/mnt/data/data_user01/adaptive"


def test_planner_rejects_explicitly_missing_disk_path():
    node = node_doc("n", "10.0.0.1", [0])
    node["hardware"]["disks"] = [{"path": "/", "available_bytes": 10 * 1024**3}]
    node["errors"] = ["/scratch: path does not exist or is not readable"]
    with pytest.raises(PlanningError, match="disk path"):
        plan_tasks(
            {"nodes": [node]},
            {"tasks": [{"id": "x", "command": "true", "disk_path": "/scratch"}]},
        )


def test_planner_rejects_missing_uv_for_uv_task():
    node = node_doc("n", "10.0.0.1", [0])
    node["software"]["uv"]["installed"] = False
    with pytest.raises(PlanningError, match="environment"):
        plan_tasks(
            {"nodes": [node]},
            {"tasks": [{"id": "x", "command": "uv run true", "uv_env": "uv"}]},
        )


def test_planner_spills_across_nodes_and_reserves_gpus():
    inventory = {"generated_at": "now", "nodes": [node_doc("a", "10.0.0.1", [0, 1]), node_doc("b", "10.0.0.2", [0]) ]}
    manifest = {"tasks": [{"id": "train", "command": "python train.py --rank {rank}", "gpus": 3, "distribution": "one-to-one", "gpu_type": "A100", "disk_path": "/scratch", "uv_env": "uv"}]}
    plan = plan_tasks(inventory, manifest)
    assert plan["summary"] == {"task_count": 1, "gpu_count": 3, "node_count": 2}
    assert [(group["node_alias"], group["gpu_indices"]) for group in plan["tasks"][0]["assignments"]] == [("a", [0, 1]), ("b", [0])]


def test_planner_switches_to_free_candidates_when_one_is_busy():
    inventory_nodes = [node_doc(alias, f"10.0.0.{index}", [0]) for index, alias in enumerate("abcde", 1)]
    inventory_nodes[0]["hardware"]["gpu"]["devices"][0]["available"] = False
    inventory_nodes[0]["hardware"]["gpu"]["devices"][0]["process_count"] = 1
    plan = plan_tasks(
        {"generated_at": "now", "nodes": inventory_nodes},
        {
            "defaults": {"nodes": ["a", "b", "c", "d", "e"]},
            "tasks": [
                {
                    "id": "train",
                    "command": "python train.py --rank {rank}",
                    "gpus": 4,
                    "distribution": "one-to-one",
                    "min_nodes": 4,
                    "max_nodes": 4,
                }
            ],
        },
    )
    selected = [group["node_alias"] for group in plan["tasks"][0]["assignments"]]
    assert selected == ["b", "c", "d", "e"]
    assert "a" not in selected


def test_planner_respects_explicit_gpu_allocations():
    inventory = {"nodes": [node_doc("a", "10.0.0.1", [0, 1])]}
    manifest = {"tasks": [{"id": "x", "command": "true", "gpus": 1}]}
    plan = plan_tasks(
        inventory,
        manifest,
        allocations=[{"node_alias": "a", "gpu_index": 0, "target": "alice"}],
        current_user="bob",
    )
    assert plan["tasks"][0]["assignments"][0]["gpu_indices"] == [1]
    assert plan["tasks"][0]["assignments"][0]["gpus"][0]["allocation_target"] is None

    own = plan_tasks(
        inventory,
        manifest,
        allocations=[{"node_alias": "a", "gpu_index": 0, "target": "alice"}],
        current_user="alice",
    )
    assert own["tasks"][0]["assignments"][0]["gpu_indices"] == [0]

    with pytest.raises(PlanningError, match="no allocation"):
        plan_tasks(inventory, manifest, allocations=[], current_user="alice", require_allocation=True)


def test_planner_ignores_expired_allocations():
    inventory = {"nodes": [node_doc("a", "10.0.0.1", [0])]}
    plan = plan_tasks(
        inventory,
        {"tasks": [{"id": "x", "command": "true", "gpus": 1}]},
        allocations=[
            {
                "node_alias": "a",
                "gpu_index": 0,
                "target": "alice",
                "expires_at": "2000-01-01T00:00:00Z",
            }
        ],
        current_user="bob",
    )
    assert plan["tasks"][0]["assignments"][0]["gpu_indices"] == [0]


def test_planner_rejects_stale_node():
    inventory = {"nodes": [node_doc("a", "10.0.0.1", [0], stale=True)]}
    with pytest.raises(PlanningError, match="stale"):
        plan_tasks(inventory, {"tasks": [{"id": "x", "command": "true", "gpus": 1}]})


def test_dependency_order_and_cycle_validation():
    tasks = [
        {"id": "b", "depends_on": ["a"]},
        {"id": "a", "depends_on": []},
    ]
    assert topological_order(tasks) == ["a", "b"]
    with pytest.raises(PlanningError, match="cycle"):
        topological_order([{"id": "a", "depends_on": ["b"]}, {"id": "b", "depends_on": ["a"]}])


def test_parse_size_uses_bytes_for_explicit_byte_fields():
    assert parse_size("2GiB") == 2 * 1024**3
    assert parse_size(2048, default_unit="b") == 2048


def test_planner_allows_unverified_environment_with_explicit_repair_recipe():
    node = node_doc("n", "10.0.0.1", [0])
    node["software"]["uv"]["installed"] = False
    manifest = {
        "tasks": [
            {
                "id": "repair",
                "command": "repair",
                "gpus": 1,
                "environment_maintenance": True,
            },
            {
                "id": "train",
                "command": "train",
                "gpus": 1,
                "uv_env": "uv",
                "environment_check": "check {work_dir}",
                "environment_repair_task": "repair",
            },
        ]
    }
    plan = plan_tasks({"nodes": [node]}, manifest)
    train = next(task for task in plan["tasks"] if task["id"] == "train")
    assert train["assignments"][0]["environment_status"] == "unverified"
    assert train["environment_repair_task"] == "repair"


def test_environment_maintenance_task_does_not_reserve_workload_gpu():
    node = node_doc("n", "10.0.0.1", [0, 1])
    plan = plan_tasks(
        {"nodes": [node]},
        {
            "tasks": [
                {
                    "id": "repair",
                    "command": "repair",
                    "gpus": 1,
                    "environment_maintenance": True,
                },
                {"id": "train", "command": "train", "gpus": 1},
            ]
        },
    )
    repair = next(task for task in plan["tasks"] if task["id"] == "repair")
    train = next(task for task in plan["tasks"] if task["id"] == "train")
    assert repair["assignments"][0]["gpu_indices"] == train["assignments"][0]["gpu_indices"]


def test_dispatch_returns_environment_mismatch_without_repair():
    plan = {
        "generated_at": "now",
        "nodes": {
            "n": {
                "alias": "n",
                "ssh": {"address": "localhost", "user": "u"},
                "configured": {},
            }
        },
        "order": ["train"],
        "tasks": [
            {
                "id": "train",
                "command": "work",
                "distribution": "one-to-one",
                "environment_check": "check",
                "environment_repair_command": "uv sync",
                "assignments": [{"node_alias": "n", "gpu_indices": [0], "gpus": [{}]}],
            },
        ],
    }
    calls = []

    async def fake_route(context):
        calls.append(context.command)
        return RemoteExecResult(1, "", "No module named demo", context.target.node_alias, context.command)

    with patch("opensmi.dispatcher.route_command_to_target", new=AsyncMock(side_effect=fake_route)):
        result = dispatch_plan_sync(plan, mode="direct", skip_preflight=True)

    assert result["ok"] is False
    task = result["tasks"][0]
    assert task["status"] == "failed"
    assert task["reason"] == "environment_mismatch"
    assert task["environment"]["suggested_repair"] == "uv sync"
    assert calls == ["check"]


def test_dispatch_skips_environment_maintenance_by_default():
    plan = {
        "generated_at": "now",
        "nodes": {
            "n": {
                "alias": "n",
                "ssh": {"address": "localhost", "user": "u"},
                "configured": {},
            }
        },
        "order": ["repair", "train"],
        "tasks": [
            {
                "id": "repair",
                "command": "repair",
                "environment_maintenance": True,
                "distribution": "one-to-one",
                "assignments": [{"node_alias": "n", "gpu_indices": [0], "gpus": [{}]}],
            },
            {
                "id": "train",
                "command": "work",
                "distribution": "one-to-one",
                "assignments": [{"node_alias": "n", "gpu_indices": [0], "gpus": [{}]}],
            },
        ],
    }
    result = dispatch_plan_sync(plan, dry_run=True)
    assert [task["task_id"] for task in result["tasks"]] == ["train"]


def _marker_model_source(tmp_path):
    source = tmp_path / "qwen-source"
    source.mkdir()
    (source / "config.json").write_text("{}")
    (source / "weights.bin").write_bytes(b"w" * 8)
    return source


def _dispatch_marker_plan(tmp_path, source, cache_dir):
    plan = plan_tasks(
        {"nodes": [node_doc("local", "127.0.0.1", [0])]},
        {
            "tasks": [
                {
                    "id": "train",
                    "command": "true",
                    "work_dir": str(tmp_path / "work"),
                    "artifacts": [
                        {
                            "name": "qwen",
                            "kind": "model",
                            "source": str(source),
                            "mode": "directory",
                        }
                    ],
                }
            ]
        },
    )
    return dispatch_plan_sync(
        plan, mode="direct", skip_preflight=True, artifact_cache_dir=str(cache_dir)
    )


def test_dispatch_verification_marker_hits_on_second_run(tmp_path):
    source = _marker_model_source(tmp_path)
    cache_dir = tmp_path / "cache"

    first = _dispatch_marker_plan(tmp_path, source, cache_dir)
    assert first["ok"] is True
    first_acquisition = first["tasks"][0]["artifacts"][0]
    assert first_acquisition["status"] == "uploaded"
    assert "verify_cache" not in first_acquisition
    assert len(list((cache_dir / "verified").glob("*.json"))) == 1

    second = _dispatch_marker_plan(tmp_path, source, cache_dir)
    assert second["ok"] is True
    acquisition = second["tasks"][0]["artifacts"][0]
    assert acquisition["status"] == "available"
    assert acquisition["verify_cache"] == "hit"
    assert (tmp_path / "work" / "models" / "qwen" / "config.json").read_text() == "{}"


def test_source_edit_invalidates_verification_marker(tmp_path):
    source = _marker_model_source(tmp_path)
    cache_dir = tmp_path / "cache"

    first = _dispatch_marker_plan(tmp_path, source, cache_dir)
    assert first["ok"] is True

    (source / "config.json").write_text('{"updated": true}\n')
    second = _dispatch_marker_plan(tmp_path, source, cache_dir)
    acquisition = second["tasks"][0]["artifacts"][0]
    assert acquisition["status"] == "uploaded"
    assert "verify_cache" not in acquisition
    destination = tmp_path / "work" / "models" / "qwen" / "config.json"
    assert destination.read_text() == '{"updated": true}\n'

    third = _dispatch_marker_plan(tmp_path, source, cache_dir)
    assert third["tasks"][0]["artifacts"][0]["verify_cache"] == "hit"


def test_model_target_available_records_and_reuses_marker(tmp_path):
    work_dir = tmp_path / "work"
    destination = work_dir / "models" / "qwen"
    destination.mkdir(parents=True)
    (destination / "config.json").write_text("{}")
    cache_dir = tmp_path / "cache"
    plan = plan_tasks(
        {"nodes": [node_doc("local", "127.0.0.1", [0])]},
        {
            "tasks": [
                {
                    "id": "train",
                    "command": "true",
                    "work_dir": str(work_dir),
                    "artifacts": [
                        {
                            "name": "qwen",
                            "kind": "model",
                            "source": "modelscope:Qwen/Qwen2.5-0.5B-Instruct",
                            "required_files": ["config.json"],
                            "size_bytes": "1GiB",
                        }
                    ],
                }
            ]
        },
    )

    first = dispatch_plan_sync(
        plan, mode="direct", skip_preflight=True, artifact_cache_dir=str(cache_dir)
    )
    assert first["tasks"][0]["artifacts"][0]["status"] == "available"
    assert "verify_cache" not in first["tasks"][0]["artifacts"][0]

    second = dispatch_plan_sync(
        plan, mode="direct", skip_preflight=True, artifact_cache_dir=str(cache_dir)
    )
    acquisition = second["tasks"][0]["artifacts"][0]
    assert acquisition["status"] == "available"
    assert acquisition["verify_cache"] == "hit"
    assert acquisition["verified"] == "size-cap"


def test_corrupted_verification_marker_falls_back_to_full_verification(tmp_path):
    source = _marker_model_source(tmp_path)
    cache_dir = tmp_path / "cache"

    first = _dispatch_marker_plan(tmp_path, source, cache_dir)
    assert first["tasks"][0]["artifacts"][0]["status"] == "uploaded"
    marker_path = next((cache_dir / "verified").glob("*.json"))
    marker_path.write_text("{ truncated")

    second = _dispatch_marker_plan(tmp_path, source, cache_dir)
    acquisition = second["tasks"][0]["artifacts"][0]
    assert acquisition["status"] == "available"
    assert "verify_cache" not in acquisition
    repaired = json.loads(marker_path.read_text())
    assert repaired["schema_version"] == 1
    assert repaired["mode"] == "directory"
    assert repaired["kind"] == "model"

    third = _dispatch_marker_plan(tmp_path, source, cache_dir)
    assert third["tasks"][0]["artifacts"][0]["verify_cache"] == "hit"


def test_planner_rejects_new_non_uv_environment():
    node = node_doc("local", "127.0.0.1", [0])

    with pytest.raises(PlanningError, match="read-only legacy"):
        plan_tasks(
            {"nodes": [node]},
            {
                "tasks": [
                    {
                        "id": "train",
                        "command": "true",
                        "env_manager": "conda",
                        "env_name": "old",
                    }
                ]
            },
            source_root=REPOSITORY_ROOT,
        )


def test_planner_allows_recorded_legacy_environment():
    node = node_doc("local", "127.0.0.1", [0])
    node["software"]["environments"] = [
        {
            "manager": "conda",
            "name": "old",
            "path": "/opt/conda/envs/old",
            "version": "conda 24.1.0",
        }
    ]

    plan = plan_tasks(
        {"nodes": [node]},
        {
            "tasks": [
                {
                    "id": "train",
                    "command": "true",
                    "env_manager": "conda",
                    "env_name": "old",
                }
            ]
        },
        source_root=REPOSITORY_ROOT,
    )

    assert plan["tasks"][0]["assignments"]
