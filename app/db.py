"""上传文件元数据数据库（SQLite / MySQL）。

两种存储后端（本地磁盘、华为云 OBS）都在这里登记同一份索引：
文件名、租户、工号、**设备 SN**、相对路径、OBS 对象键、大小、SHA-256、MIME、状态与时间。
后续业务（ASR、会议纪要、知识库）只需查这张表即可定位文件，
不必再扫描磁盘或遍历 OBS 前缀。

- SQLite：``DATABASE_URL=sqlite:///./data/filetransfer.db``（相对 filetransfer/）
- MySQL ：``DATABASE_URL=mysql+pymysql://user:pwd@host:3306/db?charset=utf8mb4``

时间统一 naive UTC（与设备管理后端一致），可直接映射到 MySQL ``DATETIME``。
删除采用软删除（status=deleted / deleted_at），保留历史便于审计。

目录级别为 ``租户/工号/设备SN/``，``relpath`` 首段即设备 SN；本服务不提供
旧表结构与旧路径的兼容/迁移逻辑，升级时需重建库表与历史索引。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional, Tuple
from urllib.parse import quote

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    Integer,
    String,
    create_engine,
    select,
    text,
    update,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.config import Settings
from app.schemas import FileInfo
from app.storage import clean_device_sn, clean_path_segments


def utcnow() -> datetime:
    """naive UTC，与 MySQL DATETIME / 既有后端保持一致。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso_utc(value: Optional[datetime]) -> str:
    if value is None:
        return ""
    return value.replace(tzinfo=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sn_from_relpath(relpath: str) -> str:
    """``relpath`` 首段即设备 SN（写入前已由 ``clean_device_sn`` 净化）。"""
    return (relpath or "").split("/", 1)[0]


def inner_relpath(relpath: str) -> str:
    """去掉设备 SN 首段，取设备目录内的相对路径（用于 subdir 前缀过滤）。"""
    parts = (relpath or "").split("/", 1)
    return parts[1] if len(parts) == 2 else ""


class Base(DeclarativeBase):
    pass


class FileRecord(Base):
    """一次成功上传对应一条记录。"""

    __tablename__ = "ft_file_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    device_sn: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    relpath: Mapped[str] = mapped_column(String(512), nullable=False)
    object_key: Mapped[Optional[str]] = mapped_column(String(768), nullable=True)
    backend: Mapped[str] = mapped_column(String(16), nullable=False, default="local")
    bucket: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    content_type: Mapped[str] = mapped_column(String(128), nullable=False, default="application/octet-stream")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_ft_file_owner", "tenant_id", "username", "device_sn", "backend", "status"),
        Index("ix_ft_file_created", "created_at"),
    )


class FileRepository:
    """同步 SQLAlchemy 仓储。方法都很短，直接在请求协程里调用即可。"""

    def __init__(self, cfg: Settings, url: Optional[str] = None) -> None:
        self.cfg = cfg
        self.url = url or cfg.resolved_database_url()
        kwargs = {"echo": cfg.db_echo, "pool_pre_ping": True}
        if self.url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        else:
            kwargs["pool_recycle"] = 3600
        self.engine = create_engine(self.url, **kwargs)
        Base.metadata.create_all(self.engine)  # 幂等；生产可改用 Alembic 迁移
        self._Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def close(self) -> None:
        self.engine.dispose()

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def record_upload(
        self,
        identity,
        info: FileInfo,
        *,
        object_key: Optional[str] = None,
        backend: str = "local",
        bucket: Optional[str] = None,
    ) -> FileRecord:
        """登记一次上传；覆盖同一设备下的同名文件时，先把旧记录标记为 deleted。"""
        now = utcnow()
        device_sn = info.device_sn or sn_from_relpath(info.relpath)
        with self._Session() as session:
            session.execute(
                update(FileRecord)
                .where(
                    FileRecord.tenant_id == identity.tenant_id,
                    FileRecord.username == identity.username,
                    FileRecord.device_sn == device_sn,
                    FileRecord.backend == backend,
                    FileRecord.relpath == info.relpath,
                    FileRecord.status == "active",
                )
                .values(status="deleted", deleted_at=now)
            )
            record = FileRecord(
                tenant_id=identity.tenant_id,
                username=identity.username,
                device_sn=device_sn,
                filename=info.name,
                relpath=info.relpath,
                object_key=object_key,
                backend=backend,
                bucket=bucket,
                size_bytes=info.size,
                sha256=info.sha256 or "",
                content_type=info.content_type,
                status="active",
                created_at=now,
            )
            session.add(record)
            session.commit()
            session.refresh(record)
            return record

    def mark_deleted(self, identity, relpath: str, backend: str) -> int:
        """软删除一条记录，返回受影响行数。"""
        with self._Session() as session:
            result = session.execute(
                update(FileRecord)
                .where(
                    FileRecord.tenant_id == identity.tenant_id,
                    FileRecord.username == identity.username,
                    FileRecord.device_sn == sn_from_relpath(relpath),
                    FileRecord.backend == backend,
                    FileRecord.relpath == relpath,
                    FileRecord.status == "active",
                )
                .values(status="deleted", deleted_at=utcnow())
            )
            session.commit()
            return result.rowcount or 0

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def list_active(
        self, identity, backend: str, subdir: str = "", device_sn: str = ""
    ) -> List[FileRecord]:
        """列当前用户在该后端下仍有效的文件。

        ``device_sn`` 非空时只列该设备（同一工号多台设备互不串数据）；
        ``subdir`` 是**设备目录内**的子目录前缀，用内存过滤避免 LIKE 通配符歧义。
        """
        with self._Session() as session:
            stmt = (
                select(FileRecord)
                .where(
                    FileRecord.tenant_id == identity.tenant_id,
                    FileRecord.username == identity.username,
                    FileRecord.backend == backend,
                    FileRecord.status == "active",
                )
            )
            if device_sn:
                stmt = stmt.where(FileRecord.device_sn == clean_device_sn(device_sn))
            stmt = stmt.order_by(FileRecord.created_at.desc(), FileRecord.id.desc()).limit(
                max(self.cfg.max_list_items * 2, 2000)
            )
            rows = list(session.execute(stmt).scalars())

        prefix = "/".join(clean_path_segments(subdir, max_depth=self.cfg.max_subdir_depth))
        if prefix:
            rows = [
                r for r in rows
                if inner_relpath(r.relpath) == prefix
                or inner_relpath(r.relpath).startswith(prefix + "/")
            ]
        return rows[: self.cfg.max_list_items]

    def get_active(self, identity, relpath: str, backend: str) -> Optional[FileRecord]:
        with self._Session() as session:
            stmt = select(FileRecord).where(
                FileRecord.tenant_id == identity.tenant_id,
                FileRecord.username == identity.username,
                FileRecord.device_sn == sn_from_relpath(relpath),
                FileRecord.backend == backend,
                FileRecord.relpath == relpath,
                FileRecord.status == "active",
            )
            return session.execute(stmt).scalars().first()

    def to_file_info(self, record: FileRecord) -> FileInfo:
        stamp = iso_utc(record.created_at)
        return FileInfo(
            name=record.filename,
            relpath=record.relpath,
            device_sn=record.device_sn,
            owner=record.username,
            size=record.size_bytes,
            sha256=record.sha256 or "",
            content_type=record.content_type or "application/octet-stream",
            modified_at=stamp,
            download_url=f"{self.cfg.api_prefix}/files/{quote(record.relpath, safe='/')}",
            tenant_id=record.tenant_id,
            backend=record.backend,
            object_key=record.object_key,
            bucket=record.bucket,
            record_id=record.id,
            uploaded_at=stamp,
        )

    # ------------------------------------------------------------------
    # 运维
    # ------------------------------------------------------------------
    def ready(self) -> Tuple[bool, str]:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True, "ok"
        except Exception as exc:  # noqa: BLE001 - 探针需要吞掉所有异常
            return False, f"数据库不可用: {exc}"
