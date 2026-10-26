"""Prepare a bounded HetRF-Mix subset for manual import, preserving each sample boundary."""

import argparse
import hashlib
import json
import os
from pathlib import Path

import h5py
import numpy as np
from app.schemas.iq import BinMetadata
from app.services.storage import ControlledRoot, normalize_path, signature
from dotenv import load_dotenv


def write_json(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="测试数据")
    parser.add_argument("--output", default="测试数据_可导入")
    parser.add_argument("--per-split", type=int, default=10)
    parser.add_argument("--sample-rate-hz", type=float, required=True)
    parser.add_argument("--center-frequency-hz", type=float, required=True)
    args = parser.parse_args()
    if not 1 <= args.per_split <= 50:
        parser.error("Use 1..50 samples per split to keep this manual demonstration bounded")
    data_root = Path(os.environ.get("RF_DATA_ROOT", "./data"))
    if not data_root.is_absolute():
        data_root = root / data_root
    controlled = ControlledRoot(data_root)
    source = normalize_path(args.source, directory=True)
    output = normalize_path(args.output, directory=True)
    source_path = controlled.checked_path(source, directory=True)
    target_parent = controlled.checked_path(str(Path(output).parent.as_posix()), directory=True)
    target = target_parent / Path(output).name
    if target.exists() or target.is_relative_to(source_path):
        parser.error("Output must be a new directory outside the original input directory")
    metadata = BinMetadata(
        format="BIN",
        sample_rate_hz=args.sample_rate_hz,
        center_frequency_hz=args.center_frequency_hz,
        dtype="float32",
        byte_order="little",
        interleaving="IQ",
    ).model_dump(mode="json")
    target.mkdir(exist_ok=False)
    members = []
    sources = []
    for split in ("train", "test"):
        relative = f"{source}/{split}.h5"
        split_path = target / split
        split_path.mkdir()
        with controlled.open_read(relative) as stream:
            before = signature(os.fstat(stream.fileno()))
            source_digest = hashlib.file_digest(stream, "sha256").hexdigest()
            stream.seek(0)
            with h5py.File(stream, "r") as handle:
                for name in ("mixed_signals", "labels_multihot"):
                    if not isinstance(handle.get(name, getlink=True), h5py.HardLink):
                        raise ValueError(f"{relative}: {name} must be an internal dataset")
                signals = handle["mixed_signals"]
                labels = handle["labels_multihot"]
                if (
                    not isinstance(signals, h5py.Dataset)
                    or not isinstance(labels, h5py.Dataset)
                    or signals.ndim != 3
                    or signals.shape[1:] != (4800, 2)
                    or signals.dtype != np.dtype("float32")
                    or signals.shape[0] < args.per_split
                    or labels.shape != (signals.shape[0], 26)
                    or signals.is_virtual
                    or signals.external
                    or labels.is_virtual
                    or labels.external
                ):
                    raise ValueError(f"{relative}: unsupported HetRF-Mix layout")
                sources.append(
                    {
                        "relative_path": relative,
                        "sha256": source_digest,
                        "signal_dataset_path": "/mixed_signals",
                        "signal_shape": list(signals.shape),
                        "label_dataset_path": "/labels_multihot",
                        "selected_rows": [0, args.per_split],
                    }
                )
                for index in range(args.per_split):
                    pairs = np.asarray(signals[index], dtype="<f4")
                    label_vector = np.asarray(labels[index])
                    if not np.isfinite(pairs).all() or not np.isin(label_vector, [0, 1]).all():
                        raise ValueError(f"{relative}: invalid values in sample {index}")
                    payload = pairs.tobytes(order="C")
                    path = split_path / f"sample-{index:06d}.bin"
                    with path.open("xb") as destination:
                        destination.write(payload)
                    write_json(path.with_name(path.name + ".iq.json"), metadata)
                    members.append(
                        {
                            "relative_path": f"{output}/{split}/{path.name}",
                            "sha256": hashlib.sha256(payload).hexdigest(),
                            "sample_count": 4800,
                            "source_relative_path": relative,
                            "source_row_index": index,
                            "split": split,
                            "labels_multihot": label_vector.tolist(),
                            "active_label_indices": np.flatnonzero(label_vector).tolist(),
                        }
                    )
            if signature(os.fstat(stream.fileno())) != before:
                raise ValueError(f"{relative}: source changed during conversion")
            with controlled.open_read(relative) as reopened:
                if signature(os.fstat(reopened.fileno())) != before:
                    raise ValueError(f"{relative}: source was replaced during conversion")
    report = {
        "purpose": "manual-import demonstration subset; not a complete dataset import",
        "subset_count": len(members),
        "test_metadata_only": True,
        "parameter_note": (
            "Rate and frequency are operator-supplied test values, not verified acquisition facts. "
            "Column 0 is interpreted as I and column 1 as Q for this demonstration. "
            "Original multi-label vectors are preserved here and are not imported as human labels."
        ),
        "iq_metadata": metadata,
        "sources": sources,
        "members": members,
    }
    write_json(target / "import-provenance.json", report)
    (target / "操作说明.txt").write_text(
        f"本目录是 {len(members)} 条独立样本的手动接入演示子集，并非全部数据。\n"
        f"管理员登录 → 数据资产 → 相对目录填写：{output} → 提交扫描。\n"
        "不要填写原始目录或 Windows 绝对路径。此脚本没有提交扫描任务。\n"
        "扫描成功后选中一个 BIN：窗口4800、步长4800、起始0、结束4800，提交分段。\n"
        "采样率/中心频率仅为沿用的测试参数；原始多标签记录在 import-provenance.json，"
        "没有映射成平台人工标签。\n",
        encoding="utf-8",
    )
    # Publish completion markers only after both inputs and all outputs are ready.
    for member in members:
        (controlled.root / (member["relative_path"] + ".done")).touch(exist_ok=False)
    print(json.dumps({"prepared": output, "files": len(members), "scan_submitted": False}))


if __name__ == "__main__":
    main()
