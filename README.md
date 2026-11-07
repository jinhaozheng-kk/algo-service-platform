# algo-service-platform

> 面向 3D 打印场景的**算法应用平台**：把 3D 模型处理与视觉质检算法封装成可复用、可观测、可水平扩展的 HTTP 服务。
> 关注的问题不是“算法能不能跑”，而是**算法从 Demo 到规模化应用**中间的那一层工程：异步任务、幂等、重试、背压、超时、产物管理、版本化、指标与日志。

<p align="left">
  <a href="https://github.com/jinhaozheng-kk/algo-service-platform/actions/workflows/ci.yml"><img alt="ci" src="https://github.com/jinhaozheng-kk/algo-service-platform/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="python" src="https://img.shields.io/badge/python-3.10%2B-blue">
  <img alt="fastapi" src="https://img.shields.io/badge/FastAPI-0.115%2B-009688">
  <img alt="tests" src="https://img.shields.io/badge/tests-21%20passed-brightgreen">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-black">
</p>

## 为什么做这个项目

真实业务里，算法同学的模型往往先在 notebook 里跑通，然后卡在“怎么上线”：

- 一次请求跑 30 秒，同步接口直接超时 → 需要**异步任务 + 状态机**
- 用户重试点了三次，产生三份重复结果 → 需要**幂等键**
- 上游突发流量把内存打爆 → 需要**有界队列 + 背压（429）**
- 算法偶发失败，全量报错给用户 → 需要**重试 + 超时 + 错误分类**
- 出了事故查不到当时用的哪个模型版本、哪份输入 → 需要**版本化 + 内容寻址 + 结构化日志**
- 说不清服务到底稳不稳、慢在哪 → 需要**指标（QPS / 时延分位 / 失败率）**

本项目把这套“算法工程化接入”的最小闭环完整实现了一遍，并以 3D 打印的两个典型能力作为示范：

| 能力 | 说明 | 输入 |
| --- | --- | --- |
| `mesh_inspect` | 3D 模型可打印性检查：面积 / 体积 / 包围盒 / 水密性 / 非流形边 / 退化面，并检测重复顶点；可选输出 LOD 简化模型 | STL / OBJ |
| `print_monitor` | 打印过程图像质检：模糊度、曝光、分辨率、局部异常纹理，输出结论与**处置建议** | PNG / JPG |

## 架构

```mermaid
flowchart LR
    A[调用方 / CLI] -->|POST /v1/artifacts| B[API 层 FastAPI]
    A -->|POST /v1/jobs| B
    B -->|幂等校验 / 参数校验| C[(SQLite 任务表)]
    B -->|内容寻址写入| D[(产物存储 sha256)]
    B -->|有界队列 put_nowait| E{队列满?}
    E -->|是| F[429 背压]
    E -->|否| G[Worker 池]
    G -->|run_in_executor| H[CPU 线程池]
    H --> I[算法能力 registry\nmesh_inspect / print_monitor]
    I -->|结果 + 产物| G
    G -->|状态机 queued→running→succeeded/failed| C
    A -->|GET /v1/jobs/{id} 轮询| B
    B -->|/metrics| J[Prometheus 文本]
```

关键分层：

```
src/algo_platform/
├── main.py         # HTTP 协议层：路由、中间件、错误映射、访问日志
├── registry.py     # 能力注册表：能力名 + 版本 + 参数 + handler
├── queue.py        # 执行层：有界队列、worker 池、线程池、超时/重试/背压
├── algorithms/     # 算法层：纯函数，不感知 HTTP / 存储（可单测、可替换）
├── store.py        # 仓储层：SQLite（可替换为 Postgres）
├── artifacts.py    # 产物层：sha256 内容寻址 + 去重
├── metrics.py      # 可观测性：计数器 / 仪表盘 / 直方图
├── errors.py       # 错误分类学：4xx 可修正、429 可重试、5xx 需告警
└── cli.py          # 端到端演示客户端
```

## 快速开始

```bash
# 1) 安装依赖
python -m venv .venv
.venv/Scripts/activate          # Windows；Linux/macOS 用 source .venv/bin/activate
pip install -r requirements.txt

# 2) 生成示例数据（立方体 / 开口盒 / 打印监控图）
python examples/generate_samples.py

# 3) 启动服务（factory 模式，无导入副作用）
set PYTHONPATH=src              # Windows；Linux/macOS: export PYTHONPATH=src
uvicorn algo_platform.main:create_app --factory --port 8000
```

打开 <http://127.0.0.1:8000/docs> 可以直接在 Swagger 里点着调接口。

```bash
# 4) 一条命令跑通「上传 → 提交 → 轮询 → 结果」
python -m algo_platform.cli demo
```

线上部署：

```bash
docker compose up --build          # 或 docker build -t algo-service-platform . && docker run -p 8000:8000 ...
```

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/v1/artifacts` | 上传模型 / 图像（multipart），返回内容寻址的 `artifact_id` |
| `GET` | `/v1/artifacts/{id}` | 下载产物 |
| `GET` | `/v1/capabilities` | 查看已注册能力、版本与参数说明 |
| `POST` | `/v1/jobs` | 提交算法任务，`202` 返回任务；支持 `Idempotency-Key` 头 |
| `GET` | `/v1/jobs/{id}` | 查询任务状态 / 结果 / 运行时参数 |
| `GET` | `/v1/jobs` | 按状态、能力过滤任务列表 |
| `POST` | `/v1/jobs/{id}/cancel` | 取消排队中的任务 |
| `GET` | `/v1/stats` | 任务成功率、算法耗时 p95、队列状态、指标快照 |
| `GET` | `/metrics` | Prometheus 文本格式指标 |
| `GET` | `/healthz` `/readyz` | 存活 / 就绪探针 |

```bash
# 提交一个带 LOD 输出的模型检查任务
ART=$(curl -s -F "file=@examples/cube.stl" http://127.0.0.1:8000/v1/artifacts | python -c "import sys,json;print(json.load(sys.stdin)['id'])")
curl -s -X POST http://127.0.0.1:8000/v1/jobs \
  -H "Content-Type: application/json" -H "Idempotency-Key: demo-001" \
  -d "{\"capability\":\"mesh_inspect\",\"artifact_id\":\"$ART\",\"params\":{\"decimate_ratio\":0.4}}"
```

错误返回统一结构，调用方可据此决定重试还是告警：

```json
{"error": {"code": "queue_saturated", "message": "队列已满，请稍后重试", "detail": {"job_id": "job_..."}}}
```

| HTTP | code | 语义 |
| --- | --- | --- |
| 400 / 404 / 415 | `bad_request` / `not_found` / `unsupported_media_type` | 调用方问题，**不要重试** |
| 422 | FastAPI 校验 | 请求体不合法 |
| 429 | `queue_saturated` | 背压信号，**可退避重试** |
| 500 | `internal_error` | 服务端异常，**应告警** |

## 压测结果

环境：Windows 11 / i9-14900HX（24C32T）/ Python 3.12 / 单机回环网络。
方法：`bench/scaling.py`，固定 **200 个任务 / 并发 8**，每个任务 = 提交 + 轮询到终态；模型取 `cube.stl`（12 面）。
所有配置 **成功率 100%**，服务端算法耗时（`run_ms`）稳定在毫秒级：

| 执行模型 | 吞吐 (req/s) | 端到端 p50 (ms) | p95 (ms) | p99 (ms) | 算法耗时 avg / p95 (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| 单进程 / 1 worker | 69.91 | 87.88 | 254.40 | 345.91 | 3.04 / 5.01 |
| 单进程 / 2 worker | **89.96** | **68.19** | **184.26** | **232.60** | 2.94 / 4.98 |
| 单进程 / 4 worker | 80.76 | 79.03 | 195.66 | 307.33 | 3.34 / 6.51 |
| 4 进程 / 每进程 1 worker | 65.04 | 89.79 | 285.00 | 353.90 | 2.80 / 4.11 |

**怎么读这组数据（比数字本身更重要）：**

1. **端到端 p50 ≈ 70–90ms，而算法本身只要 ~3ms**。差额来自 HTTP 往返 + 10ms 轮询粒度。这说明压测瓶颈在“任务状态查询方式”，不在算法——生产上应改为**回调 / 长轮询 / WebSocket**，端到端时延可以再降一个量级。
2. **单进程内加 worker 收益有限**（2 worker 最好，4 worker 反而退化）。原因是 CPU 密集任务在同一进程里受 GIL 与线程切换限制；4 进程反而更慢，则暴露了另一个真实瓶颈——**SQLite 单写者锁**在多进程下成为串行点。
3. 因此生产路径是：**Redis / Kafka 做队列 + Postgres 做元数据 + 多实例无状态 API**，这个项目的仓储层与队列层就是按可替换接口设计的。

完整数据见 [`bench/results/scaling.json`](bench/results/scaling.json)。

## 设计决策

| 问题 | 做法 | 为什么 |
| --- | --- | --- |
| 慢算法阻塞接口 | 提交即返回 `202`，后台 worker 执行，客户端轮询 | 同步接口扛不住秒级算法；异步让超时可控、可重试 |
| 内存被突发流量打爆 | `asyncio.Queue(maxsize=N)`，满则立即 429 | 拒绝比堆积更安全；429 是可退避语义 |
| CPU 密集拖垮事件循环 | 算法在 `ThreadPoolExecutor` 执行，numpy 释放 GIL | HTTP 处理与算法执行隔离，避免一个慢任务拖垮所有请求 |
| 用户重复提交 | `Idempotency-Key` + 唯一索引，重复请求返回同一任务 | 移动端弱网重试很常见，重复执行既浪费又危险 |
| 算法偶发失败 | 指数退避重试（默认 2 次），超时强制失败 | 区分“可重试”和“不该重试”（4xx 直接失败） |
| 事故复盘困难 | 每次任务记录能力版本、参数快照、输入 sha256、`request_id` | 同一输入可复现；算法升级可灰度并能回溯 |
| 同一文件重复上传 | 内容寻址（sha256）落盘去重 | 省存储；天然支持“同输入同结果”的校验 |
| 看不到服务状态 | 结构化 JSON 日志 + Prometheus 指标 + `/v1/stats` | 排查问题从“猜”变成“看分位数” |
| 算法与平台耦合 | 能力注册表：算法只是 `(bytes, params) -> dict` 的纯函数 | 算法可独立单测、独立升级；平台不关心算法细节 |
| 错误语义混乱 | 统一 `{"error": {code, message, detail}}` + 错误分类学 | 调用方能程序化决定重试 / 告警 / 放弃 |

## 测试

```bash
pytest -q        # 21 passed
ruff check src tests examples bench
```

覆盖点：

- **算法正确性**：立方体体积/面积/包围盒精确值、开口盒水密性判定、非流形边、ASCII/Binary STL 与 OBJ（四边形三角化）、LOD 简化后体积偏差 < 15%
- **视觉质检**：清晰图 vs 高斯模糊图的拉普拉斯方差与分数排序、过曝识别、低分辨率告警、坏图抛领域异常而非崩溃
- **HTTP 契约**：上传 → 提交 → 轮询 → 下载全链路、幂等键去重、未知能力/产物/任务的状态码、415/413/422 错误语义、指标与统计接口
- **执行层**：瞬时错误重试后成功（`attempts=2`）、超时快速失败、队列满返回 429 且被拒任务落库为失败（不产生“幽灵任务”）

## 项目结构

```
.
├── src/algo_platform/          # 平台代码（见上方分层）
├── src/algo_platform/algorithms/# 3D 几何 + 视觉质检算法（纯函数）
├── examples/                   # 示例生成脚本与样例文件（示例即回归用例）
├── tests/                      # 21 个测试
├── bench/
│   ├── load_test.py            # 端到端压测（含服务端耗时汇总）
│   ├── scaling.py              # 固定变量的执行模型对比
│   ├── run_matrix.py           # 多配置矩阵压测
│   └── results/                # 实测数据
├── .github/workflows/ci.yml    # 双 Python 版本 + lint + 测试 + Docker 构建
├── Dockerfile / docker-compose.yml
└── pyproject.toml
```

## 运行参数

| 环境变量 | 默认 | 说明 |
| --- | ---: | --- |
| `ALGO_DATA_DIR` | `./data` | SQLite 与产物目录 |
| `ALGO_WORKERS` | 4 | 单进程内 worker 数 |
| `ALGO_CPU_THREADS` | 4 | 算法线程池大小 |
| `ALGO_QUEUE_CAPACITY` | 256 | 有界队列容量，满则 429 |
| `ALGO_JOB_TIMEOUT_S` | 20 | 单任务超时（秒） |
| `ALGO_MAX_RETRIES` | 2 | 可重试错误的最大重试次数 |
| `ALGO_MAX_ARTIFACT_MB` | 32 | 上传产物大小上限 |
| `ALGO_LOG_LEVEL` | INFO | JSON 日志级别 |

## Roadmap

- [ ] 队列与元数据换成 Redis + Postgres，支持多实例横向扩展（接口已按可替换设计）
- [ ] 任务完成回调 / 长轮询，替代固定间隔轮询，进一步压低端到端时延
- [ ] 能力版本灰度：同一能力多版本并行，按流量比例路由
- [ ] 真实视觉模型接入（检测/分割 GPU 推理服务化），补充批量推理与模型缓存
- [ ] OpenTelemetry 链路追踪，串联“上传 → 队列 → 算法 → 产物”全链路

## License

MIT
