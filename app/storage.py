"""存储层公共契约、路径安全与本地文件系统实现。

两种后端共用同一套**归属口径**（租户与用户名由服务端从 Token 推导，设备 SN 由
客户端声明后严格净化，三者共同构成不可伪造的存储根目录）：

```
本地磁盘：{STORAGE_DIR}/{tenant_id}/{user_id}/{device_sn}/{subdir}/{filename}
华为云 OBS：obs://{bucket}/{OBS_PREFIX}/{tenant_id}/{user_id}/{device_sn}/{subdir}/{filename}
```

其中 ``user_id`` 取 Casdoor ``preferred_username``（工号）——沿用设备管理后端的
业务约定；``device_sn`` 取录音卡 SN（如 ``VH-2026-0001``），使同一工号名下
多台设备的录音互不混放。``subdir`` 是设备目录内的可选子目录，不传时文件直接
落在 ``{tenant}/{user}/{sn}/{filename}``。

对外暴露的 ``relpath`` 一律是**相对用户根目录**的完整路径，首段即设备 SN
（``VH-2026-0001/2026-11-16/周会.ogg``），下载 / 删除 / 列表都用它定位，
不需要额外传设备参数。

安全边界（四道）：

1. **净化**：租户 / 用户名 / 设备 SN / 文件名 / 子目录逐段白名单化，剥离路径分隔符、
   控制字符、``.`` / ``..`` 与 Windows 保留设备名；设备 SN 只允许
   ``[A-Za-z0-9._-]``，出现其它字符直接 422 拒绝而不是猜测；
2. **归属**：所有路径都从当前用户的根目录出发，不接受客户端传绝对/相对根路径；
3. **落点校验**：``resolve()`` 后必须仍在设备根目录内（同时挡住符号链接逃逸）；
4. **原子写入**：先写同目录 ``.part`` 临时文件，流式分块 + 超限即删，
   完成后 ``os.replace`` 发布，下载方永远不会读到半截文件。
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import quote

from fastapi import UploadFile

from app.auth import Identity
from app.config import Settings
from app.errors import (
    NotFoundError,
    PayloadTooLargeError,
    StorageError,
    UnsupportedMediaTypeError,
    ValidationError,
)
from app.schemas import FileInfo

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_UNSAFE_RE = re.compile(r"[^\w.\- ]", re.UNICODE)
_MAX_NAME_LEN = 180
#: 设备 SN 目录段：与 backend 的 device.sn（String(64)）同长，且只接受
#: ASCII 字母数字与 . _ -，首字符必须是字母数字（避免隐藏目录 / 路径歧义）。
_DEVICE_SN_MAX_LEN = 64
_DEVICE_SN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_MIME_FALLBACK = {
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".webm": "audio/webm",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
}


# ---------------------------------------------------------------------------
# 路径净化（本地与 OBS 共用）
# ---------------------------------------------------------------------------
def _avoid_reserved(name: str) -> str:
    """Windows 上 CON / COM1 等是设备名，直接写会失败或写到设备。"""
    stem = name.split(".", 1)[0].upper()
    return f"_{name}" if stem in _WINDOWS_RESERVED else name


def clean_username(username: str) -> str:
    """把 Token 里的工号转成安全的目录名；非法字符一律拒绝而不是猜测。"""
    value = (username or "").strip()
    if not value or len(value) > 64 or _CONTROL_RE.search(value):
        raise ValidationError("身份中的用户名不适合作为目录名")
    if "/" in value or "\\" in value or ":" in value:
        raise ValidationError("身份中的用户名包含路径分隔符")
    if value.startswith(".") or value in {".", ".."}:
        raise ValidationError("身份中的用户名不能以点开头")
    cleaned = _UNSAFE_RE.sub("_", value).strip()
    if not cleaned:
        raise ValidationError("身份中的用户名为空")
    return _avoid_reserved(cleaned)


def clean_tenant(tenant_id: str, fallback: str) -> str:
    """租户段：Token 未带组织时回退到配置值，但同样做路径净化。"""
    parts = clean_path_segments((tenant_id or fallback or "").strip(), max_depth=1)
    if not parts:
        raise ValidationError("租户标识为空，无法隔离存储目录")
    return parts[0]


def clean_device_sn(raw: str) -> str:
    """把录音卡 SN 转成安全的目录段；格式不合规一律 422 拒绝，不做猜测性替换。

    与设备管理后端 ``device.sn``（String(64)）保持同一长度上限。只允许
    ``[A-Za-z0-9._-]`` 且以字母/数字开头，因此不会出现路径分隔符、``..``、
    控制字符或隐藏目录。大小写保持原样（SN 在业务库里是精确匹配口径）。
    """
    value = (raw or "").strip()
    if not value:
        raise ValidationError(
            "缺少设备 SN，无法定位设备目录", details={"field": "device_sn"}
        )
    if (
        len(value) > _DEVICE_SN_MAX_LEN
        or _CONTROL_RE.search(value)
        or not _DEVICE_SN_RE.fullmatch(value)
    ):
        raise ValidationError(
            "设备 SN 格式非法：只允许字母、数字、点、下划线、连字符，且以字母或数字开头",
            details={"device_sn": raw, "max_length": _DEVICE_SN_MAX_LEN},
        )
    return _avoid_reserved(value)


def clean_filename(raw: str) -> str:
    """取 basename 并净化；客户端传 ``../../x.ogg`` 也只会落到 ``x.ogg``。"""
    value = (raw or "").strip().replace("\\", "/")
    name = value.rsplit("/", 1)[-1]
    name = _CONTROL_RE.sub("", name).strip().strip(".")
    name = _UNSAFE_RE.sub("_", name).strip()
    if not name or name in {".", ".."}:
        raise ValidationError("文件名非法或为空", details={"filename": raw})
    if len(name) > _MAX_NAME_LEN:
        stem, ext = os.path.splitext(name)
        name = stem[: _MAX_NAME_LEN - len(ext)] + ext
    return _avoid_reserved(name)


def clean_path_segments(raw: str, *, max_depth: int) -> List[str]:
    """把 ``subdir`` / ``relpath`` 拆成净化后的段；``..`` 一律拒绝。"""
    if not raw:
        return []
    value = (raw or "").strip().replace("\\", "/")
    if value.startswith("/"):
        raise ValidationError("不接受绝对路径")
    parts: List[str] = []
    for piece in value.split("/"):
        if piece == "":
            continue
        if piece in {".", ".."}:
            raise ValidationError("路径中不允许出现 . 或 ..", details={"path": raw})
        segment = _CONTROL_RE.sub("", piece).strip().strip(".")
        segment = _UNSAFE_RE.sub("_", segment).strip()
        if not segment:
            raise ValidationError("路径包含空段", details={"path": raw})
        parts.append(_avoid_reserved(segment))
    if len(parts) > max_depth:
        raise ValidationError(
            f"目录层数超过上限 {max_depth}", details={"path": raw, "depth": len(parts)}
        )
    return parts


def ensure_within(root: Path, target: Path) -> Path:
    resolved_root = root.resolve()
    resolved = target.resolve()  # strict=False，不存在的路径也会被规范化
    if not resolved.is_relative_to(resolved_root):
        raise ValidationError("目标路径越出允许范围")
    return resolved


def check_extension(cfg: Settings, name: str) -> None:
    allowed = cfg.extension_set
    if "*" in allowed:
        return
    ext = Path(name).suffix.lower()
    if ext not in allowed:
        raise UnsupportedMediaTypeError(
            f"不支持的文件扩展名 {ext or '(无)'}",
            details={"filename": name, "allowed": sorted(allowed)},
        )


def content_type(name: str) -> str:
    guessed = mimetypes.guess_type(name)[0]
    return guessed or _MIME_FALLBACK.get(Path(name).suffix.lower(), "application/octet-stream")


def build_relpath(cfg: Settings, device_sn: str, subdir: str, name: str) -> str:
    """构造对外 ``relpath``：``{设备SN}/{subdir}/{name}``（相对用户根目录）。"""
    sn = clean_device_sn(device_sn)
    parts = clean_path_segments(subdir, max_depth=cfg.max_subdir_depth)
    parts.append(name)
    return "/".join([sn, *parts])


def join_relpath(device_sn: str, parts: List[str]) -> str:
    """把设备内路径段拼回对外 ``relpath``（首段始终是净化后的设备 SN）。"""
    return "/".join([clean_device_sn(device_sn), *parts])


def parse_relpath(cfg: Settings, relpath: str) -> Tuple[str, List[str]]:
    """下载 / 删除共用：拆成 ``(设备SN, 设备内路径段)``。

    ``relpath`` 首段必须是合法的设备 SN，其后至少要有文件名。禁止绝对路径、
    ``.`` / ``..``、隐藏文件与 ``.part`` 临时文件。返回的第二段列表是相对
    **设备目录**的路径，交给存储后端拼接根目录。
    """
    value = (relpath or "").strip().replace("\\", "/")
    if value.startswith("/"):
        raise ValidationError("不接受绝对路径")
    pieces = [piece for piece in value.split("/") if piece != ""]
    if len(pieces) < 2:
        raise ValidationError(
            "文件路径必须包含设备 SN 段（设备SN/文件名）",
            details={"relpath": relpath},
        )
    sn = clean_device_sn(pieces[0])
    inner = clean_path_segments(
        "/".join(pieces[1:]), max_depth=cfg.max_subdir_depth + 1
    )
    if not inner:
        raise ValidationError("请指定文件路径")
    if inner[-1].startswith(".") or inner[-1].endswith(".part"):
        raise NotFoundError("文件不存在或不可访问")
    return sn, inner


def make_file_info(
    cfg: Settings,
    identity: Identity,
    *,
    device_sn: str,
    name: str,
    relpath: str,
    size: int,
    sha256: str,
    backend: str,
    object_key: Optional[str] = None,
    bucket: Optional[str] = None,
    modified_at: Optional[str] = None,
) -> FileInfo:
    stamp = modified_at or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return FileInfo(
        name=name,
        relpath=relpath,
        device_sn=device_sn,
        owner=identity.username,
        size=size,
        sha256=sha256,
        content_type=content_type(name),
        modified_at=stamp,
        download_url=f"{cfg.api_prefix}/files/{quote(relpath, safe='/')}",
        tenant_id=identity.tenant_id,
        backend=backend,
        object_key=object_key,
        bucket=bucket,
    )


# ---------------------------------------------------------------------------
# 后端契约
# ---------------------------------------------------------------------------
@dataclass
class SaveResult:
    info: FileInfo
    object_key: Optional[str] = None


@dataclass
class DownloadResult:
    filename: str
    content_type: str
    size: int
    #: 本地路径（本地后端直接指向正式文件；OBS 后端指向临时下载文件）
    local_path: Optional[Path] = None
    #: 响应结束后是否需要删除该临时文件
    cleanup: bool = False


class FileBackend(ABC):
    """可插拔存储后端。列表统一由元数据数据库提供，后端只负责存取。"""

    name: str = "base"

    @abstractmethod
    async def save(
        self,
        identity: Identity,
        upload: UploadFile,
        *,
        device_sn: str,
        filename=None,
        subdir: str = "",
    ) -> SaveResult: ...

    @abstractmethod
    async def download(self, identity: Identity, relpath: str) -> DownloadResult: ...

    @abstractmethod
    async def delete(self, identity: Identity, relpath: str) -> str: ...

    @abstractmethod
    async def ready(self) -> Tuple[bool, str]: ...

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# 公共：把 multipart 流写成临时文件
# ---------------------------------------------------------------------------
async def write_upload_to_temp(
    upload: UploadFile,
    cfg: Settings,
    *,
    directory: Optional[Path] = None,
) -> Tuple[Path, int, str]:
    """分块流式接收，返回 ``(临时文件, 字节数, sha256)``。

    ``directory`` 不为空时临时文件建在目标目录内，保证与最终文件同一卷，
    ``os.replace`` 才能原子发布（Windows 跨盘 rename 会失败）。
    OBS 后端不需要原子发布，用系统临时目录即可。
    """
    kwargs = {"prefix": ".voicehub-", "suffix": ".part"}
    if directory is not None:
        kwargs["dir"] = str(directory)
    fd, raw = tempfile.mkstemp(**kwargs)
    tmp = Path(raw)
    size = 0
    digest = hashlib.sha256()
    limit = cfg.max_upload_bytes
    try:
        with os.fdopen(fd, "wb") as handle:
            while True:
                chunk = await upload.read(cfg.upload_chunk_size)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise PayloadTooLargeError(
                        f"文件超过 {cfg.max_upload_mb:g} MB 上限",
                        details={"max_bytes": limit, "received_bytes": size},
                    )
                digest.update(chunk)
                handle.write(chunk)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()
    return tmp, size, digest.hexdigest()


# ---------------------------------------------------------------------------
# 本地文件系统后端
# ---------------------------------------------------------------------------
class LocalStorage(FileBackend):
    """``{STORAGE_DIR}/{tenant_id}/{user_id}/{device_sn}/`` 布局。"""

    name = "local"

    def __init__(self, cfg: Settings, root: Optional[Path] = None) -> None:
        self.cfg = cfg
        self.root = (root or cfg.storage_path).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def user_root(self, identity: Identity, *, create: bool = True) -> Path:
        """用户根目录 ``{root}/{tenant}/{工号}``（不含设备层）。"""
        tenant = clean_tenant(identity.tenant_id, self.cfg.casdoor_organization)
        user = clean_username(identity.username)
        path = self.root / tenant / user
        ensure_within(self.root, path)
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def device_root(self, identity: Identity, device_sn: str, *, create: bool = True) -> Path:
        """设备根目录 ``{user_root}/{设备SN}``，所有落盘都从这里出发。"""
        sn = clean_device_sn(device_sn)
        user_root = self.user_root(identity, create=create)
        path = user_root / sn
        ensure_within(user_root, path)
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def resolve_file(self, identity: Identity, relpath: str) -> Path:
        """按 ``设备SN/子目录/文件名`` 定位；越界 / 隐藏 / .part 一律拒绝。"""
        sn, parts = parse_relpath(self.cfg, relpath)
        device_root = self.device_root(identity, sn, create=False)
        resolved = ensure_within(device_root, device_root.joinpath(*parts))
        if not resolved.is_file():
            raise NotFoundError("文件不存在或不可访问")
        return resolved

    @staticmethod
    def _available_name(directory: Path, name: str) -> str:
        if not (directory / name).exists():
            return name
        stem, ext = os.path.splitext(name)
        for index in range(1, 10000):
            candidate = f"{stem}-{index}{ext}"
            if not (directory / candidate).exists():
                return candidate
        raise StorageError("同名文件过多，请指定新的文件名")

    async def save(
        self,
        identity: Identity,
        upload: UploadFile,
        *,
        device_sn: str,
        filename: Optional[str] = None,
        subdir: str = "",
    ) -> SaveResult:
        sn = clean_device_sn(device_sn)  # 缺 SN / 非法 SN 直接 422，不建目录
        name = clean_filename(filename or upload.filename or "")
        check_extension(self.cfg, name)
        base_parts = clean_path_segments(subdir, max_depth=self.cfg.max_subdir_depth)
        device_root = self.device_root(identity, sn)
        directory = device_root.joinpath(*base_parts) if base_parts else device_root
        ensure_within(device_root, directory)
        directory.mkdir(parents=True, exist_ok=True)

        final_name = name if self.cfg.overwrite_existing else self._available_name(directory, name)
        final = directory / final_name
        ensure_within(device_root, final)

        tmp, size, digest = await write_upload_to_temp(upload, self.cfg, directory=directory)
        try:
            os.replace(tmp, final)  # 原子发布
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        inner = final.relative_to(device_root).as_posix()
        info = make_file_info(
            self.cfg, identity, device_sn=sn, name=final_name,
            relpath=f"{sn}/{inner}", size=size, sha256=digest, backend=self.name,
        )
        return SaveResult(info=info)

    async def download(self, identity: Identity, relpath: str) -> DownloadResult:
        path = self.resolve_file(identity, relpath)
        return DownloadResult(
            filename=path.name,
            content_type=content_type(path.name),
            size=path.stat().st_size,
            local_path=path,
            cleanup=False,
        )

    async def delete(self, identity: Identity, relpath: str) -> str:
        sn, _ = parse_relpath(self.cfg, relpath)
        path = self.resolve_file(identity, relpath)
        device_root = self.device_root(identity, sn, create=False)
        rel = f"{sn}/{path.relative_to(device_root).as_posix()}"
        try:
            path.unlink()
        except OSError as exc:
            raise StorageError(f"删除文件失败: {exc}") from exc
        return rel

    async def ready(self) -> Tuple[bool, str]:
        if not self.root.is_dir():
            return False, "存储目录不存在"
        probe = self.root / f".readyz-{os.urandom(8).hex()}"
        try:
            probe.write_bytes(b"ok")
            probe.unlink()
            return True, "ok"
        except OSError as exc:
            return False, f"存储目录不可写: {exc}"
