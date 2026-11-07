"""API 出入参模型（pydantic v2）。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CapabilityOut(BaseModel):
    name: str
    version: str
    input_kind: str
    description: str
    default_params: dict[str, Any]
    param_hints: dict[str, str]


class ArtifactOut(BaseModel):
    id: str
    sha256: str
    size: int
    media_type: str
    created_at: float
    download_url: str


class JobCreate(BaseModel):
    capability: str = Field(..., description="能力名，例如 mesh_inspect / print_monitor")
    artifact_id: str = Field(..., description="先通过 POST /v1/artifacts 上传得到的产物 ID")
    params: dict[str, Any] = Field(default_factory=dict)


class JobOut(BaseModel):
    id: str
    capability: str
    version: str
    status: Literal["queued", "running", "succeeded", "failed", "canceled"]
    artifact_id: str | None
    params: dict[str, Any]
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    attempts: int
    created_at: float
    started_at: float | None
    finished_at: float | None
    queue_wait_ms: float | None
    run_ms: float | None
    links: dict[str, str]


class JobListOut(BaseModel):
    items: list[JobOut]
    count: int


class StatsOut(BaseModel):
    store: dict[str, Any]
    metrics: dict[str, Any]
    queue: dict[str, Any]
