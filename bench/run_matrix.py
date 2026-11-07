"""一键跑压测矩阵：不同 worker / 并发配置下启动服务并压测，输出对比表。

在本地（Windows / Linux 均可）执行：
    python bench/run_matrix.py

它会临时启动 uvicorn 子进程，压测完成后自动关闭，不影响已有服务。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = Path(__file__).resolve().parent / "results"

MATRIX = [
    {"label": "w1_c4", "workers": 1, "cpu_threads": 2, "concurrency": 4, "jobs": 120},
    {"label": "w4_c8", "workers": 4, "cpu_threads": 4, "concurrency": 8, "jobs": 240},
    {"label": "w4_c32", "workers": 4, "cpu_threads": 4, "concurrency": 32, "jobs": 320},
]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_ready(base_url: str, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/healthz", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("服务未在预期时间内就绪")


def run_case(case: dict) -> dict:
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(ROOT / "src"),
        "ALGO_WORKERS": str(case["workers"]),
        "ALGO_CPU_THREADS": str(case["cpu_threads"]),
        "ALGO_QUEUE_CAPACITY": "256",
        "ALGO_DATA_DIR": str(Path(os.environ.get("TEMP", ".")) / "algo-bench" / case["label"]),
        "ALGO_LOG_LEVEL": "WARNING",
    })
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "algo_platform.main:create_app", "--factory",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        wait_ready(base_url)
        summary_path = RESULTS / f"{case['label']}.json"
        subprocess.run(
            [sys.executable, str(Path(__file__).parent / "load_test.py"),
             "--base-url", base_url,
             "--artifact", str(ROOT / "examples" / "cube.stl"),
             "--capability", "mesh_inspect",
             "--jobs", str(case["jobs"]),
             "--concurrency", str(case["concurrency"]),
             "--label", case["label"]],
            check=True, cwd=str(ROOT), env=env,
        )
        return json.loads(summary_path.read_text(encoding="utf-8"))
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


def main() -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    rows = []
    for case in MATRIX:
        print(f"--- {case['label']}: workers={case['workers']} concurrency={case['concurrency']}")
        summary = run_case(case)
        rows.append({**case, **summary})
        print(json.dumps(summary["latency_ms"], ensure_ascii=False),
              "rps=", summary["throughput_rps"], "success=", summary["success_rate"])

    out = RESULTS / "matrix.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n矩阵结果: {out}")
    print("\n| 配置 | 并发 | 任务数 | 吞吐 (req/s) | p50 (ms) | p95 (ms) | p99 (ms) | 成功率 |")
    print("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for row in rows:
        lat = row["latency_ms"]
        print(f"| workers={row['workers']} / threads={row['cpu_threads']} | {row['concurrency']} | "
              f"{row['jobs']} | {row['throughput_rps']} | {lat['p50']} | {lat['p95']} | "
              f"{lat['p99']} | {row['success_rate']:.1%} |")


if __name__ == "__main__":
    main()
