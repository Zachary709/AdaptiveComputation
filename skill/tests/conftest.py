import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / "skill"
os.environ.setdefault("ADAPTIVE_STATE_DIR", str(ROOT / ".work" / "pytest-state"))
sys.path.insert(0, str(SKILL_ROOT))

from adaptive_core import sshutil as _core_sshutil

import opensmi

# 历史测试按旧路径打补丁；共享 SSH 模块只注册这一个测试别名。
sys.modules.setdefault("opensmi.sshutil", _core_sshutil)
setattr(opensmi, "sshutil", _core_sshutil)
