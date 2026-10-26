import hashlib
import json
from typing import Any

READER_VERSION = "iq-reader-v1"
PIPELINE_VERSION = "segment-index-v1"


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def content_key(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
