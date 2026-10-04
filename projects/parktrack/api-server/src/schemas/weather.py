from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class WeatherObservationResponse(BaseModel):
    camera_id:     int
    observed_at:   datetime
    temperature:   float
    precipitation: float


class CreateWeatherObservationRequest(BaseModel):
    camera_id:     int   = Field(ge=1)
    observed_at:   datetime
    temperature:   float
    precipitation: float = Field(ge=0)


class WeatherObservationKeyResponse(BaseModel):
    camera_id:   int
    observed_at: datetime
