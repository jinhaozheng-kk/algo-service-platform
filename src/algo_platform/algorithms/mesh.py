"""3D 模型几何算法：STL / OBJ 解析、可打印性检查、顶点聚类简化（LOD）。

只用 numpy 实现，不依赖 trimesh，方便在算法服务里控制依赖体积与启动时间。
"""
from __future__ import annotations

import io
import struct
from dataclasses import dataclass

import numpy as np

STL_HEADER = 80
STL_TRIANGLE_RECORD = 50


@dataclass
class Mesh:
    vertices: np.ndarray  # (N, 3) float64
    faces: np.ndarray     # (M, 3) int64

    @property
    def triangle_count(self) -> int:
        return int(self.faces.shape[0])

    @property
    def vertex_count(self) -> int:
        return int(self.vertices.shape[0])


class MeshFormatError(ValueError):
    """模型文件无法解析。"""


def _parse_binary_stl(data: bytes) -> Mesh:
    count = struct.unpack_from("<I", data, STL_HEADER)[0]
    expected = STL_HEADER + 4 + count * STL_TRIANGLE_RECORD
    if expected > len(data):
        raise MeshFormatError(f"binary STL 长度不符：需要 {expected} 字节，实际 {len(data)}")
    tris = np.frombuffer(
        data, dtype=np.uint8, count=count * STL_TRIANGLE_RECORD, offset=STL_HEADER + 4
    ).reshape(count, STL_TRIANGLE_RECORD)
    coords = tris[:, 12:48].copy().view("<f4").reshape(count, 3, 3).astype(np.float64)
    vertices = coords.reshape(-1, 3)
    # 面内顶点去重会破坏索引关系，这里保留每个三角形独立顶点，信息无损。
    faces = np.arange(count * 3, dtype=np.int64).reshape(count, 3)
    return Mesh(vertices=vertices, faces=faces)


def _parse_ascii_stl(data: bytes) -> Mesh:
    text = data.decode("ascii", errors="ignore")
    verts: list[tuple[float, float, float]] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("vertex "):
            parts = line.split()
            if len(parts) < 4:
                raise MeshFormatError("ASCII STL 顶点行格式错误")
            verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
    if not verts or len(verts) % 3 != 0:
        raise MeshFormatError("ASCII STL 三角形数量不完整")
    vertices = np.asarray(verts, dtype=np.float64)
    faces = np.arange(vertices.shape[0], dtype=np.int64).reshape(-1, 3)
    return Mesh(vertices=vertices, faces=faces)


def _looks_binary_stl(data: bytes) -> bool:
    if len(data) < STL_HEADER + 4:
        return False
    count = struct.unpack_from("<I", data, STL_HEADER)[0]
    if STL_HEADER + 4 + count * STL_TRIANGLE_RECORD == len(data):
        return True
    return not data.lstrip()[:5].lower().startswith(b"solid")


def _parse_obj(data: bytes) -> Mesh:
    verts: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    for line in data.decode("utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("v "):
            parts = line.split()
            verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
        elif line.startswith("f "):
            idx = [int(p.split("/")[0]) for p in line.split()[1:]]
            idx = [i - 1 if i > 0 else len(verts) + i for i in idx]
            for k in range(1, len(idx) - 1):  # 多边形扇形三角化
                faces.append((idx[0], idx[k], idx[k + 1]))
    if not verts or not faces:
        raise MeshFormatError("OBJ 中没有有效的顶点或面")
    return Mesh(vertices=np.asarray(verts, dtype=np.float64),
                faces=np.asarray(faces, dtype=np.int64))


def parse_mesh(data: bytes, filename: str = "") -> tuple[Mesh, str]:
    """返回 (mesh, 检测到的格式)。"""
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix == "obj":
        return _parse_obj(data), "obj"
    if suffix == "stl" or suffix == "":
        if _looks_binary_stl(data):
            return _parse_binary_stl(data), "stl-binary"
        try:
            return _parse_ascii_stl(data), "stl-ascii"
        except (UnicodeDecodeError, ValueError):
            return _parse_binary_stl(data), "stl-binary"
    raise MeshFormatError(f"不支持的模型格式：{suffix or '未知'}")


def triangle_areas(mesh: Mesh) -> np.ndarray:
    tri = mesh.vertices[mesh.faces]
    cross = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    return 0.5 * np.linalg.norm(cross, axis=1)


def signed_volume(mesh: Mesh) -> float:
    tri = mesh.vertices[mesh.faces]
    # 散度定理：V = Σ (v0 · (v1 × v2)) / 6
    return float(np.einsum("ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2])).sum() / 6.0)


def edge_manifold_stats(mesh: Mesh) -> dict[str, float | int]:
    """统计每条无向边被几个三角形共享——水密模型每条边恰好 2 个。"""
    edges = np.vstack([
        mesh.faces[:, [0, 1]], mesh.faces[:, [1, 2]], mesh.faces[:, [2, 0]],
    ])
    edges = np.sort(edges, axis=1)
    _, counts = np.unique(edges, axis=0, return_counts=True)
    total = int(counts.size)
    return {
        "edges": total,
        "boundary_edges": int((counts == 1).sum()),
        "non_manifold_edges": int((counts > 2).sum()),
        "watertight": bool(total > 0 and (counts == 2).all()),
    }


def weld_vertices(mesh: Mesh, decimals: int = 6) -> tuple[Mesh, dict[str, float | int]]:
    """按坐标合并重复顶点（STL 每个三角形带独立顶点，必须先焊接才能做拓扑分析）。"""
    rounded = np.round(mesh.vertices, decimals)
    unique, inverse = np.unique(rounded, axis=0, return_inverse=True)
    faces = inverse[mesh.faces]
    welded = Mesh(vertices=unique, faces=faces.astype(np.int64))
    return welded, {
        "input_vertices": mesh.vertex_count,
        "welded_vertices": welded.vertex_count,
        "merged_ratio": round(1 - welded.vertex_count / max(1, mesh.vertex_count), 6),
    }


def analyze_mesh(mesh: Mesh) -> dict:
    welded, weld_stats = weld_vertices(mesh)
    areas = triangle_areas(mesh)
    area_total = float(areas.sum())
    volume = abs(signed_volume(mesh))
    bbox_min = mesh.vertices.min(axis=0)
    bbox_max = mesh.vertices.max(axis=0)
    extent = bbox_max - bbox_min
    manifold = edge_manifold_stats(welded)
    degenerate = int((triangle_areas(welded) <= 1e-12).sum())

    issues: list[str] = []
    suggestions: list[str] = []
    if not manifold["watertight"]:
        issues.append(f"模型非水密：{manifold['boundary_edges']} 条边界边、"
                      f"{manifold['non_manifold_edges']} 条非流形边")
        suggestions.append("先做网格修复（补洞 / 合并重复顶点）再做切片，否则易出现空腔或漏料")
    if manifold["non_manifold_edges"] > 0:
        issues.append("存在非流形边，切片器可能判定为无效实体")
    if degenerate > 0:
        issues.append(f"存在 {degenerate} 个退化三角形（面积≈0）")
        suggestions.append("清理退化面以减少切片计算量")
    if area_total <= 0:
        issues.append("表面积为 0，模型可能只含点/线")

    score = 100.0
    score -= 40 if not manifold["watertight"] else 0
    score -= min(20, degenerate / max(1, welded.triangle_count) * 200)
    score = max(0.0, min(100.0, score))
    verdict = "pass" if score >= 80 else ("warn" if score >= 55 else "fail")

    return {
        "verdict": verdict,
        "score": round(score, 2),
        "geometry": {
            "triangles": mesh.triangle_count,
            "vertices": mesh.vertex_count,
            "unique_vertices": welded.vertex_count,
            "duplicate_vertex_ratio": weld_stats["merged_ratio"],
            "bbox_min": [round(float(v), 4) for v in bbox_min],
            "bbox_max": [round(float(v), 4) for v in bbox_max],
            "extent": [round(float(v), 4) for v in extent],
            "surface_area": round(area_total, 4),
            "volume": round(volume, 4),
            "degenerate_faces": degenerate,
            "euler_characteristic": welded.vertex_count - manifold["edges"] + welded.triangle_count,
        },
        "topology": manifold,
        "issues": issues,
        "suggestions": suggestions,
    }


def decimate(mesh: Mesh, keep_ratio: float = 0.5) -> tuple[Mesh, dict]:
    """顶点聚类简化：按包围盒划分网格，每格合并为一个代表点。

    工程上用于生成预览用 LOD —— 体量大时切片/预览的 IO 开销显著下降。
    实现为「从细到粗逐步加倍格子尺寸」，直到三角形数量低于目标，
    这样对任意输入都能保证单调收敛，不会出现「参数调了但没变」的尴尬。
    """
    keep_ratio = min(0.95, max(0.02, float(keep_ratio)))
    target = max(1, int(round(mesh.triangle_count * keep_ratio)))
    bbox = mesh.vertices.max(axis=0) - mesh.vertices.min(axis=0)
    diag = float(np.linalg.norm(bbox))
    if diag <= 0 or mesh.triangle_count <= 4:
        return mesh, {"input_triangles": mesh.triangle_count, "output_triangles": mesh.triangle_count,
                      "keep_ratio": keep_ratio, "actual_ratio": 1.0, "volume_drift": 0.0,
                      "cell_size": 0.0}

    origin = mesh.vertices.min(axis=0)
    cell = diag / 32.0
    best = mesh
    for _ in range(24):
        keys = np.floor((mesh.vertices - origin) / cell).astype(np.int64)
        unique_keys, inverse = np.unique(keys, axis=0, return_inverse=True)
        counts = np.bincount(inverse, minlength=unique_keys.shape[0])
        sums = np.zeros((unique_keys.shape[0], 3))
        np.add.at(sums, inverse, mesh.vertices)
        new_vertices = sums / counts[:, None]
        new_faces = inverse[mesh.faces]
        keep = (new_faces[:, 0] != new_faces[:, 1]) & (new_faces[:, 1] != new_faces[:, 2]) & \
               (new_faces[:, 0] != new_faces[:, 2])
        new_faces = new_faces[keep]
        if new_faces.size:
            _, first = np.unique(np.sort(new_faces, axis=1), axis=0, return_index=True)
            new_faces = new_faces[np.sort(first)]
        best = Mesh(vertices=new_vertices, faces=new_faces.astype(np.int64))
        if best.triangle_count <= target or best.triangle_count == 0:
            break
        cell *= 1.6

    vol_in, vol_out = abs(signed_volume(mesh)), abs(signed_volume(best))
    return best, {
        "input_triangles": mesh.triangle_count,
        "output_triangles": best.triangle_count,
        "keep_ratio": keep_ratio,
        "actual_ratio": round(best.triangle_count / max(1, mesh.triangle_count), 4),
        "volume_drift": round((vol_out - vol_in) / vol_in, 6) if vol_in else 0.0,
        "cell_size": round(float(cell), 4),
    }


def to_binary_stl(mesh: Mesh) -> bytes:
    tri = mesh.vertices[mesh.faces]
    normals = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    lengths = np.linalg.norm(normals, axis=1)
    normals = np.divide(normals, lengths[:, None], out=np.zeros_like(normals), where=lengths[:, None] > 0)
    buf = io.BytesIO()
    buf.write(b"algo-platform LOD export".ljust(STL_HEADER, b"\0"))
    buf.write(struct.pack("<I", mesh.triangle_count))
    for normal, triangle in zip(normals, tri, strict=False):
        buf.write(struct.pack("<3f", *normal))
        for vertex in triangle:
            buf.write(struct.pack("<3f", *vertex))
        buf.write(struct.pack("<H", 0))
    return buf.getvalue()
