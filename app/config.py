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
    port: int = 8030

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

    # ---------------- 文件标签 ----------------
    #: 单个文件最多标签数（去重后）；超出 422，不做静默截断。
    max_tags_per_file: int = 20
    #: 单个标签最大字符数；超出 422。
    max_tag_length: int = 32

    # ---------------- 存储后端选择 ----------------
    #: local = 本地文件系统；obs = 华为云 OBS（官方 esdk-obs-python SDK）
    storage_backend: str = "local"

    # ---------------- 华为云 OBS ----------------
    obs_access_key_id: str = ""
    obs_secret_access_key: str = ""
    obs_server: str = "obs.cn-south-1.myhuaweicloud.com"
    obs_bucket: str = "delocalization-public-files"
    #: 桶内统一前缀，最终对象键为
    #: {prefix}/{tenant_id}/{user_id}/{device_sn}/{subdir}/{filename}
    obs_prefix: str = "voicehub/sounds"
    #: OBS 没有真实目录；开启后会上传以 / 结尾的 0 字节对象，便于控制台按层级浏览
    obs_create_folder: bool = True
    obs_max_list_keys: int = 1000

    # ---------------- 元数据数据库 ----------------
    #: SQLite（相对路径以 filetransfer/ 为基准）或 MySQL：
    #: mysql+pymysql://user:password@127.0.0.1:3306/voicehub_files?charset=utf8mb4
    database_url: str = "sqlite:///./data/filetransfer.db"
    db_echo: bool = False

    # ---------------- Casdoor ----------------
    #: Casdoor 实例配置了 authPrefix=auth，所有接口都在 /auth 之下。
    casdoor_base_url: str = "https://uassist.cn/auth"
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

    @field_validator("storage_backend")
    @classmethod
    def _check_storage_backend(cls, v: str) -> str:
        value = (v or "local").strip().lower()
        if value not in {"local", "obs"}:
            raise ValueError("storage_backend 必须为 local 或 obs")
        return value

    @field_validator("max_tags_per_file")
    @classmethod
    def _check_max_tags_per_file(cls, v: int) -> int:
        if v < 1 or v > 100:
            raise ValueError("max_tags_per_file 必须在 [1, 100] 区间内")
        return v

    @field_validator("max_tag_length")
    @classmethod
    def _check_max_tag_length(cls, v: int) -> int:
        if v < 1 or v > 128:
            raise ValueError("max_tag_length 必须在 [1, 128] 区间内")
        return v

    @field_validator("obs_prefix")
    @classmethod
    def _clean_obs_prefix(cls, v: str) -> str:
        return "/".join(part for part in (v or "").strip().split("/") if part)

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

    @property
    def obs_has_credentials(self) -> bool:
        return bool(self.obs_access_key_id and self.obs_secret_access_key)

    @property
    def obs_endpoint(self) -> str:
        """SDK 需要带 scheme 的 endpoint；用户常只填域名，这里自动补全。"""
        value = (self.obs_server or "").strip()
        if not value.startswith(("http://", "https://")):
            value = "https://" + value
        return value.rstrip("/")

    def resolved_database_url(self) -> str:
        """把 SQLite 相对路径锚定到 filetransfer/，避免受启动 cwd 影响。

        MySQL 连接串原样返回，由 SQLAlchemy / PyMySQL 处理。
        """
        url = (self.database_url or "").strip()
        if not url.startswith("sqlite"):
            return url
        path = url[len("sqlite:///") :] if url.startswith("sqlite:///") else ""
        if not path or path == ":memory:":
            return url
        # 已是绝对路径（Unix / 或 Windows 盘符）则保持不变
        if path.startswith("/") or (len(path) > 1 and path[1] == ":"):
            return url
        absolute = (BASE_DIR / path).resolve().as_posix()
        return f"sqlite:///{absolute}"

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
        if self.storage_backend == "obs":
            if not self.obs_has_credentials:
                warnings.append("STORAGE_BACKEND=obs 但未配置 OBS_ACCESS_KEY_ID / OBS_SECRET_ACCESS_KEY")
            if not self.obs_bucket.strip():
                warnings.append("OBS_BUCKET 未配置")
        if self.database_url.startswith("sqlite"):
            warnings.append("元数据数据库使用 SQLite；多实例 / 生产环境建议改用 MySQL")
        return warnings


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
