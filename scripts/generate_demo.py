"""Generate small deterministic IQ fixtures and sidecars, completing markers last."""

import argparse
import json
import os
from pathlib import Path

import h5py
import numpy as np
from dotenv import load_dotenv


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="demo-phase2")
    args = parser.parse_args()
    if not args.name or any(
        c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        for c in args.name
    ):
        parser.error("Use a single alphanumeric directory name")
    data_root = Path(os.environ.get("RF_DATA_ROOT", "./data"))
    if not data_root.is_absolute():
        data_root = root / data_root
    target = data_root / args.name
    target.mkdir(parents=True, exist_ok=False)
    n = np.arange(4096)
    iq = np.exp(2j * np.pi * 0.0625 * n).astype(np.complex64)
    pairs = np.column_stack((iq.real, iq.imag))
    bin_path = target / "tone.bin"
    pairs.astype("<f4").tofile(target / "tone.bin.part")
    (target / "tone.bin.part").replace(bin_path)
    h5_path = target / "tone.h5"
    with h5py.File(target / "tone.h5.part", "w") as handle:
        handle.create_dataset("iq", data=iq, chunks=(1024,))
    (target / "tone.h5.part").replace(h5_path)
    for path, specific in [
        (
            bin_path,
            {
                "format": "BIN",
                "dtype": "float32",
                "byte_order": "little",
                "interleaving": "IQ",
            },
        ),
        (h5_path, {"format": "HDF5", "dataset_path": "/iq"}),
    ]:
        metadata = {
            "sample_rate_hz": 1_000_000.0,
            "center_frequency_hz": 100_000_000.0,
            **specific,
        }
        path.with_name(path.name + ".iq.json").write_text(
            json.dumps(metadata), encoding="utf-8"
        )
        path.with_name(path.name + ".done").touch(exist_ok=False)
    print(
        f"Generated two synthetic IQ sources under {args.name}/; "
        "no model predictions were produced."
    )


if __name__ == "__main__":
    main()
