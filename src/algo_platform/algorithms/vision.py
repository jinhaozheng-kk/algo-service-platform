"""打印监控图像质检：模糊度 / 曝光 / 分辨率 / 局部异常。

作为「视觉算法工程化」示例：算法本身是轻量的量化指标，
重点是把它包装成可版本化、可解释、能给出处置建议的服务能力。
"""
from __future__ import annotations

import io

import numpy as np
from PIL import Image

MAX_ANALYSIS_SIDE = 1024


class ImageFormatError(ValueError):
    """图像无法解析。"""


def _laplacian_variance(gray: np.ndarray) -> float:
    k = np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]], dtype=np.float64)
    h, w = gray.shape
    if h < 3 or w < 3:
        return 0.0
    out = np.zeros((h - 2, w - 2))
    for dy in range(3):
        for dx in range(3):
            if k[dy, dx]:
                out += k[dy, dx] * gray[dy:dy + h - 2, dx:dx + w - 2]
    return float(out.var())


def _load_gray(data: bytes) -> tuple[np.ndarray, int, int]:
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:  # noqa: BLE001 - 统一转换为领域异常
        raise ImageFormatError(f"无法解析图像：{exc}") from exc
    width, height = image.size
    image = image.convert("RGB")
    if max(width, height) > MAX_ANALYSIS_SIDE:
        scale = MAX_ANALYSIS_SIDE / max(width, height)
        image = image.resize((max(1, int(width * scale)), max(1, int(height * scale))),
                             Image.Resampling.BILINEAR)
    gray = np.asarray(image.convert("L"), dtype=np.float64)
    return gray, width, height


def analyze_image(data: bytes, min_side: int = 720) -> dict:
    gray, width, height = _load_gray(data)
    detail = max(1.0, _laplacian_variance(gray))
    brightness = float(gray.mean())
    over = float((gray >= 245).mean())
    under = float((gray <= 10).mean())
    contrast = float(gray.std())

    grad_y, grad_x = np.gradient(gray)
    magnitude = np.hypot(grad_x, grad_y)
    threshold = magnitude.mean() + 4 * magnitude.std()
    anomaly_ratio = float((magnitude > threshold).mean())

    blur_score = min(1.0, detail / 260.0)
    exposure_score = max(0.0, 1.0 - min(1.0, over * 6 + under * 4))
    resolution_score = min(1.0, min(width, height) / max(1, min_side))
    defect_score = max(0.0, 1.0 - min(1.0, anomaly_ratio * 25))

    score = 100 * (0.35 * blur_score + 0.25 * exposure_score
                   + 0.15 * resolution_score + 0.25 * defect_score)
    issues: list[str] = []
    suggestions: list[str] = []
    if detail < 60:
        issues.append(f"图像偏糊（拉普拉斯方差 {detail:.1f} < 60）")
        suggestions.append("检查相机对焦 / 镜头清洁度，或提高快门速度")
    if over > 0.05:
        issues.append(f"过曝像素占比 {over:.1%}")
        suggestions.append("降低曝光时间或加装遮光罩，避免高光区域丢失细节")
    if brightness < 45:
        issues.append(f"整体偏暗（平均亮度 {brightness:.0f}）")
        suggestions.append("补充环形光源或提高曝光")
    if min(width, height) < min_side:
        issues.append(f"分辨率偏低（{width}×{height}，建议短边 ≥ {min_side}）")
    if anomaly_ratio > 0.02:
        issues.append(f"局部异常纹理占比 {anomaly_ratio:.1%}，疑似拉丝 / 缺料 / 异物")
        suggestions.append("人工复核异常区域；若为固定位置，检查喷头与平台附着")

    verdict = "pass" if score >= 80 else ("warn" if score >= 60 else "fail")
    return {
        "verdict": verdict,
        "score": round(score, 2),
        "image": {
            "width": width, "height": height,
            "blur_laplacian_var": round(detail, 2),
            "brightness": round(brightness, 2),
            "contrast": round(contrast, 2),
            "overexposed_ratio": round(over, 4),
            "underexposed_ratio": round(under, 4),
            "anomaly_ratio": round(anomaly_ratio, 4),
        },
        "signals": {
            "blur": round(blur_score, 4),
            "exposure": round(exposure_score, 4),
            "resolution": round(resolution_score, 4),
            "defect": round(defect_score, 4),
        },
        "issues": issues,
        "suggestions": suggestions,
    }
