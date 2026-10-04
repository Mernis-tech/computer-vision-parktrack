from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from .validators import validate_optional_phone


# ---------------------------------------------------------------------------
# Partner
# ---------------------------------------------------------------------------

class PartnerResponse(BaseModel):
    partner_id:    int
    name:          str
    slug:          str
    contact_email: str
    contact_phone: str
    is_active:     bool
    created_at:    datetime
    updated_at:    datetime


class CreatePartnerRequest(BaseModel):
    legal_name:    str   = Field(min_length=2, max_length=255)
    slug:          str   = Field(min_length=2, max_length=255, pattern=r"^[a-z0-9\-]+$")
    contact_email: EmailStr
    contact_phone: str   = Field(min_length=5, max_length=255)

    @field_validator("contact_phone")
    @classmethod
    def validate_phone(cls, value: str | None) -> str | None:
        return validate_optional_phone(value)


class UpdatePartnerRequest(BaseModel):
    legal_name:    str       | None = Field(None, min_length=2, max_length=255)
    contact_email: EmailStr  | None = None
    contact_phone: str       | None = Field(None, min_length=5, max_length=255)
    is_active:     bool      | None = None

    @field_validator("contact_phone")
    @classmethod
    def validate_phone(cls, value: str | None) -> str | None:
        return validate_optional_phone(value)


class PartnerListResponse(BaseModel):
    items:  list[PartnerResponse]
    total:  int
    top:    int
    offset: int


# ---------------------------------------------------------------------------
# Partner Members
# ---------------------------------------------------------------------------

class MemberResponse(BaseModel):
    partner_membership_id: int
    user_id:               int
    email:                 str
    full_name:             str | None
    user_role:             str
    read_scope:            str
    write_scope:           str
    delete_scope:          str
    created_at:            datetime


class InviteMemberRequest(BaseModel):
    user_id:     int
    user_role:   str = Field(min_length=2, max_length=50)
    read_scope:  str = "own"
    write_scope: str = "own"
    delete_scope: str = "own"


class UpdateMemberRequest(BaseModel):
    user_role:   str | None = Field(None, min_length=2, max_length=50)
    read_scope:  str | None = None
    write_scope: str | None = None
    delete_scope: str | None = None


class MemberListResponse(BaseModel):
    items:  list[MemberResponse]
    total:  int
    top:    int
    offset: int
