from __future__ import annotations

import hashlib
import os
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..db_models import GlobalRole, PasswordResetToken, User
from ..dependencies import (
    JWT_EXPIRE_SECONDS,
    CurrentUser,
    create_access_token,
    get_effective_permissions,
    get_membership_permissions,
    hash_password,
    require,
    verify_password,
)
from ..schemas.auth import (
    AuthUserInfo,
    LoginRequest,
    MeResponse,
    PartnerMembershipInfo,
    PasswordResetConfirmRequest,
    PasswordResetConfirmResponse,
    PasswordResetRequest,
    PasswordResetRequestResponse,
    RegisterRequest,
    TokenResponse,
)

router = APIRouter(prefix="/auth", tags=["Auth"])

PASSWORD_RESET_TTL_MINUTES = int(os.environ.get("PASSWORD_RESET_TTL_MINUTES", "30"))
PASSWORD_RESET_LOGIN_URL = os.environ.get("PASSWORD_RESET_LOGIN_URL", "http://localhost:5173/#/login")
SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
SMTP_FROM_EMAIL = os.environ.get("SMTP_FROM_EMAIL") or SMTP_USERNAME
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "ParkTrack")
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "1").lower() in {"1", "true", "yes", "on"}
SMTP_USE_SSL = os.environ.get("SMTP_USE_SSL", "0").lower() in {"1", "true", "yes", "on"}
_PASSWORD_RESET_RETURN_TOKEN = os.environ.get("PASSWORD_RESET_RETURN_TOKEN")


# ---------------------------------------------------------------------------
# Вспомогательная функция сборки ответа
# ---------------------------------------------------------------------------

def _build_token_response(user: User, db: Session) -> TokenResponse:
    effective = get_effective_permissions(user)
    memberships = [
        PartnerMembershipInfo(
            partner_id=m.partner_id,
            role=m.user_role,
            permissions=sorted(get_membership_permissions(m)),
            read_scope=m.read_scope,
            write_scope=m.write_scope,
            delete_scope=m.delete_scope,
            is_active=True,
        )
        for m in user.memberships
    ]
    token = create_access_token(user.user_id)
    return TokenResponse(
        access_token=token,
        token_type="Bearer",
        expires_in=JWT_EXPIRE_SECONDS,
        user=AuthUserInfo(
            user_id=user.user_id,
            email=user.email,
            full_name=user.full_name,
            global_role=user.global_role.value,
            permissions=sorted(effective),
            partner_memberships=memberships,
        ),
    )


def _hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _is_expired(dt: datetime) -> bool:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt <= datetime.now(timezone.utc)


def _smtp_enabled() -> bool:
    return bool(SMTP_HOST and SMTP_FROM_EMAIL)


def _should_return_reset_token() -> bool:
    if _PASSWORD_RESET_RETURN_TOKEN is None:
        return not _smtp_enabled()
    return _PASSWORD_RESET_RETURN_TOKEN.lower() in {"1", "true", "yes", "on"}


def _build_password_reset_link(token: str, email: str) -> str:
    separator = "&" if "?" in PASSWORD_RESET_LOGIN_URL else "?"
    return f"{PASSWORD_RESET_LOGIN_URL}{separator}{urlencode({'reset_token': token, 'email': email})}"


def _send_password_reset_email(email: str, token: str) -> bool:
    if not _smtp_enabled():
        return False

    reset_link = _build_password_reset_link(token, email)
    message = EmailMessage()
    message["Subject"] = "Сброс пароля ParkTrack"
    message["From"] = formataddr((SMTP_FROM_NAME, SMTP_FROM_EMAIL)) if SMTP_FROM_NAME else SMTP_FROM_EMAIL
    message["To"] = email
    message.set_content(
        "Вы запросили сброс пароля ParkTrack.\n\n"
        f"Ссылка для сброса: {reset_link}\n\n"
        f"Reset-token: {token}\n\n"
        f"Токен действует {PASSWORD_RESET_TTL_MINUTES} минут. "
        "Если вы не запрашивали сброс пароля, просто проигнорируйте это письмо.\n"
    )

    try:
        if SMTP_USE_SSL:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
                if SMTP_USERNAME:
                    smtp.login(SMTP_USERNAME, SMTP_PASSWORD or "")
                smtp.send_message(message)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
                if SMTP_USE_TLS:
                    smtp.starttls()
                if SMTP_USERNAME:
                    smtp.login(SMTP_USERNAME, SMTP_PASSWORD or "")
                smtp.send_message(message)
        return True
    except Exception as exc:
        print(f"Password reset email failed: {exc}")
        return False

# ---------------------------------------------------------------------------
# POST /auth/register
# ---------------------------------------------------------------------------

@router.post("/register", status_code=status.HTTP_201_CREATED, response_model=TokenResponse)
def register(body: RegisterRequest, db: Annotated[Session, Depends(get_db)]):
    existing = db.query(User).filter(User.email == body.email).one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error_description": "User with this email already exists"},
        )

    user = User(
        email=body.email,
        hashed_password=hash_password(body.password),
        full_name=body.full_name,
        phone=body.phone,
        global_role=GlobalRole.user,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    return _build_token_response(user, db)


# ---------------------------------------------------------------------------
# POST /auth/login
# ---------------------------------------------------------------------------

@router.post("/login", status_code=status.HTTP_200_OK, response_model=TokenResponse)
def login(body: LoginRequest, db: Annotated[Session, Depends(get_db)]):
    user = db.query(User).filter(User.email == body.login).one_or_none()

    if user is None or not verify_password(body.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_description": "Invalid login or password"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_description": "Account is disabled"},
        )

    return _build_token_response(user, db)


# ---------------------------------------------------------------------------
# POST /auth/password-reset/request
# ---------------------------------------------------------------------------

@router.post("/password-reset/request", status_code=status.HTTP_200_OK, response_model=PasswordResetRequestResponse)
def request_password_reset(body: PasswordResetRequest, db: Annotated[Session, Depends(get_db)]):
    user = db.query(User).filter(User.email == body.email).one_or_none()
    raw_token: str | None = None

    if user is not None and user.is_active:
        raw_token = secrets.token_urlsafe(32)
        token = PasswordResetToken(
            user_id=user.user_id,
            token_hash=_hash_reset_token(raw_token),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=PASSWORD_RESET_TTL_MINUTES),
        )
        db.add(token)
        db.commit()
        _send_password_reset_email(user.email, raw_token)

    return PasswordResetRequestResponse(
        ok=True,
        reset_token=raw_token if raw_token and _should_return_reset_token() else None,
    )


# ---------------------------------------------------------------------------
# POST /auth/password-reset/confirm
# ---------------------------------------------------------------------------

@router.post("/password-reset/confirm", status_code=status.HTTP_200_OK, response_model=PasswordResetConfirmResponse)
def confirm_password_reset(body: PasswordResetConfirmRequest, db: Annotated[Session, Depends(get_db)]):
    token = (
        db.query(PasswordResetToken)
        .filter(PasswordResetToken.token_hash == _hash_reset_token(body.token))
        .one_or_none()
    )

    if token is None or token.used_at is not None or _is_expired(token.expires_at):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error_description": "Reset token is invalid or expired"},
        )

    user = db.query(User).filter(User.user_id == token.user_id).one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error_description": "Reset token is invalid or expired"},
        )

    user.hashed_password = hash_password(body.new_password)
    token.used_at = datetime.now(timezone.utc)
    db.commit()

    return PasswordResetConfirmResponse(ok=True)


# ---------------------------------------------------------------------------
# POST /auth/logout
# ---------------------------------------------------------------------------

@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(current_user: CurrentUser):
    # JWT stateless — при наличии sessions-таблицы здесь можно
    # проставить revoked_at. Пока просто возвращаем 204.
    return None


# ---------------------------------------------------------------------------
# GET /auth/me
# ---------------------------------------------------------------------------

@router.get("/me", response_model=MeResponse)
def me(current_user: Annotated[User, require("users.me.view")]):
    effective_permissions = get_effective_permissions(current_user)

    memberships = [
        PartnerMembershipInfo(
            partner_id=m.partner_id,
            role=m.user_role,
            permissions=sorted(get_membership_permissions(m)),
            read_scope=m.read_scope,
            write_scope=m.write_scope,
            delete_scope=m.delete_scope,
            is_active=True,
        )
        for m in current_user.memberships
    ]

    return MeResponse(
        user_id=current_user.user_id,
        email=current_user.email,
        full_name=current_user.full_name,
        global_role=current_user.global_role.value,
        permissions=sorted(effective_permissions),
        partner_memberships=memberships,
    )
