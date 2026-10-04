from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..db_models import Camera, User, WeatherObservation
from ..dependencies import require
from ..schemas.weather import (
    CreateWeatherObservationRequest,
    WeatherObservationKeyResponse,
    WeatherObservationResponse,
)

router = APIRouter(prefix="/weather", tags=["Weather"])


def _serialize(obs: WeatherObservation) -> WeatherObservationResponse:
    return WeatherObservationResponse(
        camera_id=obs.camera_id,
        observed_at=obs.observed_at,
        temperature=obs.temperature,
        precipitation=obs.precipitation,
    )


def _normalize_utc(dt: datetime, field_name: str, *, require_hour: bool = False) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_description": f"Validation error: {field_name} must be UTC"},
        )
    if require_hour and (dt.minute != 0 or dt.second != 0 or dt.microsecond != 0):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_description": f"Validation error: {field_name} must be truncated to hour"},
        )
    return dt.astimezone(timezone.utc)


def _get_weather(db: Session, camera_id: int, observed_at: datetime) -> WeatherObservation | None:
    return (
        db.query(WeatherObservation)
        .filter(
            WeatherObservation.camera_id == camera_id,
            WeatherObservation.observed_at == observed_at,
        )
        .one_or_none()
    )


def _get_weather_or_404(db: Session, camera_id: int, observed_at: datetime) -> WeatherObservation:
    obs = _get_weather(db, camera_id, observed_at)
    if obs is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error_description": "Weather observation not found"},
        )
    return obs


# ---------------------------------------------------------------------------
# GET /weather
# ---------------------------------------------------------------------------

@router.get("", response_model=list[WeatherObservationResponse])
def list_weather(
    current_user: Annotated[User, require("weather.view")],
    db: Annotated[Session, Depends(get_db)],
    camera_id: int | None = Query(None, ge=1),
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
    latest_only: bool = False,
):
    from_dt = _normalize_utc(from_, "from") if from_ is not None else None
    to_dt = _normalize_utc(to, "to") if to is not None else None
    if from_dt is not None and to_dt is not None and from_dt > to_dt:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_description": "Validation error: from must be less than or equal to to"},
        )

    query = db.query(WeatherObservation)
    if camera_id is not None:
        query = query.filter(WeatherObservation.camera_id == camera_id)
    if from_dt is not None:
        query = query.filter(WeatherObservation.observed_at >= from_dt)
    if to_dt is not None:
        query = query.filter(WeatherObservation.observed_at <= to_dt)

    if latest_only:
        latest_sq = (
            query.with_entities(
                WeatherObservation.camera_id.label("camera_id"),
                func.max(WeatherObservation.observed_at).label("max_observed_at"),
            )
            .group_by(WeatherObservation.camera_id)
            .subquery()
        )
        query = (
            db.query(WeatherObservation)
            .join(
                latest_sq,
                (WeatherObservation.camera_id == latest_sq.c.camera_id)
                & (WeatherObservation.observed_at == latest_sq.c.max_observed_at),
            )
        )

    observations = (
        query
        .order_by(WeatherObservation.camera_id.asc(), WeatherObservation.observed_at.asc())
        .all()
    )
    return [_serialize(obs) for obs in observations]


# ---------------------------------------------------------------------------
# POST /weather/new
# ---------------------------------------------------------------------------

@router.post("/new", status_code=status.HTTP_201_CREATED, response_model=WeatherObservationKeyResponse)
def create_weather(
    body: CreateWeatherObservationRequest,
    current_user: Annotated[User, require("weather.write")],
    db: Annotated[Session, Depends(get_db)],
):
    observed_at = _normalize_utc(body.observed_at, "observed_at", require_hour=True)

    camera = db.query(Camera).filter(Camera.camera_id == body.camera_id).one_or_none()
    if camera is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error_description": "Camera not found"},
        )

    existing = _get_weather(db, body.camera_id, observed_at)
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"error_description": "Weather observation for this camera and hour already exists"},
        )

    obs = WeatherObservation(
        camera_id=body.camera_id,
        observed_at=observed_at,
        temperature=body.temperature,
        precipitation=body.precipitation,
    )
    db.add(obs)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"error_description": "Weather observation for this camera and hour already exists"},
        )
    db.refresh(obs)
    return WeatherObservationKeyResponse(camera_id=obs.camera_id, observed_at=obs.observed_at)

# ---------------------------------------------------------------------------
# GET /weather/{camera_id}/{observed_at}
# ---------------------------------------------------------------------------

@router.get("/{camera_id}/{observed_at}", response_model=WeatherObservationResponse)
def get_weather(
    camera_id: int,
    observed_at: datetime,
    current_user: Annotated[User, require("weather.view")],
    db: Annotated[Session, Depends(get_db)],
):
    observed_at = _normalize_utc(observed_at, "observed_at", require_hour=True)
    return _serialize(_get_weather_or_404(db, camera_id, observed_at))
