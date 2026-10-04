from __future__ import annotations
from datetime import datetime
from pydantic import BaseModel

class SourceResponse(BaseModel):
    source_id:   int
    partner_id:  int | None
    entity_type: str
    entity_id:   int
    source_type: str
    title:       str
    status:      str
    is_active:   bool
    created_at:  datetime
    updated_at:  datetime

class SourceListResponse(BaseModel):
    items:  list[SourceResponse]
    total:  int
    top:    int
    offset: int