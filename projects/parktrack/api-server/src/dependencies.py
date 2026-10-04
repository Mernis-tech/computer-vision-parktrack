"""
Зависимости FastAPI:
  - get_current_user   — принимает API_TOKEN или JWT через Authorization: Bearer
  - require            — фабрика зависимостей для проверки permissions
  - BASE_USER_PERMISSIONS — хардкод прав для роли 'user'
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from .database import get_db
from .db_models import GlobalRole, PartnerMembership, User, UserPermission

# ---------------------------------------------------------------------------
# Конфигурация JWT
# ---------------------------------------------------------------------------

JWT_SECRET:    str = os.environ.get("JWT_SECRET", "CHANGE_ME_IN_PRODUCTION")
JWT_ALGORITHM: str = "HS256"
JWT_EXPIRE_SECONDS: int = int(os.environ.get("JWT_EXPIRE_SECONDS", 86400))  # 24 ч
API_TOKEN_USER_EMAIL = "api-token@parktrack.local"
_API_TOKEN_AUTH_ATTR = "_authenticated_via_api_token"

# ---------------------------------------------------------------------------
# Хэширование паролей
# ---------------------------------------------------------------------------
#
# Связка passlib 1.7.x + bcrypt 5.x нестабильна на части окружений
# (в том числе локально на Windows/Python 3.13). Для backend MVP используем
# встроенный в passlib PBKDF2 backend, которому не нужен внешний bcrypt-модуль.

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------

def create_access_token(user_id: int) -> str:
    expire = datetime.now(timezone.utc) + timedelta(seconds=JWT_EXPIRE_SECONDS)
    payload = {"sub": str(user_id), "exp": expire}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> int:
    """Декодирует токен и возвращает user_id. Бросает HTTPException при ошибке."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return int(payload["sub"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_description": "Token has expired"},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_description": "Missing or invalid access token"},
        )


# ---------------------------------------------------------------------------
# Базовые права роли 'user' (хардкод согласно ТЗ раздел 1.2)
# ---------------------------------------------------------------------------

BASE_USER_PERMISSIONS: frozenset[str] = frozenset({
    "users.me.view",
    "users.me.update",
    "users.password.update",
    "map.view",
    "zones.view",
    "occupancy.view",
    "forecasts.view",
    "weather.view",
    "routing.create",
    "feedback.create",
    "sources.view",
    "occupancy.view",
    "forecasts.view",
    "routing.create",
    "routing.view",
    "routing.delete"
})

BASE_ADMIN_PERMISSIONS: frozenset[str] = frozenset({
    # Администратор дополнительно получает все admin.* права
    "admin.users.view",
    "admin.users.manage",
    "admin.partners.view",
    "admin.partners.manage",
    "admin.system.view",
    "admin.system.manage",
    "admin.monitoring.view",
    "admin.analytics.view",
    "admin.forecasts.view",
    "analytics.view",
    "analytics.feedback.create",
    "analytics.feedback.view",
    "cameras.view",
    "cameras.create",
    "cameras.update",
    "cameras.delete",
    "zones.create",
    "zones.update",
    "zones.delete",
    "partner_members.view",
    "partner_members.invite",
    "partner_members.update",
    "partner_members.disable",
    # Плюс все права обычного пользователя
    *BASE_USER_PERMISSIONS,
})

PARTNER_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "partner_owner": frozenset({
        "sources.view",
        "cameras.view",
        "cameras.create",
        "cameras.update",
        "cameras.delete",
        "zones.view",
        "zones.create",
        "zones.update",
        "zones.delete",
        "partner_members.view",
        "partner_members.invite",
        "partner_members.update",
        "partner_members.disable",
        "partner_access.manage",
        "analytics.view",
        "analytics.feedback.create",
    }),
    "partner_admin": frozenset({
        "sources.view",
        "cameras.view",
        "cameras.create",
        "cameras.update",
        "cameras.delete",
        "zones.view",
        "zones.create",
        "zones.update",
        "zones.delete",
        "partner_members.view",
        "partner_members.invite",
        "partner_members.update",
        "partner_members.disable",
        "partner_access.manage",
        "analytics.view",
        "analytics.feedback.create",
    }),
    "partner_manager": frozenset({
        "sources.view",
        "cameras.view",
        "cameras.create",
        "cameras.update",
        "zones.view",
        "zones.create",
        "zones.update",
        "partner_members.view",
        "analytics.view",
        "analytics.feedback.create",
    }),
    "partner_analyst": frozenset({
        "sources.view",
        "cameras.view",
        "zones.view",
        "analytics.view",
        "analytics.feedback.create",
    }),
    "partner_viewer": frozenset({
        "sources.view",
        "cameras.view",
        "zones.view",
        "analytics.view",
    }),
}

API_TOKEN_PERMISSIONS: frozenset[str] = frozenset({
    *BASE_ADMIN_PERMISSIONS,
    *(permission for permissions in PARTNER_ROLE_PERMISSIONS.values() for permission in permissions),
    "forecasts.write",
    "forecasts.delete",
    "occupancy.write",
    "occupancy.delete",
    "weather.write",
})


def _configured_api_token() -> str | None:
    token = os.getenv("API_TOKEN")
    return token if token else None


def _api_token_matches(candidate: str | None) -> bool:
    token = _configured_api_token()
    return bool(token and candidate and secrets.compare_digest(candidate, token))


def _mark_api_token_authenticated(user: User) -> User:
    setattr(user, _API_TOKEN_AUTH_ATTR, True)
    return user


def is_api_token_authenticated(user: User) -> bool:
    return getattr(user, _API_TOKEN_AUTH_ATTR, False) is True


def _get_api_token_user(db: Session) -> User:
    user = db.query(User).filter(User.email == API_TOKEN_USER_EMAIL).one_or_none()
    if user is None:
        user = User(
            email=API_TOKEN_USER_EMAIL,
            hashed_password=hash_password(secrets.token_urlsafe(48)),
            full_name="API Token",
            global_role=GlobalRole.admin,
            is_active=False,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    elif user.is_active or user.global_role != GlobalRole.admin:
        user.is_active = False
        user.global_role = GlobalRole.admin
        db.commit()
        db.refresh(user)
    return _mark_api_token_authenticated(user)


def get_membership_permissions(membership: PartnerMembership) -> set[str]:
    return set(PARTNER_ROLE_PERMISSIONS.get(membership.user_role, frozenset()))


def get_effective_permissions(user: User) -> set[str]:
    """
    Возвращает эффективный набор прав пользователя:
    базовые (по роли) + дополнительные из user_permissions + права партнёрских ролей.
    """
    if is_api_token_authenticated(user):
        return set(API_TOKEN_PERMISSIONS)

    base = BASE_ADMIN_PERMISSIONS if user.global_role == GlobalRole.admin else BASE_USER_PERMISSIONS
    extra = {p.permission for p in user.permissions}
    partner_permissions: set[str] = set()
    for membership in user.memberships:
        partner_is_active = getattr(membership.partner, "is_active", True)
        if partner_is_active:
            partner_permissions |= get_membership_permissions(membership)
    return base | extra | partner_permissions


# ---------------------------------------------------------------------------
# FastAPI-зависимости
# ---------------------------------------------------------------------------

_bearer_scheme = HTTPBearer(auto_error=False)


def resolve_current_user(
    credentials: HTTPAuthorizationCredentials | None,
    db: Session,
) -> User:
    """Обязательная авторизация по Bearer API_TOKEN или Bearer JWT."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_description": "Missing or invalid access token"},
        )

    token = credentials.credentials
    if _api_token_matches(token):
        return _get_api_token_user(db)

    user_id = decode_access_token(token)
    user = db.query(User).filter(User.user_id == user_id).one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_description": "User not found or inactive"},
        )
    return user


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    """Обязательная авторизация. Бросает 401, если токен отсутствует или невалиден."""
    return resolve_current_user(credentials=credentials, db=db)


CurrentUser = Annotated[User, Depends(get_current_user)]


def require(*permissions: str):
    """
    Фабрика зависимостей. Использование:

        @router.get("/something")
        def handler(user: Annotated[User, Depends(require("cameras.view"))]):
            ...

    Проверяет, что у текущего пользователя есть ВСЕ перечисленные права.
    """
    def _dependency(
        current_user: CurrentUser,
    ) -> User:
        if is_api_token_authenticated(current_user):
            return current_user

        effective = get_effective_permissions(current_user)
        missing = [p for p in permissions if p not in effective]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error_description": f"Missing permissions: {', '.join(missing)}"},
            )
        return current_user

    return Depends(_dependency)
