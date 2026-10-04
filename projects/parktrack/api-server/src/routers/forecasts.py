from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, text, true
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.orm import Session, aliased

from ..database import get_db
from ..db_models import ConfidenceLevel, Forecast, ParkingZone, User
from ..dependencies import require
from ..schemas.forecasts import (
    CreateForecastRequest,
    ForecastMapItem,
    ForecastPointResponse,
    ForecastSeriesPoint,
    UpdateForecastRequest,
)

router = APIRouter(prefix="/forecasts", tags=["Forecasts"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _predicted_free_count(f: Forecast) -> int:
    # The database column is generated from the same expression. Computing it
    # from the already loaded values avoids one extra SELECT per forecast.
    return f.capacity - f.predicted_occupied


def _confidence_level(confidence: float) -> ConfidenceLevel | None:
    if confidence >= 0.85:
        return ConfidenceLevel.high
    elif confidence >= 0.65:
        return ConfidenceLevel.medium
    elif confidence >= 0.40:
        return ConfidenceLevel.low
    else:
        return ConfidenceLevel.very_low


def _serialize(f: Forecast) -> ForecastPointResponse:
    return ForecastPointResponse(
        forecast_id=f.forecast_id,
        zone_id=f.zone_id,
        camera_id=f.camera_id,
        partner_id=f.partner_id,
        model_type=f.model_type,
        model_version=f.model_version,
        generated_at=f.generated_at,
        predicted_for=f.predicted_for,
        capacity=f.capacity,
        predicted_occupied=f.predicted_occupied,
        predicted_free_count=_predicted_free_count(f),
        probability_free_space=f.probability_free_space,
        confidence=f.confidence,
        confidence_level=f.confidence_level.value if f.confidence_level else None,
        metadata=f.metadata_json,
        created_by_user_id=f.created_by_user_id,
    )


def _get_forecast_or_404(db: Session, forecast_id: int) -> Forecast:
    f = db.query(Forecast).filter(Forecast.forecast_id == forecast_id).one_or_none()
    if f is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"error_description": "Forecast not found"})
    return f


def _parse_bbox(bbox: str) -> tuple[float, float, float, float]:
    try:
        min_lon, min_lat, max_lon, max_lat = map(float, bbox.split(","))
        return min_lon, min_lat, max_lon, max_lat
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail={"error_description": "bbox must be min_lon,min_lat,max_lon,max_lat"})


def _apply_forecast_filters(
    query,
    forecast,
    *,
    camera_id: int | None,
    partner_id: int | None,
    model_type: str | None,
    generated_from: datetime | None,
    generated_to: datetime | None,
    from_: datetime | None,
    to: datetime | None,
):
    if camera_id is not None:
        query = query.filter(forecast.camera_id == camera_id)
    if partner_id is not None:
        query = query.filter(forecast.partner_id == partner_id)
    if model_type is not None:
        query = query.filter(forecast.model_type == model_type)
    if generated_from is not None:
        query = query.filter(forecast.generated_at >= generated_from)
    if generated_to is not None:
        query = query.filter(forecast.generated_at <= generated_to)
    if from_ is not None:
        query = query.filter(forecast.predicted_for >= from_)
    if to is not None:
        query = query.filter(forecast.predicted_for <= to)
    return query


def _latest_generation_query(
    *,
    db: Session,
    zone_id: int | None,
    camera_id: int | None,
    partner_id: int | None,
    model_type: str | None,
    generated_from: datetime | None,
    generated_to: datetime | None,
    from_: datetime | None,
    to: datetime | None,
    bbox: tuple[float, float, float, float] | None,
    is_active: bool | None,
):
    """
    Return only points from the latest matching generation in each zone.

    Starting from the much smaller parking_zones table lets PostgreSQL perform
    one bounded index lookup per zone instead of grouping or sorting the entire
    forecasts table. The uq_forecast_point index starts with
    (zone_id, generated_at), so ORDER BY generated_at DESC LIMIT 1 is an
    index-backed lookup.
    """
    zones = select(ParkingZone.parking_zone_id.label("zone_id"))

    if zone_id is not None:
        zones = zones.filter(ParkingZone.parking_zone_id == zone_id)
    if is_active is not None:
        zones = zones.filter(ParkingZone.is_active == is_active)
    if bbox is not None:
        min_lon, min_lat, max_lon, max_lat = bbox
        zones = zones.filter(
            ParkingZone.centroid_longitude >= min_lon,
            ParkingZone.centroid_longitude <= max_lon,
            ParkingZone.centroid_latitude >= min_lat,
            ParkingZone.centroid_latitude <= max_lat,
        )

    matching_zones = zones.subquery("matching_zones")
    generation_candidate = aliased(Forecast, name="generation_candidate")
    latest_generation = select(
        generation_candidate.generated_at.label("generated_at")
    ).filter(
        generation_candidate.zone_id == matching_zones.c.zone_id
    )
    latest_generation = _apply_forecast_filters(
        latest_generation,
        generation_candidate,
        camera_id=camera_id,
        partner_id=partner_id,
        model_type=model_type,
        generated_from=generated_from,
        generated_to=generated_to,
        from_=from_,
        to=to,
    )
    latest_generation = (
        latest_generation
        .order_by(generation_candidate.generated_at.desc())
        .limit(1)
        .lateral("latest_generation")
    )

    query = (
        db.query(Forecast)
        .select_from(matching_zones)
        .join(latest_generation, true())
        .join(
            Forecast,
            (Forecast.zone_id == matching_zones.c.zone_id)
            & (Forecast.generated_at == latest_generation.c.generated_at),
        )
    )
    return _apply_forecast_filters(
        query,
        Forecast,
        camera_id=camera_id,
        partner_id=partner_id,
        model_type=model_type,
        generated_from=generated_from,
        generated_to=generated_to,
        from_=from_,
        to=to,
    )


def _map_forecasts_statement(
    *,
    at: datetime,
    zone_id: int | None,
    camera_id: int | None,
    partner_id: int | None,
    model_type: str | None,
    generated_from: datetime | None,
    generated_to: datetime | None,
    from_: datetime | None,
    to: datetime | None,
    bbox: tuple[float, float, float, float] | None,
    is_active: bool | None,
    latest_model_only: bool,
) -> tuple[TextClause, dict[str, object]]:
    """
    Build the map query around bounded index lookups.

    For every matching zone the two lateral branches read at most one candidate:
    the nearest timestamp before ``at`` and the nearest timestamp at/after it.
    The outer lateral query applies the same tie-breakers as the former global
    ROW_NUMBER query. The composite index created by migration 000018 supports
    these lookups by ``zone_id`` and ``predicted_for``.
    """
    params: dict[str, object] = {"at": at}
    forecast_filters = ["f.zone_id = z.parking_zone_id"]
    zone_filters: list[str] = []

    optional_forecast_filters = (
        ("camera_id", camera_id, "f.camera_id = :camera_id"),
        ("partner_id", partner_id, "f.partner_id = :partner_id"),
        ("model_type", model_type, "f.model_type = :model_type"),
        ("generated_from", generated_from, "f.generated_at >= :generated_from"),
        ("generated_to", generated_to, "f.generated_at <= :generated_to"),
        ("from_", from_, "f.predicted_for >= :from_"),
        ("to", to, "f.predicted_for <= :to"),
    )

    for name, value, clause in optional_forecast_filters:
        if value is not None:
            params[name] = value
            forecast_filters.append(clause)

    if zone_id is not None:
        params["zone_id"] = zone_id
        zone_filters.append("z.parking_zone_id = :zone_id")

    if is_active is True:
        zone_filters.append("z.is_active IS TRUE")
    elif is_active is False:
        zone_filters.append("z.is_active IS FALSE")

    if bbox is not None:
        min_lon, min_lat, max_lon, max_lat = bbox
        params.update(
            {
                "min_lon": min_lon,
                "min_lat": min_lat,
                "max_lon": max_lon,
                "max_lat": max_lat,
            }
        )
        zone_filters.extend(
            (
                "z.centroid_longitude >= :min_lon",
                "z.centroid_longitude <= :max_lon",
                "z.centroid_latitude >= :min_lat",
                "z.centroid_latitude <= :max_lat",
            )
        )

    latest_generation_join = ""
    if latest_model_only:
        latest_filters = [
            clause.replace("f.", "generation_candidate.")
            for clause in forecast_filters
        ]
        latest_where = "\n                AND ".join(latest_filters)
        latest_generation_join = f"""
        CROSS JOIN LATERAL (
            SELECT generation_candidate.generated_at
            FROM forecasts AS generation_candidate
            WHERE {latest_where}
            ORDER BY generation_candidate.generated_at DESC
            LIMIT 1
        ) AS latest_generation
        """
        forecast_filters.append(
            "f.generated_at = latest_generation.generated_at"
        )

    forecast_where = "\n                    AND ".join(forecast_filters)
    zone_where = "\n        AND ".join(zone_filters) if zone_filters else "TRUE"

    statement = text(
        f"""
        SELECT
            z.parking_zone_id AS zone_id,
            f.camera_id,
            f.capacity,
            f.predicted_occupied,
            f.predicted_free_count,
            f.probability_free_space,
            f.confidence,
            CAST(f.confidence_level AS TEXT) AS confidence_level,
            f.predicted_for,
            f.generated_at,
            z.geometry,
            z.pay,
            CAST(z.zone_type AS TEXT) AS zone_type,
            CAST(z.location_type AS TEXT) AS location_type,
            z.is_accessible,
            z.is_active
        FROM parking_zones AS z
        {latest_generation_join}
        CROSS JOIN LATERAL (
            SELECT candidate.forecast_id
            FROM (
                (
                    SELECT
                        f.forecast_id,
                        f.predicted_for,
                        f.generated_at
                    FROM forecasts AS f
                    WHERE {forecast_where}
                      AND f.predicted_for >= :at
                    ORDER BY
                        f.predicted_for ASC,
                        f.generated_at DESC,
                        f.forecast_id DESC
                    LIMIT 1
                )
                UNION ALL
                (
                    SELECT
                        f.forecast_id,
                        f.predicted_for,
                        f.generated_at
                    FROM forecasts AS f
                    WHERE {forecast_where}
                      AND f.predicted_for < :at
                    ORDER BY
                        f.predicted_for DESC,
                        f.generated_at DESC,
                        f.forecast_id DESC
                    LIMIT 1
                )
            ) AS candidate
            ORDER BY
                ABS(EXTRACT(EPOCH FROM candidate.predicted_for - :at)) ASC,
                candidate.generated_at DESC,
                candidate.forecast_id DESC
            LIMIT 1
        ) AS nearest
        JOIN forecasts AS f ON f.forecast_id = nearest.forecast_id
        WHERE {zone_where}
        ORDER BY f.predicted_for ASC, z.parking_zone_id ASC
        """
    )

    return statement, params


def _list_map_forecasts(
    *,
    db: Session,
    at: datetime,
    zone_id: int | None,
    camera_id: int | None,
    partner_id: int | None,
    model_type: str | None,
    generated_from: datetime | None,
    generated_to: datetime | None,
    from_: datetime | None,
    to: datetime | None,
    bbox: str | None,
    is_active: bool | None,
    latest_model_only: bool,
) -> list[ForecastMapItem]:
    bbox_bounds = _parse_bbox(bbox) if bbox is not None else None
    statement, params = _map_forecasts_statement(
        at=at,
        zone_id=zone_id,
        camera_id=camera_id,
        partner_id=partner_id,
        model_type=model_type,
        generated_from=generated_from,
        generated_to=generated_to,
        from_=from_,
        to=to,
        bbox=bbox_bounds,
        is_active=is_active,
        latest_model_only=latest_model_only,
    )
    rows = db.execute(statement, params).mappings().all()

    return [ForecastMapItem(**row) for row in rows]


# ---------------------------------------------------------------------------
# GET /forecasts
# ---------------------------------------------------------------------------

@router.get(
    "",
    response_model=list[ForecastPointResponse]
                 | list[ForecastSeriesPoint]
                 | list[ForecastMapItem],
)
def list_forecasts(
    # 2026-05-16: открыто без авторизации (по запросу) — режим «Будущее» на карте.
    db:                Annotated[Session, Depends(get_db)],
    zone_id:           int    | None = None,
    camera_id:         int    | None = None,
    partner_id:        int    | None = None,
    model_type:        str    | None = None,
    generated_from:    datetime | None = None,
    generated_to:      datetime | None = None,
    from_:             datetime | None = Query(None, alias="from"),
    to:                datetime | None = None,
    at:                datetime | None = None,
    latest_model_only: bool          = False,
    bbox:              str    | None = None,
    is_active:         bool   | None = None,
    view:              str           = "points",
):
    if view == "map" and at is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail={"error_description": "Parameter 'at' is required for view=map"})

    if view == "map":
        return _list_map_forecasts(
            db=db,
            at=at,
            zone_id=zone_id,
            camera_id=camera_id,
            partner_id=partner_id,
            model_type=model_type,
            generated_from=generated_from,
            generated_to=generated_to,
            from_=from_,
            to=to,
            bbox=bbox,
            is_active=is_active,
            latest_model_only=latest_model_only,
        )

    bbox_bounds = _parse_bbox(bbox) if bbox is not None else None

    if latest_model_only:
        query = _latest_generation_query(
            db=db,
            zone_id=zone_id,
            camera_id=camera_id,
            partner_id=partner_id,
            model_type=model_type,
            generated_from=generated_from,
            generated_to=generated_to,
            from_=from_,
            to=to,
            bbox=bbox_bounds,
            is_active=is_active,
        )
    else:
        query = db.query(Forecast)
        if zone_id is not None:
            query = query.filter(Forecast.zone_id == zone_id)
        query = _apply_forecast_filters(
            query,
            Forecast,
            camera_id=camera_id,
            partner_id=partner_id,
            model_type=model_type,
            generated_from=generated_from,
            generated_to=generated_to,
            from_=from_,
            to=to,
        )

    if not latest_model_only and (is_active is not None or bbox_bounds is not None):
        query = query.join(
            ParkingZone,
            ParkingZone.parking_zone_id == Forecast.zone_id,
        )
        if is_active is not None:
            query = query.filter(ParkingZone.is_active == is_active)
        if bbox_bounds is not None:
            min_lon, min_lat, max_lon, max_lat = bbox_bounds
            query = query.filter(
                ParkingZone.centroid_longitude >= min_lon,
                ParkingZone.centroid_longitude <= max_lon,
                ParkingZone.centroid_latitude >= min_lat,
                ParkingZone.centroid_latitude <= max_lat,
            )

    # Если указан at, независимо от view возвращаем по одному прогнозу на каждую зону.
    # Логика:
    # 1. выбираем predicted_for, ближайший к at;
    # 2. если для этого predicted_for есть несколько версий,
    #    берём самую позднюю по generated_at;
    # 3. forecast_id.desc() нужен как стабильный tie-breaker.
    if at is not None:
        time_distance = func.abs(
            func.extract("epoch", Forecast.predicted_for - at)
        )

        ranked_sq = (
            query.with_entities(
                Forecast.forecast_id.label("forecast_id"),
                func.row_number().over(
                    partition_by=Forecast.zone_id,
                    order_by=(
                        time_distance.asc(),
                        Forecast.generated_at.desc(),
                        Forecast.forecast_id.desc(),
                    ),
                ).label("rn"),
            )
            .subquery()
        )

        query = (
            db.query(Forecast)
            .join(ranked_sq, Forecast.forecast_id == ranked_sq.c.forecast_id)
            .filter(ranked_sq.c.rn == 1)
        )

    forecasts = query.order_by(Forecast.predicted_for.asc()).all()

    if view == "series":
        return [
            ForecastSeriesPoint(
                predicted_for=f.predicted_for,
                predicted_occupied=f.predicted_occupied,
                predicted_free_count=_predicted_free_count(f),
                capacity=f.capacity,
                probability_free_space=f.probability_free_space,
                confidence=f.confidence,
                confidence_level=f.confidence_level.value if f.confidence_level else None,
                model_type=f.model_type,
                generated_at=f.generated_at,
            )
            for f in forecasts
        ]

    # view=points (default)
    return [_serialize(f) for f in forecasts]


# ---------------------------------------------------------------------------
# POST /forecasts/new
# ---------------------------------------------------------------------------

@router.post("/new", status_code=status.HTTP_201_CREATED)
def create_forecast(
    body:         CreateForecastRequest,
    current_user: Annotated[User, require("forecasts.write")],
    db:           Annotated[Session, Depends(get_db)],
):
    zone = db.query(ParkingZone).filter(
        ParkingZone.parking_zone_id == body.zone_id
    ).one_or_none()
    if zone is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"error_description": "Zone not found"})

    capacity = body.capacity if body.capacity is not None else zone.capacity

    if body.predicted_occupied > capacity:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail={"error_description": "Validation error: predicted_occupied must be between 0 and capacity"})

    conflict = db.query(Forecast).filter_by(
        zone_id=body.zone_id,
        generated_at=body.generated_at,
        predicted_for=body.predicted_for,
    ).one_or_none()
    if conflict:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail={"error_description": "Forecast for this zone/generated_at/predicted_for already exists"})

    f = Forecast(
        zone_id=body.zone_id,
        camera_id=zone.camera_id,
        partner_id=zone.partner_id,
        model_type=body.model_type,
        model_version=body.model_version,
        generated_at=body.generated_at,
        predicted_for=body.predicted_for,
        capacity=capacity,
        predicted_occupied=body.predicted_occupied,
        probability_free_space=body.probability_free_space,
        confidence=body.confidence,
        confidence_level=_confidence_level(body.confidence),
        metadata_json=body.metadata,
        created_by_user_id=current_user.user_id,
    )
    db.add(f)
    db.commit()
    db.refresh(f)
    return {"forecast_id": f.forecast_id}


# ---------------------------------------------------------------------------
# GET /forecasts/{forecast_id}
# ---------------------------------------------------------------------------

@router.get("/{forecast_id}", response_model=ForecastPointResponse)
def get_forecast(
    forecast_id:  int,
    current_user: Annotated[User, require("forecasts.view")],
    db:           Annotated[Session, Depends(get_db)],
):
    return _serialize(_get_forecast_or_404(db, forecast_id))


# ---------------------------------------------------------------------------
# PUT /forecasts/{forecast_id}
# ---------------------------------------------------------------------------

@router.put("/{forecast_id}", response_model=ForecastPointResponse)
def update_forecast(
    forecast_id:  int,
    body:         UpdateForecastRequest,
    current_user: Annotated[User, require("forecasts.write")],
    db:           Annotated[Session, Depends(get_db)],
):
    f = _get_forecast_or_404(db, forecast_id)

    new_capacity = body.capacity if body.capacity is not None else f.capacity
    new_occupied = body.predicted_occupied if body.predicted_occupied is not None else f.predicted_occupied

    if new_occupied > new_capacity:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail={"error_description": "Validation error: predicted_occupied must be between 0 and capacity"})

    if body.model_version is not None:
        f.model_version = body.model_version
    if body.generated_at is not None:
        f.generated_at = body.generated_at
    if body.predicted_for is not None:
        f.predicted_for = body.predicted_for
    if body.predicted_occupied is not None:
        f.predicted_occupied = body.predicted_occupied
    if body.capacity is not None:
        f.capacity = body.capacity
    if body.probability_free_space is not None:
        f.probability_free_space = body.probability_free_space
    if body.confidence is not None:
        f.confidence = body.confidence
        f.confidence_level = _confidence_level(body.confidence)
    if body.metadata is not None:
        f.metadata_json = body.metadata

    db.commit()
    db.refresh(f)
    return _serialize(f)


# ---------------------------------------------------------------------------
# DELETE /forecasts/{forecast_id}
# ---------------------------------------------------------------------------

@router.delete("/{forecast_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_forecast(
    forecast_id:  int,
    current_user: Annotated[User, require("forecasts.delete")],
    db:           Annotated[Session, Depends(get_db)],
):
    f = _get_forecast_or_404(db, forecast_id)
    db.delete(f)
    db.commit()
    return None
