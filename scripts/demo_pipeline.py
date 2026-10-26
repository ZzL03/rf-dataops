"""Real local HTTP demo on generated IQ only; labels are explicitly demonstration records."""

import hashlib
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
from dotenv import dotenv_values


def main():
    root = Path(__file__).resolve().parents[1]
    settings = dotenv_values(root / ".env")
    base = f"http://127.0.0.1:{settings.get('WEB_PORT', '18080')}"
    accounts = json.loads(
        (root / "artifacts/demo-accounts.json").read_text(encoding="utf-8")
    )["accounts"]
    jobs = []
    clients = []

    def account(role):
        item = next(row for row in accounts if row["role"] == role)
        client = httpx.Client(base_url=base + "/api/v1", timeout=20)
        response = client.post(
            "/auth/token", json={key: item[key] for key in ("username", "password")}
        )
        response.raise_for_status()
        client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
        clients.append(client)
        return client

    def call(client, method, path, body=None):
        response = client.request(
            method,
            path,
            json=body,
            headers={"Idempotency-Key": str(uuid.uuid4())} if method == "POST" else {},
        )
        response.raise_for_status()
        return response.json()

    def wait(client, job):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            current = call(client, "GET", f"/jobs/{job['id']}")
            if current["status"] == "SUCCEEDED":
                jobs.append(
                    {
                        key: current[key]
                        for key in ("id", "job_type", "status", "worker_id")
                    }
                )
                return current["result"]
            if current["status"] == "FAILED":
                raise RuntimeError(
                    f"{current['job_type']} failed: {current['last_error']}"
                )
            time.sleep(0.25)
        raise TimeoutError("Job is still running; inspect the jobs page")

    def task(body):
        return wait(admin, call(admin, "POST", "/jobs", body))

    try:
        admin, reviewer, researcher = [
            account(role) for role in ("ADMIN", "REVIEWER", "RESEARCHER")
        ]
        batch = "demo-phase5-" + uuid.uuid4().hex[:10]
        subprocess.run(
            [sys.executable, root / "scripts/generate_demo.py", "--name", batch],
            cwd=root,
            check=True,
        )
        model = call(admin, "POST", "/models", {})
        scanned = task({"job_type": "SCAN", "directory": batch})
        assert len(scanned["files"]) == 2 and not scanned["errors"]
        reviews = []
        for source in scanned["files"]:
            refs = task(
                {
                    "job_type": "SEGMENT",
                    "raw_file_id": source["id"],
                    "config": {"window_samples": 128, "stride_samples": 128},
                    "start": 0,
                    "end": 512,
                }
            )
            for segment_id in refs["segment_ids"][:2]:
                task({"job_type": "QUALITY", "segment_id": segment_id})
                inferred = task(
                    {
                        "job_type": "INFER",
                        "segment_id": segment_id,
                        "model_version_id": model["id"],
                    }
                )
                assert inferred["is_mock"]
                review = call(reviewer, "GET", f"/reviews/{inferred['review_id']}")
                call(
                    reviewer,
                    "POST",
                    f"/reviews/{review['id']}/annotations",
                    {
                        "expected_version": review["label_version"],
                        "label": "QPSK",
                        "action": "CORRECT",
                        "prediction_id": inferred["prediction_id"],
                        "comment": (
                            "Synthetic IQ workflow demonstration; "
                            "label is not measured ground truth"
                        ),
                    },
                )
                reviews.append(review["id"])
        dataset = call(
            researcher,
            "POST",
            "/datasets",
            {
                "name": "RF-Demo-" + batch[-10:],
                "description": "Synthetic end-to-end demonstration; no measured truth labels",
            },
        )
        selection = {
            "selection": {"raw_file_ids": [row["id"] for row in scanned["files"]]},
            "label_policy": "HUMAN_ONLY",
        }

        def publish(version):
            preview = call(
                researcher,
                "POST",
                f"/datasets/{dataset['id']}/selection-preview",
                selection,
            )
            job = call(
                researcher,
                "POST",
                f"/datasets/{dataset['id']}/versions",
                {
                    **selection,
                    "version": version,
                    "expected_selection_digest": preview["selection_digest"],
                },
            )
            result = wait(researcher, job)
            response = researcher.get(
                f"/dataset-versions/{result['dataset_version_id']}/manifest"
            )
            response.raise_for_status()
            assert (
                hashlib.sha256(response.content).hexdigest()
                == result["manifest_sha256"]
                == response.headers["x-manifest-sha256"]
            )
            return result, response.content

        first, initial = publish("v1.0.0")
        assert first["item_count"] == 4
        call(
            reviewer,
            "POST",
            f"/reviews/{reviews[0]}/annotations",
            {
                "expected_version": 1,
                "label": "BPSK",
                "action": "CORRECT",
                "comment": "Synthetic demonstration: change one label and publish a new version",
            },
        )
        second, updated = publish("v1.0.1")
        historical = researcher.get(
            f"/dataset-versions/{first['dataset_version_id']}/manifest"
        )
        assert historical.content == initial and initial != updated
        assert second["label_distribution"] == {"BPSK": 1, "QPSK": 3}
        (root / "artifacts/phase5-demo-manifest.json").write_bytes(initial)
        report = {
            "batch": batch,
            "dataset_id": dataset["id"],
            "versions": [first, second],
            "historical_manifest_unchanged": True,
            "jobs": jobs,
            "synthetic_only": True,
        }
        (root / "artifacts/phase5-demo.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "dataset_id": dataset["id"],
                    "versions": ["v1.0.0", "v1.0.1"],
                    "samples_per_version": 4,
                    "historical_manifest_unchanged": True,
                    "report": "artifacts/phase5-demo.json",
                }
            )
        )
    finally:
        for client in clients:
            try:
                client.post("/auth/logout")
            except httpx.HTTPError:
                pass
            finally:
                client.close()


if __name__ == "__main__":
    main()
