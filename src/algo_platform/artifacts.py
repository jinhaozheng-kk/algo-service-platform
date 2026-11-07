"""内容寻址的产物存储。

同一个文件重复上传只落一份盘（sha256 去重），这既是成本优化，
也让“同一输入 → 同一结果”的复现实验成为可能。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def digest(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _path_for(self, sha256: str) -> Path:
        return self.root / sha256[:2] / sha256

    def save(self, data: bytes) -> tuple[str, Path]:
        sha = self.digest(data)
        path = self._path_for(sha)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        return sha, path

    def load(self, sha256: str) -> bytes:
        return self._path_for(sha256).read_bytes()

    def exists(self, sha256: str) -> bool:
        return self._path_for(sha256).exists()
