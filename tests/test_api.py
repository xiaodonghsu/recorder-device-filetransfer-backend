"""接口与存储层测试（离线）。"""

from __future__ import annotations

import hashlib

import pytest

import app.main as main_module
from app.config import settings
from app.errors import NotFoundError, ValidationError
from app.storage import Storage, clean_filename, clean_path_segments, clean_username

PREFIX = "/voicehub-ogg"
ALICE_TOKEN = "alice-token"
BOB_TOKEN = "bob-token"


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def upload_files(payload: bytes = b"OggS-fake-payload", name: str = "test.ogg") -> dict:
    return {"file": (name, payload, "audio/ogg")}


# ---------------------------------------------------------------------------
# 探针 / 身份
# ---------------------------------------------------------------------------
async def test_probes(make_client, storage: Storage):
    async with make_client() as client:
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get(f"{PREFIX}/healthz")).status_code == 200
        ready = await client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"


async def test_me_returns_identity_without_token_leak(make_client):
    async with make_client() as client:
        response = await client.get(f"{PREFIX}/me", headers=auth(ALICE_TOKEN))
    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "JH000059"          # 工号，不是姓名 / UUID
    assert data["display_name"] == "胥晓冬"
    assert data["tenant_id"] == "bestlink"
    assert data["tenant_source"] == "claim"
    assert ALICE_TOKEN not in response.text          # 令牌绝不能出现在响应里


async def test_query_token_disabled_by_default(make_client):
    async with make_client() as client:
        response = await client.get(f"{PREFIX}/me?access_token=alice-token")
    assert response.status_code == 401


async def test_other_organization_rejected(make_client):
    async with make_client() as client:
        response = await client.get(f"{PREFIX}/me", headers=auth("outsider-token"))
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


# ---------------------------------------------------------------------------
# 认证失败分支
# ---------------------------------------------------------------------------
async def test_upload_without_token_is_rejected_before_reading_body(make_client, storage: Storage):
    async with make_client() as client:
        response = await client.post(f"{PREFIX}/upload", files=upload_files())
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
    assert not (storage.root / "JH000059").exists()   # 依赖未执行，目录都没建


async def test_upload_with_invalid_token(make_client):
    async with make_client() as client:
        response = await client.post(
            f"{PREFIX}/upload", headers=auth("bad-token"), files=upload_files()
        )
    assert response.status_code == 401


async def test_casdoor_unavailable_is_502_not_401(make_client):
    async with make_client() as client:
        response = await client.post(
            f"{PREFIX}/upload", headers=auth("casdoor-down"), files=upload_files()
        )
    assert response.status_code == 502
    assert response.json()["code"] == "casdoor_error"


# ---------------------------------------------------------------------------
# 上传 / 落盘
# ---------------------------------------------------------------------------
async def test_upload_saves_under_username(make_client, storage: Storage):
    payload = b"OggS" + bytes(range(256)) * 4
    async with make_client() as client:
        response = await client.post(
            f"{PREFIX}/upload", headers=auth(ALICE_TOKEN), files=upload_files(payload)
        )
    assert response.status_code == 201
    info = response.json()["file"]
    assert info["owner"] == "JH000059"
    assert info["name"] == "test.ogg"
    assert info["relpath"] == "test.ogg"
    assert info["size"] == len(payload)
    assert info["sha256"] == hashlib.sha256(payload).hexdigest()
    assert info["content_type"] == "audio/ogg"
    assert info["download_url"] == f"{PREFIX}/files/test.ogg"

    saved = storage.root / "JH000059" / "test.ogg"
    assert saved.read_bytes() == payload
    assert not list((storage.root / "JH000059").glob("*.part"))


async def test_filename_path_traversal_is_sanitized(make_client, storage: Storage):
    async with make_client() as client:
        response = await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            files=upload_files(b"safe", "../../evil.ogg"),
        )
    assert response.status_code == 201
    assert response.json()["file"]["name"] == "evil.ogg"
    assert (storage.root / "JH000059" / "evil.ogg").exists()
    assert not (storage.root / "evil.ogg").exists()


async def test_subdir_traversal_is_rejected(make_client):
    async with make_client() as client:
        response = await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data={"subdir": "a/../../b"},
            files=upload_files(),
        )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_subdir_and_unicode_filename(make_client, storage: Storage):
    async with make_client() as client:
        response = await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data={"subdir": "2026-11-12/会议", "filename": "周会.ogg"},
            files=upload_files(b"abc", "raw.ogg"),
        )
        assert response.status_code == 201
        info = response.json()["file"]
        assert info["relpath"] == "2026-11-12/会议/周会.ogg"
        assert "%E5" in info["download_url"]
        assert (storage.root / "JH000059" / "2026-11-12" / "会议" / "周会.ogg").exists()

        downloaded = await client.get(info["download_url"], headers=auth(ALICE_TOKEN))
        assert downloaded.status_code == 200
        assert downloaded.content == b"abc"


async def test_extension_whitelist(make_client, storage: Storage):
    async with make_client() as client:
        response = await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            files=upload_files(b"MZ", "malware.exe"),
        )
    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_media_type"
    assert not (storage.root / "JH000059" / "malware.exe").exists()


async def test_same_filename_does_not_overwrite(make_client, storage: Storage):
    async with make_client() as client:
        first = await client.post(
            f"{PREFIX}/upload", headers=auth(ALICE_TOKEN), files=upload_files(b"A")
        )
        second = await client.post(
            f"{PREFIX}/upload", headers=auth(ALICE_TOKEN), files=upload_files(b"B")
        )
    assert first.status_code == second.status_code == 201
    assert second.json()["file"]["name"] == "test-1.ogg"
    assert (storage.root / "JH000059" / "test.ogg").read_bytes() == b"A"
    assert (storage.root / "JH000059" / "test-1.ogg").read_bytes() == b"B"


# ---------------------------------------------------------------------------
# 大小限制（中间件 + 存储层两道）
# ---------------------------------------------------------------------------
async def test_storage_layer_size_limit_cleans_temp_file(make_client, storage, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_mb", 0.001)  # ≈ 1 KB，请求体仍小于 1 MB 预算
    async with make_client() as client:
        response = await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            files=upload_files(b"x" * 4096),
        )
    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"
    assert "received_bytes" in response.json()["details"]
    user_dir = storage.root / "JH000059"
    assert not (user_dir / "test.ogg").exists()
    assert not list(user_dir.glob("*.part"))


async def test_early_guard_rejects_oversized_content_length(make_client, storage, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_mb", 0.001)
    monkeypatch.setattr(main_module, "MULTIPART_OVERHEAD_BYTES", 0)
    async with make_client() as client:
        response = await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            files=upload_files(b"x" * 4096),
        )
    assert response.status_code == 413
    assert "content_length" in response.json()["details"]
    assert not (storage.root / "JH000059").exists()   # 依赖未执行


# ---------------------------------------------------------------------------
# 列表 / 下载 / 删除的用户隔离
# ---------------------------------------------------------------------------
async def test_files_are_isolated_per_user(make_client, storage: Storage):
    async with make_client() as client:
        await client.post(
            f"{PREFIX}/upload", headers=auth(ALICE_TOKEN), files=upload_files(b"alice-data")
        )

        listed = await client.get(f"{PREFIX}/files", headers=auth(BOB_TOKEN))
        assert listed.status_code == 200
        assert listed.json()["total"] == 0

        denied = await client.get(f"{PREFIX}/files/test.ogg", headers=auth(BOB_TOKEN))
        assert denied.status_code == 404

        denied_delete = await client.delete(f"{PREFIX}/files/test.ogg", headers=auth(BOB_TOKEN))
        assert denied_delete.status_code == 404

        own = await client.get(f"{PREFIX}/files", headers=auth(ALICE_TOKEN))
        assert own.json()["total"] == 1
        assert own.json()["items"][0]["relpath"] == "test.ogg"

        downloaded = await client.get(f"{PREFIX}/files/test.ogg", headers=auth(ALICE_TOKEN))
        assert downloaded.status_code == 200
        assert downloaded.content == b"alice-data"

        removed = await client.delete(f"{PREFIX}/files/test.ogg", headers=auth(ALICE_TOKEN))
        assert removed.status_code == 200
        assert not (storage.root / "JH000059" / "test.ogg").exists()


def test_resolve_file_rejects_relative_escape(alice, storage: Storage):
    with pytest.raises(ValidationError):
        storage.resolve_file(alice, "../JH000060/secret.ogg")
    with pytest.raises(ValidationError):
        storage.resolve_file(alice, "/etc/passwd.ogg")


def test_hidden_and_part_files_are_not_accessible(alice, storage: Storage):
    hidden = storage.user_root(alice.username) / ".hidden.ogg"
    hidden.write_bytes(b"secret")
    with pytest.raises(NotFoundError):
        storage.resolve_file(alice, ".hidden.ogg")


# ---------------------------------------------------------------------------
# 路径净化纯函数
# ---------------------------------------------------------------------------
def test_clean_helpers():
    assert clean_username("JH000059") == "JH000059"
    assert clean_filename("C:\\tmp\\..\\meeting.ogg") == "meeting.ogg"
    assert clean_filename("  ..hidden.ogg ") == "hidden.ogg"
    assert clean_path_segments("2026/11/12", max_depth=3) == ["2026", "11", "12"]
    with pytest.raises(ValidationError):
        clean_username("../escape")
    with pytest.raises(ValidationError):
        clean_path_segments("a/../b", max_depth=3)
    with pytest.raises(ValidationError):
        clean_path_segments("a/b/c/d", max_depth=3)
    with pytest.raises(ValidationError):
        clean_filename("...")


async def test_openapi_available_without_token(make_client):
    async with make_client() as client:
        response = await client.get(f"{PREFIX}/openapi.json")
        docs_with_slash = await client.get(f"{PREFIX}/docs/")
    assert response.status_code == 200
    # 公开文档的尾斜杠形式不能被早拒绝中间件误拦成 401
    assert docs_with_slash.status_code in (200, 307)
    paths = response.json()["paths"]
    assert f"{PREFIX}/upload" in paths
    assert f"{PREFIX}/files" in paths
    assert f"{PREFIX}/files/{{relpath}}" in paths
    assert f"{PREFIX}/me" in paths
