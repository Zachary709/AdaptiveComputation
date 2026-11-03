import json

import pytest

from opensmi.registration import (
    RegistrationError,
    build_probe_script,
    expected_kind_root,
    parse_probe_output,
    validate_directory,
    validate_work_path,
)


def test_expected_kind_root_is_fixed_under_work_path():
    assert expected_kind_root("/data/u/adaptive", "data") == "/data/u/adaptive/data"
    assert (
        expected_kind_root("/data/u/adaptive/", "model")
        == "/data/u/adaptive/models"
    )
    with pytest.raises(RegistrationError):
        expected_kind_root("", "data")


def test_work_path_requires_existing_writable_directory():
    validate_work_path(
        "/data/u/adaptive", {"exists": True, "is_dir": True, "writable": True}
    )
    for report in (
        {"exists": False},
        {"exists": True, "is_dir": False},
        {"exists": True, "is_dir": True, "writable": False},
    ):
        with pytest.raises(RegistrationError):
            validate_work_path("/data/u/adaptive", report)


def test_data_entries_need_files():
    validate_directory(
        "data",
        "/data/u/adaptive/data",
        {
            "exists": True,
            "is_dir": True,
            "entries": [{"name": "toolace", "has_data": True}],
        },
    )
    with pytest.raises(RegistrationError, match="toolace"):
        validate_directory(
            "data",
            "/data/u/adaptive/data",
            {
                "exists": True,
                "is_dir": True,
                "entries": [{"name": "toolace", "has_data": False}],
            },
        )


def test_model_entries_need_complete_weight_files():
    validate_directory(
        "model",
        "/data/u/adaptive/models",
        {"exists": True, "is_dir": True, "entries": []},
    )
    complete = {
        "name": "Qwen2.5-7B-Instruct",
        "weights": [f"model-0000{index}-of-00004.safetensors" for index in range(1, 5)],
    }
    validate_directory(
        "model",
        "/data/u/adaptive/models",
        {"exists": True, "is_dir": True, "entries": [complete]},
    )
    with pytest.raises(RegistrationError, match="没有权重文件"):
        validate_directory(
            "model",
            "/data/u/adaptive/models",
            {
                "exists": True,
                "is_dir": True,
                "entries": [{"name": "qwen", "weights": []}],
            },
        )
    missing = {
        "name": "Qwen2.5-7B-Instruct",
        "weights": [
            "model-00001-of-00004.safetensors",
            "model-00003-of-00004.safetensors",
            "model-00004-of-00004.safetensors",
        ],
    }
    with pytest.raises(RegistrationError, match=r"缺少分片 \[2\]"):
        validate_directory(
            "model",
            "/data/u/adaptive/models",
            {"exists": True, "is_dir": True, "entries": [missing]},
        )


def test_probe_output_and_script_are_usable():
    report = parse_probe_output(
        json.dumps({"exists": True, "is_dir": True, "entries": []}), ""
    )
    assert report["exists"] is True
    with pytest.raises(RegistrationError):
        parse_probe_output("not json", "")
    script = build_probe_script("data", "/data/u/adaptive data")
    assert "'/data/u/adaptive data'" in script
