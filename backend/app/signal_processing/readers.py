import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import BinaryIO, Protocol

import h5py
import numpy as np
from numpy.typing import NDArray

from app.core.errors import DomainError
from app.schemas.iq import BinMetadata, Hdf5Metadata, Metadata

MAX_READ_SAMPLES = 1_000_000
MAX_HDF5_CHUNK_BYTES = 16 * 1024 * 1024


class IQReader(Protocol):
    sample_count: int

    def read(self, start: int, count: int) -> NDArray[np.complex64]: ...


def validate_interval(start: int, count: int, total: int) -> None:
    if start < 0 or count <= 0 or count > MAX_READ_SAMPLES or start > total - count:
        raise DomainError(
            "INVALID_INTERVAL", "Requested sample interval is out of bounds or too large"
        )


class BinReader:
    def __init__(self, stream: BinaryIO, metadata: BinMetadata) -> None:
        self.stream = stream
        prefix = "<" if metadata.byte_order == "little" else ">"
        self.dtype = np.dtype(prefix + ("i2" if metadata.dtype == "int16" else "f4"))
        self.bytes_per_sample = 2 * self.dtype.itemsize
        size = os.fstat(stream.fileno()).st_size
        if size == 0 or size % self.bytes_per_sample:
            raise DomainError(
                "INVALID_BIN_SIZE", "BIN length must contain complete interleaved IQ samples"
            )
        self.sample_count = size // self.bytes_per_sample

    def read(self, start: int, count: int) -> NDArray[np.complex64]:
        validate_interval(start, count, self.sample_count)
        self.stream.seek(start * self.bytes_per_sample)
        raw = self.stream.read(count * self.bytes_per_sample)
        if len(raw) != count * self.bytes_per_sample:
            raise DomainError("SOURCE_CHANGED", "Source was truncated while reading", 409)
        values = np.frombuffer(raw, dtype=self.dtype).reshape(-1, 2).astype(np.float32)
        return (values[:, 0] + 1j * values[:, 1]).astype(np.complex64)


class Hdf5Reader:
    def __init__(self, handle: h5py.File, metadata: Hdf5Metadata) -> None:
        group = handle
        parts = metadata.dataset_path.strip("/").split("/")
        for index, part in enumerate(parts):
            if not isinstance(group, h5py.Group) or not isinstance(
                group.get(part, getlink=True), h5py.HardLink
            ):
                raise DomainError(
                    "UNSAFE_HDF5", "Dataset path must use existing internal hard links"
                )
            group = group[part]
            if index < len(parts) - 1 and not isinstance(group, h5py.Group):
                raise DomainError("INVALID_HDF5", "Dataset parent is not a group")
        if not isinstance(group, h5py.Dataset):
            raise DomainError("INVALID_HDF5", "Configured HDF5 path is not a dataset")
        self.dataset = group
        if group.is_virtual or group.external:
            raise DomainError("UNSAFE_HDF5", "Virtual datasets and external storage are forbidden")
        if (
            group.chunks
            and np.prod(group.chunks, dtype=object) * group.dtype.itemsize > MAX_HDF5_CHUNK_BYTES
        ):
            raise DomainError("HDF5_CHUNK_LIMIT", "HDF5 chunk exceeds the 16 MiB read budget")
        complex_layout = group.ndim == 1 and group.dtype.kind == "c" and group.dtype.itemsize == 8
        pair_layout = (
            group.ndim == 2
            and group.shape[1] == 2
            and group.dtype.kind in "iuf"
            and group.dtype.itemsize <= 8
        )
        if not (complex_layout or pair_layout) or group.shape[0] == 0:
            raise DomainError(
                "INVALID_HDF5", "Use nonempty complex64 [N] or numeric [N,2] datasets"
            )
        self.is_complex = complex_layout
        self.sample_count = group.shape[0]

    def read(self, start: int, count: int) -> NDArray[np.complex64]:
        validate_interval(start, count, self.sample_count)
        data = self.dataset[start : start + count]
        if self.is_complex:
            return np.asarray(data, dtype=np.complex64)
        values = np.asarray(data, dtype=np.float32)
        return (values[:, 0] + 1j * values[:, 1]).astype(np.complex64)


@contextmanager
def open_reader(stream: BinaryIO, metadata: Metadata) -> Iterator[IQReader]:
    """Use an already-secured descriptor; HDF5 reads only the requested hyperslab."""
    stream.seek(0)
    if isinstance(metadata, BinMetadata):
        yield BinReader(stream, metadata)
    else:
        try:
            with h5py.File(stream, "r", rdcc_nbytes=1024 * 1024) as handle:
                yield Hdf5Reader(handle, metadata)
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            raise DomainError(
                "INVALID_HDF5", "HDF5 data cannot be read with the specified layout"
            ) from exc
