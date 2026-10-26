"""Run local backend commands from the repository root with root .env settings."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

root = Path(__file__).resolve().parents[1]
load_dotenv(root / ".env", override=False)
data_root = Path(os.environ.get("RF_DATA_ROOT", "./data"))
if not data_root.is_absolute():
    os.environ["RF_DATA_ROOT"] = str((root / data_root).resolve())
model_root = Path(os.environ.get("RF_MODEL_ROOT", "./models"))
if not model_root.is_absolute():
    os.environ["RF_MODEL_ROOT"] = str((root / model_root).resolve())
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "command",
    choices=[
        "api",
        "migrate",
        "current",
        "check",
        "revision",
        "test",
        "lint",
        "scan",
        "segment",
        "quality",
        "jobs",
        "rerun",
        "worker",
        "admin",
        "infer",
    ],
)
args, extra = parser.parse_known_args()
commands = {
    "api": [
        "uvicorn",
        "app.main:create_app",
        "--factory",
        "--host",
        "127.0.0.1",
        "--port",
        os.getenv("API_PORT", "8000"),
    ],
    "migrate": ["alembic", "upgrade", "head"],
    "current": ["alembic", "current"],
    "check": ["alembic", "check"],
    "revision": ["alembic", "revision"],
    "scan": ["app.cli", "scan"],
    "segment": ["app.cli", "segment"],
    "quality": ["app.cli", "quality"],
    "jobs": ["app.cli", "jobs"],
    "rerun": ["app.cli", "rerun"],
    "worker": ["app.workers.runner"],
    "admin": ["app.admin"],
    "infer": ["app.cli", "infer"],
    "test": ["pytest", "-q"],
    "lint": ["ruff", "check", ".", "../scripts"],
}
raise SystemExit(
    subprocess.call(
        [sys.executable, "-m", *commands[args.command], *extra], cwd=root / "backend"
    )
)
