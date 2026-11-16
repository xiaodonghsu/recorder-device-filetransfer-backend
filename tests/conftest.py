"""测试前置：隔离环境变量 + 用假 Casdoor 校验器替换外部依赖。

测试完全离线：不访问真实 Casdoor，不写真实 data/ 目录（用 tmp_path）。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# pydantic-settings 优先级：环境变量 > .env > 默认值。
# 必须在导入应用之前固定，否则本机 filetransfer/.env 或 shell 里的
# API_PREFIX（例如设备管理后端的 /voicehub-api）会让测试大面积 404。
os.environ["API_PREFIX"] = "/voicehub-ogg"
os.environ["DOCS_ENABLED"] = "true"
os.environ["CASDOOR_ORGANIZATION"] = "bestlink"
os.environ["CASDOOR_REQUIRE_ORGANIZATION"] = "true"
os.environ["CASDOOR_VERIFY_METHOD"] = "auto"
os.environ["ALLOW_QUERY_TOKEN"] = "false"
os.environ["MAX_UPLOAD_MB"] = "100"
os.environ["ALLOWED_EXTENSIONS"] = ".ogg,.wav,.mp3"
os.environ["STORAGE_BACKEND"] = "local"
os.environ["OBS_PREFIX"] = "voicehub/sounds"
# 默认库指向系统临时目录；实际测试全部 override 到 tmp_path 的独立 SQLite。
# 这样即使意外走到 fallback，也不会在项目里生成 data/*.db。
os.environ["DATABASE_URL"] = "sqlite:///" + (
    Path(tempfile.gettempdir()) / "voicehub-ogg-test-fallback.db"
).as_posix()

import httpx
import pytest

from app.api import get_backend, get_repository
from app.auth import Identity, get_verifier
from app.config import settings
from app.db import FileRepository
from app.errors import AuthenticationError, CasdoorError
from app.main import app
from app.storage import LocalStorage

ALICE = Identity(
    user_id="uuid-alice",
    username="JH000059",
    display_name="胥晓冬",
    tenant_id="bestlink",
    email="alice@example.com",
    auth_method="userinfo",
    tenant_source="claim",
)
BOB = Identity(
    user_id="uuid-bob",
    username="JH000060",
    display_name="李四",
    tenant_id="bestlink",
    auth_method="userinfo",
    tenant_source="claim",
)
OUTSIDER = Identity(
    user_id="uuid-outsider",
    username="JH999999",
    display_name="外部用户",
    tenant_id="another-org",
    auth_method="userinfo",
    tenant_source="claim",
)


class FakeVerifier:
    """按约定返回身份，覆盖 401 / 403 / 502 三条分支。"""

    async def verify(self, token: str) -> Identity:
        if token == "alice-token":
            return ALICE
        if token == "bob-token":
            return BOB
        if token == "outsider-token":
            return OUTSIDER
        if token == "casdoor-down":
            raise CasdoorError("Casdoor 服务不可达")
        raise AuthenticationError("访问令牌无效或已过期")

    async def aclose(self) -> None:
        return None


@pytest.fixture
def alice() -> Identity:
    return ALICE


@pytest.fixture
def bob() -> Identity:
    return BOB


@pytest.fixture
def backend(tmp_path: Path) -> LocalStorage:
    return LocalStorage(settings, root=tmp_path / "uploads")


@pytest.fixture
def storage(backend: LocalStorage) -> LocalStorage:
    """既有测试沿用的别名。"""
    return backend


@pytest.fixture
def repository(tmp_path: Path) -> FileRepository:
    url = "sqlite:///" + (tmp_path / "meta.db").as_posix()
    return FileRepository(settings, url=url)


@pytest.fixture(autouse=True)
def _overrides(backend: LocalStorage, repository: FileRepository):
    app.dependency_overrides[get_backend] = lambda: backend
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.state.backend = backend
    app.state.repository = repository
    yield
    app.dependency_overrides.clear()
    app.state.backend = None
    app.state.repository = None


@pytest.fixture
def make_client():
    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )

    return factory
