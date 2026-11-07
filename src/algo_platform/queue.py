"""异步任务执行：有界队列 + worker 池 + 线程池 + 超时 / 重试 / 背压。

设计要点
--------
1. 有界队列：队列满时立刻拒绝（429），而不是无限堆积把内存打爆；
2. 线程池隔离：CPU 密集的算法不占用事件循环，避免慢任务拖垮所有请求；
3. 超时与重试：算法内部偶发错误按指数退避重试，超时任务强制失败并记录；
4. 指标：队列深度、运行耗时、成功/失败计数，全部通过 /metrics 暴露。
"""
from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .artifacts import ArtifactStore
from .config import Settings
from .errors import BadRequest, RetryableJobError
from .metrics import Metrics
from .registry import Capability
from .store import Store

logger = logging.getLogger("algo_platform.queue")


class WorkerPool:
    def __init__(
        self,
        *,
        store: Store,
        registry: dict[str, Capability],
        artifacts: ArtifactStore,
        metrics: Metrics,
        settings: Settings,
    ) -> None:
        self.store = store
        self.registry = registry
        self.artifacts = artifacts
        self.metrics = metrics
        self.settings = settings
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=settings.queue_capacity)
        self._workers: list[asyncio.Task] = []
        self._cpu = ThreadPoolExecutor(max_workers=settings.cpu_threads, thread_name_prefix="algo")
        self.accepted = 0
        self.rejected = 0
        self.completed = 0
        self._started = False

    # ---------------- lifecycle ----------------
    async def start(self) -> None:
        self._started = True
        for index in range(self.settings.workers):
            self._workers.append(asyncio.create_task(self._worker(index), name=f"algo-worker-{index}"))
        self.metrics.set_gauge("algo_queue_depth", 0)
        logger.info("worker_pool_started", extra={"extra_fields": {
            "workers": self.settings.workers, "capacity": self.settings.queue_capacity}})

    async def stop(self) -> None:
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._cpu.shutdown(wait=False, cancel_futures=True)
        self._started = False
        logger.info("worker_pool_stopped", extra={"extra_fields": {"completed": self.completed}})

    # ---------------- submission ----------------
    def submit(self, job_id: str) -> bool:
        try:
            self._queue.put_nowait(job_id)
        except asyncio.QueueFull:
            self.rejected += 1
            self.metrics.inc("algo_jobs_rejected_total")
            return False
        self.accepted += 1
        self.metrics.set_gauge("algo_queue_depth", self._queue.qsize())
        return True

    def state(self) -> dict[str, Any]:
        return {
            "started": self._started,
            "workers": len(self._workers),
            "capacity": self.settings.queue_capacity,
            "pending": self._queue.qsize(),
            "accepted": self.accepted,
            "rejected": self.rejected,
            "completed": self.completed,
        }

    async def drain(self) -> None:
        """等待队列中的任务被消费完（测试与优雅停机用）。"""
        await self._queue.join()

    # ---------------- worker ----------------
    async def _worker(self, index: int) -> None:
        loop = asyncio.get_running_loop()
        while True:
            job_id = await self._queue.get()
            try:
                await self._process(job_id, loop)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - worker 不能因单任务异常退出
                logger.exception("worker_crashed", extra={"extra_fields": {"worker": index, "job": job_id}})
            finally:
                self._queue.task_done()
                self.metrics.set_gauge("algo_queue_depth", self._queue.qsize())

    async def _process(self, job_id: str, loop: asyncio.AbstractEventLoop) -> None:
        job = await asyncio.to_thread(self.store.get_job, job_id)
        if not job or job["status"] != "queued":
            return
        await asyncio.to_thread(self.store.mark_running, job_id)
        capability = self.registry.get(job["capability"])
        if capability is None:  # 理论上提交时已校验，兜底防脏数据
            await asyncio.to_thread(self.store.finish_job, job_id, status="failed", error={
                "code": "bad_request", "message": f"未注册的能力：{job['capability']}"})
            return

        started = time.perf_counter()
        attempt = 0
        while True:
            attempt += 1
            try:
                outcome = await asyncio.wait_for(
                    loop.run_in_executor(self._cpu, self._execute, job, capability),
                    timeout=self.settings.job_timeout_s,
                )
                result = await self._finalize_artifacts(outcome)
                run_ms = round((time.perf_counter() - started) * 1000, 2)
                await asyncio.to_thread(self.store.finish_job, job_id, status="succeeded", result=result)
                self.completed += 1
                self.metrics.inc("algo_jobs_total", status="succeeded", capability=job["capability"])
                self.metrics.observe("algo_job_run_ms", run_ms, capability=job["capability"])
                logger.info("job_succeeded", extra={"extra_fields": {
                    "job_id": job_id, "capability": job["capability"],
                    "version": capability.version, "run_ms": run_ms, "attempt": attempt}})
                return
            except asyncio.TimeoutError:
                await self._fail(job_id, job, "job_timeout",
                                 f"任务超时（>{self.settings.job_timeout_s}s）")
                return
            except RetryableJobError as exc:
                if attempt <= self.settings.max_retries:
                    backoff = self.settings.retry_backoff_s * (2 ** (attempt - 1))
                    self.metrics.inc("algo_job_retries_total", capability=job["capability"])
                    await asyncio.to_thread(self.store.bump_attempt, job_id)
                    logger.warning("job_retry", extra={"extra_fields": {
                        "job_id": job_id, "attempt": attempt, "backoff_s": backoff, "reason": str(exc)}})
                    await asyncio.sleep(backoff)
                    continue
                await self._fail(job_id, job, "retry_exhausted", f"重试 {attempt} 次仍失败：{exc}")
                return
            except BadRequest as exc:
                await self._fail(job_id, job, exc.code, exc.message)
                return
            except Exception as exc:  # noqa: BLE001 - 统一兜底为内部错误
                logger.exception("job_failed_internal", extra={"extra_fields": {"job_id": job_id}})
                await self._fail(job_id, job, "internal_error", f"算法执行异常：{exc}")
                return

    def _execute(self, job: dict[str, Any], capability: Capability) -> dict[str, Any]:
        """在 CPU 线程池内执行：读产物 → 调用算法 → 返回结果字典。"""
        artifact = self.store.get_artifact(job["artifact_id"])
        if artifact is None:
            raise RetryableJobError("产物不存在，可能是并发清理导致，重试一次")
        data = self.artifacts.load(artifact["sha256"])
        params = job.get("params") or {}
        if isinstance(params, str):
            import json
            params = json.loads(params)
        return capability.handler(data, params, artifact.get("filename") or "")

    async def _finalize_artifacts(self, outcome: dict[str, Any]) -> dict[str, Any]:
        """把算法返回的二进制产物落盘并登记，替换为可下载引用。"""
        payload = outcome.pop("artifact", None)
        if payload:
            sha, _path = await asyncio.to_thread(self.artifacts.save, payload["data"])
            record = await asyncio.to_thread(
                self.store.create_artifact, sha256=sha, size=len(payload["data"]),
                media_type=payload["media_type"], path=str(payload["filename"]),
            )
            outcome["artifact"] = {
                "artifact_id": record["id"],
                "filename": payload["filename"],
                "media_type": payload["media_type"],
                "size": record["size"],
                "sha256": sha,
                "stats": payload.get("stats", {}),
            }
        return outcome

    async def _fail(self, job_id: str, job: dict[str, Any], code: str, message: str) -> None:
        await asyncio.to_thread(self.store.finish_job, job_id, status="failed",
                                error={"code": code, "message": message})
        self.completed += 1
        self.metrics.inc("algo_jobs_total", status="failed", capability=job["capability"])
        self.metrics.inc("algo_job_failures_total", code=code)
        logger.warning("job_failed", extra={"extra_fields": {
            "job_id": job_id, "code": code, "message": message}})
