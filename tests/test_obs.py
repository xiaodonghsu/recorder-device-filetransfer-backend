"""华为云 OBS 后端 + 元数据数据库测试（离线，注入假 SDK 客户端）。

不联网、不写生产桶：FakeObsClient 实现官方 SDK 用到的 6 个方法，
数据库用 tmp_path 下独立的 SQLite 文件。

对象键：``{prefix}/{tenant}/{工号}/{设备SN}/[subdir]/{filename}``，
首次上传会创建四级 0 字节目录对象（含设备 SN 一级）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.api import get_backend
from app.config import settings
from app.errors import StorageError
from app.main import app
from app.schemas import FileInfo
from app.storage_obs import ObsStorage

PREFIX = "/voicehub-ogg"
ALICE_TOKEN = "alice-token"
BOB_TOKEN = "bob-token"
SN_A = "VH-2026-0001"
SN_B = "VH-2026-0002"
USER_PREFIX = "voicehub/sounds/bestlink/JH000059"
KEY = f"{USER_PREFIX}/{SN_A}/meeting.ogg"
KEY_B = f"{USER_PREFIX}/{SN_B}/meeting.ogg"
FOLDERS = (
    "voicehub/sounds/",
    "voicehub/sounds/bestlink/",
    "voicehub/sounds/bestlink/JH000059/",
    f"voicehub/sounds/bestlink/JH000059/{SN_A}/",
)


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def upload_data(sn: str = SN_A, **extra) -> dict:
    return {"device_sn": sn, **extra}


def upload_files(payload: bytes = b"OGG-DATA", name: str = "meeting.ogg") -> dict:
    return {"file": (name, payload, "audio/ogg")}


class FakeResponse:
    """模拟 SDK 响应对象（status / body / errorCode / errorMessage）。"""

    def __init__(self, status=200, body=None, message=""):
        self.status = status
        self.body = body
        self.errorCode = "" if status < 300 else "MockError"
        self.errorMessage = message
        self.reason = message


class FakeObsBody:
    def __init__(self, contents):
        self.contents = contents
        self.is_truncated = False
        self.next_marker = None


class FakeObsClient:
    """内存版 OBS：对象键 → 字节内容。"""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple] = []
        self.fail_on: set[str] = set()

    def _record(self, *args):
        self.calls.append(args)

    def putObject(self, bucket, key, content=b"", **kwargs):
        self._record("putObject", bucket, key)
        if "putObject" in self.fail_on:
            return FakeResponse(500, message="mock putObject failure")
        self.objects[key] = content if isinstance(content, bytes) else str(content).encode()
        return FakeResponse(200)

    def putFile(self, bucket, key, file_path, **kwargs):
        self._record("putFile", bucket, key)
        if "putFile" in self.fail_on:
            return FakeResponse(403, message="mock putFile failure")
        self.objects[key] = Path(file_path).read_bytes()
        return FakeResponse(200)

    def headObject(self, bucket, key, **kwargs):
        self._record("headObject", bucket, key)
        return FakeResponse(200 if key in self.objects else 404)

    def getObject(self, bucket, key, downloadPath=None, **kwargs):
        self._record("getObject", bucket, key)
        if key not in self.objects:
            return FakeResponse(404)
        if downloadPath:
            Path(downloadPath).write_bytes(self.objects[key])
        return FakeResponse(200)

    def deleteObject(self, bucket, key, **kwargs):
        self._record("deleteObject", bucket, key)
        self.objects.pop(key, None)
        return FakeResponse(204)

    def listObjects(self, bucket, prefix=None, marker=None, max_keys=None, **kwargs):
        self._record("listObjects", bucket, prefix)
        if "listObjects" in self.fail_on:
            return FakeResponse(500, message="mock listObjects failure")
        contents = [
            {"key": k, "size": len(v)}
            for k, v in sorted(self.objects.items())
            if prefix is None or k.startswith(prefix)
        ]
        return FakeResponse(200, body=FakeObsBody(contents))

    def close(self):
        self._record("close")


@pytest.fixture
def obs_client() -> FakeObsClient:
    return FakeObsClient()


@pytest.fixture
def obs_backend(obs_client: FakeObsClient, _overrides, repository) -> ObsStorage:
    """覆盖 autouse 的 local 后端；显式依赖 _overrides 保证执行顺序。"""
    backend = ObsStorage(settings, client=obs_client)
    app.dependency_overrides[get_backend] = lambda: backend
    app.state.backend = backend
    return backend


# ---------------------------------------------------------------------------
# 上传 / 目录对象 / 数据库
# ---------------------------------------------------------------------------
async def test_obs_upload_object_key_and_folder_hierarchy(
    make_client, obs_backend, obs_client
):
    async with make_client() as client:
        response = await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(),
            files=upload_files(),
        )
    assert response.status_code == 201
    info = response.json()["file"]
    assert info["backend"] == "obs"
    assert info["tenant_id"] == "bestlink"
    assert info["bucket"] == settings.obs_bucket
    assert info["device_sn"] == SN_A
    assert info["relpath"] == f"{SN_A}/meeting.ogg"
    assert info["download_url"] == f"{PREFIX}/files/{SN_A}/meeting.ogg"
    assert info["object_key"] == KEY
    assert obs_client.objects[KEY] == b"OGG-DATA"

    # 不存在的租户 / 用户 / 设备目录要显式创建（OBS 目录 = 以 / 结尾的 0 字节对象）
    for folder in FOLDERS:
        assert folder in obs_client.objects
        assert obs_client.objects[folder] == b""


async def test_obs_upload_requires_device_sn(make_client, obs_backend, obs_client):
    async with make_client() as client:
        response = await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            files=upload_files(),
        )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert not any(c[0] == "putFile" for c in obs_client.calls)


async def test_obs_upload_records_metadata_in_database(
    make_client, obs_backend, repository, alice
):
    payload = b"OggS" + bytes(range(64))
    async with make_client() as client:
        response = await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(),
            files=upload_files(payload),
        )
    assert response.status_code == 201
    info = response.json()["file"]

    records = repository.list_active(alice, "obs")
    assert len(records) == 1
    record = records[0]
    assert record.filename == "meeting.ogg"
    assert record.tenant_id == "bestlink"
    assert record.username == "JH000059"
    assert record.device_sn == SN_A
    assert record.relpath == f"{SN_A}/meeting.ogg"
    assert record.object_key == KEY
    assert record.backend == "obs"
    assert record.bucket == settings.obs_bucket
    assert record.size_bytes == len(payload)
    assert record.sha256 == hashlib.sha256(payload).hexdigest()
    assert record.status == "active"
    assert record.created_at is not None
    assert info["record_id"] == record.id


async def test_obs_folder_objects_created_only_once(make_client, obs_backend, obs_client):
    async with make_client() as client:
        await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(),
            files=upload_files(b"A"),
        )
        await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(),
            files=upload_files(b"B"),
        )
    put_object_calls = [c for c in obs_client.calls if c[0] == "putObject"]
    assert len(put_object_calls) == 4  # 四级目录各一次，进程内缓存生效


async def test_obs_each_device_has_its_own_folder_object(
    make_client, obs_backend, obs_client
):
    async with make_client() as client:
        await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(SN_A),
            files=upload_files(b"A"),
        )
        await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(SN_B),
            files=upload_files(b"B"),
        )
    assert f"{USER_PREFIX}/{SN_A}/" in obs_client.objects
    assert f"{USER_PREFIX}/{SN_B}/" in obs_client.objects
    put_object_calls = [c for c in obs_client.calls if c[0] == "putObject"]
    # 前三级共享，第四级按设备各写一次
    assert len(put_object_calls) == 5


async def test_obs_same_name_does_not_overwrite(make_client, obs_backend, obs_client):
    async with make_client() as client:
        await client.post(
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
    assert second.json()["file"]["relpath"] == f"{SN_A}/meeting-1.ogg"
    assert obs_client.objects[KEY] == b"A"
    assert obs_client.objects[f"{USER_PREFIX}/{SN_A}/meeting-1.ogg"] == b"B"


# ---------------------------------------------------------------------------
# 列表 / 下载 / 删除
# ---------------------------------------------------------------------------
async def test_obs_list_download_delete_and_soft_delete(
    make_client, obs_backend, obs_client, repository, alice
):
    async with make_client() as client:
        await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(),
            files=upload_files(b"hello-ogg"),
        )
        listed = await client.get(f"{PREFIX}/files", headers=auth(ALICE_TOKEN))
        assert listed.json()["backend"] == "obs"
        assert listed.json()["total"] == 1
        assert listed.json()["items"][0]["object_key"] == KEY

        downloaded = await client.get(
            f"{PREFIX}/files/{SN_A}/meeting.ogg", headers=auth(ALICE_TOKEN)
        )
        assert downloaded.status_code == 200
        assert downloaded.content == b"hello-ogg"

        removed = await client.delete(
            f"{PREFIX}/files/{SN_A}/meeting.ogg", headers=auth(ALICE_TOKEN)
        )
        assert removed.status_code == 200
        assert removed.json()["relpath"] == f"{SN_A}/meeting.ogg"
        assert KEY not in obs_client.objects

    # 物理对象删除 + 数据库软删除，列表不再返回，但历史记录仍可查
    assert repository.list_active(alice, "obs") == []
    assert repository.get_active(alice, f"{SN_A}/meeting.ogg", "obs") is None
    with repository._Session() as session:
        from app.db import FileRecord

        total = session.query(FileRecord).count()
        deleted = session.query(FileRecord).filter(FileRecord.status == "deleted").count()
    assert total == 1 and deleted == 1


async def test_obs_download_missing_object_returns_404(make_client, obs_backend):
    async with make_client() as client:
        response = await client.get(
            f"{PREFIX}/files/{SN_A}/nope.ogg", headers=auth(ALICE_TOKEN)
        )
    assert response.status_code == 404


async def test_obs_download_without_sn_segment_is_422(make_client, obs_backend):
    async with make_client() as client:
        response = await client.get(
            f"{PREFIX}/files/meeting.ogg", headers=auth(ALICE_TOKEN)
        )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_obs_tenant_and_user_isolation(make_client, obs_backend, obs_client, alice, bob):
    async with make_client() as client:
        await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(),
            files=upload_files(b"alice"),
        )
        await client.post(
            f"{PREFIX}/upload",
            headers=auth(BOB_TOKEN),
            data=upload_data(),
            files=upload_files(b"bob"),
        )
        alice_list = await client.get(f"{PREFIX}/files", headers=auth(ALICE_TOKEN))
        bob_list = await client.get(f"{PREFIX}/files", headers=auth(BOB_TOKEN))

    assert f"voicehub/sounds/bestlink/JH000060/{SN_A}/meeting.ogg" in obs_client.objects
    assert alice_list.json()["total"] == 1
    assert bob_list.json()["total"] == 1
    assert alice_list.json()["items"][0]["object_key"] == KEY
    assert bob_list.json()["items"][0]["object_key"].endswith(f"/JH000060/{SN_A}/meeting.ogg")


async def test_obs_multiple_devices_are_isolated(
    make_client, obs_backend, obs_client, alice
):
    async with make_client() as client:
        a = await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(SN_A),
            files=upload_files(b"from-A"),
        )
        b = await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(SN_B),
            files=upload_files(b"from-B"),
        )
        only_a = await client.get(
            f"{PREFIX}/files?device_sn={SN_A}", headers=auth(ALICE_TOKEN)
        )
        only_b = await client.get(
            f"{PREFIX}/files?device_sn={SN_B}", headers=auth(ALICE_TOKEN)
        )
        downloaded_b = await client.get(
            f"{PREFIX}/files/{SN_B}/meeting.ogg", headers=auth(ALICE_TOKEN)
        )
    assert a.json()["file"]["object_key"] == KEY
    assert b.json()["file"]["object_key"] == KEY_B
    assert obs_client.objects[KEY] == b"from-A"
    assert obs_client.objects[KEY_B] == b"from-B"
    assert only_a.json()["total"] == only_b.json()["total"] == 1
    assert only_a.json()["items"][0]["device_sn"] == SN_A
    assert only_b.json()["items"][0]["device_sn"] == SN_B
    assert downloaded_b.content == b"from-B"


async def test_obs_device_prefix_and_object_key_parsing(obs_backend, alice):
    assert obs_backend.device_prefix(alice, SN_A) == f"{USER_PREFIX}/{SN_A}"
    assert obs_backend.object_key(alice, f"{SN_A}/2026-11-16/a.ogg") == (
        f"{USER_PREFIX}/{SN_A}/2026-11-16/a.ogg"
    )
    keys = await obs_backend.list_object_keys(alice, device_sn=SN_A)
    assert keys == []


async def test_obs_remote_failure_is_502_and_not_recorded(
    make_client, obs_backend, obs_client, repository, alice
):
    obs_client.fail_on.add("putFile")
    async with make_client() as client:
        response = await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(),
            files=upload_files(),
        )
    assert response.status_code == 502
    assert response.json()["code"] == "remote_storage_error"
    assert repository.list_active(alice, "obs") == []
    assert KEY not in obs_client.objects


# ---------------------------------------------------------------------------
# 标签（两种后端共用；OBS 侧不产生额外对象操作）
# ---------------------------------------------------------------------------
async def test_obs_tags_are_metadata_only(
    make_client, obs_backend, obs_client, repository, alice
):
    async with make_client() as client:
        created = await client.post(
            f"{PREFIX}/upload",
            headers=auth(ALICE_TOKEN),
            data=upload_data(tags="会议,重要"),
            files=upload_files(),
        )
        assert created.status_code == 201
        info = created.json()["file"]
        assert info["tags"] == ["会议", "重要"]
        assert info["object_key"] == KEY

        data_calls = [
            c for c in obs_client.calls
            if c[0] in {"putFile", "getObject", "deleteObject"}
        ]
        updated = await client.put(
            f"{PREFIX}/files/{SN_A}/meeting.ogg/tags",
            headers=auth(ALICE_TOKEN),
            json={"tags": ["归档"]},
        )
        assert updated.status_code == 200
        assert updated.json()["tags"] == ["归档"]
        assert updated.json()["object_key"] == KEY
        assert [
            c for c in obs_client.calls if c[0] in {"putFile", "getObject", "deleteObject"}
        ] == data_calls                                    # 改标签不碰对象

        listed = await client.get(
            f"{PREFIX}/files?tag=归档", headers=auth(ALICE_TOKEN)
        )
    assert listed.json()["total"] == 1
    assert repository.get_active(alice, f"{SN_A}/meeting.ogg", "obs").tags == ["归档"]


# ---------------------------------------------------------------------------
# 配置 / 数据库纯函数
# ---------------------------------------------------------------------------
def test_repository_soft_delete_and_backend_filter(repository, alice):
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
        backend="local",
    )
    record = repository.record_upload(alice, info, backend="local")
    assert record.id is not None
    assert repository.list_active(alice, "local")[0].filename == "a.ogg"
    assert repository.list_active(alice, "local", device_sn=SN_B) == []
    assert repository.list_active(alice, "obs") == []  # 后端隔离，切换后端不会串数据
    assert repository.mark_deleted(alice, f"{SN_A}/a.ogg", "local") == 1
    assert repository.list_active(alice, "local") == []


def test_database_url_supports_sqlite_and_mysql(monkeypatch):
    monkeypatch.setattr(
        settings, "database_url", "mysql+pymysql://user:pwd@127.0.0.1:3306/voicehub_files?charset=utf8mb4"
    )
    assert settings.resolved_database_url().startswith("mysql+pymysql://")
    monkeypatch.setattr(settings, "database_url", "sqlite:///./data/filetransfer.db")
    resolved = settings.resolved_database_url()
    assert resolved.startswith("sqlite:///") and "data/filetransfer.db" in resolved
    assert "./" not in resolved  # 相对路径已锚定到 filetransfer/ 的绝对路径


def test_obs_endpoint_adds_https_scheme(monkeypatch):
    monkeypatch.setattr(settings, "obs_server", "obs.cn-south-1.myhuaweicloud.com")
    assert settings.obs_endpoint == "https://obs.cn-south-1.myhuaweicloud.com"


def test_obs_backend_requires_credentials(monkeypatch):
    monkeypatch.setattr(settings, "obs_access_key_id", "")
    with pytest.raises(StorageError):
        ObsStorage(settings)
