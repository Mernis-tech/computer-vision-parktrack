from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Ответы
# ---------------------------------------------------------------------------

class OccupancyObservationResponse(BaseModel):
    observation_id:     int
    zone_id:            int
    camera_id:          int | None
    partner_id:         int | None
    source_type:        str
    source_ref:         str | None
    capacity:           int
    occupied:           int
    free_count:         int
    confidence:         float
    confidence_level:   str | None
    observed_at:        datetime
    ingested_at:        datetime
    metadata:           Any | None
    created_by_user_id: int | None


class OccupancySeriesPoint(BaseModel):
    observed_at:      datetime
    occupied:         int
    free_count:       int
    capacity:         int
    confidence:       float
    confidence_level: str | None
    source_type:      str


class OccupancyMapItem(BaseModel):
    zone_id:          int
    camera_id:        int | None
    capacity:         int
    occupied:         int
    free_count:       int
    confidence:       float
    confidence_level: str | None
    observed_at:      datetime
    geometry:         Any           # GeoJSON Polygon
    pay:              int
    zone_type:        str
    location_type:    str | None
    is_accessible:    bool | None
    is_active:        bool


# ---------------------------------------------------------------------------
# Запросы
# ---------------------------------------------------------------------------

class CreateOccupancyRequest(BaseModel):
    zone_id:     int        = Field(ge=1)
    source_type: str        = Field(min_length=1, max_length=50)
    observed_at: datetime
    occupied:    int        = Field(ge=0)
    confidence:  float      = Field(ge=0.0, le=1.0)
    source_ref:  str | None = Field(None, max_length=255)
    capacity:    int | None = Field(None, ge=0)
    metadata:    Any        = None


class UpdateOccupancyRequest(BaseModel):
    observed_at: datetime   | None = None
    occupied:    int        | None = Field(None, ge=0)
    confidence:  float      | None = Field(None, ge=0.0, le=1.0)
    capacity:    int        | None = Field(None, ge=0)
    source_ref:  str        | None = Field(None, max_length=255)
    metadata:    Any               = None

    @model_validator(mode="after")
    def occupied_lte_capacity(self) -> "UpdateOccupancyRequest":
        if self.occupied is not None and self.capacity is not None:
            if self.occupied > self.capacity:
                raise ValueError("occupied must be <= capacity")
        return self
