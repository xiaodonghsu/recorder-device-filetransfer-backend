"""接口与存储层测试（离线）。

目录级别：``{租户}/{工号}/{设备SN}/[subdir]/{filename}``。
``relpath`` 首段恒为设备 SN，所有上传都必须带表单字段 ``device_sn``。
"""

from __future__ import annotations

import hashlib

import pytest

import app.main as main_module
from app.config import settings
from app.errors import NotFoundError, ValidationError
from app.storage import (
    LocalStorage as Storage,
    clean_device_sn,
    clean_filename,
    clean_path_segments,
    clean_username,
    parse_relpath,
)

PREFIX = "/voicehub-ogg"
ALICE_TOKEN = "alice-token"
BOB_TOKEN = "bob-token"
SN_A = "VH-2026-0001"
SN_B = "VH-2026-0002"


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def upload_data(sn: str = SN_A, **extra) -> dict:
    """上传表单：device_sn 必填，其余字段按需追加。"""
    return {"device_sn": sn, **extra}


def upload_files(payload: bytes = b"OggS-fake-payload", name: str = "test.ogg") -> dict:
    return {"file": (name, payload, "audio/ogg")}


def alice_device(storage: Storage, sn: str = SN_A):
    return storage.root / "bestlink" / "JH000059" / sn


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
        response = await client.post(
            f"{PREFIX}/upload", data=upload_data(), files=upload_files()
        )
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
    assert not (storage.root / "bestlink" / "JH000059").exists()   # 依赖未执行，目录都没建


async def test_upload_with_invalid_token(make_client):
    async with make_client() as client:
        response = await client.post(
            f"{PREFIX}/upload",
            headers=auth("bad-token"),
            data=upload_data(),
            files=upload_files(),
        )
    assert response.status_code == 401


async def test_casdoor_unavailable_is_502_not_401(make_client):
    async with make_client() as client:
        response = await client.post(
            f"{PREFIX}/upload",
            headers=auth("casdoor-down"),
            data=upload_data(),
            files=upload_files(),
        )
    assert response.status_code == 502
    assert response.json()["code"] == "casdoor_error"


# ---------------------------------------------------------------------------
# 设备 SN：必填 / 净化 / 目录级别
# ---------------------------------------------------------------------------
async def test_upload_requires_device_sn(make_client, storage: Storage):
    async with make_client() as client:
        response = await client.post(
            f"{PREFIX}/upload", headers=auth(ALICE_TOKEN), files=upload_files()
        )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert not (storage.root / "bestlink" / "JH000059").exists()


@pytest.mark.parametrize(
    "bad_sn",
    ["", "   ", "../escape", "a/b", "a\\b", "..", ".hidden", "中文SN", "a b", "x" * 65],
)
async def test_upload_rejects_illegal_device_sn(make_client, storage: Storage, bad_sn):
    async with make_client() as client:
        response = await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(bad_sn),
            files=upload_files(),
        )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert not (storage.root / "bestlink" / "JH000059").exists()


def test_clean_device_sn_and_parse_relpath():
    assert clean_device_sn("VH-2026-0001") == "VH-2026-0001"
    assert clean_device_sn("ABC_123.4") == "ABC_123.4"
    sn, inner = parse_relpath(settings, "VH-2026-0001/2026-11-16/周会.ogg")
    assert sn == "VH-2026-0001"
    assert inner == ["2026-11-16", "周会.ogg"]
    with pytest.raises(ValidationError):
        parse_relpath(settings, "test.ogg")          # 缺设备 SN 段
    with pytest.raises(ValidationError):
        parse_relpath(settings, "VH-2026-0001/../secret.ogg")
    with pytest.raises(ValidationError):
        clean_device_sn("../escape")


# ---------------------------------------------------------------------------
# 上传 / 落盘
# ---------------------------------------------------------------------------
async def test_upload_saves_under_device_sn(make_client, storage: Storage):
    payload = b"OggS" + bytes(range(256)) * 4
    async with make_client() as client:
        response = await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(),
            files=upload_files(payload),
        )
    assert response.status_code == 201
    info = response.json()["file"]
    assert info["device_sn"] == SN_A
    assert info["owner"] == "JH000059"
    assert info["name"] == "test.ogg"
    assert info["relpath"] == f"{SN_A}/test.ogg"
    assert info["size"] == len(payload)
    assert info["sha256"] == hashlib.sha256(payload).hexdigest()
    assert info["content_type"] == "audio/ogg"
    assert info["download_url"] == f"{PREFIX}/files/{SN_A}/test.ogg"
    assert info["tenant_id"] == "bestlink"
    assert info["backend"] == "local"
    assert info["object_key"] is None
    assert info["record_id"] is not None
    assert info["uploaded_at"]

    saved = alice_device(storage) / "test.ogg"
    assert saved.read_bytes() == payload
    assert not list(alice_device(storage).glob("*.part"))


async def test_filename_path_traversal_is_sanitized(make_client, storage: Storage):
    async with make_client() as client:
        response = await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(),
            files=upload_files(b"safe", "../../evil.ogg"),
        )
    assert response.status_code == 201
    assert response.json()["file"]["name"] == "evil.ogg"
    assert (alice_device(storage) / "evil.ogg").exists()
    assert not (storage.root / "evil.ogg").exists()


async def test_subdir_traversal_is_rejected(make_client):
    async with make_client() as client:
        response = await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(subdir="a/../../b"),
            files=upload_files(),
        )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_subdir_and_unicode_filename(make_client, storage: Storage):
    async with make_client() as client:
        response = await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(subdir="2026-11-12/会议", filename="周会.ogg"),
            files=upload_files(b"abc", "raw.ogg"),
        )
        assert response.status_code == 201
        info = response.json()["file"]
        assert info["relpath"] == f"{SN_A}/2026-11-12/会议/周会.ogg"
        assert "%E5" in info["download_url"]
        assert (alice_device(storage) / "2026-11-12" / "会议" / "周会.ogg").exists()

        downloaded = await client.get(info["download_url"], headers=auth(ALICE_TOKEN))
        assert downloaded.status_code == 200
        assert downloaded.content == b"abc"


async def test_extension_whitelist(make_client, storage: Storage):
    async with make_client() as client:
        response = await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(),
            files=upload_files(b"MZ", "malware.exe"),
        )
    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_media_type"
    assert not (alice_device(storage) / "malware.exe").exists()


async def test_same_filename_does_not_overwrite(make_client, storage: Storage):
    async with make_client() as client:
        first = await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(),
            files=upload_files(b"A"),
        )
        second = await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(),
            files=upload_files(b"B"),
        )
    assert first.status_code == second.status_code == 201
    assert second.json()["file"]["name"] == "test-1.ogg"
    assert (alice_device(storage) / "test.ogg").read_bytes() == b"A"
    assert (alice_device(storage) / "test-1.ogg").read_bytes() == b"B"


# ---------------------------------------------------------------------------
# 同一工号多台设备
# ---------------------------------------------------------------------------
async def test_multiple_devices_are_isolated(make_client, storage: Storage):
    async with make_client() as client:
        a = await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(SN_A),
            files=upload_files(b"from-A", "daily.ogg"),
        )
        b = await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(SN_B),
            files=upload_files(b"from-B", "daily.ogg"),
        )
        # 只存在于 A 设备目录里的文件
        await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(SN_A),
            files=upload_files(b"only-A", "only-a.ogg"),
        )
    assert a.status_code == b.status_code == 201
    assert a.json()["file"]["relpath"] == f"{SN_A}/daily.ogg"
    assert b.json()["file"]["relpath"] == f"{SN_B}/daily.ogg"
    # 同名文件在不同设备目录下互不覆盖
    assert (alice_device(storage, SN_A) / "daily.ogg").read_bytes() == b"from-A"
    assert (alice_device(storage, SN_B) / "daily.ogg").read_bytes() == b"from-B"

    async with make_client() as client:
        all_files = await client.get(f"{PREFIX}/files", headers=auth(ALICE_TOKEN))
        only_a = await client.get(
            f"{PREFIX}/files?device_sn={SN_A}", headers=auth(ALICE_TOKEN)
        )
        only_b = await client.get(
            f"{PREFIX}/files?device_sn={SN_B}", headers=auth(ALICE_TOKEN)
        )
        downloaded_a = await client.get(
            f"{PREFIX}/files/{SN_A}/daily.ogg", headers=auth(ALICE_TOKEN)
        )
        downloaded_b = await client.get(
            f"{PREFIX}/files/{SN_B}/daily.ogg", headers=auth(ALICE_TOKEN)
        )
        # 换用 B 的目录去取 A 独有的文件 → 404，不能跨设备读到
        cross = await client.get(
            f"{PREFIX}/files/{SN_B}/only-a.ogg", headers=auth(ALICE_TOKEN)
        )

    assert all_files.json()["total"] == 3
    assert all_files.json()["device_sn"] is None
    assert only_a.json()["total"] == 2
    assert only_a.json()["device_sn"] == SN_A
    assert only_a.json()["items"][0]["device_sn"] == SN_A
    assert only_b.json()["items"][0]["device_sn"] == SN_B
    assert downloaded_a.content == b"from-A"
    assert downloaded_b.content == b"from-B"
    assert cross.status_code == 404


async def test_subdir_filter_is_relative_to_device_directory(make_client):
    async with make_client() as client:
        await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(SN_A, subdir="2026-11-16"),
            files=upload_files(b"A", "a.ogg"),
        )
        await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(SN_B, subdir="2026-11-16"),
            files=upload_files(b"B", "b.ogg"),
        )
        await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(SN_B, subdir="2026-11-15"),
            files=upload_files(b"C", "c.ogg"),
        )
        all_in_subdir = await client.get(
            f"{PREFIX}/files?subdir=2026-11-16", headers=auth(ALICE_TOKEN)
        )
        only_b = await client.get(
            f"{PREFIX}/files?device_sn={SN_B}&subdir=2026-11-16",
            headers=auth(ALICE_TOKEN),
        )
    assert all_in_subdir.json()["total"] == 2       # 跨设备按设备内子目录过滤
    assert only_b.json()["total"] == 1
    assert only_b.json()["items"][0]["name"] == "b.ogg"


async def test_download_and_delete_require_device_sn_segment(make_client):
    async with make_client() as client:
        await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(),
            files=upload_files(b"data"),
        )
        old_style = await client.get(f"{PREFIX}/files/test.ogg", headers=auth(ALICE_TOKEN))
        missing_sn_delete = await client.delete(
            f"{PREFIX}/files/test.ogg", headers=auth(ALICE_TOKEN)
        )
    assert old_style.status_code == 422
    assert old_style.json()["code"] == "validation_error"
    assert missing_sn_delete.status_code == 422


async def test_delete_returns_relpath_with_device_sn(make_client, repository, alice):
    async with make_client() as client:
        await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(),
            files=upload_files(b"data"),
        )
        removed = await client.delete(
            f"{PREFIX}/files/{SN_A}/test.ogg", headers=auth(ALICE_TOKEN)
        )
    assert removed.status_code == 200
    assert removed.json()["relpath"] == f"{SN_A}/test.ogg"
    assert repository.get_active(alice, f"{SN_A}/test.ogg", "local") is None
    assert repository.list_active(alice, "local", device_sn=SN_A) == []


def test_metadata_stores_device_sn(repository, alice):
    from app.schemas import FileInfo

    info = FileInfo(
        name="a.ogg",
        relpath=f"{SN_A}/a.ogg",
        device_sn=SN_A,
        owner=alice.username,
        size=3,
        sha256="abc",
        content_type="audio/ogg",
        modified_at="2026-11-16T00:00:00Z",
        download_url=f"{PREFIX}/files/{SN_A}/a.ogg",
        tenant_id="bestlink",
    )
    record = repository.record_upload(alice, info, backend="local")
    assert record.device_sn == SN_A
    assert record.relpath == f"{SN_A}/a.ogg"
    assert repository.to_file_info(record).device_sn == SN_A
    assert repository.list_active(alice, "local", device_sn=SN_A)
    assert repository.list_active(alice, "local", device_sn=SN_B) == []


# ---------------------------------------------------------------------------
# 大小限制（中间件 + 存储层两道）
# ---------------------------------------------------------------------------
async def test_storage_layer_size_limit_cleans_temp_file(make_client, storage, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_mb", 0.001)  # ≈ 1 KB，请求体仍小于 1 MB 预算
    async with make_client() as client:
        response = await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(),
            files=upload_files(b"x" * 4096),
        )
    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"
    assert "received_bytes" in response.json()["details"]
    device_dir = alice_device(storage)
    assert not (device_dir / "test.ogg").exists()
    assert not list(device_dir.glob("*.part"))


async def test_early_guard_rejects_oversized_content_length(make_client, storage, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_mb", 0.001)
    monkeypatch.setattr(main_module, "MULTIPART_OVERHEAD_BYTES", 0)
    async with make_client() as client:
        response = await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(),
            files=upload_files(b"x" * 4096),
        )
    assert response.status_code == 413
    assert "content_length" in response.json()["details"]
    assert not (storage.root / "bestlink" / "JH000059").exists()   # 依赖未执行


# ---------------------------------------------------------------------------
# 列表 / 下载 / 删除的用户隔离
# ---------------------------------------------------------------------------
async def test_files_are_isolated_per_user(make_client, storage: Storage):
    async with make_client() as client:
        await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(),
            files=upload_files(b"alice-data"),
        )

        listed = await client.get(f"{PREFIX}/files", headers=auth(BOB_TOKEN))
        assert listed.status_code == 200
        assert listed.json()["total"] == 0

        denied = await client.get(
            f"{PREFIX}/files/{SN_A}/test.ogg", headers=auth(BOB_TOKEN)
        )
        assert denied.status_code == 404

        denied_delete = await client.delete(
            f"{PREFIX}/files/{SN_A}/test.ogg", headers=auth(BOB_TOKEN)
        )
        assert denied_delete.status_code == 404

        own = await client.get(f"{PREFIX}/files", headers=auth(ALICE_TOKEN))
        assert own.json()["total"] == 1
        assert own.json()["backend"] == "local"
        assert own.json()["items"][0]["relpath"] == f"{SN_A}/test.ogg"
        assert own.json()["items"][0]["device_sn"] == SN_A
        assert own.json()["items"][0]["record_id"] is not None

        downloaded = await client.get(
            f"{PREFIX}/files/{SN_A}/test.ogg", headers=auth(ALICE_TOKEN)
        )
        assert downloaded.status_code == 200
        assert downloaded.content == b"alice-data"

        removed = await client.delete(
            f"{PREFIX}/files/{SN_A}/test.ogg", headers=auth(ALICE_TOKEN)
        )
        assert removed.status_code == 200
        assert not (alice_device(storage) / "test.ogg").exists()


def test_resolve_file_rejects_relative_escape(alice, storage: Storage):
    with pytest.raises(ValidationError):
        storage.resolve_file(alice, f"{SN_A}/../JH000060/secret.ogg")
    with pytest.raises(ValidationError):
        storage.resolve_file(alice, "/etc/passwd.ogg")
    with pytest.raises(ValidationError):
        storage.resolve_file(alice, "test.ogg")      # 缺设备 SN 段


def test_hidden_and_part_files_are_not_accessible(alice, storage: Storage):
    hidden = storage.device_root(alice, SN_A) / ".hidden.ogg"
    hidden.write_bytes(b"secret")
    with pytest.raises(NotFoundError):
        storage.resolve_file(alice, f"{SN_A}/.hidden.ogg")


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
    spec = response.json()
    paths = spec["paths"]
    assert f"{PREFIX}/upload" in paths
    assert f"{PREFIX}/files" in paths
    assert f"{PREFIX}/files/{{relpath}}" in paths
    assert f"{PREFIX}/me" in paths
    # device_sn 是上传的必填表单字段，且出现在响应模型里
    form_schema = paths[f"{PREFIX}/upload"]["post"]["requestBody"]["content"][
        "multipart/form-data"
    ]["schema"]
    if "$ref" in form_schema:
        name = form_schema["$ref"].rsplit("/", 1)[-1]
        form_schema = spec["components"]["schemas"][name]
    assert "device_sn" in form_schema.get("required", [])
    assert "device_sn" in form_schema["properties"]
    assert "device_sn" in spec["components"]["schemas"]["FileInfo"]["properties"]
