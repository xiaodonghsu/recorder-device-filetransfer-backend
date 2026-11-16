"""华为云 OBS 存储后端（官方 ``esdk-obs-python`` SDK）。

对象键规则（与用户约定一致，设备 SN 是强制的一级目录）：

```
obs://{OBS_BUCKET}/{OBS_PREFIX}/{tenant_id}/{user_id}/{device_sn}/{subdir}/{filename}
例：obs://delocalization-public-files/voicehub/sounds/bestlink/JH000059/VH-2026-0001/a.ogg
```

OBS 是扁平对象存储、没有真实目录。开启 ``OBS_CREATE_FOLDER`` 后，服务端会在首次
上传时以 ``putObject`` 写入以 ``/`` 结尾的 0 字节「目录对象」，把
``voicehub/sounds/``、``.../{tenant}/``、``.../{tenant}/{user}/``、
``.../{tenant}/{user}/{sn}/`` 四级路径显式创建出来，控制台即可按层级浏览。
目录对象按设备 SN 分别缓存，同一工号下每台设备首次上传各写一次，幂等。

SDK 是同步阻塞客户端，所有网络调用都通过 ``asyncio.to_thread`` 放到线程池，
不阻塞 FastAPI 事件循环。上传先流式落临时文件再 ``putFile``，下载用
``getObject(downloadPath=...)`` 落到临时文件由 FileResponse 返回并在响应后清理。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any, List, Optional, Set, Tuple

from fastapi import UploadFile

from app.auth import Identity
from app.config import Settings
from app.errors import NotFoundError, RemoteStorageError, StorageError
from app.schemas import FileInfo
from app.storage import (
    DownloadResult,
    FileBackend,
    SaveResult,
    build_relpath,
    check_extension,
    clean_device_sn,
    clean_filename,
    clean_path_segments,
    clean_tenant,
    clean_username,
    content_type,
    join_relpath,
    make_file_info,
    parse_relpath,
    write_upload_to_temp,
)


class ObsStorage(FileBackend):
    """华为云 OBS 后端。``client`` 可注入以便离线测试（不联网）。"""

    name = "obs"

    def __init__(self, cfg: Settings, client: Optional[Any] = None) -> None:
        self.cfg = cfg
        self._owns_client = client is None
        if client is None:
            if not cfg.obs_has_credentials:
                raise StorageError(
                    "STORAGE_BACKEND=obs 需要配置 OBS_ACCESS_KEY_ID / OBS_SECRET_ACCESS_KEY"
                )
            try:
                from obs import ObsClient  # 官方 SDK，延迟导入以便 local 模式不必安装
            except ImportError as exc:  # pragma: no cover - 缺依赖时才触发
                raise StorageError(
                    "未安装华为云 OBS 官方 Python SDK，请执行 pip install esdk-obs-python"
                ) from exc
            client = ObsClient(
                access_key_id=cfg.obs_access_key_id,
                secret_access_key=cfg.obs_secret_access_key,
                server=cfg.obs_endpoint,  # 自动补 https://，见 Settings.obs_endpoint
            )
        self._client = client
        self._folders: Set[str] = set()

    # ------------------------------------------------------------------
    # 对象键
    # ------------------------------------------------------------------
    def _identity_parts(self, identity: Identity) -> Tuple[str, str]:
        return (
            clean_tenant(identity.tenant_id, self.cfg.casdoor_organization),
            clean_username(identity.username),
        )

    def user_prefix(self, identity: Identity) -> str:
        tenant, user = self._identity_parts(identity)
        return f"{self.cfg.obs_prefix}/{tenant}/{user}"

    def device_prefix(self, identity: Identity, device_sn: str) -> str:
        """设备前缀：``{prefix}/{tenant}/{工号}/{设备SN}``。"""
        return f"{self.user_prefix(identity)}/{clean_device_sn(device_sn)}"

    def object_key(self, identity: Identity, relpath: str) -> str:
        sn, inner = parse_relpath(self.cfg, relpath)
        return f"{self.device_prefix(identity, sn)}/{'/'.join(inner)}"

    # ------------------------------------------------------------------
    # SDK 响应处理
    # ------------------------------------------------------------------
    @staticmethod
    def _status(resp: Any) -> int:
        return int(getattr(resp, "status", 0) or 0)

    def _ensure_ok(self, resp: Any, action: str) -> None:
        status = self._status(resp)
        if 0 < status < 300:
            return
        # 只取 SDK 的错误描述，绝不把 AK/SK 或完整请求对象写进异常/日志
        message = (
            getattr(resp, "errorMessage", None)
            or getattr(resp, "reason", None)
            or "未知错误"
        )
        raise RemoteStorageError(
            f"OBS {action} 失败: {message}",
            details={
                "status": status,
                "error_code": getattr(resp, "errorCode", None) or "",
            },
        )

    def _ensure_folders_sync(self, identity: Identity, device_sn: str) -> None:
        if not self.cfg.obs_create_folder:
            return
        tenant, user = self._identity_parts(identity)
        sn = clean_device_sn(device_sn)
        prefix = self.cfg.obs_prefix
        keys = [
            f"{prefix}/",
            f"{prefix}/{tenant}/",
            f"{prefix}/{tenant}/{user}/",
            f"{prefix}/{tenant}/{user}/{sn}/",
        ]
        for key in keys:
            if key in self._folders:
                continue
            resp = self._client.putObject(self.cfg.obs_bucket, key, b"")
            self._ensure_ok(resp, f"创建目录 {key}")
            self._folders.add(key)

    def _head_exists(self, key: str) -> bool:
        try:
            return 0 < self._status(self._client.headObject(self.cfg.obs_bucket, key)) < 300
        except Exception:  # noqa: BLE001 - headObject 在某些版本对 404 抛异常
            return False

    def _available_rel(self, identity: Identity, rel: str) -> str:
        if self.cfg.overwrite_existing:
            return rel
        base = self.user_prefix(identity)
        stem, ext = os.path.splitext(rel)
        for index in range(10000):
            candidate = rel if index == 0 else f"{stem}-{index}{ext}"
            if not self._head_exists(f"{base}/{candidate}"):
                return candidate
        raise StorageError("同名对象过多，请指定新的文件名")

    # ------------------------------------------------------------------
    # 存取
    # ------------------------------------------------------------------
    async def save(
        self,
        identity: Identity,
        upload: UploadFile,
        *,
        device_sn: str,
        filename: Optional[str] = None,
        subdir: str = "",
    ) -> SaveResult:
        sn = clean_device_sn(device_sn)
        name = clean_filename(filename or upload.filename or "")
        check_extension(self.cfg, name)
        rel = build_relpath(self.cfg, sn, subdir, name)
        rel = await asyncio.to_thread(self._available_rel, identity, rel)
        key = f"{self.user_prefix(identity)}/{rel}"

        tmp, size, digest = await write_upload_to_temp(upload, self.cfg)
        try:
            # 首次上传时把缺失的租户 / 用户 / 设备目录对象创建出来
            await asyncio.to_thread(self._ensure_folders_sync, identity, sn)
            resp = await asyncio.to_thread(
                self._client.putFile, self.cfg.obs_bucket, key, str(tmp)
            )
            self._ensure_ok(resp, "上传对象")
        finally:
            tmp.unlink(missing_ok=True)

        info: FileInfo = make_file_info(
            self.cfg,
            identity,
            device_sn=sn,
            name=Path(rel).name,
            relpath=rel,
            size=size,
            sha256=digest,
            backend=self.name,
            object_key=key,
            bucket=self.cfg.obs_bucket,
        )
        return SaveResult(info=info, object_key=key)

    async def download(self, identity: Identity, relpath: str) -> DownloadResult:
        key = self.object_key(identity, relpath)
        name = Path(relpath.replace("\\", "/")).name
        fd, raw = tempfile.mkstemp(prefix="voicehub-dl-", suffix=Path(name).suffix)
        os.close(fd)
        tmp = Path(raw)
        try:
            resp = await asyncio.to_thread(
                self._client.getObject,
                self.cfg.obs_bucket,
                key,
                downloadPath=str(tmp),
            )
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        status = self._status(resp)
        if status == 404:
            tmp.unlink(missing_ok=True)
            raise NotFoundError("文件不存在或不可访问")
        try:
            self._ensure_ok(resp, "下载对象")
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return DownloadResult(
            filename=name,
            content_type=content_type(name),
            size=tmp.stat().st_size,
            local_path=tmp,
            cleanup=True,
        )

    async def delete(self, identity: Identity, relpath: str) -> str:
        sn, inner = parse_relpath(self.cfg, relpath)
        key = f"{self.device_prefix(identity, sn)}/{'/'.join(inner)}"
        resp = await asyncio.to_thread(self._client.deleteObject, self.cfg.obs_bucket, key)
        # OBS 删除不存在的对象可能返回 404，视为已删除
        if self._status(resp) >= 300 and self._status(resp) != 404:
            self._ensure_ok(resp, "删除对象")
        return join_relpath(sn, inner)

    # ------------------------------------------------------------------
    # 运维
    # ------------------------------------------------------------------
    async def list_object_keys(
        self, identity: Identity, subdir: str = "", device_sn: str = ""
    ) -> List[str]:
        """列出某用户 / 某设备在 OBS 下的对象键（运维 / 对账用，业务列表走数据库）。

        ``device_sn`` 为空时列该用户名下**所有设备**；否则只看该设备目录。
        ``subdir`` 是设备目录内的子目录前缀。
        """
        if device_sn:
            prefix = self.device_prefix(identity, device_sn) + "/"
        else:
            prefix = self.user_prefix(identity) + "/"
        extra = "/".join(clean_path_segments(subdir, max_depth=self.cfg.max_subdir_depth))
        if extra:
            if device_sn:
                prefix += extra + "/"
            else:
                # 跨设备筛选：OBS 前缀只能匹配开头，改为在结果里按设备内路径过滤
                keys = await self._list_all(prefix)
                base = self.user_prefix(identity) + "/"
                return [
                    k for k in keys
                    if k.startswith(base)
                    and (k[len(base):].split("/", 1)[1] if "/" in k[len(base):] else "").startswith(extra + "/")
                ]
        return await self._list_all(prefix)

    async def _list_all(self, prefix: str) -> List[str]:
        keys: List[str] = []
        marker: Optional[str] = None
        while True:
            resp = await asyncio.to_thread(
                self._client.listObjects,
                self.cfg.obs_bucket,
                prefix=prefix,
                marker=marker,
                max_keys=min(self.cfg.obs_max_list_keys, 1000),
            )
            self._ensure_ok(resp, "列举对象")
            body = getattr(resp, "body", None)
            contents = getattr(body, "contents", None) or []
            for item in contents:
                key = item.get("key") if isinstance(item, dict) else getattr(item, "key", None)
                if key and not key.endswith("/"):
                    keys.append(key)
            is_truncated = getattr(body, "is_truncated", False)
            if isinstance(body, dict):
                is_truncated = body.get("is_truncated", False)
            if not is_truncated:
                break
            marker = getattr(body, "next_marker", None)
            if isinstance(body, dict):
                marker = body.get("next_marker")
            if not marker or len(keys) >= self.cfg.max_list_items:
                break
        return keys

    async def ready(self) -> Tuple[bool, str]:
        try:
            resp = await asyncio.to_thread(
                self._client.listObjects,
                self.cfg.obs_bucket,
                prefix=f"{self.cfg.obs_prefix}/",
                max_keys=1,
            )
        except Exception as exc:  # noqa: BLE001 - 探针吞掉所有异常
            return False, f"OBS 不可访问: {exc}"
        if 0 < self._status(resp) < 300:
            return True, "ok"
        return False, f"OBS 返回 HTTP {self._status(resp)}"

    async def close(self) -> None:
        if self._owns_client and hasattr(self._client, "close"):
            await asyncio.to_thread(self._client.close)
