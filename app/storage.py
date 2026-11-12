"""按用户名隔离的文件存储层。

目录布局（用户名由服务端从 Token 推导，客户端无法伪造）：

```
{STORAGE_DIR}/
└── {username}/                 # Casdoor 工号 preferred_username
    ├── 2026-11-12/             # 可选子目录（subdir 表单字段）
    │   └── meeting-01.ogg
    └── standup.ogg
```

安全边界（四道）：

1. **净化**：用户名 / 文件名 / 子目录逐段白名单化，剥离路径分隔符、
   控制字符、``.`` / ``..`` 与 Windows 保留设备名；
2. **归属**：所有路径都从当前用户的根目录出发，不接受客户端传绝对/相对根路径；
3. **落点校验**：``resolve()`` 后必须仍在用户根目录内（同时挡住符号链接逃逸）；
4. **原子写入**：先写同目录 ``.part`` 临时文件，流式分块 + 超限即删，
   完成后 ``os.replace`` 发布，下载方永远不会读到半截文件。
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import uuid
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


class Storage:
    """文件系统存储。所有公开方法都以 :class:`Identity` 为唯一归属依据。"""

    def __init__(self, cfg: Settings, root: Optional[Path] = None) -> None:
        self.cfg = cfg
        self.root = (root or cfg.storage_path).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------
    @staticmethod
    def _ensure_within(root: Path, target: Path) -> Path:
        resolved_root = root.resolve()
        resolved = target.resolve()  # strict=False，不存在的路径也会被规范化
        if not resolved.is_relative_to(resolved_root):
            raise ValidationError("目标路径越出允许范围")
        return resolved

    def user_root(self, username: str, *, create: bool = True) -> Path:
        path = self.root / clean_username(username)
        self._ensure_within(self.root, path)
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def _directory(self, user_root: Path, subdir: str, *, create: bool = True) -> Path:
        parts = clean_path_segments(subdir, max_depth=self.cfg.max_subdir_depth)
        target = user_root.joinpath(*parts) if parts else user_root
        self._ensure_within(user_root, target)
        if create:
            target.mkdir(parents=True, exist_ok=True)
        return target

    def resolve_file(self, identity: Identity, relpath: str) -> Path:
        """下载 / 删除共用：解析相对路径并确认归属，绝不越出用户目录。"""
        parts = clean_path_segments(
            relpath, max_depth=self.cfg.max_subdir_depth + 1
        )
        if not parts:
            raise ValidationError("请指定文件路径")
        if parts[-1].startswith(".") or parts[-1].endswith(".part"):
            raise NotFoundError("文件不存在或不可访问")

        user_root = self.user_root(identity.username, create=False)
        resolved = self._ensure_within(user_root, user_root.joinpath(*parts))
        if not resolved.is_file():
            raise NotFoundError("文件不存在或不可访问")
        return resolved

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
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

    def _check_extension(self, name: str) -> None:
        allowed = self.cfg.extension_set
        if "*" in allowed:
            return
        ext = Path(name).suffix.lower()
        if ext not in allowed:
            raise UnsupportedMediaTypeError(
                f"不支持的文件扩展名 {ext or '(无)'}",
                details={"filename": name, "allowed": sorted(allowed)},
            )

    async def save(
        self,
        identity: Identity,
        upload: UploadFile,
        *,
        filename: Optional[str] = None,
        subdir: str = "",
    ) -> FileInfo:
        """流式接收文件并落到 ``{root}/{username}/{subdir}/``。

        ``UploadFile`` 由 Starlette 落到 SpooledTemporaryFile，这里按块读取，
        单请求内存占用恒定（约一个 chunk），适合几十 MB 的录音文件。
        """
        name = clean_filename(filename or upload.filename or "")
        self._check_extension(name)

        user_root = self.user_root(identity.username)
        directory = self._directory(user_root, subdir)
        final_name = name if self.cfg.overwrite_existing else self._available_name(directory, name)
        final = self._directory_path(directory, final_name, user_root)

        tmp = directory / f".{uuid.uuid4().hex}.part"
        size = 0
        digest = hashlib.sha256()
        limit = self.cfg.max_upload_bytes
        try:
            with tmp.open("xb") as handle:
                while True:
                    chunk = await upload.read(self.cfg.upload_chunk_size)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise PayloadTooLargeError(
                            f"文件超过 {self.cfg.max_upload_mb:g} MB 上限",
                            details={"max_bytes": limit, "received_bytes": size},
                        )
                    digest.update(chunk)
                    handle.write(chunk)
            os.replace(tmp, final)  # 原子发布；同名并发时后者覆盖，见 README 说明
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        finally:
            await upload.close()

        return self._file_info(identity, user_root, final, size=size, sha256=digest.hexdigest())

    def _directory_path(self, directory: Path, name: str, user_root: Path) -> Path:
        target = directory / name
        self._ensure_within(user_root, target)
        return target

    # ------------------------------------------------------------------
    # 读取 / 删除
    # ------------------------------------------------------------------
    def content_type(self, name: str) -> str:
        guessed = mimetypes.guess_type(name)[0]
        return guessed or _MIME_FALLBACK.get(Path(name).suffix.lower(), "application/octet-stream")

    def _file_info(
        self,
        identity: Identity,
        user_root: Path,
        path: Path,
        *,
        size: Optional[int] = None,
        sha256: str = "",
    ) -> FileInfo:
        stat = path.stat()
        rel = path.relative_to(user_root).as_posix()
        modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        return FileInfo(
            name=path.name,
            relpath=rel,
            owner=identity.username,
            size=size if size is not None else stat.st_size,
            sha256=sha256,
            content_type=self.content_type(path.name),
            modified_at=modified,
            download_url=f"{self.cfg.api_prefix}/files/{quote(rel, safe='/')}",
        )

    def list_files(self, identity: Identity, subdir: str = "") -> List[FileInfo]:
        user_root = self.user_root(identity.username, create=False)
        if not user_root.is_dir():
            return []
        base = self._directory(user_root, subdir, create=False)
        if not base.is_dir():
            return []

        items: List[FileInfo] = []
        for path in base.rglob("*"):
            if not path.is_file() or path.name.startswith(".") or path.suffix == ".part":
                continue
            try:
                self._ensure_within(user_root, path)
                items.append(self._file_info(identity, user_root, path))
            except (ValidationError, OSError):
                # 跳过逃逸的符号链接 / 读取失败的文件，不影响整体列表
                continue
        items.sort(key=lambda item: item.modified_at, reverse=True)
        return items[: self.cfg.max_list_items]

    def delete(self, identity: Identity, relpath: str) -> str:
        path = self.resolve_file(identity, relpath)
        user_root = self.user_root(identity.username, create=False)
        rel = path.relative_to(user_root).as_posix()
        try:
            path.unlink()
        except OSError as exc:
            raise StorageError(f"删除文件失败: {exc}") from exc
        return rel

    def ready(self) -> Tuple[bool, str]:
        """就绪探针：存储根目录必须可写。"""
        if not self.root.is_dir():
            return False, "存储目录不存在"
        probe = self.root / f".readyz-{uuid.uuid4().hex}"
        try:
            probe.write_bytes(b"ok")
            probe.unlink()
            return True, "ok"
        except OSError as exc:
            return False, f"存储目录不可写: {exc}"
