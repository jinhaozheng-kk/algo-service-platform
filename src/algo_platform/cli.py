"""命令行客户端：一条命令跑通「上传 → 提交 → 轮询 → 打印结果」的完整链路。

用法：
    python -m algo_platform.cli demo
    python -m algo_platform.cli demo --base-url http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
import uuid
from pathlib import Path

DEFAULT_BASE = "http://127.0.0.1:8000"


def _request(method: str, url: str, *, data: bytes | None = None,
             headers: dict[str, str] | None = None, timeout: float = 30) -> dict:
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        return json.loads(body) if body else {}


def upload(base: str, path: Path) -> dict:
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    return _request("POST", f"{base}/v1/artifacts", data=body, headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}"})


def submit(base: str, capability: str, artifact_id: str, params: dict) -> dict:
    body = json.dumps({"capability": capability, "artifact_id": artifact_id,
                       "params": params}).encode()
    return _request("POST", f"{base}/v1/jobs", data=body,
                    headers={"Content-Type": "application/json"})


def poll(base: str, job_id: str, timeout: float = 60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = _request("GET", f"{base}/v1/jobs/{job_id}")
        if job["status"] in {"succeeded", "failed", "canceled"}:
            return job
        time.sleep(0.2)
    raise TimeoutError(f"任务 {job_id} 在 {timeout}s 内未完成")


def run_demo(base: str) -> int:
    examples = Path(__file__).resolve().parents[2] / "examples"
    stl = examples / "cube.stl"
    image = examples / "print_sample.png"
    for path in (stl, image):
        if not path.exists():
            raise SystemExit(f"缺少示例文件：{path}（先运行 python examples/generate_samples.py）")

    print(f"[1/4] 上传模型 {stl.name}")
    artifact = upload(base, stl)
    print("    ->", artifact["id"], artifact["sha256"][:12], artifact["size"], "bytes")

    print("[2/4] 提交 mesh_inspect（含 LOD 简化）")
    job = submit(base, "mesh_inspect", artifact["id"], {"decimate_ratio": 0.4})
    print("    ->", job["id"], job["status"])

    print("[3/4] 轮询任务")
    done = poll(base, job["id"])
    print("    ->", done["status"], f'{done["run_ms"]}ms')
    print(json.dumps(done.get("result"), ensure_ascii=False, indent=2)[:1200])

    print("[4/4] 上传打印图片并提交 print_monitor")
    image_artifact = upload(base, image)
    image_job = submit(base, "print_monitor", image_artifact["id"], {})
    done = poll(base, image_job["id"])
    print("    ->", done["status"], f'{done["run_ms"]}ms')
    print(json.dumps(done.get("result"), ensure_ascii=False, indent=2)[:1200])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="algo-service-platform CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="跑通上传 → 提交 → 轮询 → 结果打印")
    demo.add_argument("--base-url", default=DEFAULT_BASE)
    args = parser.parse_args(argv)
    if args.command == "demo":
        return run_demo(args.base_url.rstrip("/"))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
