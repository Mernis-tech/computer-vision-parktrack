from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..db_models import GlobalRole, Partner, PartnerMembership, User
from ..dependencies import CurrentUser, get_effective_permissions, hash_password, require, verify_password
from ..schemas.users import (
    AdminUpdateUserRequest,
    UpdatePasswordRequest,
    UpdateUserRequest,
    UserListResponse,
    UserResponse,
    UserProfile,
    PartnerMembershipInfo,
    AdminCreateUserRequest
)

router = APIRouter(prefix="/users", tags=["Users"])


def _active_partner_ids(user: User) -> set[int]:
    return {
        membership.partner_id
        for membership in user.memberships
        if getattr(membership.partner, "is_active", True)
    }


def _ensure_can_view_users(current_user: User) -> bool:
    permissions = get_effective_permissions(current_user)
    if "admin.users.view" in permissions:
        return True
    if "partner_members.view" in permissions:
        return False
    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        detail={"error_description": "Missing permissions: admin.users.view or partner_members.view"},
    )


def _scope_users_query(query, current_user: User, has_global_access: bool):
    if has_global_access:
        return query

    partner_ids = _active_partner_ids(current_user)
    if not partner_ids:
        return query.filter(User.user_id.in_([]))

    return (
        query
        .join(PartnerMembership, PartnerMembership.user_id == User.user_id)
        .filter(PartnerMembership.partner_id.in_(partner_ids))
        .distinct()
    )


def _ensure_user_visible(user: User, current_user: User, has_global_access: bool):
    if has_global_access:
        return
    partner_ids = _active_partner_ids(current_user)
    if not any(membership.partner_id in partner_ids for membership in user.memberships):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error_description": "User not found"},
        )


def _serialize(user: User) -> UserResponse:
    return UserResponse(
        user_id=user.user_id,
        email=user.email,
        full_name=user.full_name,
        phone=user.phone,
        global_role=user.global_role.value,
        is_active=user.is_active,
        created_at=user.created_at,
        updated_at=user.updated_at,
        is_email_verified=True
    )


# ---------------------------------------------------------------------------
# GET /users  (admin only)
# ---------------------------------------------------------------------------

@router.get("", response_model=UserListResponse)
def list_users(
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    q:        str  | None = None,
    is_active: bool | None = None,
    top:      int = 20,
    offset:   int = 0,
):
    has_global_access = _ensure_can_view_users(current_user)
    query = _scope_users_query(db.query(User), current_user, has_global_access)
    if q:
        query = query.filter(User.email.icontains(q) | User.full_name.icontains(q))
    if is_active is not None:
        query = query.filter(User.is_active == is_active)

    total = query.count()
    users = query.order_by(User.user_id).offset(offset).limit(top).all()

    return UserListResponse(
        items=[_serialize(u) for u in users],
        total=total,
        top=top,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# GET /users/me
# ---------------------------------------------------------------------------

@router.get("/me", response_model=UserProfile)
def get_me(
    current_user: Annotated[User, require("users.me.view")],
    db: Annotated[Session, Depends(get_db)],
):
    # Fetch partner memberships with partner’s is_active
    memberships = (
        db.query(PartnerMembership, Partner.is_active)
        .join(Partner, PartnerMembership.partner_id == Partner.partner_id)
        .filter(PartnerMembership.user_id == current_user.user_id)
        .all()
    )

    membership_data = [
        PartnerMembershipInfo(
            partner_id=m.partner_id,
            role=m.user_role,
            is_active=partner_is_active,
            read_scope=m.read_scope,
            write_scope=m.write_scope,
            delete_scope=m.delete_scope,
        )
        for m, partner_is_active in memberships
    ]

    return UserProfile(
        user=_serialize(current_user),
        partner_memberships=membership_data,
    )

# ---------------------------------------------------------------------------
# PUT /users/me
# ---------------------------------------------------------------------------

@router.put("/me", response_model=UserResponse)
def update_me(
    body: UpdateUserRequest,
    current_user: Annotated[User, require("users.me.update")],
    db: Annotated[Session, Depends(get_db)],
):
    if body.email and body.email != current_user.email:
        conflict = db.query(User).filter(User.email == body.email).one_or_none()
        if conflict:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                detail={"error_description": "Email already in use"})
        current_user.email = body.email
    if body.full_name is not None:
        current_user.full_name = body.full_name
    if body.phone is not None:
        current_user.phone = body.phone

    db.commit()
    db.refresh(current_user)
    return _serialize(current_user)


# ---------------------------------------------------------------------------
# PUT /users/me/password
# ---------------------------------------------------------------------------

@router.put("/me/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    body: UpdatePasswordRequest,
    current_user: Annotated[User, require("users.password.update")],
    db: Annotated[Session, Depends(get_db)],
):
    if not verify_password(body.current_password, current_user.hashed_password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            detail={"error_description": "Old password is incorrect"})
    current_user.hashed_password = hash_password(body.new_password)
    db.commit()
    return None


# ---------------------------------------------------------------------------
# GET /users/{user_id}
# ---------------------------------------------------------------------------

@router.get("/{user_id}", response_model=UserResponse)
def get_user(
    user_id: int,
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    has_global_access = _ensure_can_view_users(current_user)
    user = db.query(User).filter(User.user_id == user_id).one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"error_description": "User not found"})
    _ensure_user_visible(user, current_user, has_global_access)
    return _serialize(user)


# ---------------------------------------------------------------------------
# PUT /users/{user_id}  (admin only)
# ---------------------------------------------------------------------------

@router.put("/{user_id}", response_model=UserResponse)
def admin_update_user(
    user_id: int,
    body: AdminUpdateUserRequest,
    current_user: Annotated[User, require("admin.users.manage")],
    db: Annotated[Session, Depends(get_db)],
):
    user = db.query(User).filter(User.user_id == user_id).one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"error_description": "User not found"})

    if body.email and body.email != user.email:
        conflict = db.query(User).filter(User.email == body.email).one_or_none()
        if conflict:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                detail={"error_description": "Email already in use"})
        user.email = body.email
    if body.full_name is not None:
        user.full_name = body.full_name
    if body.phone is not None:
        user.phone = body.phone
    if body.global_role is not None:
        try:
            user.global_role = GlobalRole(body.global_role)
        except ValueError:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail={"error_description": f"Unknown role: {body.global_role}"})
    if body.is_active is not None:
        user.is_active = body.is_active

    db.commit()
    db.refresh(user)
    return _serialize(user)


# ---------------------------------------------------------------------------
# DELETE /users/{user_id}  (admin only)
# ---------------------------------------------------------------------------

@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: int,
    current_user: Annotated[User, require("admin.users.manage")],
    db: Annotated[Session, Depends(get_db)],
):
    user = db.query(User).filter(User.user_id == user_id).one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"error_description": "User not found"})
    db.delete(user)
    db.commit()
    return None

@router.post("", status_code=status.HTTP_201_CREATED, response_model=UserResponse)
def admin_create_user(
    body: AdminCreateUserRequest,
    current_user: Annotated[User, require("admin.users.manage")],
    db: Annotated[Session, Depends(get_db)],
):
    if db.query(User).filter(User.email == body.email).one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail={"error_description": "User with this email already exists"})
    try:
        role = GlobalRole(body.global_role)
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail={"error_description": f"Unknown role: {body.global_role}"})
    user = User(
        email=body.email,
        hashed_password=hash_password(body.password),
        full_name=body.full_name,
        phone=body.phone,
        global_role=role,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _serialize(user)
