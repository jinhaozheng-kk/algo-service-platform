"""极简指标注册表：计数器 / 仪表盘 / 直方图，输出 Prometheus 文本格式。

不引入 prometheus_client，是为了让平台在零外部依赖下也能跑通；
生产环境可在此基础上替换为官方 SDK，接口保持不变。
"""
from __future__ import annotations

import math
import threading
from collections import defaultdict

LabelKey = tuple[tuple[str, str], ...]


def _key(labels: dict[str, str] | None) -> LabelKey:
    return tuple(sorted((labels or {}).items()))


def _render_labels(labels: LabelKey, extra: tuple[str, str] | None = None) -> str:
    items = list(labels)
    if extra:
        items.append(extra)
    if not items:
        return ""
    inner = ",".join(f'{k}="{v}"' for k, v in sorted(items))
    return "{" + inner + "}"


def _fmt(value: float) -> str:
    if math.isinf(value):
        return "+Inf"
    return f"{value:g}"


class Metrics:
    """线程安全的进程内指标注册表。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, LabelKey], float] = defaultdict(float)
        self._gauges: dict[tuple[str, LabelKey], float] = {}
        self._hist: dict[tuple[str, LabelKey], list[float]] = defaultdict(list)
        self._hist_max_samples = 20_000

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        with self._lock:
            self._counters[(name, _key(labels))] += value

    def set_gauge(self, name: str, value: float, **labels: str) -> None:
        with self._lock:
            self._gauges[(name, _key(labels))] = value

    def observe(self, name: str, value: float, **labels: str) -> None:
        key = (name, _key(labels))
        with self._lock:
            bucket = self._hist[key]
            if len(bucket) < self._hist_max_samples:
                bucket.append(value)

    @staticmethod
    def _quantile(values: list[float], q: float) -> float:
        if not values:
            return 0.0
        idx = min(len(values) - 1, max(0, int(round(q * (len(values) - 1)))))
        return round(values[idx], 4)

    def snapshot(self) -> dict[str, dict[str, float]]:
        """给 /v1/stats 用的聚合视图（avg / p50 / p95 / p99）。"""
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            hist = {k: sorted(v) for k, v in self._hist.items() if v}

        out: dict[str, dict[str, float]] = {"counters": {}, "gauges": {}, "histograms": {}}
        for (name, labels), value in counters.items():
            out["counters"][f"{name}{_render_labels(labels)}"] = round(value, 4)
        for (name, labels), value in gauges.items():
            out["gauges"][f"{name}{_render_labels(labels)}"] = round(value, 4)
        for (name, labels), values in hist.items():
            out["histograms"][f"{name}{_render_labels(labels)}"] = {
                "count": len(values),
                "avg": round(sum(values) / len(values), 4),
                "p50": self._quantile(values, 0.50),
                "p95": self._quantile(values, 0.95),
                "p99": self._quantile(values, 0.99),
                "max": round(values[-1], 4),
            }
        return out

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            for (name, labels), value in sorted(self._counters.items()):
                lines.append(f"{name}{_render_labels(labels)} {_fmt(value)}")
            for (name, labels), value in sorted(self._gauges.items()):
                lines.append(f"{name}{_render_labels(labels)} {_fmt(value)}")
            for (name, labels), values in sorted(self._hist.items()):
                if not values:
                    continue
                ordered = sorted(values)
                for q in (0.5, 0.95, 0.99):
                    lines.append(
                        f"{name}{_render_labels(labels, ('quantile', str(q)))} "
                        f"{_fmt(ordered[self._quantile_index(ordered, q)])}"
                    )
                lines.append(f"{name}_count{_render_labels(labels)} {len(ordered)}")
                lines.append(f"{name}_sum{_render_labels(labels)} {_fmt(sum(ordered))}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _quantile_index(ordered: list[float], q: float) -> int:
        return min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._hist.clear()
