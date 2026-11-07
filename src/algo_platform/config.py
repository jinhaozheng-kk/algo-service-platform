"""运行配置：全部来自环境变量，便于容器化与压测时快速调参。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """不可变配置。字段含义见 README「运行参数」表。"""

    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("ALGO_DATA_DIR", "./data")))
    max_artifact_mb: int = field(default_factory=lambda: _int("ALGO_MAX_ARTIFACT_MB", 32))
    queue_capacity: int = field(default_factory=lambda: _int("ALGO_QUEUE_CAPACITY", 256))
    workers: int = field(default_factory=lambda: _int("ALGO_WORKERS", 4))
    cpu_threads: int = field(default_factory=lambda: _int("ALGO_CPU_THREADS", 4))
    job_timeout_s: float = field(default_factory=lambda: _float("ALGO_JOB_TIMEOUT_S", 20.0))
    max_retries: int = field(default_factory=lambda: _int("ALGO_MAX_RETRIES", 2))
    retry_backoff_s: float = field(default_factory=lambda: _float("ALGO_RETRY_BACKOFF_S", 0.2))
    log_level: str = field(default_factory=lambda: os.environ.get("ALGO_LOG_LEVEL", "INFO"))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "platform.db"

    @property
    def artifact_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def max_artifact_bytes(self) -> int:
        return self.max_artifact_mb * 1024 * 1024
