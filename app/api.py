"""HTTP 接口（路由本身不带前缀，由 main.py 统一挂载到 API_PREFIX）。

所有业务接口都需要 ``Authorization: Bearer <Casdoor Access Token>``。
用户名（工号）完全由服务端从 Token 推导，客户端无法指定别人的目录。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse

from app.auth import Identity, get_current_identity
from app.config import settings
from app.schemas import FileListResponse, MeResponse, MessageResponse, UploadResponse
from app.storage import Storage

router = APIRouter(tags=["文件传输"])


def get_storage(request: Request) -> Storage:
    """取应用状态里的存储对象（由 lifespan 创建）。"""
    storage = getattr(request.app.state, "storage", None)
    if storage is None:  # pragma: no cover - 仅在 lifespan 未启用时触发
        storage = Storage(settings)
        request.app.state.storage = storage
    return storage


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
    filename: Optional[str] = Form(
        None, description="可选：自定义落盘文件名；不传则用上传时的文件名（均会做安全净化）"
    ),
    subdir: str = Form(
        "", description="可选：用户目录内的子目录，如 2026-11-12/meeting，最多 3 层"
    ),
    identity: Identity = Depends(get_current_identity),
    storage: Storage = Depends(get_storage),
) -> UploadResponse:
    """校验通过后保存到 ``{STORAGE_DIR}/{工号}/{subdir}/{文件名}``。

    同名文件默认自动追加 ``-1`` / ``-2``（``OVERWRITE_EXISTING=true`` 时覆盖）。
    返回体含 ``relpath`` 与 ``download_url``，可直接用于后续下载 / 删除。
    """
    info = await storage.save(identity, file, filename=filename, subdir=subdir)
    return UploadResponse(file=info)


@router.get("/files", response_model=FileListResponse, summary="列出当前用户名下的文件")
async def list_files(
    subdir: str = Query("", description="可选：只列该子目录下的文件"),
    identity: Identity = Depends(get_current_identity),
    storage: Storage = Depends(get_storage),
) -> FileListResponse:
    items = storage.list_files(identity, subdir)
    return FileListResponse(total=len(items), items=items)


@router.get(
    "/files/{relpath:path}",
    summary="下载自己的文件",
    response_class=FileResponse,
)
async def download_file(
    relpath: str,
    identity: Identity = Depends(get_current_identity),
    storage: Storage = Depends(get_storage),
) -> FileResponse:
    """``relpath`` 用上传响应里的值（含中文请 URL 编码）。

    只能下载当前用户目录内的文件，路径穿越返回 422 / 404。
    """
    path = storage.resolve_file(identity, relpath)
    return FileResponse(
        path,
        filename=path.name,
        media_type=storage.content_type(path.name),
        content_disposition_type="attachment",
    )


@router.delete(
    "/files/{relpath:path}",
    response_model=MessageResponse,
    summary="删除自己的文件",
)
async def delete_file(
    relpath: str,
    identity: Identity = Depends(get_current_identity),
    storage: Storage = Depends(get_storage),
) -> MessageResponse:
    rel = storage.delete(identity, relpath)
    return MessageResponse(message="已删除", relpath=rel)
