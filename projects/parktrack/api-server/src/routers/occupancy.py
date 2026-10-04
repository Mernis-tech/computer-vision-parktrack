from __future__ import annotations

import ast
import json
from datetime import datetime, timezone
from typing import Annotated, Any, Union, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import ValidationError
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ..database import get_db
from ..db_models import ConfidenceLevel, OccupancyObservation, ParkingZone, User
from ..dependencies import require
from ..schemas.occupancy import (
    CreateOccupancyRequest,
    OccupancyMapItem,
    OccupancyObservationResponse,
    OccupancySeriesPoint,
    UpdateOccupancyRequest,
)

router = APIRouter(prefix="/occupancy", tags=["Occupancy"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_count(obs: OccupancyObservation, db: Session) -> int:
    row = db.execute(
        text("SELECT free_count FROM occupancy_observations WHERE observation_id = :id"),
        {"id": obs.observation_id},
    ).one_or_none()
    return row[0] if row else (obs.capacity - obs.occupied)


def _confidence_level(confidence: float) -> ConfidenceLevel | None:
    if confidence >= 0.85:
        return ConfidenceLevel.high
    elif confidence >= 0.65:
        return ConfidenceLevel.medium
    elif confidence >= 0.40:
        return ConfidenceLevel.low
    else:
        return ConfidenceLevel.very_low

def _to_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value

    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _is_newer_or_same_observation(
    incoming_observed_at: datetime,
    current_zone_updated_at: datetime | None,
) -> bool:
    if current_zone_updated_at is None:
        return True

    return _to_utc_naive(incoming_observed_at) >= _to_utc_naive(current_zone_updated_at)


def _clamp_int(value: int, min_value: int, max_value: int | None = None) -> int:
    value = max(value, min_value)

    if max_value is not None:
        value = min(value, max_value)

    return value


def _clamp_float(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(value, max_value))


def _confidence_level_value(confidence: float) -> str:
    level = _confidence_level(confidence)

    if level is None:
        return "very_low"

    return level.value
def _serialize_obs(obs: OccupancyObservation, db: Session) -> OccupancyObservationResponse:
    return OccupancyObservationResponse(
        observation_id=obs.observation_id,
        zone_id=obs.zone_id,
        camera_id=obs.camera_id,
        partner_id=obs.partner_id,
        source_type=obs.source_type,
        source_ref=obs.source_ref,
        capacity=obs.capacity,
        occupied=obs.occupied,
        free_count=_free_count(obs, db),
        confidence=obs.confidence,
        confidence_level=obs.confidence_level.value if obs.confidence_level else None,
        observed_at=obs.observed_at,
        ingested_at=obs.ingested_at,
        metadata=obs.metadata_json,
        created_by_user_id=obs.created_by_user_id,
    )


def _get_obs_or_404(db: Session, observation_id: int) -> OccupancyObservation:
    obs = db.query(OccupancyObservation).filter(
        OccupancyObservation.observation_id == observation_id
    ).one_or_none()
    if obs is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"error_description": "Observation not found"})
    return obs


def _parse_bbox(bbox: str) -> tuple[float, float, float, float]:
    try:
        min_lon, min_lat, max_lon, max_lat = map(float, bbox.split(","))
        return min_lon, min_lat, max_lon, max_lat
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail={"error_description": "bbox must be min_lon,min_lat,max_lon,max_lat"})


# ---------------------------------------------------------------------------
# GET /occupancy
# ---------------------------------------------------------------------------

@router.get(
    "",
    response_model=list[OccupancyObservationResponse]
                 | list[OccupancySeriesPoint]
                 | list[OccupancyMapItem],
)
def list_occupancy(
    # 2026-05-16: открыто без авторизации (по запросу) — режим «Прошлое» на карте.
    db:           Annotated[Session, Depends(get_db)],
    zone_id:      int    | None = None,
    camera_id:    int    | None = None,
    partner_id:   int    | None = None,
    source_type:  str    | None = None,
    from_: datetime      | None = Query(None, alias="from"),
    to: datetime         | None = None,
    at: datetime         | None = None,
    latest_only:  bool          = False,
    bbox:         str    | None = None,
    view:         str           = "observations",
):
    query = db.query(OccupancyObservation)

    if zone_id is not None:
        query = query.filter(OccupancyObservation.zone_id == zone_id)
    if camera_id is not None:
        query = query.filter(OccupancyObservation.camera_id == camera_id)
    if partner_id is not None:
        query = query.filter(OccupancyObservation.partner_id == partner_id)
    if source_type is not None:
        query = query.filter(OccupancyObservation.source_type == source_type)
    if from_:
        query = query.filter(OccupancyObservation.observed_at >= from_)
    if to:
        query = query.filter(OccupancyObservation.observed_at <= to)

    # Если указан at, независимо от view возвращаем по одному наблюдению
    # на каждую зону: последнее observed_at <= at.
    #
    # Важно: row_number нужен, потому что у одной зоны может быть несколько
    # наблюдений с одинаковым observed_at. Тогда берём более позднее ingested_at,
    # а если и оно совпало — большее observation_id.
    if at is not None:
        ranked_sq = (
            query
            .filter(OccupancyObservation.observed_at <= at)
            .with_entities(
                OccupancyObservation.observation_id.label("observation_id"),
                func.row_number().over(
                    partition_by=OccupancyObservation.zone_id,
                    order_by=(
                        OccupancyObservation.observed_at.desc(),
                        OccupancyObservation.ingested_at.desc(),
                        OccupancyObservation.observation_id.desc(),
                    ),
                ).label("rn"),
            )
            .subquery()
        )

        query = (
            db.query(OccupancyObservation)
            .join(
                ranked_sq,
                OccupancyObservation.observation_id == ranked_sq.c.observation_id,
            )
            .filter(ranked_sq.c.rn == 1)
        )

    # Если at не указан, но нужен последний срез для карты или latest_only=true,
    # оставляем старую идею: по одной последней записи на каждую зону.
    elif view == "map" or latest_only:
        ranked_sq = (
            query
            .with_entities(
                OccupancyObservation.observation_id.label("observation_id"),
                func.row_number().over(
                    partition_by=OccupancyObservation.zone_id,
                    order_by=(
                        OccupancyObservation.observed_at.desc(),
                        OccupancyObservation.ingested_at.desc(),
                        OccupancyObservation.observation_id.desc(),
                    ),
                ).label("rn"),
            )
            .subquery()
        )

        query = (
            db.query(OccupancyObservation)
            .join(
                ranked_sq,
                OccupancyObservation.observation_id == ranked_sq.c.observation_id,
            )
            .filter(ranked_sq.c.rn == 1)
        )
        
    if bbox and view == "map":
        min_lon, min_lat, max_lon, max_lat = _parse_bbox(bbox)
        zone_ids_in_bbox = []
        zones = db.query(ParkingZone).all()
        for z in zones:
            try:
                coords = z.geometry["coordinates"][0]
                z_lon = sum(c[0] for c in coords) / len(coords)
                z_lat = sum(c[1] for c in coords) / len(coords)
                if min_lon <= z_lon <= max_lon and min_lat <= z_lat <= max_lat:
                    zone_ids_in_bbox.append(z.parking_zone_id)
            except Exception:
                pass
        query = query.filter(OccupancyObservation.zone_id.in_(zone_ids_in_bbox))

    observations = query.order_by(OccupancyObservation.observed_at.desc()).all()

    if view == "series":
        return [
            OccupancySeriesPoint(
                observed_at=obs.observed_at,
                occupied=obs.occupied,
                free_count=_free_count(obs, db),
                capacity=obs.capacity,
                confidence=obs.confidence,
                confidence_level=obs.confidence_level.value if obs.confidence_level else None,
                source_type=obs.source_type,
            )
            for obs in observations
        ]

    if view == "map":
        result = []
        for obs in observations:
            zone = db.query(ParkingZone).filter(
                ParkingZone.parking_zone_id == obs.zone_id
            ).one_or_none()
            if zone is None:
                continue
            result.append(OccupancyMapItem(
                zone_id=obs.zone_id,
                camera_id=obs.camera_id,
                capacity=obs.capacity,
                occupied=obs.occupied,
                free_count=_free_count(obs, db),
                confidence=obs.confidence,
                confidence_level=obs.confidence_level.value if obs.confidence_level else None,
                observed_at=obs.observed_at,
                geometry=zone.geometry,
                pay=zone.pay,
                zone_type=zone.zone_type.value,
                location_type=zone.location_type.value if zone.location_type else None,
                is_accessible=zone.is_accessible,
                is_active=zone.is_active,
            ))
        return result

    # view=observations (default)
    return [_serialize_obs(obs, db) for obs in observations]


async def _parse_lenient_body(request: Request) -> dict[str, Any]:
    raw = await request.body()

    if not raw:
        return {}

    text = raw.decode("utf-8", errors="replace").strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            data = ast.literal_eval(text)
        except (SyntaxError, ValueError):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error_description": "Request body must be JSON or Python-like dict"},
            )

    if not isinstance(data, dict):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_description": "Request body must be an object"},
        )

    return data

# ---------------------------------------------------------------------------
# POST /occupancy/new
# ---------------------------------------------------------------------------

@router.post("/new", status_code=status.HTTP_201_CREATED)
async def create_observation(
    request:      Request,
    current_user: Annotated[User, require("occupancy.write")],
    db:           Annotated[Session, Depends(get_db)],
):
    raw_body = await _parse_lenient_body(request)

    try:
        body = CreateOccupancyRequest.model_validate(raw_body)
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_description": "Invalid occupancy payload",
                "errors": exc.errors(),
            },
        )

    zone = db.query(ParkingZone).filter(
        ParkingZone.parking_zone_id == body.zone_id
    ).one_or_none()

    if zone is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error_description": "Zone not found"},
        )

    zone_id = body.zone_id
    zone_camera_id = cast(int | None, zone.camera_id)
    zone_partner_id = cast(int | None, zone.partner_id)
    zone_capacity = cast(int, zone.capacity)
    zone_occupancy_updated_at = cast(datetime | None, zone.occupancy_updated_at)
    current_user_id = cast(int | None, current_user.user_id)

    capacity = body.capacity if body.capacity is not None else zone_capacity
    capacity = _clamp_int(capacity, min_value=0)

    occupied = _clamp_int(body.occupied, min_value=0, max_value=capacity)
    confidence = _clamp_float(body.confidence, min_value=0.0, max_value=1.0)
    confidence_level = _confidence_level_value(confidence)

    now = datetime.now(timezone.utc)

    try:
        result = db.execute(
            text(
                """
                INSERT INTO occupancy_observations (
                    zone_id,
                    camera_id,
                    partner_id,
                    source_type,
                    source_ref,
                    capacity,
                    occupied,
                    confidence,
                    confidence_level,
                    observed_at,
                    ingested_at,
                    metadata,
                    created_by_user_id
                )
                VALUES (
                    :zone_id,
                    :camera_id,
                    :partner_id,
                    :source_type,
                    :source_ref,
                    :capacity,
                    :occupied,
                    :confidence,
                    :confidence_level,
                    :observed_at,
                    :ingested_at,
                    CAST(:metadata AS jsonb),
                    :created_by_user_id
                )
                ON CONFLICT (source_type, source_ref)
                DO UPDATE SET
                    zone_id = EXCLUDED.zone_id,
                    camera_id = EXCLUDED.camera_id,
                    partner_id = EXCLUDED.partner_id,
                    capacity = EXCLUDED.capacity,
                    occupied = EXCLUDED.occupied,
                    confidence = EXCLUDED.confidence,
                    confidence_level = EXCLUDED.confidence_level,
                    observed_at = EXCLUDED.observed_at,
                    ingested_at = EXCLUDED.ingested_at,
                    metadata = EXCLUDED.metadata,
                    created_by_user_id = COALESCE(
                        occupancy_observations.created_by_user_id,
                        EXCLUDED.created_by_user_id
                    )
                RETURNING observation_id
                """
            ),
            {
                "zone_id": zone_id,
                "camera_id": zone_camera_id,
                "partner_id": zone_partner_id,
                "source_type": body.source_type,
                "source_ref": body.source_ref,
                "capacity": capacity,
                "occupied": occupied,
                "confidence": confidence,
                "confidence_level": confidence_level,
                "observed_at": body.observed_at,
                "ingested_at": now,
                "metadata": json.dumps(body.metadata),
                "created_by_user_id": current_user_id,
            },
        )

        observation_id = result.scalar_one()

        if _is_newer_or_same_observation(body.observed_at, zone_occupancy_updated_at):
            db.execute(
                text(
                    """
                    UPDATE parking_zones
                    SET
                        occupied = :occupied,
                        confidence = :confidence,
                        confidence_level = CAST(:confidence_level AS confidence_level_types),
                        occupancy_updated_at = :occupancy_updated_at,
                        updated_at = NOW()
                    WHERE parking_zone_id = :zone_id
                    """
                ),
                {
                    "zone_id": zone_id,
                    "occupied": occupied,
                    "confidence": confidence,
                    "confidence_level": confidence_level,
                    "occupancy_updated_at": _to_utc_naive(body.observed_at),
                },
            )

        db.commit()

    except IntegrityError as exc:
        db.rollback()
        orig = getattr(exc, "orig", None)
        diag = getattr(orig, "diag", None)
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_description": "Occupancy payload could not be saved",
                "error_type": "IntegrityError",
                "pg_error": str(orig),
                "constraint": getattr(diag, "constraint_name", None),
                "table": getattr(diag, "table_name", None),
                "column": getattr(diag, "column_name", None),
            },
        )

    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_description": "Occupancy payload could not be saved",
                "error_type": exc.__class__.__name__,
            },
        )

    return {"observation_id": observation_id}


# ---------------------------------------------------------------------------
# GET /occupancy/{observation_id}
# ---------------------------------------------------------------------------

@router.get("/{observation_id}", response_model=OccupancyObservationResponse)
def get_observation(
    observation_id: int,
    current_user:   Annotated[User, require("occupancy.view")],
    db:             Annotated[Session, Depends(get_db)],
):
    return _serialize_obs(_get_obs_or_404(db, observation_id), db)


# ---------------------------------------------------------------------------
# PUT /occupancy/{observation_id}
# ---------------------------------------------------------------------------

@router.put("/{observation_id}", response_model=OccupancyObservationResponse)
def update_observation(
    observation_id: int,
    body:           UpdateOccupancyRequest,
    current_user:   Annotated[User, require("occupancy.write")],
    db:             Annotated[Session, Depends(get_db)],
):
    obs = _get_obs_or_404(db, observation_id)

    new_capacity = body.capacity if body.capacity is not None else obs.capacity
    new_occupied = body.occupied if body.occupied is not None else obs.occupied

    if new_occupied > new_capacity:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail={"error_description": "Validation error: occupied must be between 0 and capacity"})

    if body.capacity is not None:
        obs.capacity = body.capacity
    if body.occupied is not None:
        obs.occupied = body.occupied
    if body.confidence is not None:
        obs.confidence = body.confidence
        obs.confidence_level = _confidence_level(body.confidence)
    if body.observed_at is not None:
        obs.observed_at = body.observed_at
    if body.source_ref is not None:
        obs.source_ref = body.source_ref
    if body.metadata is not None:
        obs.metadata_json = body.metadata

    db.commit()
    db.refresh(obs)
    return _serialize_obs(obs, db)


# ---------------------------------------------------------------------------
# DELETE /occupancy/{observation_id}
# ---------------------------------------------------------------------------

@router.delete("/{observation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_observation(
    observation_id: int,
    current_user:   Annotated[User, require("occupancy.delete")],
    db:             Annotated[Session, Depends(get_db)],
):
    obs = _get_obs_or_404(db, observation_id)
    db.delete(obs)
    db.commit()
    return None
