"""HTTP 接口（路由本身不带前缀，由 main.py 统一挂载到 API_PREFIX）。

所有业务接口都需要 ``Authorization: Bearer <Casdoor Access Token>``。
租户与用户名（工号）完全由服务端从 Token 推导；设备 SN 由客户端在上传时声明，
经严格净化后成为用户目录下的强制一级，同一工号名下多台设备的录音互不混放。
下载 / 删除直接使用 ``设备SN/子目录/文件名`` 形式的 ``relpath`` 定位。

编排顺序：认证 → 存储后端保存 → 元数据数据库登记 → 返回。
列表统一读数据库（跨后端一致），删除同时做后端删除与数据库软删除。
标签是与路径无关的文件元数据：上传时经表单 ``tags`` 指定，可用
``PUT /files/{relpath}/tags`` 整体覆盖，也可用 ``GET /files?tag=`` 筛选。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.auth import Identity, get_current_identity
from app.config import settings
from app.db import FileRepository, iso_utc
from app.errors import NotFoundError, StorageError, ValidationError
from app.schemas import (
    FileInfo,
    FileListResponse,
    MeResponse,
    MessageResponse,
    TagsUpdateRequest,
    UploadResponse,
)
from app.storage import FileBackend, LocalStorage
from app.storage_obs import ObsStorage
from app.tags import normalize_tags, parse_tags_form

router = APIRouter(tags=["文件传输"])


def get_backend(request: Request) -> FileBackend:
    """取应用状态里的存储后端（由 lifespan 创建，可被测试覆盖）。"""
    backend = getattr(request.app.state, "backend", None)
    if backend is None:  # pragma: no cover - 仅在 lifespan 未启用时触发
        backend = (
            ObsStorage(settings) if settings.storage_backend == "obs" else LocalStorage(settings)
        )
        request.app.state.backend = backend
    return backend


def get_repository(request: Request) -> FileRepository:
    """取应用状态里的元数据仓储（由 lifespan 创建，可被测试覆盖）。"""
    repository = getattr(request.app.state, "repository", None)
    if repository is None:  # pragma: no cover
        repository = FileRepository(settings)
        request.app.state.repository = repository
    return repository


@router.get("/healthz", summary="存活探针（经 nginx 可访问）", include_in_schema=False)
async def prefix_healthz() -> dict:
    return {"status": "ok", "app": settings.app_name, "version": settings.app_version}


@router.get("/me", response_model=MeResponse, summary="查看当前 Token 解析出的身份")
async def me(identity: Identity = Depends(get_current_identity)) -> MeResponse:
    """排障用：确认工号、组织与校验方式，返回值绝不包含令牌本身。"""
    return MeResponse(**identity.public())


@router.post(
    "/upload",
    status_code=201,
    response_model=UploadResponse,
    summary="上传录音文件（multipart/form-data）",
)
async def upload_file(
    file: UploadFile = File(..., description="文件本体，表单字段名固定为 file"),
    device_sn: str = Form(
        ...,
        description="必填：录音卡设备 SN（如 VH-2026-0001），作为用户目录下的设备分层目录名",
    ),
    filename: Optional[str] = Form(
        None, description="可选：自定义落盘文件名；不传则用上传时的文件名（均会做安全净化）"
    ),
    subdir: str = Form(
        "", description="可选：设备目录内的子目录，如 2026-11-12/meeting，最多 3 层"
    ),
    tags: str = Form(
        "",
        description=(
            "可选：文件标签，多个用逗号（中英文均可）分隔，如 会议,重要；"
            "留空表示无标签。空项与重复项自动忽略，超限返回 422"
        ),
    ),
    identity: Identity = Depends(get_current_identity),
    backend: FileBackend = Depends(get_backend),
    repository: FileRepository = Depends(get_repository),
) -> UploadResponse:
    """校验通过后按 ``租户/工号/设备SN`` 隔离保存，并写入元数据数据库。

    本地后端：``{STORAGE_DIR}/{tenant_id}/{user_id}/{device_sn}/{subdir}/{filename}``
    OBS 后端：``obs://{bucket}/{prefix}/{tenant_id}/{user_id}/{device_sn}/{subdir}/{filename}``

    ``relpath`` 首段恒为设备 SN；同名文件默认自动追加 ``-1`` / ``-2``
    （``OVERWRITE_EXISTING=true`` 时覆盖）。返回体含 ``device_sn`` /
    ``object_key`` / ``record_id`` / ``download_url`` / ``tags``，供后续业务直接使用。

    标签在写盘前校验，只是元数据、不影响落盘路径；覆盖同名文件时新记录以本次
    上传携带的标签为准，不会继承旧记录的标签。
    """
    # 标签非法时早失败，避免「文件已落盘但请求 422」的不一致。
    file_tags = parse_tags_form(tags)
    result = await backend.save(
        identity, file, device_sn=device_sn, filename=filename, subdir=subdir
    )
    info = result.info
    record = repository.record_upload(
        identity,
        info,
        object_key=result.object_key,
        backend=backend.name,
        bucket=info.bucket,
        tags=file_tags,
    )
    info.record_id = record.id
    info.uploaded_at = iso_utc(record.created_at)
    info.tags = record.tags
    return UploadResponse(file=info)


@router.get(
    "/files", response_model=FileListResponse, summary="列出当前用户名下的文件（可按设备 / 标签筛选）"
)
async def list_files(
    subdir: str = Query("", description="可选：只列设备目录内该子目录下的文件"),
    device_sn: Optional[str] = Query(
        None, description="可选：只列该设备 SN 的文件；不传则列出该用户名下所有设备"
    ),
    tag: Optional[str] = Query(
        None, description="可选：只列包含该标签的文件；与 device_sn / subdir 可叠加"
    ),
    identity: Identity = Depends(get_current_identity),
    backend: FileBackend = Depends(get_backend),
    repository: FileRepository = Depends(get_repository),
) -> FileListResponse:
    """从元数据数据库读取（当前后端、当前租户/工号、未删除），不扫描磁盘/桶。

    ``tag`` 筛选接收单个标签；逗号分隔的多个值会被拒绝（422），避免误当成「或」查询。
    """
    tag_filter = ""
    if tag is not None:
        values = parse_tags_form(tag)
        if len(values) > 1:
            raise ValidationError(
                "标签筛选只接受单个标签", details={"field": "tag"}
            )
        tag_filter = values[0] if values else ""
    records = repository.list_active(
        identity, backend.name, subdir, device_sn or "", tag_filter
    )
    items = [repository.to_file_info(record) for record in records]
    return FileListResponse(
        total=len(items),
        backend=backend.name,
        device_sn=device_sn,
        tag=tag_filter or None,
        items=items,
    )


@router.put(
    "/files/{relpath:path}/tags",
    response_model=FileInfo,
    summary="修改文件标签（整体覆盖）",
)
async def update_file_tags(
    relpath: str,
    payload: TagsUpdateRequest,
    identity: Identity = Depends(get_current_identity),
    backend: FileBackend = Depends(get_backend),
    repository: FileRepository = Depends(get_repository),
) -> FileInfo:
    """整体覆盖 ``relpath`` 指向文件的标签，返回更新后的完整文件信息。

    - ``relpath`` 用上传响应里的值（形如 ``设备SN/子目录/文件名``），首段必须是设备 SN；
    - 请求体 ``{"tags": ["会议", "重要"]}`` 是**完整列表**，传 ``[]`` 即清空，不是增量追加；
    - 只改元数据，不触碰对象存储，故不会改变文件内容 / 对象键 / 下载链接；
    - 文件不存在、已删除或属于其他用户 / 设备一律 404。

    标签校验与上传一致：去空白、丢空项、去重、超限 422。
    """
    tags = normalize_tags(payload.tags)
    record = repository.update_tags(identity, relpath, backend.name, tags)
    if record is None:
        raise NotFoundError("文件不存在或不属于当前用户")
    return repository.to_file_info(record)


@router.get(
    "/files/{relpath:path}",
    summary="下载自己的文件",
    response_class=FileResponse,
)
async def download_file(
    relpath: str,
    identity: Identity = Depends(get_current_identity),
    backend: FileBackend = Depends(get_backend),
) -> FileResponse:
    """``relpath`` 用上传响应里的值（形如 ``设备SN/子目录/文件名``，含中文请 URL 编码）。

    OBS 后端会先把对象下载到临时文件，响应结束后由后台任务清理。
    只能下载当前用户、当前设备目录内的文件，路径穿越返回 422 / 404。
    """
    result = await backend.download(identity, relpath)
    if result.local_path is None:
        raise StorageError("存储后端未返回可下载的文件")
    response = FileResponse(
        result.local_path,
        filename=result.filename,
        media_type=result.content_type,
        content_disposition_type="attachment",
    )
    if result.cleanup:
        path = result.local_path
        response.background = BackgroundTask(path.unlink, missing_ok=True)
    return response


@router.delete(
    "/files/{relpath:path}",
    response_model=MessageResponse,
    summary="删除自己的文件",
)
async def delete_file(
    relpath: str,
    identity: Identity = Depends(get_current_identity),
    backend: FileBackend = Depends(get_backend),
    repository: FileRepository = Depends(get_repository),
) -> MessageResponse:
    """删除 ``设备SN/子目录/文件名`` 指向的后端对象，并把数据库记录标记为 deleted。"""
    rel = await backend.delete(identity, relpath)
    repository.mark_deleted(identity, rel, backend.name)
    return MessageResponse(message="已删除", relpath=rel)
