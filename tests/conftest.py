"""测试夹具：用临时目录构造独立应用，避免测试之间共享数据库。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from algo_platform.config import Settings  # noqa: E402
from algo_platform.main import create_app  # noqa: E402
from examples.generate_samples import (  # noqa: E402
    cube_mesh,
    open_box_mesh,
    print_photo,
)


@pytest.fixture()
def stl_bytes() -> bytes:
    from algo_platform.algorithms.mesh import to_binary_stl
    return to_binary_stl(cube_mesh())


@pytest.fixture()
def open_stl_bytes() -> bytes:
    from algo_platform.algorithms.mesh import to_binary_stl
    return to_binary_stl(open_box_mesh())


@pytest.fixture()
def image_bytes() -> bytes:
    from io import BytesIO
    buffer = BytesIO()
    print_photo().save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture()
def blurry_image_bytes() -> bytes:
    from io import BytesIO
    buffer = BytesIO()
    print_photo(blur=True).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", workers=2, cpu_threads=2,
                    queue_capacity=16, job_timeout_s=10.0)


@pytest.fixture()
def client(settings: Settings):
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client
