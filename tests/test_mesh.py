"""几何算法测试：指标正确性、拓扑判定、格式兼容、LOD 简化。"""
from __future__ import annotations

import numpy as np
import pytest

from algo_platform.algorithms.mesh import (
    Mesh,
    analyze_mesh,
    decimate,
    edge_manifold_stats,
    parse_mesh,
    signed_volume,
    to_binary_stl,
)
from examples.generate_samples import cube_mesh

SIZE = 20.0


def test_cube_metrics(stl_bytes: bytes) -> None:
    mesh, fmt = parse_mesh(stl_bytes, "cube.stl")
    assert fmt == "stl-binary"
    assert mesh.triangle_count == 12
    report = analyze_mesh(mesh)
    geom = report["geometry"]
    assert geom["triangles"] == 12
    assert geom["surface_area"] == pytest.approx(6 * SIZE ** 2, rel=1e-3)
    assert geom["volume"] == pytest.approx(SIZE ** 3, rel=1e-3)
    assert geom["extent"] == pytest.approx([SIZE, SIZE, SIZE], rel=1e-6)
    assert report["topology"]["watertight"] is True
    assert report["topology"]["boundary_edges"] == 0
    assert report["verdict"] == "pass"
    assert report["score"] == 100.0
    assert report["issues"] == []


def test_open_box_is_not_watertight(open_stl_bytes: bytes) -> None:
    mesh, _ = parse_mesh(open_stl_bytes, "open_box.stl")
    report = analyze_mesh(mesh)
    assert report["topology"]["watertight"] is False
    assert report["topology"]["boundary_edges"] == 4  # 缺一个面 → 4 条边界边
    assert report["score"] < 80
    assert report["verdict"] in {"warn", "fail"}
    assert any("非水密" in issue for issue in report["issues"])
    assert report["suggestions"]


def test_ascii_stl_roundtrip() -> None:
    triangle = [
        "solid t", "facet normal 0 0 1",
        "outer loop", "vertex 0 0 0", "vertex 1 0 0", "vertex 0 1 0",
        "endloop", "endfacet", "endsolid t",
    ]
    mesh, fmt = parse_mesh(("\n".join(triangle)).encode(), "t.stl")
    assert fmt == "stl-ascii"
    assert mesh.triangle_count == 1
    assert mesh.vertices.shape == (3, 3)


def test_obj_quad_triangulates() -> None:
    obj = b"v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nf 1 2 3 4\n"
    mesh, fmt = parse_mesh(obj, "quad.obj")
    assert fmt == "obj"
    assert mesh.triangle_count == 2


def test_bad_format_raises() -> None:
    from algo_platform.algorithms.mesh import MeshFormatError
    with pytest.raises(MeshFormatError):
        parse_mesh(b"not a mesh", "model.xyz")


def test_decimate_reduces_triangles_and_keeps_volume() -> None:
    dense = cube_mesh(size=SIZE, subdivisions=6)
    assert dense.triangle_count == 6 * 6 * 6 * 2
    lod, info = decimate(dense, keep_ratio=0.6)
    assert lod.triangle_count < dense.triangle_count
    assert info["actual_ratio"] <= 0.6
    assert info["input_triangles"] == dense.triangle_count
    assert info["output_triangles"] == lod.triangle_count
    drift = abs(signed_volume(lod) - signed_volume(dense)) / abs(signed_volume(dense))
    assert drift < 0.15  # 预览用 LOD 允许 ~7% 体积偏差
    payload = to_binary_stl(lod)
    reread, _ = parse_mesh(payload, "lod.stl")
    assert reread.triangle_count == lod.triangle_count


def test_edge_stats_on_non_manifold_mesh() -> None:
    # 三个三角形共一条边 → 非流形
    vertices = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1]], float)
    faces = np.array([[0, 1, 2], [0, 1, 3], [0, 1, 4]], np.int64)
    stats = edge_manifold_stats(Mesh(vertices=vertices, faces=faces))
    assert stats["non_manifold_edges"] == 1
    assert stats["watertight"] is False
