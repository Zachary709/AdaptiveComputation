from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

ENV_STATE_DIR = "ADAPTIVE_STATE_DIR"
DEFAULT_STATE_DIRNAME = ".adaptive-computation"


def get_state_dir(state_dir: Optional[str] = None) -> Path:
    """Return the state dir.

    Default is ~/.adaptive-computation (intended to live on NFS home on the
    cluster). Override priority:
      1) CLI flag --state-dir
      2) env ADAPTIVE_STATE_DIR
      3) ~/.adaptive-computation
    """
    if state_dir:
        return Path(state_dir).expanduser().resolve()

    env = os.environ.get(ENV_STATE_DIR)
    if env:
        return Path(env).expanduser().resolve()

    return (Path.home() / DEFAULT_STATE_DIRNAME).resolve()


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f"{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass


_KST = timezone(timedelta(hours=9))


def now_kst_iso() -> str:
    """Return current KST time as ISO-8601 string (seconds precision)."""
    return datetime.now(_KST).isoformat(timespec="seconds")
