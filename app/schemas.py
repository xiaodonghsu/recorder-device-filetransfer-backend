"""对外响应的 Pydantic 模型。"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class FileInfo(BaseModel):
    """单个已保存文件的描述。"""

    name: str = Field(..., description="净化后落盘的文件名")
    relpath: str = Field(
        ..., description="相对用户根目录的 POSIX 路径，首段为设备 SN（设备SN/子目录/文件名），用于下载 / 删除"
    )
    device_sn: str = Field("", description="录音卡设备 SN，即用户目录下的设备分层目录名")
    owner: str = Field(..., description="所属用户名（Casdoor 工号 preferred_username）")
    size: int = Field(..., description="字节数")
    sha256: str = Field("", description="文件内容的 SHA-256（上传响应中返回）")
    content_type: str = Field("application/octet-stream", description="按扩展名推断的 MIME 类型")
    modified_at: str = Field(..., description="最后修改时间，UTC ISO-8601")
    download_url: str = Field(..., description="可直接用于下载的 URL（不含域名）")
    tenant_id: str = Field("", description="所属租户（Casdoor 组织）")
    backend: str = Field("local", description="存储后端：local / obs")
    object_key: Optional[str] = Field(None, description="OBS 完整对象键；本地后端为空")
    bucket: Optional[str] = Field(None, description="OBS 桶名；本地后端为空")
    record_id: Optional[int] = Field(None, description="元数据数据库记录 ID")
    uploaded_at: Optional[str] = Field(None, description="上传入库时间，UTC ISO-8601")


class UploadResponse(BaseModel):
    code: str = "ok"
    message: str = "上传成功"
    file: FileInfo


class FileListResponse(BaseModel):
    code: str = "ok"
    total: int
    backend: str = "local"
    device_sn: Optional[str] = Field(
        None, description="本次列表的设备 SN 筛选条件；为空表示列出该用户名下所有设备"
    )
    items: List[FileInfo]


class MessageResponse(BaseModel):
    code: str = "ok"
    message: str
    relpath: Optional[str] = None


class MeResponse(BaseModel):
    """当前令牌解析出的身份，便于客户端 / 运维排障。"""

    code: str = "ok"
    user_id: str = Field(..., description="Casdoor 技术标识（UUID）")
    username: str = Field(..., description="工号，同时是存储目录名")
    display_name: str = Field("", description="姓名，仅展示，不参与身份匹配")
    tenant_id: str
    email: Optional[str] = None
    auth_method: str = Field(..., description="userinfo / introspect / jwt")
    tenant_source: str = Field(
        "config_fallback",
        description="claim=Token 自带组织；config_fallback=Token 无组织，回退配置值",
    )
