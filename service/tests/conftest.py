import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SERVICE_ROOT = ROOT / "service"
os.environ.setdefault("ADAPTIVE_STATE_DIR", str(ROOT / ".work" / "pytest-state"))
os.environ.setdefault(
    "ADAPTIVE_SERVICE_LOCAL", str(ROOT / ".work" / "pytest-service-local.json")
)
sys.path.insert(0, str(SERVICE_ROOT))
