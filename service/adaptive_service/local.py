from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

ENV_LOCAL = "ADAPTIVE_SERVICE_LOCAL"
LOCAL_NAME = "local.json"
SERVICE_ROOT = Path(__file__).resolve().parent.parent


class ServiceLocalError(ValueError):
    """服务本地配置无法读取或格式错误。"""


def local_path() -> Path:
    raw = os.environ.get(ENV_LOCAL)
    if raw:
        return Path(raw).expanduser().resolve()
    return (SERVICE_ROOT / LOCAL_NAME).resolve()


def load_default_plans() -> Optional[List[Dict[str, Any]]]:
    path = local_path()
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ServiceLocalError(f"{path} 不是有效的 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ServiceLocalError(f"{path} 的内容必须是 JSON 对象")
    plans = payload.get("default_plans")
    if plans is None:
        return None
    if not isinstance(plans, list) or not all(isinstance(item, dict) for item in plans):
        raise ServiceLocalError(f"{path} 的 default_plans 必须是对象数组")
    return plans
