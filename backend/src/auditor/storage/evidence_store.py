from __future__ import annotations

import hashlib
from pathlib import Path


class EvidenceStore:
    """Filesystem blobs; DB rows hold the returned relative ref (S3-swappable interface)."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def put(self, run_id: str, page_id: str, name: str, data: bytes) -> tuple[str, str]:
        for part in (run_id, page_id, name):  # each must be one plain path component
            if not part or part in (".", "..") or Path(part).name != part or "\\" in part:
                raise ValueError(f"bad evidence path component: {part!r}")
        rel = Path(run_id) / page_id / name
        path = (self.root / rel).resolve()
        if (self.root / run_id / page_id).resolve() not in path.parents:
            raise ValueError(f"bad evidence name: {name!r}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(rel), hashlib.sha256(data).hexdigest()

    def get(self, ref: str) -> bytes:
        path = (self.root / ref).resolve()
        if self.root not in path.parents:
            raise ValueError(f"bad evidence ref: {ref!r}")
        return path.read_bytes()
