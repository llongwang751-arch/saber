"""Password and JWT authentication with constant-shape login failures."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from .store import ApplicationStore, ConflictError, NotFoundError

logger = logging.getLogger(__name__)


USERNAME_MIN = 3
USERNAME_MAX = 32
PASSWORD_MIN = 8
PASSWORD_MAX = 64
DEFAULT_TTL_HOURS = 7 * 24
DEVELOPMENT_SECRET = "agi-saber-development-secret-change-me-32bytes"
# 公开可预测的占位密钥；任何暴露在网络上的部署都不允许使用它们签名令牌。
KNOWN_DEVELOPMENT_SECRETS = frozenset(
    {DEVELOPMENT_SECRET}
    | {
        "local-development-jwt-secret-change-before-production",
        "replace-with-at-least-32-random-characters",
    }
)
_DUMMY_HASH = bcrypt.hashpw(b"placeholder-not-a-real-account", bcrypt.gensalt(rounds=12))


class AuthenticationError(RuntimeError):
    """Stable authentication failure carrying the public HTTP error code.

    Login deliberately maps this exception to ``invalid_credentials`` at the
    delivery boundary.  Token verification keeps the finer-grained Go wire
    contract (``invalid_token`` versus ``token_expired``).
    """

    def __init__(self, message: str, *, code: str = "invalid_token"):
        super().__init__(message)
        self.code = code


class ValidationError(RuntimeError):
    pass


class AuthService:
    def __init__(
        self, store: ApplicationStore, *, secret: str | None = None,
        ttl_hours: int | None = None, issuer: str | None = None,
    ):
        self.store = store
        configured_secret = secret or os.getenv("AGI_JWT_SECRET", "")
        self.secret = configured_secret
        if len(self.secret.encode("utf-8")) < 32 or self.secret in KNOWN_DEVELOPMENT_SECRETS:
            # 回落只服务于零配置的本地开发；生产入口 main.build_deps() 会在
            # 认证开启时直接拒绝弱密钥/已知开发密钥（fail-closed）。
            if self.secret in KNOWN_DEVELOPMENT_SECRETS:
                logger.warning(
                    "检测到已公开的开发 JWT 密钥，正在使用占位开发密钥签名；"
                    "任何联网部署都必须通过 AGI_JWT_SECRET 提供强随机密钥"
                )
            self.secret = DEVELOPMENT_SECRET
            self.using_development_secret = True
        else:
            self.using_development_secret = False
        self.ttl_hours = int(ttl_hours or os.getenv("AGI_JWT_TTL_HOURS", DEFAULT_TTL_HOURS))
        self.issuer = issuer or os.getenv("AGI_JWT_ISSUER", "agi-assistant")
        self.default_tenant_id = (
            os.getenv("AGI_DEFAULT_TENANT_ID", "default").strip() or "default"
        )
        self._admin_usernames = _username_set(
            os.getenv("AGI_EXPERIMENT_ADMIN_USERNAMES", "")
        )
        self._approver_usernames = _username_set(
            os.getenv("AGI_EXPERIMENT_APPROVER_USERNAMES", "")
        )
        self.traffic_provenance = os.getenv(
            "AGI_ONLINE_TRAFFIC_PROVENANCE", "disabled"
        ).strip()
        # A self-service identity can cheaply create Sybil "unique users" and
        # is therefore incompatible with production-authenticated experiment
        # evidence.  Production accounts must be provisioned by the operator.
        self.public_registration_enabled = (
            self.traffic_provenance != "production_authenticated"
        )

    @staticmethod
    def validate_credentials(username: str, password: str) -> tuple[str, str]:
        username = (username or "").strip()
        if len(username) < USERNAME_MIN:
            raise ValidationError("用户名长度需不少于 3 个字符")
        if len(username) > USERNAME_MAX:
            raise ValidationError("用户名长度不能超过 32 个字符")
        password_bytes = (password or "").encode("utf-8")
        if len(password_bytes) < PASSWORD_MIN:
            raise ValidationError("密码长度需不少于 8 个字节")
        if len(password_bytes) > PASSWORD_MAX:
            raise ValidationError("密码长度不能超过 64 个字节")
        return username, password

    def register(self, username: str, password: str) -> dict[str, Any]:
        username, password = self.validate_credentials(username, password)
        if not self.public_registration_enabled:
            raise ValidationError(
                "生产实验模式禁止公开注册，请联系管理员预置业务账户"
            )
        if self.is_privileged_username(username):
            # Role allowlists are operator configuration, not an invitation to
            # claim that username through the public registration endpoint.
            # Privileged principals must be provisioned out of band.
            raise ValidationError("该用户名保留给预置的实验管理账户，不能公开注册")
        password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")
        try:
            user = self.store.create_user(
                username,
                password_hash,
                tenant_id=self.default_tenant_id,
                roles=self.configured_roles(username),
                identity_provenance="self_service",
                experiment_eligible=False,
            )
        except ConflictError:
            raise
        return self._issue(user)

    def login(self, username: str, password: str) -> dict[str, Any]:
        username, password = self.validate_credentials(username, password)
        try:
            user = self.store.find_user_by_username(username, include_secret=True)
            encoded_hash = user["password_hash"].encode("ascii")
        except NotFoundError:
            bcrypt.checkpw(password.encode("utf-8"), _DUMMY_HASH)
            raise AuthenticationError("用户名或密码错误")
        if not bcrypt.checkpw(password.encode("utf-8"), encoded_hash):
            raise AuthenticationError("用户名或密码错误")
        self.store.touch_last_login(user["id"])
        user.pop("password_hash", None)
        user = self._sync_roles(user)
        return self._issue(user)

    def verify(self, token: str) -> dict[str, Any]:
        try:
            claims = jwt.decode(
                token,
                self.secret,
                algorithms=["HS256"],
                issuer=self.issuer,
                options={"require": ["sub", "exp", "iat", "iss"]},
            )
            user = self.store.find_user(str(claims["sub"]))
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationError(
                "访问令牌已过期", code="token_expired"
            ) from exc
        except (jwt.InvalidTokenError, NotFoundError, KeyError) as exc:
            raise AuthenticationError("无效的访问令牌", code="invalid_token") from exc
        return self._sync_roles(user)

    def configured_roles(self, username: str) -> list[str]:
        normalized = str(username or "").strip().casefold()
        roles = {"participant"}
        if normalized in self._admin_usernames:
            roles.add("experiment_admin")
        if normalized in self._approver_usernames:
            roles.add("experiment_approver")
        return sorted(roles)

    def is_privileged_username(self, username: str) -> bool:
        normalized = str(username or "").strip().casefold()
        return normalized in self._admin_usernames or normalized in self._approver_usernames

    def _sync_roles(self, user: dict[str, Any]) -> dict[str, Any]:
        configured = self.configured_roles(str(user.get("username") or ""))
        if sorted(user.get("roles") or []) != configured:
            return self.store.set_user_identity(user["id"], roles=configured)
        return user

    def _issue(self, user: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=self.ttl_hours)
        token = jwt.encode(
            {
                "sub": user["id"],
                "username": user["username"],
                "tenant_id": user["tenant_id"],
                "roles": list(user.get("roles") or ["participant"]),
                "iat": now,
                "exp": expires_at,
                "iss": self.issuer,
            },
            self.secret,
            algorithm="HS256",
        )
        return {
            "token": token,
            "access_token": token,
            "token_type": "bearer",
            "expires_at": expires_at,
            "user_id": user["id"],
            "username": user["username"],
            "tenant_id": user["tenant_id"],
            "roles": list(user.get("roles") or ["participant"]),
        }


def _username_set(value: str) -> set[str]:
    return {
        item.strip().casefold()
        for item in str(value or "").split(",")
        if item.strip()
    }
