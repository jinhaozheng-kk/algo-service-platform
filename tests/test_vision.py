"""视觉质检测试：模糊、过曝、低分辨率应被稳定识别。"""
from __future__ import annotations

from io import BytesIO

from algo_platform.algorithms.vision import analyze_image
from examples.generate_samples import print_photo


def _png(image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_sharp_image_scores_higher_than_blurry(image_bytes: bytes, blurry_image_bytes: bytes) -> None:
    sharp = analyze_image(image_bytes)
    blurred = analyze_image(blurry_image_bytes)
    assert sharp["image"]["blur_laplacian_var"] > blurred["image"]["blur_laplacian_var"]
    assert sharp["score"] > blurred["score"]
    assert blurred["verdict"] in {"warn", "fail"}
    assert any("偏糊" in issue for issue in blurred["issues"])


def test_overexposed_image_is_flagged() -> None:
    result = analyze_image(_png(print_photo(overexposed=True)))
    assert result["image"]["overexposed_ratio"] > 0.05
    assert any("过曝" in issue for issue in result["issues"])
    assert result["score"] < 100


def test_low_resolution_warning() -> None:
    result = analyze_image(_png(print_photo(width=320, height=240)), min_side=720)
    assert any("分辨率偏低" in issue for issue in result["issues"])
    assert result["signals"]["resolution"] < 1.0


def test_missing_artifact_is_not_a_crash() -> None:
    import pytest

    from algo_platform.algorithms.vision import ImageFormatError
    with pytest.raises(ImageFormatError):
        analyze_image(b"definitely not an image")
