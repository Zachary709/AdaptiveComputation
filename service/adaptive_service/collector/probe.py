"""Hardware and runtime inventory for local and SSH-accessible GPU nodes.

The inventory collector deliberately has no agent/daemon dependency on the target
machine. It runs one small, read-only bash script locally or over SSH and stores a
local, JSON-serialisable snapshot. Private key contents are never read; OpenSSH
is responsible for using the key referenced by the user's SSH configuration.
"""

from __future__ import annotations

import asyncio
import base64
import glob
import ipaddress
import json
import math
import os
import re
import shlex
import socket
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from adaptive_core.processes import run_managed
from adaptive_core.models import NodeConfig, node_config_from_inventory_node
from adaptive_core.sshutil import SSHRunError, is_local_node, ssh_bash_script
from adaptive_core.state import now_kst_iso


INVENTORY_SCHEMA_VERSION = 1
INVENTORY_FILENAME = "hardware_inventory.json"
_BEGIN = "__OPENSMI_INVENTORY_BEGIN__"
_END = "__OPENSMI_INVENTORY_END__"
_PREFIX = "__OPENSMI_INVENTORY__"


# The script emits pipe-delimited records.  Human-readable fields are base64
# encoded so commas, spaces, newlines, and non-ASCII GPU names cannot corrupt
# the local parser.  It uses only tools normally present on Linux GPU nodes.
_REMOTE_INVENTORY_SCRIPT = r'''#!/usr/bin/env bash
set +e

# These assignments are replaced by build_remote_inventory_script().  They are
# shell-quoted there; they are not values read from a config file on the node.
STORAGE_PATHS_B64=__OPENSMI_STORAGE_PATHS_B64__
ENVIRONMENT_PATHS_B64=__OPENSMI_ENVIRONMENT_PATHS_B64__
BENCHMARK_COMMAND_B64=__OPENSMI_BENCHMARK_COMMAND_B64__

_b64() {
  printf '%s' "$1" | base64 2>/dev/null | tr -d '\n'
}
_b64dec() {
  printf '%s' "$1" | base64 -d 2>/dev/null || printf '%s' "$1" | base64 --decode 2>/dev/null
}
_trim() {
  printf '%s' "$1" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}
_expand_path() {
  case "$1" in
    '~') printf '%s' "$HOME" ;;
    '~/'*) printf '%s/%s' "$HOME" "${1#\~/}" ;;
    *) printf '%s' "$1" ;;
  esac
}
_emit() {
  printf '%s%s\n' "__OPENSMI_INVENTORY__" "$*"
}
_emit_b64() {
  # First argument is the record type; remaining arguments are already safe
  # numeric values or are encoded here by the caller-specific helpers.
  _emit "$*"
}

printf '%s\n' "__OPENSMI_INVENTORY_BEGIN__"

# Metadata
_hn=$(hostname -s 2>/dev/null || hostname 2>/dev/null || printf 'unknown')
_emit "meta|hostname|$(_b64 "$_hn")"
_mac=""
_iface=$(ip route show default 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i == "dev") { print $(i + 1); exit }}')
if [ -n "$_iface" ] && [ -r "/sys/class/net/$_iface/address" ]; then
  _mac=$(cat "/sys/class/net/$_iface/address" 2>/dev/null)
else
  for _dev in /sys/class/net/*; do
    _name=${_dev##*/}
    case "$_name" in
      lo|docker*|veth*|br-*|tailscale*|tun*|wg*|virbr*|dummy*) continue ;;
    esac
    [ -e "$_dev/device" ] || continue
    _mac=$(cat "$_dev/address" 2>/dev/null)
    [ -n "$_mac" ] && break
  done
fi
[ -n "$_mac" ] && _emit "meta|mac|$(_b64 "$_mac")"
if [ -f /etc/os-release ]; then
  . /etc/os-release
  _emit "meta|os|$(_b64 "${PRETTY_NAME:-${NAME:-unknown}}")"
else
  _emit "meta|os|$(_b64 'unknown')"
fi
_emit "meta|kernel|$(_b64 "$(uname -sr 2>/dev/null || printf 'unknown')")"
_emit "meta|arch|$(_b64 "$(uname -m 2>/dev/null || printf 'unknown')")"
_tmux_bin=$(command -v tmux 2>/dev/null || true)
if [ -z "$_tmux_bin" ]; then
  for _candidate in /usr/local/bin/tmux /usr/bin/tmux /bin/tmux; do
    if [ -x "$_candidate" ]; then
      _tmux_bin=$_candidate
      break
    fi
  done
fi
if [ -n "$_tmux_bin" ]; then
  _emit "meta|tmux_path|$(_b64 "$_tmux_bin")"
  _emit "meta|tmux|$(_b64 "$("$_tmux_bin" -V 2>/dev/null | head -1)")"
fi

# CPU and memory
_cpu_model=$(awk -F: '/model name|Model Name|Hardware/ {gsub(/^[ \t]+/, "", $2); print $2; exit}' /proc/cpuinfo 2>/dev/null)
[ -n "$_cpu_model" ] || _cpu_model=$(lscpu 2>/dev/null | sed -n 's/^Model name:[[:space:]]*//p' | head -1)
[ -n "$_cpu_model" ] || _cpu_model=unknown
_logical=$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || printf '0')
_physical=$(lscpu 2>/dev/null | awk -F: '/^Core\(s\) per socket:/ {gsub(/[ \t]/, "", $2); c=$2} /^Socket\(s\):/ {gsub(/[ \t]/, "", $2); s=$2} END {if(c && s) print c*s; else print 0}')
_mhz=$(awk -F: '/cpu MHz/ {gsub(/^[ \t]+/, "", $2); print $2; exit}' /proc/cpuinfo 2>/dev/null)
_load_1m=$(awk '{print $1; exit}' /proc/loadavg 2>/dev/null)
_emit "cpu|$(_b64 "$_cpu_model")|$_logical|$_physical|$_mhz|$_load_1m"
_mem_total=$(awk '/MemTotal:/ {printf "%.0f", $2*1024; exit}' /proc/meminfo 2>/dev/null)
_mem_available=$(awk '/MemAvailable:/ {printf "%.0f", $2*1024; exit}' /proc/meminfo 2>/dev/null)
_emit "memory|${_mem_total:-0}|${_mem_available:-0}"

# GPU inventory and current compute processes.  The first query uses fields
# supported by old and new nvidia-smi versions alike.
_gpu_query=$(nvidia-smi --query-gpu=index,uuid,name,memory.total,memory.used,utilization.gpu,utilization.memory,temperature.gpu,power.draw --format=csv,noheader,nounits 2>/dev/null)
_gpu_indices=""
if [ -n "$_gpu_query" ]; then
  while IFS=',' read -r _i _uuid _name _mt _mu _util _memutil _temp _power; do
    _i=$(_trim "$_i"); _uuid=$(_trim "$_uuid"); _name=$(_trim "$_name")
    _mt=$(_trim "$_mt"); _mu=$(_trim "$_mu"); _util=$(_trim "$_util")
    _memutil=$(_trim "$_memutil"); _temp=$(_trim "$_temp"); _power=$(_trim "$_power")
    case "$_i" in ''|*[!0-9]*) continue;; esac
    _emit "gpu|$_i|$(_b64 "$_uuid")|$(_b64 "$_name")|${_mt:-}|${_mu:-}|${_util:-}|${_memutil:-}|${_temp:-}|${_power:-}"
    _gpu_indices="${_gpu_indices}${_i}"$'\n'
  done <<EOF
$_gpu_query
EOF
fi

# Optional/driver fields are queried separately so an unsupported optional
# field never hides the basic GPU inventory.
_gpu_extra=$(nvidia-smi --query-gpu=index,compute_cap,clocks.sm,clocks.max.sm,power.limit,driver_version --format=csv,noheader,nounits 2>/dev/null)
if [ -n "$_gpu_extra" ]; then
  while IFS=',' read -r _i _cc _clock _maxclock _plimit _driver; do
    _i=$(_trim "$_i"); _cc=$(_trim "$_cc"); _clock=$(_trim "$_clock")
    _maxclock=$(_trim "$_maxclock"); _plimit=$(_trim "$_plimit"); _driver=$(_trim "$_driver")
    case "$_i" in ''|*[!0-9]*) continue;; esac
    _emit "gpu_extra|$_i|$(_b64 "$_cc")|${_clock:-}|${_maxclock:-}|${_plimit:-}|$(_b64 "$_driver")"
  done <<EOF
$_gpu_extra
EOF
fi
_driver=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader,nounits 2>/dev/null | head -1)
[ -n "$_driver" ] && _emit "meta|driver|$(_b64 "$(_trim "$_driver")")"
_cuda=$(nvidia-smi 2>/dev/null | sed -n 's/.*CUDA Version:[[:space:]]*\([^ |]*\).*/\1/p' | head -1)
[ -n "$_cuda" ] && _emit "meta|cuda|$(_b64 "$_cuda")"

_proc_query=$(nvidia-smi --query-compute-apps=gpu_uuid,pid,process_name,used_memory --format=csv,noheader,nounits 2>/dev/null)
if [ -n "$_proc_query" ]; then
  while IFS=',' read -r _uuid _pid _pname _pused; do
    _uuid=$(_trim "$_uuid"); _pid=$(_trim "$_pid"); _pname=$(_trim "$_pname"); _pused=$(_trim "$_pused")
    case "$_pid" in ''|*[!0-9]*) continue;; esac
    _puser=$(stat -c '%U' "/proc/$_pid" 2>/dev/null || printf 'unknown')
    _emit "proc|$(_b64 "$_uuid")|$_pid|$(_b64 "$_pname")|${_pused:-}|$(_b64 "$_puser")"
  done <<EOF
$_proc_query
EOF
fi

# Filesystem capacity.  df -P makes each filesystem one record; mount paths
# are encoded by the local parser and therefore do not need shell escaping.
_df_lines=$(df -P -k 2>/dev/null | awk 'NR > 1 && $1 !~ /^(tmpfs|devtmpfs|squashfs|overlay)$/ { mount=$6; for(i=7;i<=NF;i++) mount=mount " " $i; printf "%s|%s|%s|%s|%s\n", $2,$3,$4,$5,mount }')
if [ -n "$_df_lines" ]; then
  while IFS='|' read -r _total_k _used_k _avail_k _pct _mount; do
    _mount=$(_trim "$_mount")
    [ -n "$_mount" ] || continue
    _total=$(( ${_total_k:-0} * 1024 )); _used=$(( ${_used_k:-0} * 1024 )); _avail=$(( ${_avail_k:-0} * 1024 ))
    _pct=$(_trim "$_pct"); _pct=${_pct%%%}
    _emit "disk|auto|$(_b64 "$_mount")|$_total|$_used|$_avail|${_pct:-}"
  done <<EOF
$_df_lines
EOF
fi

# Explicit paths are useful for /scratch, /data, or a project worktree.  They
# are supplied as newline-delimited base64 and are probed with df as well.
_storage_paths=$(_b64dec "$STORAGE_PATHS_B64")
if [ -n "$_storage_paths" ]; then
  while IFS= read -r _path; do
    [ -n "$_path" ] || continue
    _path=$(_expand_path "$_path")
    _line=$(df -P -k -- "$_path" 2>/dev/null | tail -1 | awk '{printf "%s|%s|%s|%s", $2,$3,$4,$5}')
    if [ -n "$_line" ]; then
      IFS='|' read -r _total_k _used_k _avail_k _pct <<EOF
$_line
EOF
      _total=$(( ${_total_k:-0} * 1024 )); _used=$(( ${_used_k:-0} * 1024 )); _avail=$(( ${_avail_k:-0} * 1024 ))
      _pct=$(_trim "$_pct"); _pct=${_pct%%%}
      _emit "disk|explicit|$(_b64 "$_path")|$_total|$_used|$_avail|${_pct:-}"
    else
      _emit "disk_error|$(_b64 "$_path")|$(_b64 'path does not exist or is not readable')"
    fi
  done <<EOF
$_storage_paths
EOF
fi

# uv/venv/conda discovery is intentionally shallow.  It avoids recursively
# walking large datasets while still finding the common project layouts.
_env_paths=$(_b64dec "$ENVIRONMENT_PATHS_B64")
_uv_bin=$(command -v uv 2>/dev/null || true)
if [ -z "$_uv_bin" ]; then
  for _candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" "$HOME/.local/share/uv/uv" /usr/local/bin/uv; do
    if [ -x "$_candidate" ]; then
      _uv_bin=$_candidate
      break
    fi
  done
fi
if [ -z "$_uv_bin" ] && [ -n "$_storage_paths" ]; then
  while IFS= read -r _root; do
    [ -z "$_root" ] && continue
    for _candidate in "$_root"/data_*/adaptive/tools/uv; do
      if [ -x "$_candidate" ]; then
        _uv_bin=$_candidate
        break 2
      fi
    done
  done <<EOF
$_storage_paths
EOF
fi
if [ -n "$_uv_bin" ]; then
  _uv_version=$($_uv_bin --version 2>/dev/null | head -1)
  _emit "env|uv|$(_b64 "$_uv_bin")|$(_b64 "$HOME")|$(_b64 "$_uv_version")|$(_b64 '')"
  _uv_python=$($_uv_bin python list --only-installed 2>/dev/null | head -100)
  [ -n "$_uv_python" ] && _emit "uv_python|$(_b64 "$_uv_python")"
fi
_conda_bin=$(command -v conda 2>/dev/null || true)
if [ -n "$_conda_bin" ]; then
  _conda_version=$($_conda_bin --version 2>/dev/null | head -1)
  _emit "env|conda|$(_b64 "$_conda_bin")|$(_b64 "$HOME")|$(_b64 "$_conda_version")|$(_b64 '')"
fi

# Build a bounded list of roots.  Defaults cover conventional lab layouts;
# configured paths are appended and de-duplicated by the local parser.
_roots="$HOME
$HOME/projects
$HOME/work
$HOME/workspace
$_env_paths"
while IFS= read -r _root; do
  [ -n "$_root" ] || continue
  _root=$(_expand_path "$_root")
  [ -d "$_root" ] || continue
  find "$_root" -maxdepth 4 -type d -name .venv -print 2>/dev/null | head -100 | while IFS= read -r _venv; do
    _py="$_venv/bin/python"; _version=''
    [ -x "$_py" ] && _version=$($_py --version 2>&1 | head -1)
    _emit "env|venv|$(_b64 "$_py")|$(_b64 "$_venv")|$(_b64 "$_version")|$(_b64 "$_venv")"
  done
  find "$_root" -maxdepth 4 -type f -name uv.lock -print 2>/dev/null | head -100 | while IFS= read -r _lock; do
    _emit "uv_project|$(_b64 "${_lock%/uv.lock}")|$(_b64 "$_lock")"
  done
  find "$_root" -maxdepth 4 -type f -name pyproject.toml -print 2>/dev/null | head -100 | while IFS= read -r _project; do
    _emit "python_project|$(_b64 "${_project%/pyproject.toml}")|$(_b64 "$_project")"
  done
done <<EOF
$_roots
EOF

# Optional calibration command.  It must print OPENSMI_BENCHMARK_SCORE=<number>
# for each selected GPU.  We intentionally discard all other output.
_benchmark_command=$(_b64dec "$BENCHMARK_COMMAND_B64")
if [ -n "$_benchmark_command" ] && [ -n "$_gpu_indices" ]; then
  while IFS= read -r _i; do
    [ -n "$_i" ] || continue
    _out=$(CUDA_VISIBLE_DEVICES="$_i" bash -lc "$_benchmark_command" 2>/dev/null)
    _score=$(printf '%s\n' "$_out" | sed -n 's/^OPENSMI_BENCHMARK_SCORE[=:][[:space:]]*\([-+0-9.eE]*\).*$/\1/p' | tail -1)
    [ -n "$_score" ] && _emit "benchmark|$_i|$_score"
  done <<EOF
$_gpu_indices
EOF
fi

printf '%s\n' "__OPENSMI_INVENTORY_END__"
exit 0
'''


# Service domains are commonly present in ~/.ssh/config but are not compute
# devices.  This avoids accidentally probing GitHub/GitLab when importing all
# aliases. A user can still place such a host explicitly in adaptive.json.
_SERVICE_DOMAINS = {
    "github.com",
    "gitlab.com",
    "bitbucket.org",
    "sourceforge.net",
}
_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_IGNORED_ALIASES = frozenset({"win", "honor-win", "phone"})


def _b64_text(value: Any) -> str:
    return base64.b64encode(str(value or "").encode("utf-8")).decode("ascii")


def _decode_text(value: str) -> str:
    if not value:
        return ""
    try:
        # Missing padding is tolerated because some base64 implementations
        # omit it when emitting short values.
        padded = value + ("=" * (-len(value) % 4))
        return base64.b64decode(padded, validate=False).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def build_remote_inventory_script(
    *, storage_paths: Sequence[str] = (), environment_paths: Sequence[str] = (), benchmark_command: str = ""
) -> str:
    """Return the read-only script sent to a node.

    Paths and the optional benchmark command are encoded before being inserted
    into the script.  This prevents a path such as ``$(rm -rf ...)`` from being
    interpreted by the remote shell.  A benchmark is never run unless the
    caller explicitly supplies a command.
    """

    def encoded_lines(values: Sequence[str]) -> str:
        return _b64_text("\n".join(str(v) for v in values if str(v).strip()))

    replacements = {
        "__OPENSMI_STORAGE_PATHS_B64__": shlex.quote(encoded_lines(storage_paths)),
        "__OPENSMI_ENVIRONMENT_PATHS_B64__": shlex.quote(encoded_lines(environment_paths)),
        "__OPENSMI_BENCHMARK_COMMAND_B64__": shlex.quote(_b64_text(benchmark_command)),
    }
    script = _REMOTE_INVENTORY_SCRIPT
    for needle, replacement in replacements.items():
        script = script.replace(needle, replacement)
    return script


def _safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    text = str(value or "").strip().replace(",", "")
    if not text or text.lower() in {"n/a", "na", "unknown", "-"}:
        return default
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    text = str(value or "").strip().replace(",", "")
    if not text or text.lower() in {"n/a", "na", "unknown", "-"}:
        return default
    try:
        value = float(text)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _serial_float(value: Optional[float]) -> Optional[Union[float, int]]:
    if value is None:
        return None
    if value.is_integer():
        return int(value)
    return value


def _empty_node_document(node: NodeConfig) -> Dict[str, Any]:
    """Create a stable node shape even when SSH probing fails."""

    return {
        "alias": node.alias,
        "mac": None,
        "hostname": None,
        "channel": None,
        "connection": None,
        "candidates": [],
        "ssh": {
            "address": node.address,
            "user": node.user,
            "port": int(node.port),
            "connect_timeout_s": int(node.connect_timeout_s),
            # A path is not a secret.  The private key itself is never copied
            # into the document or opened by this module.
            "identity_file": str(Path(node.identityfile).expanduser()) if node.identityfile else None,
            "proxy_jump": node.proxyjump or None,
        },
        "reachable": False,
        "stale": False,
        "observed_at": None,
        "last_successful_at": None,
        "errors": [],
        "hardware": {
            "gpu": {
                "count": 0,
                "models": [],
                "devices": [],
                "available_count": 0,
                "available_indices": [],
                "compute_efficiency": {
                    "definition": "observed utilization; benchmark_score is optional",
                    "average_utilization_percent": None,
                    "benchmark_scores": {},
                },
            },
            "cpu": {
                "model": None,
                "logical_cores": None,
                "physical_cores": None,
                "observed_mhz": None,
                "load_1m": None,
            },
            "memory": {"total_bytes": None, "available_bytes": None},
            "disks": [],
            "available_disk_paths": [],
        },
        "software": {
            "os": None,
            "kernel": None,
            "architecture": None,
            "driver_version": None,
            "cuda_version": None,
            "tmux": {
                "installed": False,
                "path": None,
                "version": None,
            },
            "uv": {
                "installed": False,
                "path": None,
                "version": None,
                "python_list": None,
                "python_runtimes": [],
            },
            "environments": [],
            "projects": [],
        },
        "configured": {
            "env_manager": node.env_manager or None,
            "env_name": node.env_name or None,
            "work_dir": node.work_dir or None,
            "storage_paths": list(getattr(node, "storage_paths", []) or []),
            "environment_paths": list(getattr(node, "environment_paths", []) or []),
        },
        "availability": {"gpu_indices": [], "storage_paths": []},
    }


def _record_payload(line: str) -> Optional[List[str]]:
    if not line.startswith(_PREFIX):
        return None
    return line[len(_PREFIX) :].split("|")


def parse_inventory_output(node: NodeConfig, stdout: str) -> Dict[str, Any]:
    """Parse one remote inventory response into the public node schema."""

    lines = stdout.splitlines()
    try:
        begin = next(i for i, line in enumerate(lines) if line.strip() == _BEGIN)
        end = next(i for i, line in enumerate(lines) if line.strip() == _END and i > begin)
    except StopIteration as exc:
        raise ValueError("Unexpected remote output (missing inventory markers)") from exc

    doc = _empty_node_document(node)
    doc["reachable"] = True
    observed = now_kst_iso()
    doc["observed_at"] = observed
    doc["last_successful_at"] = observed

    gpu_by_index: Dict[int, Dict[str, Any]] = {}
    extra_by_index: Dict[int, Dict[str, Any]] = {}
    benchmark_by_index: Dict[int, float] = {}
    processes: List[Dict[str, Any]] = []
    disks: List[Dict[str, Any]] = []
    explicit_disk_errors: List[str] = []
    environments: List[Dict[str, Any]] = []
    projects: List[Dict[str, Any]] = []
    uv_python_list: Optional[str] = None

    for raw in lines[begin + 1 : end]:
        fields = _record_payload(raw.strip())
        if not fields or not fields[0]:
            continue
        kind = fields[0]

        if kind == "meta" and len(fields) >= 3:
            key, value = fields[1], _decode_text(fields[2])
            if key == "hostname":
                doc["hostname"] = value
            elif key == "mac":
                doc["mac"] = value.strip().lower() or None
            elif key == "os":
                doc["software"]["os"] = value
            elif key == "kernel":
                doc["software"]["kernel"] = value
            elif key == "arch":
                doc["software"]["architecture"] = value
            elif key == "driver":
                text = value.strip()
                if re.fullmatch(r"\d+(?:\.\d+)+", text):
                    doc["software"]["driver_version"] = text
            elif key == "cuda":
                doc["software"]["cuda_version"] = value
            elif key == "tmux":
                text = value.strip()
                doc["software"]["tmux"]["version"] = text or None
                doc["software"]["tmux"]["installed"] = bool(text)
            elif key == "tmux_path":
                doc["software"]["tmux"]["path"] = value or None
                doc["software"]["tmux"]["installed"] = True
            continue

        if kind == "cpu" and len(fields) >= 5:
            doc["hardware"]["cpu"] = {
                "model": _decode_text(fields[1]),
                "logical_cores": _safe_int(fields[2]),
                "physical_cores": _safe_int(fields[3]),
                "observed_mhz": _serial_float(_safe_float(fields[4])),
                "load_1m": _serial_float(_safe_float(fields[5])) if len(fields) >= 6 else None,
            }
            continue

        if kind == "memory" and len(fields) >= 3:
            doc["hardware"]["memory"] = {
                "total_bytes": _safe_int(fields[1]),
                "available_bytes": _safe_int(fields[2]),
            }
            continue

        if kind == "gpu" and len(fields) >= 5:
            index = _safe_int(fields[1])
            if index is None:
                continue
            # Keep parsing tolerant of older/custom collectors that emit only
            # index/uuid/name/memory.total.
            mem_total = _safe_int(fields[4] if len(fields) > 4 else "")
            mem_used = _safe_int(fields[5] if len(fields) > 5 else "")
            util = _safe_int(fields[6] if len(fields) > 6 else "")
            mem_util = _safe_int(fields[7] if len(fields) > 7 else "")
            temp = _safe_int(fields[8] if len(fields) > 8 else "")
            power = _safe_float(fields[9] if len(fields) > 9 else "")
            gpu_by_index[index] = {
                "index": index,
                "uuid": _decode_text(fields[2]),
                "name": _decode_text(fields[3]),
                "memory_total_mib": mem_total,
                "memory_used_mib": mem_used,
                "memory_free_mib": (mem_total - mem_used) if mem_total is not None and mem_used is not None else None,
                "utilization_gpu_percent": util,
                "utilization_memory_percent": mem_util,
                "temperature_c": temp,
                "power_draw_w": _serial_float(power),
            }
            continue

        if kind == "gpu_extra" and len(fields) >= 7:
            index = _safe_int(fields[1])
            if index is not None:
                extra_by_index[index] = {
                    "compute_capability": _decode_text(fields[2]) or None,
                    "sm_clock_mhz": _safe_int(fields[3]),
                    "max_sm_clock_mhz": _safe_int(fields[4]),
                    "power_limit_w": _serial_float(_safe_float(fields[5])),
                    "driver_version": _decode_text(fields[6]) or None,
                }
            continue

        if kind == "proc" and len(fields) >= 5:
            pid = _safe_int(fields[2])
            if pid is None:
                continue
            processes.append(
                {
                    "gpu_uuid": _decode_text(fields[1]),
                    "pid": pid,
                    "process_name": _decode_text(fields[3]),
                    "used_memory_mib": _safe_int(fields[4]),
                    "user": _decode_text(fields[5]) if len(fields) >= 6 else "unknown",
                }
            )
            continue

        if kind == "disk" and len(fields) >= 7:
            total = _safe_int(fields[3])
            used = _safe_int(fields[4])
            available = _safe_int(fields[5])
            percent = _safe_float(fields[6])
            disks.append(
                {
                    "path": _decode_text(fields[2]),
                    "source": fields[1] or "auto",
                    "total_bytes": total,
                    "used_bytes": used,
                    "available_bytes": available,
                    "used_percent": _serial_float(percent),
                }
            )
            continue

        if kind == "disk_error" and len(fields) >= 3:
            explicit_disk_errors.append(f"{_decode_text(fields[1])}: {_decode_text(fields[2])}")
            continue

        if kind == "env" and len(fields) >= 6:
            manager = fields[1]
            environment = {
                "manager": manager,
                "path": _decode_text(fields[2]),
                "name": _decode_text(fields[3]),
                "version": _decode_text(fields[4]),
                "project": _decode_text(fields[5]) or None,
            }
            environments.append(environment)
            if manager == "uv":
                uv = doc["software"]["uv"]
                uv["installed"] = True
                uv["path"] = environment["path"]
                uv["version"] = environment["version"]
            continue

        if kind == "uv_python" and len(fields) >= 2:
            uv_python_list = _decode_text(fields[1])
            continue

        if kind == "uv_project" and len(fields) >= 3:
            projects.append({"manager": "uv", "path": _decode_text(fields[1]), "lockfile": _decode_text(fields[2])})
            continue

        if kind == "python_project" and len(fields) >= 3:
            projects.append({"manager": "python", "path": _decode_text(fields[1]), "file": _decode_text(fields[2])})
            continue

        if kind == "benchmark" and len(fields) >= 3:
            index = _safe_int(fields[1])
            score = _safe_float(fields[2])
            if index is not None and score is not None:
                benchmark_by_index[index] = score
            continue

    proc_by_uuid: Dict[str, List[Dict[str, Any]]] = {}
    for proc in processes:
        proc_by_uuid.setdefault(str(proc.get("gpu_uuid") or ""), []).append(proc)

    devices: List[Dict[str, Any]] = []
    for index in sorted(gpu_by_index):
        gpu = dict(gpu_by_index[index])
        gpu.update(extra_by_index.get(index, {}))
        gpu_processes = proc_by_uuid.get(str(gpu.get("uuid") or ""), [])
        benchmark = benchmark_by_index.get(index)
        clock = _safe_float(gpu.get("sm_clock_mhz"))
        max_clock = _safe_float(gpu.get("max_sm_clock_mhz"))
        clock_ratio = (clock / max_clock * 100.0) if clock is not None and max_clock and max_clock > 0 else None
        util = gpu.get("utilization_gpu_percent")
        gpu["process_count"] = len(gpu_processes)
        gpu["processes"] = gpu_processes
        gpu["available"] = len(gpu_processes) == 0
        gpu["availability_reason"] = "active_process" if gpu_processes else None
        gpu["compute_efficiency"] = {
            "observed_utilization_percent": util,
            "benchmark_score": _serial_float(benchmark),
            "sm_clock_ratio_percent": _serial_float(clock_ratio),
        }
        devices.append(gpu)

    avg_utils = [float(g["utilization_gpu_percent"]) for g in devices if g.get("utilization_gpu_percent") is not None]
    gpu_efficiency = {
        "definition": "observed utilization; benchmark_score is optional",
        "average_utilization_percent": _serial_float(sum(avg_utils) / len(avg_utils)) if avg_utils else None,
        "benchmark_scores": {str(k): _serial_float(v) for k, v in sorted(benchmark_by_index.items())},
    }
    available_devices = [g for g in devices if g.get("available")]
    doc["hardware"]["gpu"] = {
        "count": len(devices),
        "models": sorted({str(g.get("name")) for g in devices if g.get("name")}),
        "devices": devices,
        "available_count": len(available_devices),
        "available_indices": [int(g["index"]) for g in available_devices],
        "compute_efficiency": gpu_efficiency,
    }
    doc["hardware"]["disks"] = _dedupe_disks(disks)
    doc["hardware"]["available_disk_paths"] = [
        d["path"] for d in doc["hardware"]["disks"] if d.get("available_bytes", 0) and int(d["available_bytes"]) > 0
    ]
    doc["availability"] = {
        "gpu_indices": [int(g["index"]) for g in available_devices],
        "storage_paths": [
            {"path": d["path"], "available_bytes": d.get("available_bytes")}
            for d in doc["hardware"]["disks"]
            if d.get("available_bytes", 0) and int(d["available_bytes"]) > 0
        ],
    }
    doc["software"]["environments"] = _dedupe_environment_records(environments)
    doc["software"]["projects"] = _dedupe_records(projects)
    doc["software"]["uv"]["python_list"] = uv_python_list
    doc["software"]["uv"]["python_runtimes"] = [
        line.strip() for line in (uv_python_list or "").splitlines() if line.strip()
    ]
    if explicit_disk_errors:
        doc["errors"].extend(explicit_disk_errors)
    doc["processes"] = processes
    return doc


def _dedupe_records(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        key = json.dumps(record, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            out.append(record)
            seen.add(key)
    return out


def _dedupe_environment_records(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # A uv installation and a discovered .venv can be reported repeatedly when
    # roots overlap.  Keep deterministic, unique entries.
    return _dedupe_records(records)


def _dedupe_disks(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Prefer the explicit probe for a path, since it is what a task requested.
    by_path: Dict[str, Dict[str, Any]] = {}
    for record in records:
        path = str(record.get("path") or "")
        if not path:
            continue
        old = by_path.get(path)
        if old is None or (record.get("source") == "explicit" and old.get("source") != "explicit"):
            by_path[path] = record
    return [by_path[path] for path in sorted(by_path)]


async def _run_inventory_script(node: NodeConfig, script: str, timeout_s: int, max_retries: int) -> Tuple[int, str, str]:
    if is_local_node(node):
        try:
            return await run_managed(["bash", "-s"], stdin_bytes=script.encode("utf-8"), timeout_s=timeout_s)
        except asyncio.TimeoutError as exc:
            raise SSHRunError(f"local inventory script timed out after {timeout_s}s; processes cleaned") from exc
    return await ssh_bash_script(node, script, timeout_s=timeout_s, max_retries=max_retries, managed=True)


async def collect_node_inventory(
    node: NodeConfig,
    *,
    timeout_s: int = 20,
    max_retries: int = 2,
    benchmark: bool = False,
    benchmark_command: str = "",
) -> Dict[str, Any]:
    """Collect one node.  Failures become data, so other nodes still refresh."""

    doc = _empty_node_document(node)
    try:
        configured_benchmark = benchmark_command or str(getattr(node, "benchmark_command", "") or "")
        if not benchmark:
            configured_benchmark = ""
        script = build_remote_inventory_script(
            storage_paths=list(getattr(node, "storage_paths", []) or []),
            environment_paths=list(getattr(node, "environment_paths", []) or []),
            benchmark_command=configured_benchmark,
        )
        rc, stdout, stderr = await _run_inventory_script(node, script, timeout_s, max_retries)
        if rc != 0:
            raise SSHRunError((stderr or stdout or f"remote inventory exited {rc}").strip())
        doc = parse_inventory_output(node, stdout)
        if stderr.strip():
            doc.setdefault("warnings", []).append(stderr.strip()[:1000])
    except Exception as exc:
        doc["errors"] = [str(exc)]
        doc["reachable"] = False
        doc["stale"] = False
    return doc


_TAILSCALE_NETWORK = ipaddress.ip_network("100.64.0.0/10")
_CHANNEL_PRIORITY = {"local": 0, "tailscale": 1, "frp": 2, "direct": 3}


def node_channel(node: NodeConfig) -> str:
    """Classify how an SSH entry reaches its device."""

    if is_local_node(node):
        return "local"
    try:
        if ipaddress.ip_address(node.address.strip()) in _TAILSCALE_NETWORK:
            return "tailscale"
    except ValueError:
        pass
    if node.alias.endswith("-frp"):
        return "frp"
    return "direct"


def _canonical_alias(items: Sequence[Tuple[NodeConfig, Dict[str, Any]]]) -> str:
    pairs = [(node.alias, node_channel(node)) for node, _ in items]
    for channel in ("tailscale", "direct", "frp", "local"):
        for alias, alias_channel in pairs:
            if alias_channel == channel and not alias.endswith("-frp"):
                return alias
    for alias, _ in pairs:
        if not alias.endswith("-frp"):
            return alias
    return pairs[0][0]


def _merge_device_group(
    items: Sequence[Tuple[NodeConfig, Dict[str, Any]]],
    known_alias: Optional[str],
    work_aliases: Optional[set] = None,
) -> Dict[str, Any]:
    ordered = [
        item
        for _, item in sorted(
            enumerate(items),
            key=lambda pair: (_CHANNEL_PRIORITY[node_channel(pair[1][0])], pair[0]),
        )
    ]
    chosen_node, chosen_doc = ordered[0]
    for candidate_node, candidate_doc in ordered:
        if candidate_doc.get("reachable"):
            chosen_node, chosen_doc = candidate_node, candidate_doc
            break

    aliases = {node.alias for node, _ in items}
    canonical_known = known_alias if known_alias in aliases else None
    if canonical_known and canonical_known.endswith("-frp"):
        if any(not alias.endswith("-frp") for alias in aliases):
            canonical_known = None
    canonical = canonical_known or _canonical_alias(items)
    if work_aliases:
        preferred = sorted(
            alias
            for alias in aliases
            if alias in work_aliases and not alias.endswith("-frp")
        )
        if preferred:
            canonical = preferred[0]
    channel = node_channel(chosen_node)
    merged = dict(chosen_doc)
    merged["alias"] = canonical
    merged["mac"] = next((str(doc.get("mac")) for _, doc in items if doc.get("mac")), None)
    merged["hostname"] = next(
        (str(doc.get("hostname")) for _, doc in items if doc.get("hostname")), None
    )
    merged["channel"] = channel
    merged["connection"] = {
        "channel": channel,
        "alias": chosen_node.alias,
        "address": chosen_node.address,
        "port": int(chosen_node.port),
        "reachable": bool(chosen_doc.get("reachable")),
    }
    merged["candidates"] = [
        {
            "alias": node.alias,
            "channel": node_channel(node),
            "address": node.address,
            "port": int(node.port),
            "reachable": bool(doc.get("reachable")),
            "error": (doc.get("errors") or [None])[0],
        }
        for node, doc in ordered
    ]
    return merged


def _merge_devices(
    nodes: Sequence[NodeConfig],
    fresh: Sequence[Dict[str, Any]],
    known_aliases: Optional[Mapping[str, str]] = None,
    known_macs: Optional[Mapping[str, str]] = None,
    work_aliases: Optional[set] = None,
) -> List[Dict[str, Any]]:
    aliases = {node.alias for node in nodes}
    entries: List[Tuple[NodeConfig, Dict[str, Any], List[str]]] = []
    for node, doc in zip(nodes, fresh):
        if not str(doc.get("mac") or "").strip():
            recorded_mac = str((known_macs or {}).get(node.alias) or "").strip().lower()
            if recorded_mac:
                doc = dict(doc)
                doc["mac"] = recorded_mac
        keys: List[str] = []
        mac = str(doc.get("mac") or "").strip().lower()
        if mac:
            keys.append(f"mac:{mac}")
        base = node.alias[:-4] if node.alias.endswith("-frp") else node.alias
        if f"{base}-frp" in aliases:
            keys.append(f"alias:{base}")
        if not keys:
            keys.append(
                f"endpoint:{node.address}:{node.port}:{node.user}:{node.identityfile}"
            )
        entries.append((node, doc, keys))

    parent: Dict[str, str] = {}

    def find(key: str) -> str:
        parent.setdefault(key, key)
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for _, _, keys in entries:
        for key in keys[1:]:
            union(keys[0], key)

    groups: Dict[str, List[Tuple[NodeConfig, Dict[str, Any]]]] = {}
    order: List[str] = []
    for node, doc, keys in entries:
        root = find(keys[0])
        if root not in groups:
            groups[root] = []
            order.append(root)
        groups[root].append((node, doc))

    merged: List[Dict[str, Any]] = []
    for root in order:
        items = groups[root]
        mac = next(
            (str(doc.get("mac") or "").strip().lower() for _, doc in items if doc.get("mac")),
            "",
        )
        merged.append(
            _merge_device_group(items, (known_aliases or {}).get(mac), work_aliases)
        )
    return merged


async def collect_inventory(
    nodes: Sequence[NodeConfig],
    *,
    timeout_s: int = 20,
    max_retries: int = 2,
    max_concurrency: int = 16,
    benchmark: bool = False,
    benchmark_command: str = "",
    previous: Optional[Dict[str, Any]] = None,
    ssh_config: Optional[str] = None,
    known_aliases: Optional[Mapping[str, str]] = None,
    known_macs: Optional[Mapping[str, str]] = None,
    work_aliases: Optional[set] = None,
) -> Dict[str, Any]:
    """Collect all nodes concurrently and return the complete local document."""

    nodes = [node for node in nodes if node.alias not in _IGNORED_ALIASES]
    limit = max(1, min(int(max_concurrency or 1), max(1, len(nodes))))
    semaphore = asyncio.Semaphore(limit)

    async def one(node: NodeConfig) -> Dict[str, Any]:
        async with semaphore:
            return await collect_node_inventory(
                node,
                timeout_s=timeout_s,
                max_retries=max_retries,
                benchmark=benchmark,
                benchmark_command=benchmark_command,
            )

    fresh = list(await asyncio.gather(*(one(node) for node in nodes)))
    device_docs = _merge_devices(
        nodes, fresh, known_aliases, known_macs, work_aliases
    )
    old_nodes = {
        str(item.get("alias")): item
        for item in (previous or {}).get("nodes", [])
        if isinstance(item, dict) and item.get("alias")
    }
    merged: List[Dict[str, Any]] = []
    for node in device_docs:
        if node.get("reachable"):
            merged.append(node)
            continue
        old = old_nodes.get(str(node.get("alias")))
        if old and old.get("hardware"):
            # Do not present stale data as free.  Keeping the last successful
            # hardware description makes the document useful for diagnosis,
            # while the planner excludes stale nodes by default.
            preserved = dict(node)
            preserved["hardware"] = old.get("hardware", preserved["hardware"])
            preserved["software"] = old.get("software", preserved["software"])
            preserved["last_successful_at"] = old.get("last_successful_at") or old.get("observed_at")
            preserved["stale"] = True
            preserved["availability"] = {"gpu_indices": [], "storage_paths": []}
            merged.append(preserved)
        else:
            merged.append(node)

    return {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "generated_at": now_kst_iso(),
        "collector": {
            "name": "adaptive inventory",
            "host": socket.gethostname(),
            "ssh_config": str(Path(ssh_config).expanduser()) if ssh_config else None,
            "benchmark_enabled": bool(
                benchmark
                and (
                    bool(benchmark_command)
                    or any(str(getattr(node, "benchmark_command", "") or "") for node in nodes)
                )
            ),
        },
        "nodes": merged,
    }


def _valid_alias(token: str) -> bool:
    return bool(token and not token.startswith("!") and "*" not in token and "?" not in token and _ALIAS_RE.match(token))


def _ssh_aliases(config_text: str) -> List[str]:
    aliases: List[str] = []
    seen: set[str] = set()
    for raw in config_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "host":
            continue
        for token in parts[1].split():
            if _valid_alias(token) and token not in seen:
                aliases.append(token)
                seen.add(token)
    return aliases


def _parse_ssh_g(stdout: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {"address": "", "user": "", "port": 22, "identity_file": "", "proxy_jump": ""}
    identity_files: List[str] = []
    for raw in stdout.splitlines():
        key, sep, value = raw.strip().partition(" ")
        if not sep or not value.strip():
            continue
        key = key.lower()
        value = value.strip()
        if key == "hostname":
            result["address"] = value
        elif key == "user":
            result["user"] = value
        elif key == "port":
            result["port"] = _safe_int(value, 22) or 22
        elif key == "identityfile" and value.lower() != "none":
            identity_files.append(str(Path(value).expanduser()))
        elif key == "proxyjump" and value.lower() != "none":
            result["proxy_jump"] = value
    if identity_files:
        result["identity_file"] = identity_files[0]
    return result


def _ssh_aliases_from_path(path: Path, seen_paths: Optional[set[str]] = None) -> List[str]:
    """Collect aliases from a config and its Include files (never key files)."""

    seen_paths = seen_paths or set()
    resolved = str(path.expanduser().resolve())
    if resolved in seen_paths or not Path(resolved).is_file():
        return []
    seen_paths.add(resolved)
    text = Path(resolved).read_text(encoding="utf-8", errors="replace")
    aliases = _ssh_aliases(text)
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "include":
            continue
        for pattern in shlex.split(parts[1]):
            include_pattern = os.path.expanduser(pattern)
            if not os.path.isabs(include_pattern):
                include_pattern = str(Path(resolved).parent / include_pattern)
            for include_path in sorted(glob.glob(include_pattern)):
                aliases.extend(_ssh_aliases_from_path(Path(include_path), seen_paths))
    out: List[str] = []
    seen_aliases: set[str] = set()
    for alias in aliases:
        if alias not in seen_aliases:
            out.append(alias)
            seen_aliases.add(alias)
    return out


def discover_ssh_nodes(
    ssh_config_path: Union[str, Path] = "~/.ssh/config", *, include_service_hosts: bool = False
) -> List[NodeConfig]:
    """Discover concrete Host entries using ``ssh -G``.

    The function reads SSH *configuration text* and invokes OpenSSH's effective
    config expansion.  It never opens an IdentityFile, and it skips wildcard
    hosts and well-known source-control service aliases by default.
    """

    path = Path(ssh_config_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"SSH config not found: {path}")
    aliases = _ssh_aliases_from_path(path)
    nodes: List[NodeConfig] = []
    seen: set[str] = set()
    for alias in aliases:
        if alias in _IGNORED_ALIASES:
            continue
        try:
            result = subprocess.run(
                ["ssh", "-G", "-F", str(path), alias],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode != 0:
            continue
        effective = _parse_ssh_g(result.stdout)
        address = str(effective.get("address") or "").strip()
        if not address or "*" in address or "?" in address:
            continue
        address_l = address.lower().rstrip(".")
        if not include_service_hosts and (
            address_l in _SERVICE_DOMAINS or any(address_l.endswith("." + d) for d in _SERVICE_DOMAINS)
        ):
            continue
        if alias in seen:
            continue
        user = str(effective.get("user") or os.environ.get("USER") or "root")
        nodes.append(
            NodeConfig(
                alias=alias,
                address=address,
                user=user,
                port=int(effective.get("port") or 22),
                identityfile=str(effective.get("identity_file") or ""),
                proxyjump=str(effective.get("proxy_jump") or ""),
            )
        )
        seen.add(alias)
    return nodes
