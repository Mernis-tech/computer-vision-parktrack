from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..db_models import Partner, PartnerMembership, User
from ..dependencies import CurrentUser, require
from ..schemas.partners import (
    CreatePartnerRequest,
    InviteMemberRequest,
    MemberListResponse,
    MemberResponse,
    PartnerListResponse,
    PartnerResponse,
    UpdateMemberRequest,
    UpdatePartnerRequest,
)

router = APIRouter(prefix="/partners", tags=["Partners"])


def _serialize_partner(p: Partner) -> PartnerResponse:
    return PartnerResponse(
        partner_id=p.partner_id,
        name=p.legal_name,
        slug=p.slug,
        contact_email=p.contact_email,
        contact_phone=p.contact_phone,
        is_active=p.is_active,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


def _serialize_member(m: PartnerMembership) -> MemberResponse:
    return MemberResponse(
        partner_membership_id=m.partner_membership_id,
        user_id=m.user_id,
        email=m.user.email,
        full_name=m.user.full_name,
        user_role=m.user_role,
        read_scope=m.read_scope,
        write_scope=m.write_scope,
        delete_scope=m.delete_scope,
        created_at=m.created_at,
    )


def _get_partner_or_404(db: Session, partner_id: int) -> Partner:
    partner = db.query(Partner).filter(Partner.partner_id == partner_id).one_or_none()
    if partner is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"error_description": "Partner not found"})
    return partner


# ---------------------------------------------------------------------------
# GET /partners
# ---------------------------------------------------------------------------

@router.get("", response_model=PartnerListResponse)
def list_partners(
    current_user: Annotated[User, require("admin.partners.view")],
    db: Annotated[Session, Depends(get_db)],
    q:         str  | None = None,
    is_active: bool | None = None,
    top:       int = 20,
    offset:    int = 0,
):
    query = db.query(Partner)
    if q:
        query = query.filter(Partner.legal_name.icontains(q))
    if is_active is not None:
        query = query.filter(Partner.is_active == is_active)

    total = query.count()
    partners = query.order_by(Partner.partner_id).offset(offset).limit(top).all()

    return PartnerListResponse(
        items=[_serialize_partner(p) for p in partners],
        total=total,
        top=top,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# POST /partners/new
# ---------------------------------------------------------------------------

@router.post("/new", status_code=status.HTTP_201_CREATED)
def create_partner(
    body: CreatePartnerRequest,
    current_user: Annotated[User, require("admin.partners.manage")],
    db: Annotated[Session, Depends(get_db)],
):
    for field, value, label in [
        (Partner.legal_name,    body.legal_name,    "legal_name"),
        (Partner.slug,          body.slug,           "slug"),
        (Partner.contact_email, body.contact_email,  "contact_email"),
        (Partner.contact_phone, body.contact_phone,  "contact_phone"),
    ]:
        if db.query(Partner).filter(field == value).one_or_none():
            raise HTTPException(status.HTTP_409_CONFLICT,
                                detail={"error_description": f"Partner with this {label} already exists"})

    partner = Partner(
        legal_name=body.legal_name,
        slug=body.slug,
        contact_email=body.contact_email,
        contact_phone=body.contact_phone,
        is_active=True,
    )
    db.add(partner)
    db.commit()
    db.refresh(partner)
    return {"partner_id": partner.partner_id}


# ---------------------------------------------------------------------------
# GET /partners/{partner_id}
# ---------------------------------------------------------------------------

@router.get("/{partner_id}", response_model=PartnerResponse)
def get_partner(
    partner_id: int,
    current_user: Annotated[User, require("admin.partners.view")],
    db: Annotated[Session, Depends(get_db)],
):
    return _serialize_partner(_get_partner_or_404(db, partner_id))


# ---------------------------------------------------------------------------
# PUT /partners/{partner_id}
# ---------------------------------------------------------------------------

@router.put("/{partner_id}", response_model=PartnerResponse)
def update_partner(
    partner_id: int,
    body: UpdatePartnerRequest,
    current_user: Annotated[User, require("admin.partners.manage")],
    db: Annotated[Session, Depends(get_db)],
):
    partner = _get_partner_or_404(db, partner_id)

    if body.legal_name is not None:
        partner.legal_name = body.legal_name
    if body.contact_email is not None:
        partner.contact_email = body.contact_email
    if body.contact_phone is not None:
        partner.contact_phone = body.contact_phone
    if body.is_active is not None:
        partner.is_active = body.is_active

    db.commit()
    db.refresh(partner)
    return _serialize_partner(partner)


# ---------------------------------------------------------------------------
# DELETE /partners/{partner_id}
# ---------------------------------------------------------------------------

@router.delete("/{partner_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_partner(
    partner_id: int,
    current_user: Annotated[User, require("admin.partners.manage")],
    db: Annotated[Session, Depends(get_db)],
):
    partner = _get_partner_or_404(db, partner_id)
    db.delete(partner)
    db.commit()
    return None


# ---------------------------------------------------------------------------
# GET /partners/{partner_id}/members
# ---------------------------------------------------------------------------

@router.get("/{partner_id}/members", response_model=MemberListResponse)
def list_members(
    partner_id: int,
    current_user: Annotated[User, require("partner_members.view")],
    db: Annotated[Session, Depends(get_db)],
    top:    int = 20,
    offset: int = 0,
):
    _get_partner_or_404(db, partner_id)

    query = (
        db.query(PartnerMembership)
        .filter(PartnerMembership.partner_id == partner_id)
    )
    total = query.count()
    members = query.offset(offset).limit(top).all()

    return MemberListResponse(
        items=[_serialize_member(m) for m in members],
        total=total,
        top=top,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# POST /partners/{partner_id}/members
# ---------------------------------------------------------------------------

@router.post("/{partner_id}/members", status_code=status.HTTP_201_CREATED)
def invite_member(
    partner_id: int,
    body: InviteMemberRequest,
    current_user: Annotated[User, require("partner_members.invite")],
    db: Annotated[Session, Depends(get_db)],
):
    _get_partner_or_404(db, partner_id)

    user = db.query(User).filter(User.user_id == body.user_id).one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"error_description": "User not found"})

    existing = (
        db.query(PartnerMembership)
        .filter_by(user_id=body.user_id, partner_id=partner_id)
        .one_or_none()
    )
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail={"error_description": "User is already a member of this partner"})

    membership = PartnerMembership(
        user_id=body.user_id,
        partner_id=partner_id,
        user_role=body.user_role,
        read_scope=body.read_scope,
        write_scope=body.write_scope,
        delete_scope=body.delete_scope,
    )
    db.add(membership)
    db.commit()
    db.refresh(membership)
    return {"partner_membership_id": membership.partner_membership_id}


# ---------------------------------------------------------------------------
# PUT /partners/{partner_id}/members/{user_id}
# ---------------------------------------------------------------------------

@router.put("/{partner_id}/members/{user_id}", response_model=MemberResponse)
def update_member(
    partner_id: int,
    user_id: int,
    body: UpdateMemberRequest,
    current_user: Annotated[User, require("partner_members.update")],
    db: Annotated[Session, Depends(get_db)],
):
    _get_partner_or_404(db, partner_id)

    membership = (
        db.query(PartnerMembership)
        .filter_by(user_id=user_id, partner_id=partner_id)
        .one_or_none()
    )
    if membership is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"error_description": "Membership not found"})

    if body.user_role is not None:
        membership.user_role = body.user_role
    if body.read_scope is not None:
        membership.read_scope = body.read_scope
    if body.write_scope is not None:
        membership.write_scope = body.write_scope
    if body.delete_scope is not None:
        membership.delete_scope = body.delete_scope

    db.commit()
    db.refresh(membership)
    return _serialize_member(membership)


# ---------------------------------------------------------------------------
# DELETE /partners/{partner_id}/members/{user_id}  (disable / kick)
# ---------------------------------------------------------------------------

@router.delete("/{partner_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(
    partner_id: int,
    user_id: int,
    current_user: Annotated[User, require("partner_members.disable")],
    db: Annotated[Session, Depends(get_db)],
):
    _get_partner_or_404(db, partner_id)

    membership = (
        db.query(PartnerMembership)
        .filter_by(user_id=user_id, partner_id=partner_id)
        .one_or_none()
    )
    if membership is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"error_description": "Membership not found"})

    db.delete(membership)
    db.commit()
    return None
