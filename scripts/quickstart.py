"""Start the local MVP and initialize private demo identities without overwriting data."""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Also run a fresh synthetic end-to-end example",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if sys.version_info < (3, 11):  # noqa: UP036 - bootstrap runs before project requirements apply
        raise SystemExit("Python 3.11 or newer is required")
    if not shutil.which("docker"):
        raise SystemExit("Install and start Docker Desktop with its Linux engine first")

    def run(command):
        subprocess.run(list(map(str, command)), cwd=root, check=True)

    subprocess.run(
        ["docker", "info", "--format", "{{.OSType}}"],
        cwd=root,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    executable = (
        root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    if not executable.exists():
        run([sys.executable, "-m", "venv", root / ".venv"])
    lock = (root / "backend/requirements.lock").read_bytes() + (
        root / "backend/pyproject.toml"
    ).read_bytes()
    digest = hashlib.sha256(lock).hexdigest()
    stamp = root / ".venv/.rf-dependencies"
    if not stamp.exists() or stamp.read_text() != digest:
        run([executable, "-m", "pip", "install", "-r", "backend/requirements.lock"])
        run([executable, "-m", "pip", "install", "--no-deps", "-e", "./backend"])
        stamp.write_text(digest)
    if not (root / ".env").exists():
        run([executable, "scripts/init_env.py"])
    (root / "data").mkdir(exist_ok=True)
    (root / "models").mkdir(exist_ok=True)
    (root / "artifacts").mkdir(exist_ok=True)
    run(
        [
            "docker",
            "compose",
            "up",
            "--build",
            "-d",
            "--wait",
            "--scale",
            "worker=2",
            "frontend",
            "api",
            "worker",
        ]
    )
    if not (root / "artifacts/demo-accounts.json").exists():
        run(
            [
                executable,
                "scripts/dev.py",
                "admin",
                "demo-accounts",
                "--output",
                "../artifacts/demo-accounts.json",
            ]
        )
    run([executable, "scripts/dev.py", "admin", "mock-model"])
    if args.demo:
        run([executable, "scripts/demo_pipeline.py"])
    print("Ready. Open the WEB_PORT address in .env (default http://127.0.0.1:18080).")
    print("Private generated login credentials: artifacts/demo-accounts.json")


if __name__ == "__main__":
    main()
