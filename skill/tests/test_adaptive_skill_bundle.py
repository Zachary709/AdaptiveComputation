from __future__ import annotations

import os
import subprocess
import sys
import zipapp
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = ROOT / "skill"
RUNNER = SKILL_DIR / "scripts" / "adaptive-runner"
SERVICE_ROOT = ROOT / "service" / "adaptive_service"


def test_bundled_runner_does_not_require_ambient_opensmi(tmp_path: Path) -> None:
    ambient = tmp_path / "ambient"
    ambient_package = ambient / "opensmi"
    ambient_package.mkdir(parents=True)
    (ambient_package / "__init__.py").write_text('__version__ = "ambient"\n')
    (ambient_package / "cli.py").write_text(
        'raise SystemExit("ambient package was imported")\n'
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ambient)
    result = subprocess.run(
        [sys.executable, str(RUNNER), "--version"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "adaptive-runner 0.2.0"


def test_zipapp_runner_loads_shared_core(tmp_path: Path) -> None:
    archive = tmp_path / "adaptive-runner.pyz"
    zipapp.create_archive(SKILL_DIR, target=archive, main="opensmi.cli:main")
    result = subprocess.run(
        [sys.executable, str(archive), "--version"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "adaptive-runner 0.2.0"


def test_vendored_core_matches_service_core() -> None:
    service_core = ROOT / "service" / "adaptive_core"
    vendored = SKILL_DIR / "adaptive_core"
    for path in service_core.glob("*.py"):
        copy = vendored / path.name
        assert copy.is_file(), copy
        assert copy.read_bytes() == path.read_bytes(), copy


def test_service_does_not_import_skill_runtime() -> None:
    for path in SERVICE_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "from opensmi" not in text, path
        assert "import opensmi" not in text, path
