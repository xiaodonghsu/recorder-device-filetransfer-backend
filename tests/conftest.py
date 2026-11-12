"""测试前置：隔离环境变量 + 用假 Casdoor 校验器替换外部依赖。

测试完全离线：不访问真实 Casdoor，不写真实 data/ 目录（用 tmp_path）。
"""

from __future__ import annotations

import os
import sys
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

import httpx
import pytest

from app.api import get_storage
from app.auth import Identity, get_verifier
from app.config import settings
from app.errors import AuthenticationError, CasdoorError
from app.main import app
from app.storage import Storage

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
def storage(tmp_path: Path) -> Storage:
    return Storage(settings, root=tmp_path / "uploads")


@pytest.fixture(autouse=True)
def _overrides(storage: Storage):
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.state.storage = storage
    yield
    app.dependency_overrides.clear()
    app.state.storage = None


@pytest.fixture
def make_client():
    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )

    return factory
