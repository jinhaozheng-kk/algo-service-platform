"""HTTP 层：把算法能力暴露为标准 REST 服务。

路由设计围绕“异步任务”展开：
    上传产物 → 提交任务（202）→ 轮询/回调 → 下载结果与产物
所有错误都返回统一结构 {"error": {"code", "message", "detail"}}。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from . import __version__
from .artifacts import ArtifactStore
from .config import Settings
from .errors import (
    BadRequest,
    NotFound,
    PayloadTooLarge,
    PlatformError,
    QueueSaturated,
    UnsupportedMedia,
)
from .logging_setup import configure_logging
from .metrics import Metrics
from .queue import WorkerPool
from .registry import DEFAULT_REGISTRY, Capability
from .schemas import ArtifactOut, CapabilityOut, JobCreate, JobListOut, JobOut, StatsOut
from .store import Store

logger = logging.getLogger("algo_platform.api")

ALLOWED_UPLOADS = {
    ".stl": "model/stl",
    ".obj": "model/obj",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def _media_type_for(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_UPLOADS:
        raise UnsupportedMedia(
            f"不支持的文件类型：{suffix or '无扩展名'}（支持 {', '.join(sorted(ALLOWED_UPLOADS))}）"
        )
    return ALLOWED_UPLOADS[suffix]


def _job_payload(job: dict) -> dict:
    def loads(value):
        if value is None:
            return None
        return json.loads(value) if isinstance(value, str) else value

    result = loads(job.get("result"))
    payload = {
        "id": job["id"],
        "capability": job["capability"],
        "version": job["version"],
        "status": job["status"],
        "artifact_id": job.get("artifact_id"),
        "params": loads(job.get("params")) or {},
        "result": result,
        "error": loads(job.get("error")),
        "attempts": job.get("attempts", 0),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "queue_wait_ms": job.get("queue_wait_ms"),
        "run_ms": job.get("run_ms"),
        "links": {"self": f"/v1/jobs/{job['id']}"},
    }
    if result and isinstance(result.get("artifact"), dict):
        artifact_id = result["artifact"].get("artifact_id")
        if artifact_id:
            payload["links"]["artifact"] = f"/v1/artifacts/{artifact_id}"
    return payload


def create_app(settings: Settings | None = None,
               registry: dict[str, Capability] | None = None) -> FastAPI:
    settings = settings or Settings()
    registry = registry or DEFAULT_REGISTRY
    configure_logging(settings.log_level)

    store = Store(settings.db_path)
    artifacts = ArtifactStore(settings.artifact_dir)
    metrics = Metrics()
    pool = WorkerPool(store=store, registry=registry, artifacts=artifacts,
                      metrics=metrics, settings=settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await pool.start()
        logger.info("app_started", extra={"extra_fields": {
            "version": __version__, "capabilities": sorted(registry)}})
        try:
            yield
        finally:
            await pool.stop()
            store.close()
            logger.info("app_stopped")

    app = FastAPI(
        title="algo-service-platform",
        version=__version__,
        description="3D 打印场景的算法服务化平台：算法接入、异步任务、结果闭环与可观测性。",
        lifespan=lifespan,
    )
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                       allow_headers=["*"])
    app.state.settings = settings
    app.state.registry = registry
    app.state.store = store
    app.state.artifacts = artifacts
    app.state.metrics = metrics
    app.state.pool = pool

    @app.exception_handler(PlatformError)
    async def platform_error_handler(_: Request, exc: PlatformError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message, "detail": exc.detail}},
        )

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration = (time.perf_counter() - started) * 1000
            metrics.inc("http_requests_total", method=request.method, path=request.url.path, status="500")
            metrics.observe("http_request_duration_ms", duration, path=request.url.path)
            logger.exception("http_request_crashed", extra={"extra_fields": {
                "request_id": request_id, "path": request.url.path}})
            raise
        duration = (time.perf_counter() - started) * 1000
        route = request.scope.get("route")
        path_label = getattr(route, "path", request.url.path)
        metrics.inc("http_requests_total", method=request.method, path=path_label,
                    status=str(response.status_code))
        metrics.observe("http_request_duration_ms", duration, path=path_label)
        response.headers["x-request-id"] = request_id
        logger.info("http_request", extra={"extra_fields": {
            "request_id": request_id, "method": request.method, "path": request.url.path,
            "status": response.status_code, "duration_ms": round(duration, 2)}})
        return response

    # ---------------- 基础探针 ----------------
    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", tags=["ops"])
    async def readyz() -> dict:
        if not pool.state()["started"]:
            return JSONResponse(status_code=503, content={"status": "starting"})
        store.stats()
        return {"status": "ready", "pending": pool.state()["pending"]}

    @app.get("/metrics", tags=["ops"], response_class=PlainTextResponse)
    async def metrics_endpoint() -> PlainTextResponse:
        metrics.set_gauge("algo_queue_depth", pool.state()["pending"])
        return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")

    # ---------------- 能力 ----------------
    @app.get("/v1/capabilities", response_model=list[CapabilityOut], tags=["algorithm"])
    async def list_capabilities() -> list[CapabilityOut]:
        return [
            CapabilityOut(
                name=cap.name, version=cap.version, input_kind=cap.input_kind,
                description=cap.description, default_params=cap.default_params,
                param_hints=cap.param_hints,
            )
            for cap in sorted(registry.values(), key=lambda c: c.name)
        ]

    # ---------------- 产物 ----------------
    @app.post("/v1/artifacts", response_model=ArtifactOut, status_code=201, tags=["artifact"])
    async def upload_artifact(request: Request, file: UploadFile = File(...)) -> ArtifactOut:
        media_type = _media_type_for(file.filename or "")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > settings.max_artifact_bytes:
                raise PayloadTooLarge(f"文件超过 {settings.max_artifact_mb}MB 上限")
            chunks.append(chunk)
        if total == 0:
            raise BadRequest("空文件")
        data = b"".join(chunks)
        sha, _path = artifacts.save(data)
        record = store.create_artifact(
            sha256=sha, size=len(data), media_type=media_type,
            path=f"{sha[:2]}/{sha}", filename=file.filename or "",
        )
        return ArtifactOut(
            id=record["id"], sha256=record["sha256"], size=record["size"],
            media_type=record["media_type"], created_at=record["created_at"],
            download_url=f"/v1/artifacts/{record['id']}",
        )

    @app.get("/v1/artifacts/{artifact_id}", tags=["artifact"])
    async def download_artifact(artifact_id: str) -> FileResponse:
        record = store.get_artifact(artifact_id)
        if record is None:
            raise NotFound(f"产物不存在：{artifact_id}")
        path = settings.artifact_dir / record["sha256"][:2] / record["sha256"]
        if not path.exists():
            raise NotFound("产物文件已丢失")
        return FileResponse(path, media_type=record["media_type"],
                            filename=record.get("filename") or path.name)

    # ---------------- 任务 ----------------
    @app.post("/v1/jobs", response_model=JobOut, status_code=202, tags=["job"])
    async def submit_job(body: JobCreate, request: Request) -> JobOut:
        capability = registry.get(body.capability)
        if capability is None:
            raise BadRequest(f"未注册的能力：{body.capability}，可选：{sorted(registry)}")
        artifact = store.get_artifact(body.artifact_id)
        if artifact is None:
            raise NotFound(f"产物不存在：{body.artifact_id}")
        job, created = store.create_job(
            capability=capability.name, version=capability.version,
            artifact_id=artifact["id"], params=dict(capability.default_params) | body.params,
            idempotency_key=request.headers.get("idempotency-key"),
            request_id=getattr(request.state, "request_id", None),
        )
        if created:
            if not pool.submit(job["id"]):
                store.finish_job(job["id"], status="failed", error={
                    "code": "queue_saturated", "message": "队列已满，请稍后重试"})
                metrics.inc("algo_jobs_total", status="failed", capability=capability.name)
                raise QueueSaturated("队列已满，请稍后重试", detail={"job_id": job["id"]})
            logger.info("job_accepted", extra={"extra_fields": {
                "job_id": job["id"], "capability": capability.name,
                "artifact": artifact["id"]}})
        else:
            logger.info("job_idempotent_replay", extra={"extra_fields": {"job_id": job["id"]}})
        return JobOut(**_job_payload(job))

    @app.get("/v1/jobs", response_model=JobListOut, tags=["job"])
    async def list_jobs(status: str | None = None, capability: str | None = None,
                        limit: int = 20) -> JobListOut:
        rows = store.list_jobs(status=status, capability=capability, limit=limit)
        return JobListOut(items=[JobOut(**_job_payload(r)) for r in rows], count=len(rows))

    @app.get("/v1/jobs/{job_id}", response_model=JobOut, tags=["job"])
    async def get_job(job_id: str) -> JobOut:
        job = store.get_job(job_id)
        if job is None:
            raise NotFound(f"任务不存在：{job_id}")
        return JobOut(**_job_payload(job))

    @app.post("/v1/jobs/{job_id}/cancel", response_model=JobOut, tags=["job"])
    async def cancel_job(job_id: str) -> JobOut:
        job = store.get_job(job_id)
        if job is None:
            raise NotFound(f"任务不存在：{job_id}")
        if job["status"] == "queued":
            store.cancel_job(job_id)
            metrics.inc("algo_jobs_total", status="canceled", capability=job["capability"])
        return JobOut(**_job_payload(store.get_job(job_id)))

    # ---------------- 汇总 ----------------
    @app.get("/v1/stats", response_model=StatsOut, tags=["ops"])
    async def stats() -> StatsOut:
        return StatsOut(store=store.stats(), metrics=metrics.snapshot(), queue=pool.state())

    return app

