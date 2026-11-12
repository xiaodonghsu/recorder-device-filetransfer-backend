"""filetransfer —— 配置中心。

配置优先级：环境变量 > ``filetransfer/.env`` > 代码默认值。

**独立性**：本模块只读取 ``filetransfer/.env``，不导入 backend/ 的任何模块，
也不共用 backend 的配置对象。两个服务恰好使用同名的 ``CASDOOR_*`` 变量，
但各自读取自己目录下的 .env，互不干扰（见 README「与现有服务的关系」）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List, Set

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# filetransfer/ 目录（本文件位于 filetransfer/app/config.py）
BASE_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------- 应用 ----------------
    app_name: str = "嘉环声枢录音文件传输服务"
    app_version: str = "1.0.0"
    app_env: str = "development"
    debug: bool = False
    #: 服务自身监听的接口前缀。nginx 原样透传，不剥离该前缀。
    api_prefix: str = "/voicehub-ogg"
    docs_enabled: bool = True
    #: 仅 ``python -m app.main`` 本地开发时使用；生产请用 uvicorn 命令行指定。
    host: str = "127.0.0.1"
    port: int = 8050

    # ---------------- 存储 ----------------
    #: 文件根目录。相对路径以 filetransfer/ 为基准解析。
    storage_dir: str = "./data/uploads"
    #: 单个文件上限（MB）。nginx 的 client_max_body_size 必须 >= 该值。
    max_upload_mb: float = 100.0
    #: 允许的扩展名白名单；填 ``*`` 表示不限制扩展名。
    allowed_extensions: str = ".ogg,.oga,.opus,.wav,.mp3,.m4a,.flac,.webm"
    #: 客户端可指定的子目录最大层数。
    max_subdir_depth: int = 3
    #: 同名文件是否直接覆盖；False 时自动追加 -1 / -2 后缀。
    overwrite_existing: bool = False
    upload_chunk_size: int = 64 * 1024
    max_list_items: int = 1000

    # ---------------- Casdoor ----------------
    #: Casdoor 实例配置了 authPrefix=auth，所有接口都在 /auth 之下。
    casdoor_base_url: str = "https://office.uassist.cn:40000/auth"
    casdoor_issuer: str = ""
    #: clientId 是公开标识符（前端 OAuth 跳转里会出现），可安全内置默认值。
    casdoor_client_id: str = "6932b8ce0b655b9421b5"
    #: clientSecret 是机密，只能通过环境变量 / .env 提供。
    casdoor_client_secret: str = ""
    #: 组织名即 tenant_id，与设备管理后端保持一致。
    casdoor_organization: str = "bestlink"
    #: auto | userinfo | introspect | jwt
    casdoor_verify_method: str = "auto"
    casdoor_timeout: float = 10.0
    casdoor_verify_tls: bool = True
    casdoor_jwks_cache_ttl: int = 3600
    #: 是否要求 Token 中的组织与上面配置的组织一致（防跨组织使用）。
    casdoor_require_organization: bool = True

    # ---------------- 安全 ----------------
    #: 是否允许用 ?access_token= 传令牌（方便 APP / 浏览器直接下载）。
    #: 默认关闭：URL 会被 nginx、浏览器历史、access log 记录，有泄漏风险。
    allow_query_token: bool = False
    cors_origins: str = "https://office.uassist.cn:40000"

    # ---------------- 校验器 ----------------
    @field_validator("api_prefix")
    @classmethod
    def _check_api_prefix(cls, v: str) -> str:
        """规范化前缀，并拦截 Git Bash 的路径自动转换。

        在 MSYS / Git Bash 下执行 ``API_PREFIX=/voicehub-ogg python ...``，
        以 ``/`` 开头的值会被转换成 ``C:/Program Files/Git/voicehub-ogg``，
        导致所有接口挂在错误路径上。请改用 filetransfer/.env 配置，
        或先执行 ``export MSYS_NO_PATHCONV=1``。
        """
        value = (v or "").strip()
        if not value:
            return "/voicehub-ogg"
        if ":" in value or "\\" in value:
            raise ValueError(
                "api_prefix 不应是文件路径（检测到 ':' 或 '\\'）。"
                "在 Git Bash / MSYS 下直接写 `API_PREFIX=/voicehub-ogg` 会被"
                "自动转换成 Windows 绝对路径，请改用 filetransfer/.env，"
                "或执行 `export MSYS_NO_PATHCONV=1` 后再设置环境变量。"
            )
        if not value.startswith("/"):
            value = "/" + value
        return value.rstrip("/") or "/voicehub-ogg"

    @field_validator("casdoor_verify_method")
    @classmethod
    def _check_verify_method(cls, v: str) -> str:
        allowed = {"auto", "userinfo", "introspect", "jwt"}
        value = (v or "auto").strip().lower()
        if value not in allowed:
            raise ValueError(f"casdoor_verify_method 必须为 {sorted(allowed)} 之一")
        return value

    @field_validator("casdoor_base_url")
    @classmethod
    def _strip_base_url(cls, v: str) -> str:
        return (v or "").rstrip("/")

    @field_validator("max_upload_mb")
    @classmethod
    def _check_max_upload_mb(cls, v: float) -> float:
        if v <= 0 or v > 4096:
            raise ValueError("max_upload_mb 必须在 (0, 4096] 区间内")
        return v

    # ---------------- 派生属性 ----------------
    @property
    def storage_path(self) -> Path:
        path = Path(self.storage_dir).expanduser()
        if not path.is_absolute():
            path = BASE_DIR / path
        return path.resolve()

    @property
    def max_upload_bytes(self) -> int:
        return int(self.max_upload_mb * 1024 * 1024)

    @property
    def extension_set(self) -> Set[str]:
        values = {e.strip().lower() for e in self.allowed_extensions.split(",") if e.strip()}
        return {e if e.startswith(".") else f".{e}" for e in values}

    @property
    def cors_origin_list(self) -> List[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def casdoor_has_client_credentials(self) -> bool:
        return bool(self.casdoor_client_id and self.casdoor_client_secret)

    def validate_runtime(self) -> List[str]:
        """返回生产环境下的配置风险清单（空列表表示无风险项）。"""
        warnings: List[str] = []
        if self.app_env.lower() != "production":
            return warnings
        if self.casdoor_verify_method == "introspect" and not self.casdoor_has_client_credentials:
            warnings.append("introspect 模式必须配置 CASDOOR_CLIENT_SECRET")
        if not self.casdoor_has_client_credentials:
            warnings.append("未配置 CASDOOR_CLIENT_SECRET，Token 校验走 userinfo 在线模式")
        if self.casdoor_base_url.startswith("http://"):
            warnings.append("CASDOOR_BASE_URL 使用明文 http，生产环境建议启用 https")
        if self.allow_query_token:
            warnings.append("ALLOW_QUERY_TOKEN=true，令牌会出现在 URL 与访问日志中")
        if self.docs_enabled:
            warnings.append("DOCS_ENABLED=true，生产环境建议关闭在线接口文档")
        if "*" in self.extension_set:
            warnings.append("ALLOWED_EXTENSIONS=*，未限制上传文件类型")
        return warnings


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
