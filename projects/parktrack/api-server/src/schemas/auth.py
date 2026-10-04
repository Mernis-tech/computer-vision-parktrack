from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field, field_validator

from .validators import validate_optional_phone


# ---------------------------------------------------------------------------
# Запросы
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    email:     EmailStr
    password:  str   = Field(min_length=6)
    full_name: str | None = Field(None, max_length=255)
    phone:     str | None = Field(None, max_length=50)

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str | None) -> str | None:
        return validate_optional_phone(value)


class LoginRequest(BaseModel):
    login:    str
    password: str


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirmRequest(BaseModel):
    token:        str = Field(min_length=16)
    new_password: str = Field(min_length=6, max_length=72)


# ---------------------------------------------------------------------------
# Ответы
# ---------------------------------------------------------------------------

class PasswordResetRequestResponse(BaseModel):
    ok: bool = True
    reset_token: str | None = None


class PasswordResetConfirmResponse(BaseModel):
    ok: bool = True



class AuthUserInfo(BaseModel):
    user_id:      int
    email:        str
    full_name:    str | None
    global_role:  str
    permissions:  list[str]
    partner_memberships: list[PartnerMembershipInfo]


class TokenResponse(BaseModel):
    access_token: str
    token_type:   str = "Bearer"
    expires_in:   int
    user:         AuthUserInfo


# ---------------------------------------------------------------------------
# GET /auth/me
# ---------------------------------------------------------------------------

class PartnerMembershipInfo(BaseModel):
    partner_id:   int
    role:         str
    permissions:  list[str]
    read_scope:   str
    write_scope:  str
    delete_scope: str
    is_active:    bool


class MeResponse(BaseModel):
    user_id:             int
    email:               str
    full_name:           str | None
    global_role:        str
    permissions:         list[str]
    partner_memberships: list[PartnerMembershipInfo]
