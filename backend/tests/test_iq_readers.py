import os
from pathlib import Path

import h5py
import numpy as np
import pytest
from pydantic import ValidationError

from app.core.errors import DomainError
from app.schemas.iq import BinMetadata, Hdf5Metadata, SegmentConfig, metadata_adapter
from app.services import ingestion
from app.services.storage import ControlledRoot, normalize_path
from app.signal_processing.preview import build_preview
from app.signal_processing.readers import MAX_HDF5_CHUNK_BYTES, open_reader
from tests.iq_helpers import make_source


@pytest.mark.parametrize("dtype", ["int16", "float32"])
@pytest.mark.parametrize("byte_order", ["little", "big"])
def test_bin_explicit_dtype_endianness_and_interval(
    tmp_path: Path, dtype: str, byte_order: str
) -> None:
    path = tmp_path / "iq.bin"
    expected = np.array([1 + 2j, -30 + 20j, 500 - 400j, 0 + 1j], dtype=np.complex64)
    pairs = np.column_stack((expected.real, expected.imag))
    prefix = "<" if byte_order == "little" else ">"
    pairs.astype(prefix + ("i2" if dtype == "int16" else "f4")).tofile(path)
    metadata = BinMetadata(
        format="BIN",
        dtype=dtype,
        byte_order=byte_order,
        interleaving="IQ",
        sample_rate_hz=1000.0,
        center_frequency_hz=0.0,
    )
    with path.open("rb") as stream, open_reader(stream, metadata) as reader:
        assert reader.sample_count == 4
        np.testing.assert_array_equal(reader.read(1, 2), expected[1:3])
        assert stream.tell() == 3 * 2 * (2 if dtype == "int16" else 4)
        with pytest.raises(DomainError, match="out of bounds"):
            reader.read(3, 2)
        with pytest.raises(DomainError):
            reader.read(0, 1_000_001)


@pytest.mark.parametrize("paired", [False, True])
def test_hdf5_chunked_interval(tmp_path: Path, paired: bool) -> None:
    path = tmp_path / "iq.h5"
    iq = (np.arange(100) + 1j * np.arange(100)[::-1]).astype(np.complex64)
    with h5py.File(path, "w") as handle:
        data = np.column_stack((iq.real, iq.imag)) if paired else iq
        handle.create_dataset(
            "capture/iq", data=data, chunks=(16, 2) if paired else (16,), compression="gzip"
        )
    metadata = Hdf5Metadata(
        format="HDF5", dataset_path="/capture/iq", sample_rate_hz=1000.0, center_frequency_hz=0.0
    )
    with path.open("rb") as stream, open_reader(stream, metadata) as reader:
        assert reader.sample_count == 100
        np.testing.assert_array_equal(reader.read(31, 5), iq[31:36])


@pytest.mark.parametrize(
    "kind",
    ["external_link", "soft_parent", "external_storage", "virtual", "oversize_chunk", "shape"],
)
def test_hdf5_rejects_unsafe_or_unsupported_storage(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "unsafe.h5"
    dataset_path = "/iq"
    with h5py.File(path, "w") as handle:
        if kind == "external_link":
            handle["iq"] = h5py.ExternalLink("outside.h5", "/iq")
        elif kind == "soft_parent":
            handle.create_dataset("real/iq", data=np.ones(4, dtype=np.complex64))
            handle["alias"] = h5py.SoftLink("/real")
            dataset_path = "/alias/iq"
        elif kind == "external_storage":
            handle.create_dataset(
                "iq", shape=(4,), dtype="c8", external=[("outside.raw", 0, h5py.h5f.UNLIMITED)]
            )
        elif kind == "virtual":
            layout = h5py.VirtualLayout(shape=(4,), dtype="c8")
            layout[:] = h5py.VirtualSource("outside.h5", "iq", shape=(4,))
            handle.create_virtual_dataset("iq", layout)
        elif kind == "oversize_chunk":
            count = MAX_HDF5_CHUNK_BYTES // 8 + 1
            handle.create_dataset("iq", shape=(count,), chunks=(count,), dtype="c8")
        else:
            handle.create_dataset("iq", shape=(4, 3), dtype="f4")
    metadata = Hdf5Metadata(
        format="HDF5", dataset_path=dataset_path, sample_rate_hz=1000.0, center_frequency_hz=0.0
    )
    with path.open("rb") as stream, pytest.raises(DomainError), open_reader(stream, metadata):
        pass


@pytest.mark.parametrize(
    "path",
    [
        "../secret",
        "/etc/passwd",
        "C:/secret",
        "a/../secret",
        "a\\b",
        "a//b",
        "file:stream",
        "a\x00b",
    ],
)
def test_reject_path_traversal(path: str) -> None:
    with pytest.raises(DomainError):
        normalize_path(path)


@pytest.mark.parametrize("directory", [False, True])
def test_reject_symlink_escape(tmp_path: Path, directory: bool) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.bin").write_bytes(b"secret")
    link = root / ("linked" if directory else "secret.bin")
    try:
        link.symlink_to(
            outside if directory else outside / "secret.bin", target_is_directory=directory
        )
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip(
                "Windows symlink privilege unavailable; this test must run in Linux container"
            )
        raise
    name = "linked/secret.bin" if directory else "secret.bin"
    with pytest.raises(DomainError), ControlledRoot(root).open_read(name):
        pass


@pytest.mark.parametrize(
    "change",
    [
        {"sample_rate_hz": 0},
        {"center_frequency_hz": -1},
        {"dtype": "guess"},
        {"sample_rate_hz": float("nan")},
        {"unexpected": 1},
    ],
)
def test_reject_invalid_explicit_metadata(change: dict) -> None:
    metadata = {
        "format": "BIN",
        "dtype": "int16",
        "byte_order": "little",
        "interleaving": "IQ",
        "sample_rate_hz": 1000.0,
        "center_frequency_hz": 0.0,
        **change,
    }
    with pytest.raises(ValidationError):
        metadata_adapter.validate_python(metadata)


@pytest.mark.parametrize(
    "values",
    [
        {"window_samples": 0},
        {"window_samples": 8, "stride_samples": 9},
        {"window_samples": 8, "stride_samples": 2, "overlap_ratio": 0.5},
        {"window_samples": 8, "overlap_ratio": 0.3},
    ],
)
def test_segment_configuration_validation(values: dict) -> None:
    with pytest.raises(ValidationError):
        SegmentConfig(**values)


def test_overlap_and_stride_have_same_stable_identity() -> None:
    assert (
        SegmentConfig(window_samples=128, overlap_ratio=0.5).normalized()
        == SegmentConfig(window_samples=128, stride_samples=64).normalized()
    )


def test_detect_change_between_hash_and_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _, _ = make_source(tmp_path)
    original = ingestion.verify_observation

    def mutate(root, observed):
        path.write_bytes(b"changed bytes")
        original(root, observed)

    monkeypatch.setattr(ingestion, "verify_observation", mutate)
    with pytest.raises(DomainError, match="identity changed"):
        ingestion.inspect_file(ControlledRoot(tmp_path), path.name)


def test_preview_fft_and_bounded_waveform() -> None:
    n = np.arange(1024)
    iq = np.exp(2j * np.pi * 0.125 * n).astype(np.complex64)
    preview = build_preview(iq, 200, 1024.0, 2048)
    assert preview["frequency_offset_hz"][np.argmax(preview["power_db"])] == 128.0
    assert preview["stats"]["rms"] == pytest.approx(1.0)
    assert preview["sample_indices"][0] == 200
    assert len(build_preview(iq, 0, 1024.0, 32)["i"]) == 32
    with pytest.raises(DomainError, match="NaN"):
        build_preview(np.array([np.nan + 1j], dtype=np.complex64), 0, 1.0, 32)
