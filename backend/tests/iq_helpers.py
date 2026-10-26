import json
from pathlib import Path

import h5py
import numpy as np


def make_source(
    root: Path, name: str = "sample.bin", samples: int = 256
) -> tuple[Path, dict, np.ndarray]:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    iq = (np.arange(samples) + 1j * -np.arange(samples)).astype(np.complex64)
    metadata = {"sample_rate_hz": 1000.0, "center_frequency_hz": 10000.0}
    if path.suffix == ".bin":
        np.column_stack((iq.real, iq.imag)).astype("<f4").tofile(path)
        metadata.update(format="BIN", dtype="float32", byte_order="little", interleaving="IQ")
    else:
        with h5py.File(path, "w") as handle:
            handle.create_dataset("iq", data=iq, chunks=(min(64, samples),))
        metadata.update(format="HDF5", dataset_path="/iq")
    write_metadata(path, metadata)
    path.with_name(path.name + ".done").touch()
    return path, metadata, iq


def write_metadata(path: Path, metadata: dict) -> None:
    path.with_name(path.name + ".iq.json").write_text(json.dumps(metadata), encoding="utf-8")
