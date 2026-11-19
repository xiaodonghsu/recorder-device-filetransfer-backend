"""文件标签功能测试（离线）：上传带标签、整体覆盖修改、按标签筛选。

标签是纯元数据，不改变存储路径 / 对象键 / 下载契约；两种后端共用同一套逻辑。
"""

from __future__ import annotations

import pytest

from app.tags import decode_tags, encode_tags, normalize_tags, parse_tags_form

PREFIX = "/voicehub-ogg"
ALICE_TOKEN = "alice-token"
BOB_TOKEN = "bob-token"
SN_A = "VH-2026-0001"
SN_B = "VH-2026-0002"


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def upload_data(sn: str = SN_A, **extra) -> dict:
    return {"device_sn": sn, **extra}


def upload_files(payload: bytes = b"OggS-tag", name: str = "tagged.ogg") -> dict:
    return {"file": (name, payload, "audio/ogg")}


async def _upload(client, sn: str = SN_A, token: str = ALICE_TOKEN, **form):
    return await client.post(
        f"{PREFIX}/upload",
        headers=auth(token),
        data=upload_data(sn, **form),
        files=upload_files(),
    )


# ---------------------------------------------------------------------------
# 纯函数：解析 / 归一化 / 序列化
# ---------------------------------------------------------------------------
def test_parse_tags_form_splits_both_commas_and_dedupes():
    assert parse_tags_form("会议, 重要，客户A,会议, ,") == ["会议", "重要", "客户A"]
    assert parse_tags_form("") == []
    assert parse_tags_form(None) == []


def test_tag_codec_roundtrip_and_corrupt_data():
    tags = ["会议", "重要", "客户A"]
    assert decode_tags(encode_tags(tags)) == tags
    assert "会议" in encode_tags(tags)          # 中文在库里可读，不是 \u4f1a 转义
    assert decode_tags("not json") == []
    assert decode_tags('{"a": 1}') == []
    assert decode_tags('["ok", 123]') == ["ok"]


def test_normalize_tags_rejects_invalid(monkeypatch):
    from app.config import settings
    from app.errors import ValidationError

    monkeypatch.setattr(settings, "max_tags_per_file", 2)
    monkeypatch.setattr(settings, "max_tag_length", 5)
    assert normalize_tags([" ab ", "ab", "", "cde"]) == ["ab", "cde"]
    with pytest.raises(ValidationError):
        normalize_tags(["a", "b", "c"])
    with pytest.raises(ValidationError):
        normalize_tags(["abcdef"])
    with pytest.raises(ValidationError):
        normalize_tags(["ab\x01cd"])


# ---------------------------------------------------------------------------
# 上传带标签
# ---------------------------------------------------------------------------
async def test_upload_accepts_tags_and_stores_in_metadata(make_client, repository, alice):
    async with make_client() as client:
        response = await _upload(client, tags="会议, 重要，客户A,会议, ,")
    assert response.status_code == 201
    info = response.json()["file"]
    assert info["tags"] == ["会议", "重要", "客户A"]
    assert info["relpath"] == f"{SN_A}/tagged.ogg"   # 标签不进入路径

    records = repository.list_active(alice, "local", device_sn=SN_A)
    assert len(records) == 1
    assert records[0].tags == ["会议", "重要", "客户A"]
    assert records[0].tags_json == '["会议","重要","客户A"]'

    async with make_client() as client:
        listed = await client.get(f"{PREFIX}/files", headers=auth(ALICE_TOKEN))
    assert listed.json()["items"][0]["tags"] == ["会议", "重要", "客户A"]


async def test_upload_without_tags_defaults_to_empty(make_client, repository, alice):
    async with make_client() as client:
        response = await _upload(client)
    assert response.status_code == 201
    assert response.json()["file"]["tags"] == []
    assert repository.list_active(alice, "local")[0].tags == []


async def test_upload_rejects_bad_tags_before_writing_file(make_client, storage):
    bad_payloads = [
        {"tags": ",".join(f"t{i}" for i in range(21))},   # 超过 20 个（去重后）
        {"tags": "x" * 33},                                # 单个超过 32 字符
        {"tags": "会议\x01重要"},                           # 控制字符
    ]
    async with make_client() as client:
        for form in bad_payloads:
            response = await _upload(client, **form)
            assert response.status_code == 422
            assert response.json()["code"] == "validation_error"
    assert not (storage.root / "bestlink" / "JH000059" / SN_A).exists()


# ---------------------------------------------------------------------------
# 修改标签
# ---------------------------------------------------------------------------
async def test_update_tags_replaces_and_can_clear(make_client, repository, alice):
    async with make_client() as client:
        created = await _upload(client, tags="旧标签")
        relpath = created.json()["file"]["relpath"]
        record_id = created.json()["file"]["record_id"]

        updated = await client.put(
            f"{PREFIX}/files/{relpath}/tags",
            headers=auth(ALICE_TOKEN),
            json={"tags": [" 新标签 ", "重要", "新标签", ""]},
        )
        assert updated.status_code == 200
        body = updated.json()
        assert body["tags"] == ["新标签", "重要"]
        assert body["record_id"] == record_id
        assert body["relpath"] == relpath

        listed = await client.get(f"{PREFIX}/files", headers=auth(ALICE_TOKEN))
        assert listed.json()["items"][0]["tags"] == ["新标签", "重要"]

        cleared = await client.put(
            f"{PREFIX}/files/{relpath}/tags",
            headers=auth(ALICE_TOKEN),
            json={"tags": []},
        )
        assert cleared.status_code == 200
        assert cleared.json()["tags"] == []

    assert repository.list_active(alice, "local")[0].tags == []


async def test_update_tags_does_not_touch_stored_file(make_client, storage):
    async with make_client() as client:
        created = await _upload(client, tags="A")
        info = created.json()["file"]
        relpath = info["relpath"]

        updated = await client.put(
            f"{PREFIX}/files/{relpath}/tags",
            headers=auth(ALICE_TOKEN),
            json={"tags": ["B"]},
        )
        assert updated.status_code == 200
        result = updated.json()
        for key in ("relpath", "download_url", "record_id", "size", "sha256", "backend"):
            assert result[key] == info[key]

        downloaded = await client.get(info["download_url"], headers=auth(ALICE_TOKEN))
    assert downloaded.status_code == 200
    assert downloaded.content == b"OggS-tag"
    assert (storage.root / "bestlink" / "JH000059" / SN_A / "tagged.ogg").exists()


async def test_update_tags_missing_or_foreign_file_is_404(make_client):
    async with make_client() as client:
        created = await _upload(client, tags="A")
        relpath = created.json()["file"]["relpath"]

        missing = await client.put(
            f"{PREFIX}/files/{SN_A}/nope.ogg/tags",
            headers=auth(ALICE_TOKEN),
            json={"tags": ["B"]},
        )
        assert missing.status_code == 404
        assert missing.json()["code"] == "not_found"

        foreign = await client.put(
            f"{PREFIX}/files/{relpath}/tags",
            headers=auth(BOB_TOKEN),
            json={"tags": ["B"]},
        )
        assert foreign.status_code == 404

        other_device = await client.put(
            f"{PREFIX}/files/{SN_B}/tagged.ogg/tags",
            headers=auth(ALICE_TOKEN),
            json={"tags": ["B"]},
        )
        assert other_device.status_code == 404

        unchanged = await client.get(f"{PREFIX}/files", headers=auth(ALICE_TOKEN))
    assert unchanged.json()["items"][0]["tags"] == ["A"]


async def test_update_tags_deleted_file_is_404(make_client):
    async with make_client() as client:
        created = await _upload(client, tags="A")
        relpath = created.json()["file"]["relpath"]
        deleted = await client.delete(
            f"{PREFIX}/files/{relpath}", headers=auth(ALICE_TOKEN)
        )
        assert deleted.status_code == 200
        updated = await client.put(
            f"{PREFIX}/files/{relpath}/tags",
            headers=auth(ALICE_TOKEN),
            json={"tags": ["B"]},
        )
    assert updated.status_code == 404


async def test_update_tags_validates_body(make_client):
    async with make_client() as client:
        created = await _upload(client)
        relpath = created.json()["file"]["relpath"]

        too_long = await client.put(
            f"{PREFIX}/files/{relpath}/tags",
            headers=auth(ALICE_TOKEN),
            json={"tags": ["x" * 33]},
        )
        not_string = await client.put(
            f"{PREFIX}/files/{relpath}/tags",
            headers=auth(ALICE_TOKEN),
            json={"tags": ["ok", 123]},
        )
        missing_body = await client.put(
            f"{PREFIX}/files/{relpath}/tags", headers=auth(ALICE_TOKEN)
        )
    assert too_long.status_code == 422
    assert not_string.status_code == 422
    assert missing_body.status_code == 422


async def test_update_tags_requires_token(make_client):
    async with make_client() as client:
        created = await _upload(client)
        relpath = created.json()["file"]["relpath"]
        response = await client.put(
            f"{PREFIX}/files/{relpath}/tags", json={"tags": ["B"]}
        )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# 按标签筛选
# ---------------------------------------------------------------------------
async def test_list_filter_by_tag_combines_with_device(make_client):
    async with make_client() as client:
        await _upload(client, SN_A, tags="会议,重要")
        await _upload(client, SN_A, tags="摘要")
        await _upload(client, SN_B, tags="会议")

        by_tag = await client.get(
            f"{PREFIX}/files?tag=%E4%BC%9A%E8%AE%AE", headers=auth(ALICE_TOKEN)
        )
        by_tag_and_device = await client.get(
            f"{PREFIX}/files?tag=%E4%BC%9A%E8%AE%AE&device_sn={SN_B}",
            headers=auth(ALICE_TOKEN),
        )
        by_missing = await client.get(
            f"{PREFIX}/files?tag=%E4%B8%8D%E5%AD%98%E5%9C%A8", headers=auth(ALICE_TOKEN)
        )
        no_filter = await client.get(f"{PREFIX}/files", headers=auth(ALICE_TOKEN))

    assert by_tag.status_code == 200
    assert by_tag.json()["total"] == 2
    assert by_tag.json()["tag"] == "会议"
    assert {i["device_sn"] for i in by_tag.json()["items"]} == {SN_A, SN_B}
    assert all("会议" in i["tags"] for i in by_tag.json()["items"])

    assert by_tag_and_device.json()["total"] == 1
    assert by_tag_and_device.json()["items"][0]["device_sn"] == SN_B
    assert by_missing.json()["total"] == 0
    assert by_missing.json()["tag"] == "不存在"
    assert no_filter.json()["total"] == 3
    assert no_filter.json()["tag"] is None


async def test_list_rejects_multiple_tag_filter(make_client):
    async with make_client() as client:
        response = await client.get(
            f"{PREFIX}/files?tag=A%2CB", headers=auth(ALICE_TOKEN)
        )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_tag_filter_is_per_user(make_client):
    async with make_client() as client:
        await _upload(client, token=ALICE_TOKEN, tags="机密")
        bob_list = await client.get(
            f"{PREFIX}/files?tag=%E6%9C%BA%E5%AF%86", headers=auth(BOB_TOKEN)
        )
    assert bob_list.json()["total"] == 0


# ---------------------------------------------------------------------------
# OpenAPI 契约
# ---------------------------------------------------------------------------
async def test_openapi_exposes_tag_contract(make_client):
    async with make_client() as client:
        spec = (await client.get(f"{PREFIX}/openapi.json")).json()
    paths = spec["paths"]
    assert f"{PREFIX}/files/{{relpath}}/tags" in paths
    assert "put" in paths[f"{PREFIX}/files/{{relpath}}/tags"]

    upload = paths[f"{PREFIX}/upload"]["post"]["requestBody"]["content"][
        "multipart/form-data"
    ]["schema"]
    if "$ref" in upload:
        upload = spec["components"]["schemas"][upload["$ref"].rsplit("/", 1)[-1]]
    assert "tags" in upload["properties"]
    assert "tags" in spec["components"]["schemas"]["FileInfo"]["properties"]
    assert "tag" in spec["components"]["schemas"]["FileListResponse"]["properties"]
    assert any(
        p.get("name") == "tag" for p in paths[f"{PREFIX}/files"]["get"]["parameters"]
    )
