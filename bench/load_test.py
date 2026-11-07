"""端到端压测：提交任务 → 轮询到终态，统计吞吐与端到端时延分位数。

用法：
    python bench/load_test.py --base-url http://127.0.0.1:8000 \
        --artifact examples/cube.stl --capability mesh_inspect \
        --jobs 200 --concurrency 8

输出：终端摘要 + bench/results/*.json（含每个任务的时延明细）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import uuid
from pathlib import Path

import httpx


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return round(ordered[index], 2)


async def upload(client: httpx.AsyncClient, base_url: str, path: Path) -> str:
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    response = await client.post(
        f"{base_url}/v1/artifacts", content=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    response.raise_for_status()
    return response.json()["id"]


async def run_one(client: httpx.AsyncClient, base_url: str, artifact_id: str,
                  capability: str, params: dict) -> tuple[float, str]:
    started = time.perf_counter()
    response = await client.post(f"{base_url}/v1/jobs", json={
        "capability": capability, "artifact_id": artifact_id, "params": params,
    })
    if response.status_code != 202:
        return (time.perf_counter() - started) * 1000, f"http_{response.status_code}"
    job_id = response.json()["id"]
    while True:
        poll = await client.get(f"{base_url}/v1/jobs/{job_id}")
        status = poll.json()["status"]
        if status in {"succeeded", "failed", "canceled"}:
            return (time.perf_counter() - started) * 1000, status
        await asyncio.sleep(0.01)


async def main_async(args: argparse.Namespace) -> dict:
    limits = httpx.Limits(max_connections=args.concurrency * 4)
    timeout = httpx.Timeout(60.0)
    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        artifact_id = await upload(client, args.base_url, Path(args.artifact))
        semaphore = asyncio.Semaphore(args.concurrency)

        async def worker(results: list, statuses: list) -> None:
            async with semaphore:
                latency, status = await run_one(
                    client, args.base_url, artifact_id, args.capability, args.params)
                results.append(latency)
                statuses.append(status)

        started = time.perf_counter()
        latencies: list[float] = []
        statuses: list[str] = []
        await asyncio.gather(*[worker(latencies, statuses) for _ in range(args.jobs)])
        elapsed = time.perf_counter() - started

    success = sum(1 for status in statuses if status == "succeeded")
    async with httpx.AsyncClient(timeout=timeout) as stats_client:
        stats = (await stats_client.get(f"{args.base_url}/v1/stats")).json()
    return {
        "config": {"jobs": args.jobs, "concurrency": args.concurrency,
                   "capability": args.capability, "artifact": Path(args.artifact).name},
        "elapsed_s": round(elapsed, 3),
        "throughput_rps": round(args.jobs / elapsed, 2),
        "success": success,
        "failure": args.jobs - success,
        "success_rate": round(success / max(1, args.jobs), 4),
        "latency_ms": {
            "mean": round(statistics.mean(latencies), 2),
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "p99": percentile(latencies, 0.99),
            "max": round(max(latencies), 2),
        },
        "server": {
            "run_ms": stats["store"]["run_ms"],
            "queue": stats["queue"],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="algo-service-platform load test")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--artifact", default="examples/cube.stl")
    parser.add_argument("--capability", default="mesh_inspect")
    parser.add_argument("--params", default='{"decimate_ratio": 0.4}')
    parser.add_argument("--jobs", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--label", default="run")
    args = parser.parse_args()
    args.base_url = args.base_url.rstrip("/")
    args.params = json.loads(args.params)

    summary = asyncio.run(main_async(args))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    out_dir = Path(__file__).resolve().parent / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{args.label}.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"written: {out}")


if __name__ == "__main__":
    main()
