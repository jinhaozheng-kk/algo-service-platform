"""生成示例数据：立方体 / 开口盒模型、合成打印监控图像（清晰 + 模糊 + 过曝）。

这些文件同时被 CLI demo、测试与压测脚本复用，保证「示例即回归用例」。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from algo_platform.algorithms.mesh import Mesh, to_binary_stl  # noqa: E402

EXAMPLES = ROOT / "examples"


def cube_mesh(size: float = 20.0, subdivisions: int = 1) -> Mesh:
    """生成尺寸为 size 的立方体；subdivisions 越大三角形越多。"""
    half = size / 2
    verts: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []

    # 六个面：每个面用 (u, v) 参数化，方便按 subdivisions 细分
    planes = [
        ((1, 0, 0), (0, 1, 0), half), ((1, 0, 0), (0, 1, 0), -half),
        ((0, 1, 0), (0, 0, 1), half), ((0, 1, 0), (0, 0, 1), -half),
        ((0, 0, 1), (1, 0, 0), half), ((0, 0, 1), (1, 0, 0), -half),
    ]
    for normal, up, offset in planes:
        normal_v, up_v = np.array(normal, float), np.array(up, float)
        right_v = np.cross(up_v, normal_v)
        base = len(verts)
        grid: dict[tuple[int, int], int] = {}
        for i in range(subdivisions + 1):
            for j in range(subdivisions + 1):
                u = (i / subdivisions - 0.5) * size
                v = (j / subdivisions - 0.5) * size
                point = normal_v * offset + right_v * u + up_v * v
                grid[(i, j)] = len(verts)
                verts.append(tuple(point))
        for i in range(subdivisions):
            for j in range(subdivisions):
                a, b = grid[(i, j)], grid[(i + 1, j)]
                c, d = grid[(i + 1, j + 1)], grid[(i, j + 1)]
                faces.append((a, b, c))
                faces.append((a, c, d))
        _ = base
    vertices = np.asarray(verts, float)
    face_array = np.asarray(faces, np.int64)
    # 统一面朝向：法向与“面心 - 体心”同向，保证体积为正、法向朝外
    triangles = vertices[face_array]
    normals = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    outward = np.einsum("ij,ij->i", normals, triangles.mean(axis=1) - vertices.mean(axis=0)) < 0
    face_array[outward] = face_array[outward][:, [0, 2, 1]]
    return Mesh(vertices=vertices, faces=face_array)


def open_box_mesh(size: float = 20.0) -> Mesh:
    """去掉顶面的立方体：不水密，用于演示缺陷检测。"""
    mesh = cube_mesh(size, subdivisions=1)
    # 顶面是最后 2 个三角形（见 planes 顺序），直接丢弃
    return Mesh(vertices=mesh.vertices, faces=mesh.faces[:-2])


def print_photo(width: int = 960, height: int = 720, *, blur: bool = False,
                overexposed: bool = False) -> Image.Image:
    """合成一张“打印过程监控图”：层纹 + 局部缺料暗斑 + 高光。"""
    rng = np.random.default_rng(42)
    canvas = np.full((height, width, 3), 118, dtype=np.float64)
    yy, xx = np.mgrid[0:height, 0:width]
    # 层纹（水平亮暗条纹）
    canvas += 14 * np.sin(yy / 7.0)[..., None]
    # 打印件轮廓：中间的矩形区域更亮
    part = (xx > width * 0.18) & (xx < width * 0.82) & (yy > height * 0.25) & (yy < height * 0.85)
    canvas[part] += 34
    # 缺料暗斑
    spot = (xx - width * 0.42) ** 2 + (yy - height * 0.55) ** 2 < (width * 0.045) ** 2
    canvas[spot] -= 70
    # 高光
    highlight = (xx - width * 0.68) ** 2 + (yy - height * 0.35) ** 2 < (width * 0.05) ** 2
    canvas[highlight] += 80
    canvas += rng.normal(0, 3.2, size=canvas.shape)
    if overexposed:
        canvas = canvas * 2.2
    canvas = np.clip(canvas, 0, 255).astype(np.uint8)
    image = Image.fromarray(canvas, "RGB").convert("RGB")
    if blur:
        image = image.filter(ImageFilter.GaussianBlur(6))
    return image


def main() -> None:
    EXAMPLES.mkdir(parents=True, exist_ok=True)
    (EXAMPLES / "cube.stl").write_bytes(to_binary_stl(cube_mesh()))
    (EXAMPLES / "open_box.stl").write_bytes(to_binary_stl(open_box_mesh()))
    (EXAMPLES / "detail.stl").write_bytes(to_binary_stl(cube_mesh(subdivisions=6)))
    print_photo().save(EXAMPLES / "print_sample.png")
    print_photo(blur=True).save(EXAMPLES / "print_blurry.png")
    print_photo(overexposed=True).save(EXAMPLES / "print_overexposed.png")
    print("examples generated:")
    for path in sorted(p for p in EXAMPLES.glob("*") if p.is_file()):
        if path.name == "generate_samples.py":
            continue
        print(f"  {path.name:24s} {path.stat().st_size:>9,d} bytes")


if __name__ == "__main__":
    main()
