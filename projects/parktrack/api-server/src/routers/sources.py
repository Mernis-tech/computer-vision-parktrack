from __future__ import annotations
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from ..database import get_db
from ..db_models import DataSource, User
from ..dependencies import require
from ..schemas.sources import SourceListResponse, SourceResponse

router = APIRouter(prefix="/sources", tags=["Sources"])

def _serialize(s: DataSource) -> SourceResponse:
    return SourceResponse(
        source_id=s.source_id,
        partner_id=s.partner_id,
        entity_type=s.entity_type,
        entity_id=s.entity_id,
        source_type=s.source_type,
        title=s.title,
        status=s.status,
        is_active=s.is_active,
        created_at=s.created_at,
        updated_at=s.updated_at,
    )

@router.get("", response_model=SourceListResponse)
def list_sources(
    current_user: Annotated[User, require("sources.view")],
    db: Annotated[Session, Depends(get_db)],
    partner_id: int  | None = None,
    is_active:  bool | None = None,
    top:        int = 20,
    offset:     int = 0,
):
    query = db.query(DataSource)
    if partner_id is not None:
        query = query.filter(DataSource.partner_id == partner_id)
    if is_active is not None:
        query = query.filter(DataSource.is_active == is_active)
    total = query.count()
    items = query.order_by(DataSource.source_id).offset(offset).limit(top).all()
    return SourceListResponse(items=[_serialize(s) for s in items], total=total, top=top, offset=offset)

@router.get("/{source_id}", response_model=SourceResponse)
def get_source(
    source_id: int,
    current_user: Annotated[User, require("sources.view")],
    db: Annotated[Session, Depends(get_db)],
):
    s = db.query(DataSource).filter(DataSource.source_id == source_id).one_or_none()
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"error_description": "Source not found"})
    return _serialize(s)