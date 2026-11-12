"""对外响应的 Pydantic 模型。"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class FileInfo(BaseModel):
    """单个已保存文件的描述。"""

    name: str = Field(..., description="净化后落盘的文件名")
    relpath: str = Field(..., description="相对用户目录的 POSIX 路径，用于下载 / 删除")
    owner: str = Field(..., description="所属用户名（Casdoor 工号 preferred_username）")
    size: int = Field(..., description="字节数")
    sha256: str = Field("", description="文件内容的 SHA-256（上传响应中返回）")
    content_type: str = Field("application/octet-stream", description="按扩展名推断的 MIME 类型")
    modified_at: str = Field(..., description="最后修改时间，UTC ISO-8601")
    download_url: str = Field(..., description="可直接用于下载的 URL（不含域名）")


class UploadResponse(BaseModel):
    code: str = "ok"
    message: str = "上传成功"
    file: FileInfo


class FileListResponse(BaseModel):
    code: str = "ok"
    total: int
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
