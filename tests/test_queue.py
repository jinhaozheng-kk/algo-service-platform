"""执行层测试：重试、超时、背压（队列满 → 429）。"""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from algo_platform.config import Settings
from algo_platform.errors import RetryableJobError
from algo_platform.main import create_app
from algo_platform.registry import Capability


def _capability(name: str, handler) -> Capability:
    return Capability(
        name=name, version="0.0.1", input_kind="image",
        description="测试用能力", handler=handler,
    )


def _app(settings: Settings, capabilities: dict) -> TestClient:
    return TestClient(create_app(settings, capabilities))


def _upload(client: TestClient) -> str:
    response = client.post("/v1/artifacts", files={"file": ("x.png", b"\x89PNG fake")})
    assert response.status_code == 201
    return response.json()["id"]


def _wait(client: TestClient, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/v1/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed", "canceled"}:
            return job
        time.sleep(0.05)
    raise AssertionError("任务未结束")


def test_retryable_error_then_success(tmp_path) -> None:
    attempts = {"n": 0}

    def flaky(data: bytes, params: dict, filename: str) -> dict:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RetryableJobError("模拟瞬时故障")
        return {"ok": True, "attempts": attempts["n"]}

    settings = Settings(data_dir=tmp_path / "d", max_retries=3, retry_backoff_s=0.01,
                        job_timeout_s=5.0)
    with _app(settings, {"flaky": _capability("flaky", flaky)}) as client:
        artifact_id = _upload(client)
        job = client.post("/v1/jobs", json={"capability": "flaky", "artifact_id": artifact_id}).json()
        done = _wait(client, job["id"])
        assert done["status"] == "succeeded"
        assert done["attempts"] == 2
        assert done["result"]["attempts"] == 2


def test_timeout_fails_fast(tmp_path) -> None:
    def slow(data: bytes, params: dict, filename: str) -> dict:
        time.sleep(1.0)
        return {"never": True}

    settings = Settings(data_dir=tmp_path / "d", job_timeout_s=0.1, max_retries=0)
    with _app(settings, {"slow": _capability("slow", slow)}) as client:
        artifact_id = _upload(client)
        job = client.post("/v1/jobs", json={"capability": "slow", "artifact_id": artifact_id}).json()
        done = _wait(client, job["id"])
        assert done["status"] == "failed"
        assert done["error"]["code"] == "job_timeout"


def test_backpressure_returns_429(tmp_path) -> None:
    def slow(data: bytes, params: dict, filename: str) -> dict:
        time.sleep(0.4)
        return {"ok": True}

    # 1 个 worker + 容量 1 的队列：至少会有提交被拒绝
    settings = Settings(data_dir=tmp_path / "d", workers=1, cpu_threads=1,
                        queue_capacity=1, job_timeout_s=10.0)
    with _app(settings, {"slow": _capability("slow", slow)}) as client:
        artifact_id = _upload(client)
        statuses = [
            client.post("/v1/jobs", json={"capability": "slow", "artifact_id": artifact_id}).status_code
            for _ in range(6)
        ]
        assert 429 in statuses
        assert statuses.count(202) >= 1
        # 队列被拒的任务必须落库为失败，避免“幽灵任务”
        failed = client.get("/v1/jobs", params={"status": "failed"}).json()["items"]
        assert any(item["error"]["code"] == "queue_saturated" for item in failed)
