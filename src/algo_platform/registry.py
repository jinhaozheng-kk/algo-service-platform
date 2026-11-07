"""能力注册表：算法以「能力 + 版本 + 参数」的形式对外暴露。

这是平台化的关键抽象——业务侧只依赖能力名，不关心算法实现；
算法升级时通过版本号灰度，历史任务仍可追溯当时使用的版本。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .algorithms.mesh import MeshFormatError, analyze_mesh, decimate, parse_mesh, to_binary_stl
from .algorithms.vision import ImageFormatError, analyze_image
from .errors import BadRequest

Handler = Callable[[bytes, dict[str, Any], str], dict[str, Any]]


@dataclass(frozen=True)
class Capability:
    name: str
    version: str
    input_kind: str  # mesh | image
    description: str
    handler: Handler
    default_params: dict[str, Any] = field(default_factory=dict)
    param_hints: dict[str, str] = field(default_factory=dict)


def _mesh_inspect(data: bytes, params: dict[str, Any], filename: str) -> dict[str, Any]:
    try:
        mesh, fmt = parse_mesh(data, filename)
    except MeshFormatError as exc:
        raise BadRequest(str(exc), detail={"filename": filename}) from exc

    report = analyze_mesh(mesh)
    report["format"] = fmt
    ratio = float(params.get("decimate_ratio", 0) or 0)
    artifact = None
    if ratio > 0:
        lod, info = decimate(mesh, ratio)
        artifact = {
            "filename": "lod.stl",
            "media_type": "model/stl",
            "data": to_binary_stl(lod),
            "stats": info,
        }
        report["lod"] = info
    return {"report": report, "artifact": artifact}


def _print_monitor(data: bytes, params: dict[str, Any], filename: str) -> dict[str, Any]:
    try:
        return analyze_image(data, min_side=int(params.get("min_side", 720)))
    except ImageFormatError as exc:
        raise BadRequest(str(exc), detail={"filename": filename}) from exc


DEFAULT_REGISTRY: dict[str, Capability] = {
    "mesh_inspect": Capability(
        name="mesh_inspect",
        version="1.0.0",
        input_kind="mesh",
        description="3D 模型可打印性检查：几何统计、水密性 / 非流形检测、可选 LOD 简化",
        handler=_mesh_inspect,
        default_params={"decimate_ratio": 0},
        param_hints={"decimate_ratio": "0-0.95，>0 时输出 LOD 模型产物"},
    ),
    "print_monitor": Capability(
        name="print_monitor",
        version="1.0.0",
        input_kind="image",
        description="打印过程图像质检：模糊度、曝光、分辨率、局部异常，输出结论与处置建议",
        handler=_print_monitor,
        default_params={"min_side": 720},
        param_hints={"min_side": "期望的最小图像短边像素，默认 720"},
    ),
}
