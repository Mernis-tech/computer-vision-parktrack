from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
# GeoJSON Polygon и image_polygon пропускаются как Any —
# валидация формата остаётся на совести клиента в MVP.

class ZoneResponse(BaseModel):
    zone_id:             int
    camera_id:           int
    zone_type:           str
    capacity:            int
    occupied:            int
    free_count:          int
    confidence:          float
    confidence_level:    str | None
    pay:                 int
    geometry:            Any           # GeoJSON Polygon
    image_polygon:       Any           # [[x,y]*4]
    partner_id:          int | None
    created_by_user_id:  int | None
    is_active:           bool
    location_type:       str | None
    is_private:          bool | None
    is_accessible:       bool | None
    occupancy_updated_at: datetime | None
    created_at:          datetime
    updated_at:          datetime


class ZoneMapItemResponse(BaseModel):
    zone_id:             int
    zone_type:           str
    capacity:            int
    occupied:            int
    free_count:          int
    confidence:          float
    confidence_level:    str | None
    pay:                 int
    geometry:            Any
    location_type:       str | None
    is_private:          bool | None
    is_accessible:       bool | None
    occupancy_updated_at: datetime | None
    is_active:           bool


class CreateZoneRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    camera_id:     Any | None = None
    zone_type:     Any | None = None
    capacity:      Any | None = None
    pay:           Any | None = None
    geometry:      Any | None = None
    image_polygon: Any | None = None
    partner_id:    Any | None = None
    is_active:     Any | None = True
    location_type: Any | None = None
    is_private:    Any | None = None
    is_accessible: Any | None = None


class UpdateZoneRequest(BaseModel):
    camera_id:     int   | None = Field(None, ge=1)
    zone_type:     Literal["parallel", "standard"] | None = None
    capacity:      int   | None = Field(None, gt=0)
    pay:           int   | None = Field(None, ge=0)
    occupied:      int   | None = Field(None, ge=0)
    confidence:    float | None = Field(None, ge=0, le=1)
    geometry:      Any         = None
    image_polygon: Any         = None
    location_type: str  | None = None
    is_private:    bool | None = None
    is_accessible: bool | None = None
    is_active:     bool | None = None


class ZoneListResponse(BaseModel):
    items:  list[ZoneResponse]
    total:  int
    top:    int
    offset: int
