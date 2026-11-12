"""Casdoor Access Token 校验（本服务独立实现，不依赖 backend/）。

Casdoor 实例配置了 ``authPrefix=auth``，因此所有接口都在 ``/auth`` 之下：

===========================================  ==========================================
接口                                          用途
===========================================  ==========================================
``{base}/api/userinfo``                       用 Bearer Token 换取用户对象（默认）
``{base}/api/login/oauth/introspect``        RFC7662 服务端校验（需 clientSecret）
``{base}/.well-known/jwks``                  JWT 本地验签公钥（需缓存）
===========================================  ==========================================

**身份三层标识必须区分**（与设备管理后端保持同一口径）：

======================  ==========================  ==============================
属性                     来源 claim                  用途
======================  ==========================  ==============================
``user_id``             ``sub`` / ``id``             技术 UUID，仅审计
``username``            ``preferred_username``       工号 → **存储目录名**
``display_name``        ``name`` / ``displayName``   姓名，仅展示，禁止参与身份匹配
======================  ==========================  ==============================

字段陷阱：``/api/userinfo`` 返回 OIDC claims（``name`` 是姓名），而
``/api/get-user`` 返回的 Casdoor 用户对象里 ``name`` 是工号、``displayName``
才是姓名。本模块只处理 userinfo / introspect / JWT，按来源分别解析。

**错误分类**：Token 缺失或无效 → 401；Casdoor 不可达 → 502。
绝不把「认证服务挂了」当成「Token 无效」，否则客户端会误触发重新登录。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import jwk as jose_jwk
from jose import jwt as jose_jwt

from app.config import Settings
from app.config import settings as global_settings
from app.errors import AuthenticationError, CasdoorError, PermissionDeniedError

logger = logging.getLogger(__name__)

bearer_scheme = HTTPBearer(auto_error=False, description="Casdoor Access Token")


@dataclass(frozen=True)
class Identity:
    """已认证身份。所有字段都来自服务端校验结果，不采信客户端参数。"""

    user_id: str
    username: str
    display_name: str
    tenant_id: str
    email: Optional[str] = None
    auth_method: str = "unknown"
    #: "claim" 表示 Token 里带了组织；"config_fallback" 表示回退到配置值。
    tenant_source: str = "config_fallback"

    @property
    def label(self) -> str:
        return self.display_name or self.username

    def public(self) -> Dict[str, Any]:
        """可安全返回给客户端的身份字段（绝不包含令牌）。"""
        return {
            "user_id": self.user_id,
            "username": self.username,
            "display_name": self.display_name,
            "tenant_id": self.tenant_id,
            "email": self.email,
            "auth_method": self.auth_method,
            "tenant_source": self.tenant_source,
        }


def _first(obj: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = obj.get(key)
        if value not in (None, ""):
            return value
    return default


class CasdoorVerifier:
    """Casdoor REST / OIDC 客户端，负责把 Token 解析为 :class:`Identity`。"""

    def __init__(self, cfg: Settings, http_client: Optional[httpx.AsyncClient] = None) -> None:
        self.cfg = cfg
        self._http = http_client
        self._owns_http = http_client is None
        self._jwks: Dict[str, Any] = {}
        self._jwks_fetched_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        return self.cfg.casdoor_base_url.rstrip("/")

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=self.cfg.casdoor_timeout,
                verify=self.cfg.casdoor_verify_tls,
                follow_redirects=True,
            )
            self._owns_http = True
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and self._owns_http:
            await self._http.aclose()
            self._http = None

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    # ------------------------------------------------------------------
    # 校验入口
    # ------------------------------------------------------------------
    async def verify(self, token: str) -> Identity:
        if not token or not token.strip():
            raise AuthenticationError("缺少访问令牌")

        method = self.cfg.casdoor_verify_method
        if method == "jwt":
            return await self._verify_by_jwt(token)
        if method == "introspect":
            return await self._verify_by_introspect(token)
        if method == "userinfo":
            return await self._verify_by_userinfo(token)

        # auto：优先 userinfo（能拿到完整用户对象，且不需要 clientSecret），
        # 失败且配置了客户端凭据时再退回 introspect。与设备管理后端一致。
        try:
            return await self._verify_by_userinfo(token)
        except AuthenticationError as exc:
            if not self.cfg.casdoor_has_client_credentials:
                raise
            logger.info("userinfo 校验失败（%s），回退到 introspect 模式", exc.message)
        return await self._verify_by_introspect(token)

    async def _verify_by_userinfo(self, token: str) -> Identity:
        payload = await self._get_json(
            "/api/userinfo", headers={"Authorization": f"Bearer {token}"}
        )
        return self._identity_from_payload(payload, "userinfo")

    async def _verify_by_introspect(self, token: str) -> Identity:
        if not self.cfg.casdoor_has_client_credentials:
            raise AuthenticationError(
                "introspect 模式需要配置 CASDOOR_CLIENT_ID / CASDOOR_CLIENT_SECRET"
            )
        response = await self._post_form(
            "/api/login/oauth/introspect",
            form={"token": token, "token_type_hint": "access_token"},
            basic_auth=True,
        )
        if not response.get("active"):
            raise AuthenticationError("访问令牌无效或已过期")
        try:
            return self._identity_from_payload(response, "introspect")
        except AuthenticationError:
            # 某些 Casdoor 版本的 introspect 只返回 sub / exp，用 userinfo 补全
            return await self._verify_by_userinfo(token)

    async def _verify_by_jwt(self, token: str) -> Identity:
        try:
            header = jose_jwt.get_unverified_header(token)
        except Exception as exc:  # noqa: BLE001
            raise AuthenticationError("令牌格式非法") from exc

        await self._get_jwks()
        key_data = self._find_jwk(header.get("kid"))
        if key_data is None:
            raise AuthenticationError("未找到匹配的签名公钥")

        issuer = self.cfg.casdoor_issuer or self.base_url
        try:
            claims = jose_jwt.decode(
                token,
                jose_jwk.construct(key_data),
                algorithms=[header.get("alg") or "RS256"],
                issuer=self._issuer_variants(issuer),
                options={"verify_aud": False},
            )
        except Exception as exc:  # noqa: BLE001
            raise AuthenticationError(f"令牌签名校验失败: {exc}") from exc
        return self._identity_from_payload(claims, "jwt")

    @staticmethod
    def _issuer_variants(issuer: str) -> List[str]:
        """Casdoor 内部 origin 常为 http，外部可用 https，两者都接受。"""
        variants = {issuer}
        if issuer.startswith("https://"):
            variants.add("http://" + issuer[len("https://") :])
        elif issuer.startswith("http://"):
            variants.add("https://" + issuer[len("http://") :])
        return sorted(variants)

    # ------------------------------------------------------------------
    # JWKS
    # ------------------------------------------------------------------
    async def _get_jwks(self) -> Dict[str, Any]:
        now = time.time()
        if self._jwks and now - self._jwks_fetched_at < self.cfg.casdoor_jwks_cache_ttl:
            return self._jwks
        async with self._lock:
            if self._jwks and time.time() - self._jwks_fetched_at < self.cfg.casdoor_jwks_cache_ttl:
                return self._jwks
            data = await self._get_json("/.well-known/jwks")
            if not isinstance(data, dict):
                raise CasdoorError("JWKS 响应格式异常")
            self._jwks = data
            self._jwks_fetched_at = time.time()
            return data

    def _find_jwk(self, kid: Optional[str]) -> Optional[Dict[str, Any]]:
        keys = self._jwks.get("keys", [])
        for key in keys:
            if kid is None or key.get("kid") == kid:
                return key
        # 证书轮换期间 kid 可能短暂不一致，只有一把公钥时回退使用
        return keys[0] if len(keys) == 1 else None

    # ------------------------------------------------------------------
    # 身份构造
    # ------------------------------------------------------------------
    def _identity_from_payload(self, payload: Any, method: str) -> Identity:
        if not isinstance(payload, dict):
            raise AuthenticationError("用户信息格式异常")
        # Casdoor 对无效 Token 会返回 HTTP 200 + {"status":"error","msg":...}
        if payload.get("status") == "error":
            raise AuthenticationError(str(payload.get("msg") or "身份认证失败"))
        if payload.get("data") is None and "msg" in payload:
            raise AuthenticationError(str(payload.get("msg") or "身份认证失败"))

        obj = payload
        if isinstance(payload.get("data"), dict) and payload["data"]:
            obj = payload["data"]
        if obj.get("isForbidden") is True or obj.get("isDeleted") is True:
            raise AuthenticationError("该账号已被禁用或已删除")

        oidc_claims = "preferred_username" in obj or ("sub" in obj and "id" not in obj)
        user_id = _first(obj, "id", "sub", "user_id", "uid")
        if oidc_claims:
            username = _first(obj, "preferred_username", "username", "login", "name")
            display_name = _first(obj, "name", "displayName", "display_name", default=username)
        else:
            # Casdoor 用户对象：name=工号，displayName=姓名
            username = _first(obj, "name", "preferred_username", "username", "login")
            display_name = _first(obj, "displayName", "display_name", "name", default=username)
        if not user_id and not username:
            raise AuthenticationError("访问令牌缺少用户标识")

        properties = obj.get("properties") or {}
        if not isinstance(properties, dict):
            properties = {}
        raw_tenant = _first(
            obj,
            "owner",
            "organization",
            "default",
            default=_first(properties, "tenant_id", "tenantId", "organization"),
        )
        tenant_source = "claim" if raw_tenant else "config_fallback"
        return Identity(
            user_id=str(user_id or username),
            username=str(username or user_id),
            display_name=str(display_name or username or user_id),
            tenant_id=str(raw_tenant or self.cfg.casdoor_organization),
            email=_first(obj, "email"),
            auth_method=method,
            tenant_source=tenant_source,
        )

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------
    async def _get_json(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        try:
            response = await self._client().get(self._url(path), params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise CasdoorError(f"访问 Casdoor 失败: {exc}") from exc
        if response.status_code in (401, 403):
            raise AuthenticationError("访问令牌无效或已过期")
        if response.status_code >= 500:
            raise CasdoorError(f"Casdoor 服务异常: HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise CasdoorError("Casdoor 返回非 JSON 响应") from exc

    async def _post_form(
        self, path: str, form: Dict[str, Any], *, basic_auth: bool = False
    ) -> Dict[str, Any]:
        headers: Dict[str, str] = {}
        auth = None
        if basic_auth:
            if not self.cfg.casdoor_has_client_credentials:
                raise AuthenticationError("缺少 Casdoor 客户端凭据")
            auth = (self.cfg.casdoor_client_id, self.cfg.casdoor_client_secret)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            response = await self._client().post(
                self._url(path), data=form, headers=headers, auth=auth
            )
        except httpx.HTTPError as exc:
            raise CasdoorError(f"访问 Casdoor 失败: {exc}") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code in (401, 403):
            raise AuthenticationError(str(payload.get("error_description") or "Casdoor 拒绝了本次请求"))
        if response.status_code >= 400:
            raise CasdoorError(str(payload.get("error_description") or f"HTTP {response.status_code}"))
        return payload if isinstance(payload, dict) else {}


# ---------------------------------------------------------------------------
# FastAPI 依赖
# ---------------------------------------------------------------------------
def get_verifier(request: Request) -> CasdoorVerifier:
    """取应用状态里的校验器（由 lifespan 创建）。

    测试或 ASGI 直连未触发 lifespan 时惰性创建，保证路由始终可用。
    """
    verifier = getattr(request.app.state, "verifier", None)
    if verifier is None:
        verifier = CasdoorVerifier(global_settings)
        request.app.state.verifier = verifier
    return verifier


async def get_current_identity(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    verifier: CasdoorVerifier = Depends(get_verifier),
) -> Identity:
    """校验 Casdoor Access Token，返回身份；用户名由服务端推导，客户端无法伪造。"""
    token = credentials.credentials.strip() if credentials and credentials.credentials else ""
    if not token and global_settings.allow_query_token:
        token = (request.query_params.get("access_token") or "").strip()
    if not token:
        raise AuthenticationError(
            "缺少访问令牌，请先通过 Casdoor 登录并用 Authorization: Bearer 传递"
        )

    identity = await verifier.verify(token)

    if identity.tenant_source != "claim":
        logger.warning(
            "Token 未携带组织信息，租户范围回退到配置值 %s（用户=%s）。"
            "如需强校验请改用 JWT 模式并确认 claims 含 owner。",
            global_settings.casdoor_organization,
            identity.username,
        )
    if (
        global_settings.casdoor_require_organization
        and identity.tenant_id != global_settings.casdoor_organization
    ):
        raise PermissionDeniedError(
            f"用户所属组织 {identity.tenant_id} 不在允许范围内",
            details={
                "token_tenant": identity.tenant_id,
                "allowed_tenant": global_settings.casdoor_organization,
            },
        )
    return identity
