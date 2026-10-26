"""Create a private binary PostgreSQL backup; optionally verify in an isolated temporary DB."""

import argparse
import json
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    values = dotenv_values(root / ".env")
    user, database = (
        values.get("POSTGRES_USER", "rf_dataops"),
        values.get("POSTGRES_DB", "rf_dataops"),
    )
    output = (
        root
        / "artifacts"
        / f"backup-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}.dump"
    )
    output.parent.mkdir(exist_ok=True)

    def command(*arguments):
        return ["docker", "compose", "exec", "-T", "db", *arguments]

    with output.open("xb") as stream:
        subprocess.run(
            command(
                "pg_dump",
                "-U",
                user,
                "-d",
                database,
                "-Fc",
                "--no-owner",
                "--no-privileges",
            ),
            cwd=root,
            stdout=stream,
            check=True,
        )
    verified = False
    if args.verify:
        name = "rf_dataops_restore_" + uuid.uuid4().hex[:12]
        created = False
        try:
            subprocess.run(command("createdb", "-U", user, name), cwd=root, check=True)
            created = True
            with output.open("rb") as stream:
                subprocess.run(
                    command(
                        "pg_restore",
                        "-U",
                        user,
                        "-d",
                        name,
                        "--exit-on-error",
                        "--no-owner",
                        "--no-privileges",
                    ),
                    cwd=root,
                    stdin=stream,
                    check=True,
                )
            query = (
                "SELECT version_num='0005_datasets' AND NOT EXISTS "
                "(SELECT 1 FROM dataset_versions v WHERE NOT sealed OR "
                "manifest_sha256<>encode(sha256(convert_to(manifest_text,'UTF8')),'hex') "
                "OR manifest_text::jsonb IS DISTINCT FROM rf_dataset_manifest(v.id)) "
                "FROM alembic_version"
            )
            result = subprocess.run(
                command("psql", "-U", user, "-d", name, "-tA", "-c", query),
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            if result.stdout.strip() != "t":
                raise RuntimeError("Restored schema or manifest validation failed")
            verified = True
        finally:
            if created:
                subprocess.run(
                    command("dropdb", "-U", user, "--force", name), cwd=root, check=True
                )
    print(
        json.dumps(
            {
                "backup": str(output),
                "restore_verified": verified,
                "source_iq_included": False,
            }
        )
    )


if __name__ == "__main__":
    main()
