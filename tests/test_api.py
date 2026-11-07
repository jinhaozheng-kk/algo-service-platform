"""HTTP 契约测试：上传 → 提交 → 轮询 → 下载 全链路，以及幂等、错误语义、指标。"""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

TERMINAL = {"succeeded", "failed", "canceled"}


def wait_job(client: TestClient, job_id: str, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/v1/jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] in TERMINAL:
            return job
        time.sleep(0.05)
    raise AssertionError(f"任务 {job_id} 未在 {timeout}s 内结束")


def upload(client: TestClient, data: bytes, filename: str) -> dict:
    response = client.post("/v1/artifacts", files={"file": (filename, data)})
    assert response.status_code == 201, response.text
    return response.json()


def test_health_and_capabilities(client: TestClient) -> None:
    assert client.get("/healthz").json()["status"] == "ok"
    ready = client.get("/readyz")
    assert ready.status_code == 200 and ready.json()["status"] == "ready"
    caps = client.get("/v1/capabilities").json()
    names = {cap["name"] for cap in caps}
    assert {"mesh_inspect", "print_monitor"} <= names
    mesh_cap = next(cap for cap in caps if cap["name"] == "mesh_inspect")
    assert mesh_cap["version"] == "1.0.0"


def test_mesh_job_end_to_end(client: TestClient, stl_bytes: bytes) -> None:
    artifact = upload(client, stl_bytes, "cube.stl")
    assert artifact["size"] == len(stl_bytes)
    assert len(artifact["sha256"]) == 64

    response = client.post("/v1/jobs", json={
        "capability": "mesh_inspect",
        "artifact_id": artifact["id"],
        "params": {"decimate_ratio": 0.5},
    })
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["status"] == "queued"

    done = wait_job(client, job["id"])
    assert done["status"] == "succeeded", done
    report = done["result"]["report"]
    assert report["geometry"]["triangles"] == 12
    assert report["topology"]["watertight"] is True
    assert report["format"] == "stl-binary"
    lod = done["result"]["artifact"]
    assert lod["artifact_id"].startswith("art_")
    assert lod["size"] > 0
    assert done["links"]["artifact"] == f"/v1/artifacts/{lod['artifact_id']}"

    download = client.get(done["links"]["artifact"])
    assert download.status_code == 200
    assert download.content[:5] != b"solid"  # 二进制 STL


def test_image_job_end_to_end(client: TestClient, image_bytes: bytes) -> None:
    artifact = upload(client, image_bytes, "print.png")
    job = client.post("/v1/jobs", json={
        "capability": "print_monitor", "artifact_id": artifact["id"], "params": {}}).json()
    done = wait_job(client, job["id"])
    assert done["status"] == "succeeded"
    result = done["result"]
    assert result["verdict"] in {"pass", "warn", "fail"}
    assert 0 <= result["score"] <= 100
    assert set(result["signals"]) == {"blur", "exposure", "resolution", "defect"}


def test_idempotency_key_prevents_duplicate_jobs(client: TestClient, stl_bytes: bytes) -> None:
    artifact = upload(client, stl_bytes, "cube.stl")
    headers = {"Idempotency-Key": "demo-key-001"}
    body = {"capability": "mesh_inspect", "artifact_id": artifact["id"], "params": {}}
    first = client.post("/v1/jobs", json=body, headers=headers).json()
    second = client.post("/v1/jobs", json=body, headers=headers).json()
    assert first["id"] == second["id"]
    jobs = client.get("/v1/jobs", params={"capability": "mesh_inspect"}).json()
    assert jobs["count"] == 1


def test_error_semantics(client: TestClient, stl_bytes: bytes) -> None:
    # 未注册能力
    artifact = upload(client, stl_bytes, "cube.stl")
    response = client.post("/v1/jobs", json={
        "capability": "not_exist", "artifact_id": artifact["id"], "params": {}})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"

    # 产物不存在
    response = client.post("/v1/jobs", json={
        "capability": "mesh_inspect", "artifact_id": "art_missing", "params": {}})
    assert response.status_code == 404

    # 未知任务
    assert client.get("/v1/jobs/job_missing").status_code == 404

    # 不支持的文件类型
    response = client.post("/v1/artifacts", files={"file": ("a.exe", b"x")})
    assert response.status_code == 415

    # 空文件
    response = client.post("/v1/artifacts", files={"file": ("a.stl", b"")})
    assert response.status_code == 400

    # 参数类型错误 → pydantic 422
    assert client.post("/v1/jobs", json={"capability": "mesh_inspect"}).status_code == 422


def test_artifact_deduplicates_by_sha256(client: TestClient, stl_bytes: bytes) -> None:
    first = upload(client, stl_bytes, "cube.stl")
    second = upload(client, stl_bytes, "copy.stl")
    assert first["id"] == second["id"]


def test_metrics_and_stats(client: TestClient, stl_bytes: bytes) -> None:
    artifact = upload(client, stl_bytes, "cube.stl")
    job = client.post("/v1/jobs", json={
        "capability": "mesh_inspect", "artifact_id": artifact["id"], "params": {}}).json()
    wait_job(client, job["id"])

    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    text = metrics.text
    assert "http_requests_total" in text
    assert "algo_jobs_total" in text
    assert "algo_job_run_ms" in text

    stats = client.get("/v1/stats").json()
    assert stats["store"]["totals"]["succeeded"] >= 1
    assert stats["queue"]["accepted"] >= 1
    assert stats["metrics"]["counters"]
