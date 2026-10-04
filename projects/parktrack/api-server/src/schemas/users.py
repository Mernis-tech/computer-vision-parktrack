from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from .validators import validate_optional_phone


class UserResponse(BaseModel):
    user_id:    int
    email:      str
    full_name:  str | None
    phone:      str | None
    global_role: str
    is_active:  bool
    is_email_verified: bool
    created_at: datetime
    updated_at: datetime


class UpdateUserRequest(BaseModel):
    full_name: str | None = Field(None, max_length=255)
    phone:     str | None = Field(None, max_length=50)
    email:     EmailStr   | None = None

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str | None) -> str | None:
        return validate_optional_phone(value)


class UpdatePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6)


class AdminUpdateUserRequest(BaseModel):
    """Только для администратора — позволяет менять роль и статус."""
    full_name:   str | None = Field(None, max_length=255)
    phone:       str | None = Field(None, max_length=50)
    email:       EmailStr   | None = None
    global_role: str        | None = None
    is_active:   bool       | None = None

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str | None) -> str | None:
        return validate_optional_phone(value)


class UserListResponse(BaseModel):
    items:  list[UserResponse]
    total:  int
    top:    int
    offset: int

class PartnerMembershipInfo(BaseModel):
    partner_id:   int
    role:         str          
    is_active:    bool          
    read_scope:   str
    write_scope:  str
    delete_scope: str


class UserProfile(BaseModel):
    user:                UserResponse
    partner_memberships: list[PartnerMembershipInfo]

class AdminCreateUserRequest(BaseModel):
    email:      EmailStr
    password:   str = Field(min_length=6)
    full_name:  str | None = Field(None, max_length=255)
    phone:      str | None = Field(None, max_length=50)
    global_role: str = "user"

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str | None) -> str | None:
        return validate_optional_phone(value)
    
