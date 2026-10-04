from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator
import json


class CameraResponse(BaseModel):
    camera_id:          int
    title:              str
    source:             str
    image_width:        int
    image_height:       int
    calib:              Any | None
    latitude:           float
    longitude:          float
    partner_id:         int | None
    created_by_user_id: int | None
    is_active:          bool
    last_snapshot_at:   datetime | None
    created_at:         datetime
    updated_at:         datetime


class CameraMapItemResponse(BaseModel):
    camera_id:  int
    title:      str
    latitude:   float
    longitude:  float
    partner_id: int | None
    is_active:  bool


class CreateCameraRequest(BaseModel):
    title:        str  = Field(min_length=1, max_length=200)
    source:       str
    image_width:  int  = Field(gt=0)
    image_height: int  = Field(gt=0)
    calib:        Any  = None
    latitude:     float = Field(ge=-90,  le=90)
    longitude:    float = Field(ge=-180, le=180)
    partner_id:   int | None = None

    @field_validator("calib")
    @classmethod
    def validate_calib(cls, v: Any) -> Any:
        if v is not None:
            try:
                json.dumps(v)
            except (TypeError, ValueError):
                raise ValueError("calib must be JSON-serializable")
        return v


class UpdateCameraRequest(BaseModel):
    title:        str   | None = Field(None, min_length=1, max_length=200)
    source:       str   | None = None
    image_width:  int   | None = Field(None, gt=0)
    image_height: int   | None = Field(None, gt=0)
    calib:        Any         = None
    latitude:     float | None = Field(None, ge=-90,  le=90)
    longitude:    float | None = Field(None, ge=-180, le=180)
    is_active:    bool  | None = None

    @field_validator("calib")
    @classmethod
    def validate_calib(cls, v: Any) -> Any:
        if v is not None:
            try:
                json.dumps(v)
            except (TypeError, ValueError):
                raise ValueError("calib must be JSON-serializable")
        return v


class CameraNextResponse(BaseModel):
    camera_id:          int
    source:             str
    image_width:        int
    image_height:       int
    calib:              Any | None
    partner_id:         int | None
    is_active:          bool
