from __future__ import annotations

import json
import posixpath
import re
import shlex
from typing import Any, Dict, List, Mapping, Sequence

WEIGHT_SUFFIXES = (
    ".safetensors",
    ".bin",
    ".pt",
    ".pth",
    ".gguf",
    ".onnx",
    ".ckpt",
)

_SHARD_RE = re.compile(
    r"^(?P<stem>.+)-(?P<index>\d+)-of-(?P<total>\d+)\.(?P<ext>safetensors|bin|pt|pth|ckpt)$"
)


class RegistrationError(ValueError):
    pass


def expected_kind_root(work_path: str, kind: str) -> str:
    base = str(work_path or "").strip()
    if not base:
        raise RegistrationError("该节点尚未注册 work path，先注册 work path")
    if not base.startswith("/"):
        raise RegistrationError(f"work path 不是绝对路径: {base}")
    sub = "data" if kind == "data" else "models"
    return posixpath.normpath(posixpath.join(base, sub))


def validate_work_path(path: str, report: Mapping[str, Any]) -> None:
    if not report.get("exists"):
        raise RegistrationError(f"路径不存在: {path}")
    if not report.get("is_dir"):
        raise RegistrationError(f"路径不是目录: {path}")
    if not report.get("writable"):
        raise RegistrationError(f"路径不可写: {path}")


def validate_directory(kind: str, path: str, report: Mapping[str, Any]) -> None:
    if not report.get("exists"):
        raise RegistrationError(f"路径不存在: {path}")
    if not report.get("is_dir"):
        raise RegistrationError(f"路径不是目录: {path}")
    entries = report.get("entries")
    if not isinstance(entries, list):
        raise RegistrationError(f"路径内容无法读取: {path}")
    problems: List[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            problems.append("目录条目格式错误")
            continue
        name = str(entry.get("name") or "")
        if kind == "data":
            if not entry.get("has_data"):
                problems.append(f"{name}: 不是数据集目录，没有数据文件")
            continue
        weights = [str(item) for item in entry.get("weights") or []]
        problems.extend(_model_problems(name, weights))
    if problems:
        raise RegistrationError("注册校验失败:\n  " + "\n  ".join(problems))


def _model_problems(name: str, weights: Sequence[str]) -> List[str]:
    if not weights:
        return [f"{name}: 不是模型目录，没有权重文件"]
    groups: Dict[tuple, set] = {}
    for relative in weights:
        filename = posixpath.basename(relative)
        match = _SHARD_RE.match(filename)
        if match is None:
            continue
        key = (
            posixpath.dirname(relative),
            match.group("stem"),
            int(match.group("total")),
            match.group("ext"),
        )
        groups.setdefault(key, set()).add(int(match.group("index")))
    problems: List[str] = []
    for (directory, stem, total, extension), present in sorted(groups.items()):
        missing = sorted(set(range(1, total + 1)) - present)
        if missing:
            where = f"{name}/{directory}" if directory else name
            problems.append(
                f"{where}: {stem}-*-of-*.{extension} 缺少分片 {missing}"
            )
    return problems


def _probe_python() -> str:
    return f"""
import json
import os
import sys

WEIGHT_SUFFIXES = {WEIGHT_SUFFIXES!r}


def has_data(base):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [name for name in dirnames if name != "__pycache__"]
        for name in filenames:
            if not name.endswith(".pyc"):
                return True
    return False


def weights(base):
    found = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(name for name in dirnames if name != "__pycache__")
        for name in sorted(filenames):
            if name.endswith(WEIGHT_SUFFIXES):
                path = os.path.join(dirpath, name)
                found.append(os.path.relpath(path, base))
    return found


kind = sys.argv[1]
root = sys.argv[2]
report = {{"exists": os.path.exists(root), "is_dir": os.path.isdir(root)}}
if report["exists"] and report["is_dir"]:
    if kind == "work":
        report["writable"] = os.access(root, os.W_OK)
    else:
        entries = []
        for name in sorted(os.listdir(root)):
            base = os.path.join(root, name)
            if not os.path.isdir(base):
                continue
            if kind == "data":
                entries.append({{"name": name, "has_data": has_data(base)}})
            else:
                entries.append({{"name": name, "weights": weights(base)}})
        report["entries"] = entries
print(json.dumps(report, ensure_ascii=False))
"""


def build_probe_script(kind: str, path: str) -> str:
    return "\n".join(
        [
            "PYTHON=$(command -v python3 || command -v python)",
            'if [ -z "$PYTHON" ]; then echo "python3 not found" >&2; exit 127; fi',
            f'"$PYTHON" - {shlex.quote(kind)} {shlex.quote(path)} <<\'PY\'',
            _probe_python().strip(),
            "PY",
            "",
        ]
    )


def parse_probe_output(stdout: str, stderr: str) -> Dict[str, Any]:
    text = str(stdout or "").strip()
    if not text:
        raise RegistrationError(f"注册校验没有返回内容: {str(stderr or '').strip()}")
    try:
        report = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RegistrationError(f"注册校验返回内容无法解析: {text}") from exc
    if not isinstance(report, dict):
        raise RegistrationError(f"注册校验返回内容格式错误: {text}")
    return report
