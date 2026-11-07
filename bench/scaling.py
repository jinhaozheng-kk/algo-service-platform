"""单变量压测：固定 200 个任务 / 并发 8，只改变执行模型，得到干净的对比数据。

对比：
  1) 单进程 + 1 个 worker
  2) 单进程 + 2 个 worker
  3) 单进程 + 4 个 worker
  4) 4 个 uvicorn 进程（每进程 1 个 worker）—— 绕过 GIL 的推荐部署方式
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
PY = sys.executable

CASES = [
    {"label": "p1_w1_c8", "procs": 1, "workers": 1, "threads": 2},
    {"label": "p1_w2_c8", "procs": 1, "workers": 2, "threads": 2},
    {"label": "p1_w4_c8", "procs": 1, "workers": 4, "threads": 4},
    {"label": "p4_w1_c8", "procs": 4, "workers": 1, "threads": 2},
]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_ready(base_url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/healthz", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("服务未就绪")


def main() -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    rows = []
    for case in CASES:
        port = free_port()
        base_url = f"http://127.0.0.1:{port}"
        env = os.environ.copy()
        env.update({
            "PYTHONPATH": str(ROOT / "src"),
            "ALGO_WORKERS": str(case["workers"]),
            "ALGO_CPU_THREADS": str(case["threads"]),
            "ALGO_QUEUE_CAPACITY": "512",
            "ALGO_DATA_DIR": str(ROOT.parent / "infra" / f"bench-{case['label']}"),
            "ALGO_LOG_LEVEL": "ERROR",
        })
        command = [PY, "-m", "uvicorn", "algo_platform.main:create_app", "--factory",
                   "--host", "127.0.0.1", "--port", str(port), "--log-level", "error"]
        if case["procs"] > 1:
            command += ["--workers", str(case["procs"])]
        server = subprocess.Popen(command, cwd=str(ROOT), env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_ready(base_url)
            subprocess.run(
                [PY, str(Path(__file__).parent / "load_test.py"),
                 "--base-url", base_url,
                 "--artifact", str(ROOT / "examples" / "cube.stl"),
                 "--jobs", "200", "--concurrency", "8",
                 "--label", case["label"]],
                check=True, cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL,
            )
            summary = json.loads((RESULTS / f"{case['label']}.json").read_text(encoding="utf-8"))
            rows.append({**case, **summary})
            latency = summary["latency_ms"]
            print(f"{case['label']}: rps={summary['throughput_rps']} "
                  f"p50={latency['p50']} p95={latency['p95']} p99={latency['p99']} "
                  f"success={summary['success_rate']}")
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()

    out = RESULTS / "scaling.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwritten {out}")
    print("\n| 执行模型 | 吞吐 (req/s) | p50 (ms) | p95 (ms) | p99 (ms) | 成功率 |")
    print("| --- | ---: | ---: | ---: | ---: | ---: |")
    for row in rows:
        latency = row["latency_ms"]
        model = f"单进程 / {row['workers']} worker" if row["procs"] == 1 else f"{row['procs']} 进程 / 每进程 {row['workers']} worker"
        print(f"| {model} | {row['throughput_rps']} | {latency['p50']} | {latency['p95']} "
              f"| {latency['p99']} | {row['success_rate']:.1%} |")


if __name__ == "__main__":
    main()
