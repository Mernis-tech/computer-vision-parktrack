from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Ответы
# ---------------------------------------------------------------------------

class ForecastPointResponse(BaseModel):
    forecast_id:            int
    zone_id:                int
    camera_id:              int | None
    partner_id:             int | None
    model_type:             str
    model_version:          str | None
    generated_at:           datetime
    predicted_for:          datetime
    capacity:               int
    predicted_occupied:     int
    predicted_free_count:   int
    probability_free_space: float
    confidence:             float
    confidence_level:       str | None
    metadata:               Any | None
    created_by_user_id:     int | None


class ForecastSeriesPoint(BaseModel):
    predicted_for:          datetime
    predicted_occupied:     int
    predicted_free_count:   int
    capacity:               int
    probability_free_space: float
    confidence:             float
    confidence_level:       str | None
    model_type:             str
    generated_at:           datetime


class ForecastMapItem(BaseModel):
    zone_id:                int
    camera_id:              int | None
    capacity:               int
    predicted_occupied:     int
    predicted_free_count:   int
    probability_free_space: float
    confidence:             float
    confidence_level:       str | None
    predicted_for:          datetime
    generated_at:           datetime
    geometry:               Any           # GeoJSON Polygon
    pay:                    int
    zone_type:              str
    location_type:          str | None
    is_accessible:          bool | None
    is_active:              bool


# ---------------------------------------------------------------------------
# Запросы
# ---------------------------------------------------------------------------

class CreateForecastRequest(BaseModel):
    zone_id:                int   = Field(ge=1)
    model_type:             str   = Field(min_length=1, max_length=50)
    generated_at:           datetime
    predicted_for:          datetime
    predicted_occupied:     int   = Field(ge=0)
    probability_free_space: float = Field(ge=0.0, le=1.0)
    confidence:             float = Field(ge=0.0, le=1.0)
    capacity:               int   | None = Field(None, ge=0)
    model_version:          str   | None = Field(None, max_length=100)
    metadata:               Any          = None

    @model_validator(mode="after")
    def occupied_lte_capacity(self) -> "CreateForecastRequest":
        if self.capacity is not None and self.predicted_occupied > self.capacity:
            raise ValueError("predicted_occupied must be <= capacity")
        return self


class UpdateForecastRequest(BaseModel):
    model_version:          str       | None = Field(None, max_length=100)
    generated_at:           datetime  | None = None
    predicted_for:          datetime  | None = None
    predicted_occupied:     int       | None = Field(None, ge=0)
    probability_free_space: float     | None = Field(None, ge=0.0, le=1.0)
    confidence:             float     | None = Field(None, ge=0.0, le=1.0)
    capacity:               int       | None = Field(None, ge=0)
    metadata:               Any              = None

    @model_validator(mode="after")
    def occupied_lte_capacity(self) -> "UpdateForecastRequest":
        if self.predicted_occupied is not None and self.capacity is not None:
            if self.predicted_occupied > self.capacity:
                raise ValueError("predicted_occupied must be <= capacity")
        return self
