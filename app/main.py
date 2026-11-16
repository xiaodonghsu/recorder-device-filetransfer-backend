"""FastAPI 应用入口。

启动流程：
1. 校验配置风险项（生产环境）；
2. 创建存储根目录、共享 httpx 客户端与 Casdoor 校验器；
3. 注册异常处理器、早拒绝中间件、CORS 与 ``/voicehub-ogg`` 路由。

**nginx 子路由约定**：本服务的路由自身就带 ``/voicehub-ogg`` 前缀，
nginx 必须 **原样透传**（``proxy_pass`` 不带 URI 部分），不剥离前缀。
与设备管理后端的 ``/voicehub-api`` 部署方式完全一致。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import router
from app.auth import CasdoorVerifier
from app.config import settings
from app.db import FileRepository
from app.errors import AppError, AuthenticationError, PayloadTooLargeError
from app.storage import FileBackend, LocalStorage
from app.storage_obs import ObsStorage

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("filetransfer")

PREFIX = settings.api_prefix
#: multipart 的 boundary / 表单字段开销预算；真正的文件大小在存储层逐块校验。
MULTIPART_OVERHEAD_BYTES = 1024 * 1024
#: 前缀下无需令牌的路径（其余路径在中间件里做早拒绝）。
PUBLIC_PATHS = {
    f"{PREFIX}/healthz",
    f"{PREFIX}/docs",
    f"{PREFIX}/redoc",
    f"{PREFIX}/openapi.json",
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    for item in settings.validate_runtime():
        logger.warning("配置风险提示：%s", item)

    repository = FileRepository(settings)
    backend: FileBackend = (
        ObsStorage(settings) if settings.storage_backend == "obs" else LocalStorage(settings)
    )
    http_client = httpx.AsyncClient(
        timeout=settings.casdoor_timeout,
        verify=settings.casdoor_verify_tls,
        follow_redirects=True,
    )
    verifier = CasdoorVerifier(settings, http_client)
    app.state.backend = backend
    app.state.repository = repository
    app.state.http_client = http_client
    app.state.verifier = verifier
    logger.info(
        "服务已就绪：prefix=%s, backend=%s, database=%s, casdoor=%s, verify_method=%s",
        PREFIX,
        backend.name,
        repository.engine.url.render_as_string(hide_password=True),
        settings.casdoor_base_url,
        settings.casdoor_verify_method,
    )
    try:
        yield
    finally:
        await verifier.aclose()
        await http_client.aclose()
        await backend.close()
        repository.close()
        app.state.verifier = None
        app.state.backend = None
        app.state.repository = None


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "嘉环声枢录音卡录音文件传输服务（独立部署）。\n\n"
        "**认证**：所有业务接口需要 `Authorization: Bearer <Casdoor Access Token>`；"
        "服务端校验通过后按 `租户 / 工号`（`preferred_username`）/ `设备 SN` 三级隔离存储，"
        "客户端无法指定或访问其他用户的文件。\n\n"
        "**上传**：multipart 表单必须带 `device_sn`（录音卡 SN），它是用户目录下的"
        "一级目录；`subdir` 是设备目录内的可选子目录。下载 / 删除用"
        "`设备SN/子目录/文件名` 形式的 `relpath` 直接定位。\n\n"
        "**存储后端**：`STORAGE_BACKEND=local` 本地磁盘，或 `obs` 华为云 OBS（官方 SDK）；"
        "每次上传的文件名、对象键、大小、SHA-256 等元数据登记在 SQLite / MySQL，"
        "列表接口直接读数据库。\n\n"
        "**部署**：路由自带 `/voicehub-ogg` 前缀，nginx 原样透传。"
    ),
    lifespan=lifespan,
    docs_url=f"{PREFIX}/docs" if settings.docs_enabled else None,
    redoc_url=f"{PREFIX}/redoc" if settings.docs_enabled else None,
    openapi_url=f"{PREFIX}/openapi.json" if settings.docs_enabled else None,
)


# ---------------------------------------------------------------------------
# 早拒绝中间件
#
# FastAPI 会先解析 multipart body 再执行依赖，因此仅靠 Depends 无法阻止
# 「不带 Token 的大文件」把请求体读完。这里在 body 解析之前做两件事：
#   1. 受保护路径缺令牌 → 立刻 401，不读 body；
#   2. Content-Length 明显超限 → 立刻 413，不读 body。
# 真正的令牌校验仍在 get_current_identity 依赖中完成，此处只做廉价预判。
# ---------------------------------------------------------------------------
def _has_token(request: Request) -> bool:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer ") and header[7:].strip():
        return True
    if settings.allow_query_token and (request.query_params.get("access_token") or "").strip():
        return True
    return False


@app.middleware("http")
async def early_guard(request: Request, call_next):
    path = request.url.path
    normalized = path.rstrip("/") or "/"
    guarded = normalized.startswith(f"{PREFIX}/") and normalized not in PUBLIC_PATHS
    if guarded and request.method != "OPTIONS":
        if not _has_token(request):
            return JSONResponse(
                status_code=401,
                content=AuthenticationError(
                    "缺少访问令牌，请先通过 Casdoor 登录并用 Authorization: Bearer 传递"
                ).to_payload(),
            )
        raw_length = request.headers.get("content-length")
        if raw_length:
            try:
                length = int(raw_length)
            except ValueError:
                length = -1
            if length > settings.max_upload_bytes + MULTIPART_OVERHEAD_BYTES:
                return JSONResponse(
                    status_code=413,
                    content=PayloadTooLargeError(
                        f"请求体超过 {settings.max_upload_mb:g} MB 上限",
                        details={"max_bytes": settings.max_upload_bytes, "content_length": length},
                    ).to_payload(),
                )
    return await call_next(request)


# 后添加的中间件位于外层，保证早拒绝的 401/413 也带上 CORS 响应头。
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 异常处理：与设备管理后端保持相同的 {code, message, status, details}
# ---------------------------------------------------------------------------
@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.to_payload())


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "code": "validation_error",
            "message": "参数校验失败",
            "status": 422,
            "details": {"errors": jsonable_encoder(exc.errors())},
        },
    )


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": "http_error",
            "message": str(exc.detail),
            "status": exc.status_code,
        },
    )


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
app.include_router(router, prefix=PREFIX)


@app.get("/healthz", tags=["运维"], summary="存活探针（内网 / 容器）", include_in_schema=False)
async def healthz() -> dict:
    return {"status": "ok", "app": settings.app_name, "version": settings.app_version}


@app.get("/readyz", tags=["运维"], summary="就绪探针（检查存储与数据库）", include_in_schema=False)
async def readyz(request: Request) -> JSONResponse:
    backend: FileBackend = getattr(request.app.state, "backend", None)
    repository: FileRepository = getattr(request.app.state, "repository", None)
    if backend is None or repository is None:
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "storage": "服务未初始化", "database": "服务未初始化"},
        )
    storage_ok, storage_reason = await backend.ready()
    db_ok, db_reason = repository.ready()
    ok = storage_ok and db_ok
    return JSONResponse(
        status_code=200 if ok else 503,
        content={
            "status": "ready" if ok else "unavailable",
            "backend": backend.name,
            "storage": storage_reason,
            "database": db_reason,
        },
    )


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        proxy_headers=True,
    )
