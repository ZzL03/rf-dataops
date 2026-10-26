"""Trusted local administration. Passwords come from hidden prompts, never arguments."""

import argparse
import getpass
import hashlib
import json
import re
import secrets
from pathlib import Path

from app.core.config import Settings
from app.core.errors import DomainError
from app.db.session import build_engine, build_session_factory
from app.schemas.labeling import LABELS
from app.services.authentication import create_user
from app.services.labeling import register_model
from app.services.storage import ControlledRoot, normalize_path
from app.signal_processing.models import PREPROCESS_VERSION


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    user = commands.add_parser("create-user")
    user.add_argument("--username", required=True)
    user.add_argument("--role", choices=["ADMIN", "RESEARCHER", "REVIEWER"], required=True)
    demo = commands.add_parser("demo-accounts")
    demo.add_argument("--output", type=Path, required=True)
    commands.add_parser("mock-model")
    torch = commands.add_parser("torch-model")
    torch.add_argument("--name", required=True)
    torch.add_argument("--version", required=True)
    torch.add_argument(
        "--artifact",
        required=True,
        help="Relative path under RF_MODEL_ROOT; trusted TorchScript only",
    )
    torch.add_argument("--samples", required=True, type=int)
    torch.add_argument("--labels", required=True, help="Comma separated output class order")
    args = parser.parse_args()
    settings = Settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    try:
        if args.command == "create-user":
            password = getpass.getpass("Password (12-128 characters): ")
            if password != getpass.getpass("Confirm password: "):
                raise DomainError("PASSWORD_MISMATCH", "Passwords do not match")
            user = create_user(factory, args.username, password, args.role)
            print(json.dumps({"id": str(user.id), "username": user.username, "role": user.role}))
        elif args.command == "demo-accounts":
            # Reserve output before DB changes, preventing accidental credential overwrite.
            with args.output.open("x", encoding="utf-8") as output:
                accounts = []
                for role in ("ADMIN", "REVIEWER", "RESEARCHER"):
                    password = secrets.token_urlsafe(18)
                    user = create_user(factory, "demo_" + role.lower(), password, role)
                    accounts.append({"username": user.username, "password": password, "role": role})
                    # Flush each created account in case a later username already exists.
                    output.seek(0)
                    json.dump({"accounts": accounts}, output, indent=2)
                    output.truncate()
                    output.flush()
            print("Demo accounts created; credentials saved only to the requested private file.")
        elif args.command == "mock-model":
            print(register_model(factory, "mock-modulation", "mock-v1").id)
        else:
            labels = args.labels.split(",")
            if (
                not all(
                    re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value)
                    for value in (args.name, args.version)
                )
                or not 1 <= args.samples <= 1_000_000
                or len(labels) < 2
                or len(set(labels)) != len(labels)
                or any(label not in LABELS for label in labels)
            ):
                raise DomainError(
                    "INVALID_MODEL_CONFIG", "Invalid model name, sample count or class order"
                )
            path = normalize_path(args.artifact)
            with ControlledRoot(settings.model_root).open_read(path) as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            config = {
                "labels": labels,
                "preprocessing": PREPROCESS_VERSION,
                "artifact_path": path,
                "sample_count": args.samples,
            }
            print(
                register_model(factory, args.name, args.version, "torchscript", config, digest).id
            )
        return 0
    except DomainError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}))
        return 2
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
