"""统一异常体系。

与设备管理后端保持完全相同的响应结构 ``{code, message, status, details}``，
便于 APP / 管理后台复用同一套错误处理逻辑。
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class AppError(Exception):
    """业务异常基类。"""

    status_code: int = 400
    error_code: str = "bad_request"
    message: str = "请求无法处理"

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        error_code: Optional[str] = None,
        status_code: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.message = message or self.message
        self.error_code = error_code or self.error_code
        self.status_code = status_code or self.status_code
        self.details = details or {}
        super().__init__(self.message)

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "code": self.error_code,
            "message": self.message,
            "status": self.status_code,
        }
        if self.details:
            payload["details"] = self.details
        return payload


class ValidationError(AppError):
    status_code = 422
    error_code = "validation_error"
    message = "参数校验失败"


class AuthenticationError(AppError):
    status_code = 401
    error_code = "unauthorized"
    message = "身份认证失败"


class PermissionDeniedError(AppError):
    status_code = 403
    error_code = "forbidden"
    message = "无权访问该资源"


class NotFoundError(AppError):
    status_code = 404
    error_code = "not_found"
    message = "资源不存在"


class UnsupportedMediaTypeError(AppError):
    status_code = 415
    error_code = "unsupported_media_type"
    message = "不支持的文件类型"


class PayloadTooLargeError(AppError):
    status_code = 413
    error_code = "payload_too_large"
    message = "文件超过大小限制"


class StorageError(AppError):
    status_code = 500
    error_code = "storage_error"
    message = "文件存储异常"


class CasdoorError(AppError):
    """Casdoor 交互异常：不可达 / 响应异常，与 Token 无效严格区分。"""

    status_code = 502
    error_code = "casdoor_error"
    message = "身份认证服务异常"
