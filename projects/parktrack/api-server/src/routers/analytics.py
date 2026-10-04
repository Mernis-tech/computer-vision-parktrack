from __future__ import annotations
from urllib.parse import urlencode

from collections import defaultdict
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import bindparam, or_, text
from sqlalchemy.orm import Session

from ..database import get_db
from ..db_models import (
    Camera,
    DetectionFeedback as DBDetectionFeedback,
    Forecast,
    GlobalRole,
    OccupancyObservation,
    ParkingZone,
    User,
)
from ..dependencies import is_api_token_authenticated, require
from ..schemas.analytics import (
    AnalyticsSummary,
    ConfidencePoint,
    ConfidenceResponse,
    CreateDetectionFeedbackRequest,
    CreateDetectionFeedbackResponse,
    DetectionFeedback,
    DetectionFeedbackListResponse,
    DetectionRun,
    DetectionRunListResponse,
    DetectorHealthResponse,
    DetectorHealthRow,
    ForecastQualityMetrics,
    ForecastQualityPoint,
    ForecastQualityResponse,
    ObservationsRatePoint,
    ObservationsRateResponse,
    OccupancyForecastPoint,
    OccupancyForecastResponse,
    OccupancyHistoryPoint,
    OccupancyHistoryResponse,
    UpdateFrequencyResponse,
    UpdateFrequencyZone,
)

router = APIRouter(prefix="/admin/analytics", tags=["Admin Analytics"])

GRANULARITY_SECONDS: dict[str, int] = {
    "5m": 5 * 60,
    "15m": 15 * 60,
    "1h": 60 * 60,
    "1d": 24 * 60 * 60,
}
GRANULARITY_ORDER = ("5m", "15m", "1h", "1d")
DEFAULT_GRANULARITY = "1h"
MAX_SERIES_POINTS = 1000

LOW_CONFIDENCE_THRESHOLD = 0.40
ONLINE_MAX_AGE_SEC = 5 * 60
STALE_MAX_AGE_SEC = 30 * 60

DETECTION_STATUSES = {"success", "failed", "partial"}
DETECTOR_HEALTH_STATUSES = {
    "online",
    "stale",
    "offline",
    "no_data",
    "low_confidence",
    "error",
}


# ---------------------------------------------------------------------------
# Access helpers
# ---------------------------------------------------------------------------

def _is_admin(user: User) -> bool:
    return is_api_token_authenticated(user) or user.global_role == GlobalRole.admin


def _active_partner_ids(user: User) -> set[int]:
    return {
        membership.partner_id
        for membership in user.memberships
        if getattr(membership.partner, "is_active", True)
    }


def _ensure_admin(user: User, message: str = "Endpoint is available only to administrators") -> None:
    if not _is_admin(user):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"error_description": message},
        )


def _ensure_partner_filter_allowed(user: User, partner_id: int | None) -> None:
    if partner_id is not None and not _is_admin(user):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"error_description": "partner_id filter is available only to administrators"},
        )


def _get_camera_or_404(db: Session, camera_id: int) -> Camera:
    camera = db.query(Camera).filter(Camera.camera_id == camera_id).one_or_none()
    if camera is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error_description": "Camera not found"},
        )
    return camera


def _get_zone_or_404(db: Session, zone_id: int) -> ParkingZone:
    zone = db.query(ParkingZone).filter(ParkingZone.parking_zone_id == zone_id).one_or_none()
    if zone is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error_description": "Zone not found"},
        )
    return zone


def _ensure_camera_visible(camera: Camera, user: User) -> None:
    if _is_admin(user):
        return
    if camera.partner_id not in _active_partner_ids(user):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"error_description": "Access to this camera is forbidden"},
        )


def _ensure_zone_visible(zone: ParkingZone, user: User) -> None:
    if _is_admin(user):
        return
    if zone.partner_id not in _active_partner_ids(user):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"error_description": "Access to this zone is forbidden"},
        )


def _validate_common_filters(
    db: Session,
    user: User,
    partner_id: int | None = None,
    zone_id: int | None = None,
    camera_id: int | None = None,
) -> None:
    _ensure_partner_filter_allowed(user, partner_id)

    camera: Camera | None = None
    zone: ParkingZone | None = None

    if camera_id is not None:
        camera = _get_camera_or_404(db, camera_id)
        _ensure_camera_visible(camera, user)

    if zone_id is not None:
        zone = _get_zone_or_404(db, zone_id)
        _ensure_zone_visible(zone, user)

    if camera is not None and zone is not None and zone.camera_id != camera.camera_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"error_description": "zone_id does not belong to camera_id"},
        )

    if partner_id is not None and camera is not None and camera.partner_id != partner_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"error_description": "camera_id does not belong to partner_id"},
        )

    if partner_id is not None and zone is not None and zone.partner_id != partner_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"error_description": "zone_id does not belong to partner_id"},
        )


def _apply_partner_scope(query, model, user: User, partner_id: int | None):
    if _is_admin(user):
        if partner_id is not None:
            return query.filter(model.partner_id == partner_id)
        return query

    partner_ids = _active_partner_ids(user)
    if not partner_ids:
        return query.filter(model.partner_id.in_([]))
    return query.filter(model.partner_id.in_(partner_ids))


def _apply_observation_partner_scope(
    db: Session,
    query,
    user: User,
    partner_id: int | None,
):
    if _is_admin(user):
        if partner_id is None:
            return query
        zone_ids = db.query(ParkingZone.parking_zone_id).filter(ParkingZone.partner_id == partner_id)
        camera_ids = db.query(Camera.camera_id).filter(Camera.partner_id == partner_id)
        return query.filter(or_(
            OccupancyObservation.partner_id == partner_id,
            OccupancyObservation.zone_id.in_(zone_ids),
            OccupancyObservation.camera_id.in_(camera_ids),
        ))

    partner_ids = _active_partner_ids(user)
    if not partner_ids:
        return query.filter(OccupancyObservation.partner_id.in_([]))

    zone_ids = db.query(ParkingZone.parking_zone_id).filter(ParkingZone.partner_id.in_(partner_ids))
    camera_ids = db.query(Camera.camera_id).filter(Camera.partner_id.in_(partner_ids))
    return query.filter(or_(
        OccupancyObservation.partner_id.in_(partner_ids),
        OccupancyObservation.zone_id.in_(zone_ids),
        OccupancyObservation.camera_id.in_(camera_ids),
    ))


def _apply_forecast_partner_scope(
    db: Session,
    query,
    user: User,
    partner_id: int | None,
):
    if _is_admin(user):
        if partner_id is None:
            return query
        zone_ids = db.query(ParkingZone.parking_zone_id).filter(ParkingZone.partner_id == partner_id)
        camera_ids = db.query(Camera.camera_id).filter(Camera.partner_id == partner_id)
        return query.filter(or_(
            Forecast.partner_id == partner_id,
            Forecast.zone_id.in_(zone_ids),
            Forecast.camera_id.in_(camera_ids),
        ))

    partner_ids = _active_partner_ids(user)
    if not partner_ids:
        return query.filter(Forecast.partner_id.in_([]))

    zone_ids = db.query(ParkingZone.parking_zone_id).filter(ParkingZone.partner_id.in_(partner_ids))
    camera_ids = db.query(Camera.camera_id).filter(Camera.partner_id.in_(partner_ids))
    return query.filter(or_(
        Forecast.partner_id.in_(partner_ids),
        Forecast.zone_id.in_(zone_ids),
        Forecast.camera_id.in_(camera_ids),
    ))


def _zone_query(
    db: Session,
    user: User,
    partner_id: int | None = None,
    zone_id: int | None = None,
    camera_id: int | None = None,
    active_only: bool = True,
):
    query = db.query(ParkingZone)
    query = _apply_partner_scope(query, ParkingZone, user, partner_id)

    if active_only:
        query = query.filter(ParkingZone.is_active.is_(True))
    if zone_id is not None:
        query = query.filter(ParkingZone.parking_zone_id == zone_id)
    if camera_id is not None:
        query = query.filter(ParkingZone.camera_id == camera_id)

    return query


def _observation_query(
    db: Session,
    user: User,
    from_: datetime,
    to: datetime,
    partner_id: int | None = None,
    zone_id: int | None = None,
    camera_id: int | None = None,
):
    query = (
        db.query(OccupancyObservation)
        .filter(OccupancyObservation.observed_at >= from_)
        .filter(OccupancyObservation.observed_at <= to)
    )
    query = _apply_observation_partner_scope(db, query, user, partner_id)

    if zone_id is not None:
        query = query.filter(OccupancyObservation.zone_id == zone_id)
    if camera_id is not None:
        query = query.filter(OccupancyObservation.camera_id == camera_id)

    return query


def _forecast_query(
    db: Session,
    user: User,
    from_: datetime,
    to: datetime,
    partner_id: int | None = None,
    zone_id: int | None = None,
    camera_id: int | None = None,
    forecast_created_at: datetime | None = None,
):
    query = (
        db.query(Forecast)
        .filter(Forecast.predicted_for >= from_)
        .filter(Forecast.predicted_for <= to)
    )
    query = _apply_forecast_partner_scope(db, query, user, partner_id)

    if zone_id is not None:
        query = query.filter(Forecast.zone_id == zone_id)
    if camera_id is not None:
        query = query.filter(Forecast.camera_id == camera_id)
    if forecast_created_at is not None:
        query = query.filter(Forecast.generated_at == forecast_created_at)

    return query


def _execute_sql(
    db: Session,
    sql: str,
    params: dict[str, Any],
    expanding_params: set[str] | None = None,
):
    statement = text(sql)
    for name in expanding_params or set():
        statement = statement.bindparams(bindparam(name, expanding=True))
    return db.execute(statement, params)


def _append_partner_scope_sql(
    clauses: list[str],
    params: dict[str, Any],
    expanding_params: set[str],
    user: User,
    partner_id: int | None,
    partner_columns: tuple[str, ...],
) -> None:
    if _is_admin(user):
        if partner_id is not None:
            params["partner_id"] = partner_id
            clauses.append(
                "(" + " OR ".join(f"{column} = :partner_id" for column in partner_columns) + ")"
            )
        return

    partner_ids = sorted(_active_partner_ids(user))
    if not partner_ids:
        clauses.append("FALSE")
        return

    params["partner_ids"] = partner_ids
    expanding_params.add("partner_ids")
    clauses.append(
        "(" + " OR ".join(f"{column} IN :partner_ids" for column in partner_columns) + ")"
    )


def _zone_where_sql(
    user: User,
    partner_id: int | None,
    zone_id: int | None,
    camera_id: int | None,
    active_only: bool,
    params: dict[str, Any],
    expanding_params: set[str],
) -> str:
    clauses: list[str] = []

    if active_only:
        clauses.append("pz.is_active IS TRUE")
    if zone_id is not None:
        params["zone_id"] = zone_id
        clauses.append("pz.parking_zone_id = :zone_id")
    if camera_id is not None:
        params["camera_id"] = camera_id
        clauses.append("pz.camera_id = :camera_id")

    _append_partner_scope_sql(
        clauses,
        params,
        expanding_params,
        user,
        partner_id,
        ("pz.partner_id", "c.partner_id"),
    )

    return " AND ".join(clauses) if clauses else "TRUE"


def _observation_where_sql(
    user: User,
    partner_id: int | None,
    zone_id: int | None,
    camera_id: int | None,
    active_only: bool,
    params: dict[str, Any],
    expanding_params: set[str],
) -> str:
    clauses = [
        "oo.observed_at >= :from_",
        "oo.observed_at <= :to",
    ]

    if active_only:
        clauses.append("pz.is_active IS TRUE")
    if zone_id is not None:
        params["zone_id"] = zone_id
        clauses.append("oo.zone_id = :zone_id")
    if camera_id is not None:
        params["camera_id"] = camera_id
        clauses.append("COALESCE(oo.camera_id, pz.camera_id) = :camera_id")

    _append_partner_scope_sql(
        clauses,
        params,
        expanding_params,
        user,
        partner_id,
        ("oo.partner_id", "pz.partner_id", "c.partner_id"),
    )

    return " AND ".join(clauses)


def _forecast_where_sql(
    user: User,
    partner_id: int | None,
    zone_id: int | None,
    camera_id: int | None,
    forecast_created_at: datetime | None,
    active_only: bool,
    params: dict[str, Any],
    expanding_params: set[str],
) -> str:
    clauses = [
        "f.predicted_for >= :from_",
        "f.predicted_for <= :to",
    ]

    if active_only:
        clauses.append("pz.is_active IS TRUE")
    if zone_id is not None:
        params["zone_id"] = zone_id
        clauses.append("f.zone_id = :zone_id")
    if camera_id is not None:
        params["camera_id"] = camera_id
        clauses.append("COALESCE(f.camera_id, pz.camera_id) = :camera_id")
    if forecast_created_at is not None:
        params["forecast_created_at"] = forecast_created_at
        clauses.append("f.generated_at = :forecast_created_at")

    _append_partner_scope_sql(
        clauses,
        params,
        expanding_params,
        user,
        partner_id,
        ("f.partner_id", "pz.partner_id", "c.partner_id"),
    )

    return " AND ".join(clauses)


def _bucket_sql(column: str = "observed_at") -> str:
    return (
        "to_timestamp("
        f"floor(extract(epoch FROM {column}) / :bucket_seconds) * :bucket_seconds"
        ")"
    )


def _as_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _as_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _get_detection_or_404(db: Session, detection_run_id: int) -> OccupancyObservation:
    detection = db.query(OccupancyObservation).filter(
        OccupancyObservation.observation_id == detection_run_id
    ).one_or_none()
    if detection is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error_description": "Detection run not found"},
        )
    return detection


def _ensure_detection_visible(db: Session, detection: OccupancyObservation, user: User) -> None:
    if _is_admin(user):
        return

    partner_ids = _active_partner_ids(user)
    if detection.partner_id in partner_ids:
        return

    if detection.zone_id is not None:
        zone = db.query(ParkingZone).filter(
            ParkingZone.parking_zone_id == detection.zone_id
        ).one_or_none()
        if zone is not None and zone.partner_id in partner_ids:
            return

    if detection.camera_id is not None:
        camera = db.query(Camera).filter(Camera.camera_id == detection.camera_id).one_or_none()
        if camera is not None and camera.partner_id in partner_ids:
            return

    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        detail={"error_description": "Access to this detection run is forbidden"},
    )


def _available_artifact_url(
    metadata: dict[str, Any],
    variant: str,
) -> str | None:
    snapshots = metadata.get("snapshots")
    if not isinstance(snapshots, dict):
        return None

    artifact = snapshots.get(variant)
    if not isinstance(artifact, dict):
        return None

    url = artifact.get("url")
    if not isinstance(url, str) or not url.strip():
        return None

    return url


# ---------------------------------------------------------------------------
# Time and aggregation helpers
# ---------------------------------------------------------------------------

def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_metadata_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _to_utc(value)
    if not isinstance(value, str):
        return None
    try:
        return _to_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _validate_period(from_: datetime, to: datetime) -> tuple[datetime, datetime]:
    from_ = _to_utc(from_)
    to = _to_utc(to)
    if from_ > to:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"error_description": "Parameter 'from' must be before or equal to 'to'"},
        )
    return from_, to


def _normalize_granularity(granularity: str | None, from_: datetime, to: datetime) -> str:
    requested = granularity or DEFAULT_GRANULARITY
    if requested not in GRANULARITY_SECONDS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_description": "granularity must be one of: 5m, 15m, 1h, 1d"},
        )

    span_seconds = max(0.0, (_to_utc(to) - _to_utc(from_)).total_seconds())
    start_index = GRANULARITY_ORDER.index(requested)

    for candidate in GRANULARITY_ORDER[start_index:]:
        if span_seconds / GRANULARITY_SECONDS[candidate] <= MAX_SERIES_POINTS:
            return candidate

    return "1d"


def _bucket_start(value: datetime, granularity: str) -> datetime:
    value = _to_utc(value)

    if granularity == "5m":
        minute = (value.minute // 5) * 5
        return value.replace(minute=minute, second=0, microsecond=0)

    if granularity == "15m":
        minute = (value.minute // 15) * 15
        return value.replace(minute=minute, second=0, microsecond=0)

    if granularity == "1h":
        return value.replace(minute=0, second=0, microsecond=0)

    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def _avg(values: list[float | int | None]) -> float | None:
    numbers = [float(value) for value in values if value is not None]
    if not numbers:
        return None
    return round(sum(numbers) / len(numbers), 2)


def _avg_int(values: list[float | int | None]) -> int | None:
    value = _avg(values)
    if value is None:
        return None
    return int(round(value))


def _occupancy_percent(occupied: float | int | None, capacity: float | int | None) -> float | None:
    if occupied is None or capacity is None or capacity <= 0:
        return None
    return round(float(occupied) / float(capacity) * 100.0, 2)


def _free_count(capacity: int | None, occupied: int | None) -> int | None:
    if capacity is None or occupied is None:
        return None
    return max(capacity - occupied, 0)


def _metadata(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _metadata_value(metadata: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = metadata.get(key)
        if value is not None:
            return value
    return None


def _to_int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _observation_sort_key(obs: OccupancyObservation) -> tuple[datetime, datetime, int]:
    return (
        _to_utc(obs.observed_at),
        _to_utc(obs.ingested_at) if obs.ingested_at else datetime.min.replace(tzinfo=timezone.utc),
        obs.observation_id,
    )


def _latest_observation(observations: list[OccupancyObservation]) -> OccupancyObservation | None:
    if not observations:
        return None
    return max(observations, key=_observation_sort_key)


def _intervals_for_observations(observations: list[OccupancyObservation]) -> list[float]:
    ordered = sorted(observations, key=_observation_sort_key)
    intervals: list[float] = []
    for previous, current in zip(ordered, ordered[1:]):
        seconds = (_to_utc(current.observed_at) - _to_utc(previous.observed_at)).total_seconds()
        if seconds >= 0:
            intervals.append(seconds)
    return intervals


def _interval_stats(observations: list[OccupancyObservation]) -> tuple[float | None, float | None]:
    intervals = _intervals_for_observations(observations)
    if not intervals:
        return None, None
    return round(sum(intervals) / len(intervals), 2), round(max(intervals), 2)


def _group_observations_by_zone(
    observations: list[OccupancyObservation],
) -> dict[int, list[OccupancyObservation]]:
    grouped: dict[int, list[OccupancyObservation]] = defaultdict(list)
    for obs in observations:
        grouped[obs.zone_id].append(obs)
    return grouped


def _zone_camera_map(db: Session, zone_ids: set[int]) -> dict[int, int]:
    if not zone_ids:
        return {}
    rows = db.query(ParkingZone.parking_zone_id, ParkingZone.camera_id).filter(
        ParkingZone.parking_zone_id.in_(zone_ids)
    ).all()
    return {zone_id: camera_id for zone_id, camera_id in rows}


def _camera_title_map(db: Session, camera_ids: set[int]) -> dict[int, str]:
    if not camera_ids:
        return {}
    rows = db.query(Camera.camera_id, Camera.title).filter(Camera.camera_id.in_(camera_ids)).all()
    return {camera_id: title for camera_id, title in rows}


def _feedback_run_ids(db: Session, detection_run_ids: list[int]) -> set[int]:
    if not detection_run_ids:
        return set()
    rows = db.query(DBDetectionFeedback.detection_run_id).filter(
        DBDetectionFeedback.detection_run_id.in_(detection_run_ids)
    ).distinct().all()
    return {row[0] for row in rows}


def _latest_forecasts(forecasts: list[Forecast]) -> list[Forecast]:
    latest: dict[tuple[int, datetime], Forecast] = {}
    for forecast in forecasts:
        key = (forecast.zone_id, _to_utc(forecast.predicted_for))
        current = latest.get(key)
        if current is None:
            latest[key] = forecast
            continue
        if (
            _to_utc(forecast.generated_at),
            forecast.forecast_id,
        ) > (
            _to_utc(current.generated_at),
            current.forecast_id,
        ):
            latest[key] = forecast
    return list(latest.values())


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _detection_status(metadata: dict[str, Any]) -> str:
    raw_status = _metadata_value(metadata, "status", "detection_status")
    if isinstance(raw_status, str) and raw_status in DETECTION_STATUSES:
        return raw_status

    if _metadata_value(metadata, "error_code", "error_message", "error") is not None:
        return "failed"

    return "success"


def _serialize_detection_run(
    db: Session,
    observation: OccupancyObservation,
    feedback_ids: set[int],
    zone_camera_ids: dict[int, int] | None = None,
) -> DetectionRun:
    zone_camera_ids = zone_camera_ids or {}
    metadata = _metadata(observation.metadata_json)
    camera_id = observation.camera_id or zone_camera_ids.get(observation.zone_id)

    if camera_id is None and observation.zone_id is not None:
        zone = db.query(ParkingZone).filter(
            ParkingZone.parking_zone_id == observation.zone_id
        ).one_or_none()
        camera_id = zone.camera_id if zone else None

    if camera_id is None:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error_description": "Detection run has no camera_id"},
        )

    started_at = (
        _parse_metadata_datetime(_metadata_value(metadata, "started_at", "detection_started_at"))
        or _to_utc(observation.observed_at)
    )
    finished_at = (
        _parse_metadata_datetime(_metadata_value(metadata, "finished_at", "detection_finished_at"))
        or (_to_utc(observation.ingested_at) if observation.ingested_at else None)
    )

    return DetectionRun(
        detection_run_id=observation.observation_id,
        camera_id=camera_id,
        zone_id=observation.zone_id,
        started_at=started_at,
        finished_at=finished_at,
        status=_detection_status(metadata),
        processing_time_ms=_to_int_or_none(
            _metadata_value(metadata, "processing_time_ms", "processing_ms", "duration_ms")
        ),
        model_version=_to_str_or_none(
            _metadata_value(metadata, "model_version", "detector_model_version")
        ),
        detected_cars_count=_to_int_or_none(
            _metadata_value(metadata, "detected_cars_count", "cars_count", "detections_count")
        ),
        occupied_count=observation.occupied,
        free_count=_free_count(observation.capacity, observation.occupied),
        capacity=observation.capacity,
        confidence_avg=observation.confidence,
        error_code=_to_str_or_none(_metadata_value(metadata, "error_code")),
        error_message=_to_str_or_none(_metadata_value(metadata, "error_message", "error")),
        has_feedback=observation.observation_id in feedback_ids,
        raw_snapshot_url=_available_artifact_url(metadata, "raw"),
        annotated_snapshot_url=_available_artifact_url(metadata, "annotated"),
        yolo_labels_url=_available_artifact_url(metadata, "labels"),
    )


def _serialize_feedback(feedback: DBDetectionFeedback, email: str | None = None) -> DetectionFeedback:
    return DetectionFeedback(
        feedback_id=feedback.feedback_id,
        detection_run_id=feedback.detection_run_id,
        created_by_user_id=feedback.created_by_user_id,
        created_by_email=email,
        rating=feedback.rating,
        expected_occupied_count=feedback.expected_occupied_count,
        expected_free_count=feedback.expected_free_count,
        error_type=feedback.error_type,
        comment=feedback.comment,
        created_at=feedback.created_at,
        updated_at=feedback.updated_at,
    )


def _detector_status(
    last_update_at: datetime | None,
    confidence: float | None,
    latest_metadata: dict[str, Any],
    now: datetime,
) -> str:
    if _detection_status(latest_metadata) == "failed":
        return "error"

    if last_update_at is None:
        return "no_data"

    if confidence is not None and confidence < LOW_CONFIDENCE_THRESHOLD:
        return "low_confidence"

    age_seconds = max(0.0, (_to_utc(now) - _to_utc(last_update_at)).total_seconds())
    if age_seconds <= ONLINE_MAX_AGE_SEC:
        return "online"
    if age_seconds <= STALE_MAX_AGE_SEC:
        return "stale"
    return "offline"


# ---------------------------------------------------------------------------
# GET /admin/analytics/summary
# ---------------------------------------------------------------------------

@router.get("/summary", response_model=AnalyticsSummary)
def get_analytics_summary(
    current_user: Annotated[User, require("analytics.view")],
    db: Annotated[Session, Depends(get_db)],
    from_: datetime = Query(..., alias="from"),
    to: datetime = Query(...),
    partner_id: int | None = None,
    zone_id: int | None = None,
    camera_id: int | None = None,
):
    from_, to = _validate_period(from_, to)
    _validate_common_filters(db, current_user, partner_id, zone_id, camera_id)

    params: dict[str, Any] = {"from_": from_, "to": to}
    expanding_params: set[str] = set()
    zone_where = _zone_where_sql(
        current_user,
        partner_id,
        zone_id,
        camera_id,
        active_only=True,
        params=params,
        expanding_params=expanding_params,
    )
    observation_where = _observation_where_sql(
        current_user,
        partner_id,
        zone_id,
        camera_id,
        active_only=True,
        params=params,
        expanding_params=expanding_params,
    )

    row = _execute_sql(
        db,
        f"""
        WITH scoped_zones AS (
            SELECT
                pz.parking_zone_id AS zone_id,
                pz.capacity,
                pz.occupied,
                pz.occupancy_updated_at
            FROM parking_zones pz
            LEFT JOIN cameras c ON c.camera_id = pz.camera_id
            WHERE {zone_where}
        ),
        scoped_obs AS (
            SELECT
                oo.zone_id,
                oo.observation_id,
                oo.capacity,
                oo.occupied,
                oo.confidence,
                oo.observed_at,
                oo.ingested_at
            FROM occupancy_observations oo
            JOIN parking_zones pz ON pz.parking_zone_id = oo.zone_id
            LEFT JOIN cameras c ON c.camera_id = COALESCE(oo.camera_id, pz.camera_id)
            WHERE {observation_where}
        ),
        latest_by_zone AS (
            SELECT DISTINCT ON (zone_id)
                zone_id,
                observed_at
            FROM scoped_obs
            ORDER BY zone_id, observed_at DESC, ingested_at DESC, observation_id DESC
        ),
        intervals AS (
            SELECT
                EXTRACT(EPOCH FROM (
                    observed_at - LAG(observed_at) OVER (
                        PARTITION BY zone_id
                        ORDER BY observed_at, ingested_at, observation_id
                    )
                )) AS interval_sec
            FROM scoped_obs
        ),
        zone_updates AS (
            SELECT
                sz.zone_id,
                COALESCE(lbz.observed_at, sz.occupancy_updated_at) AS last_update_at
            FROM scoped_zones sz
            LEFT JOIN latest_by_zone lbz ON lbz.zone_id = sz.zone_id
        )
        SELECT
            COUNT(sz.zone_id)::integer AS active_zones_count,
            COALESCE(SUM(sz.capacity), 0)::integer AS total_capacity,
            CASE WHEN COUNT(sz.zone_id) = 0 THEN NULL ELSE SUM(sz.occupied)::integer END AS current_occupied_count,
            CASE
                WHEN COUNT(sz.zone_id) = 0 THEN NULL
                ELSE SUM(GREATEST(sz.capacity - sz.occupied, 0))::integer
            END AS current_free_count,
            (
                SELECT ROUND(AVG(
                    CASE
                        WHEN so.capacity > 0 THEN so.occupied::double precision / so.capacity * 100.0
                        ELSE NULL
                    END
                )::numeric, 2)::double precision
                FROM scoped_obs so
            ) AS avg_occupancy_percent,
            (SELECT MAX(last_update_at) FROM zone_updates) AS freshest_update_at,
            (SELECT MIN(last_update_at) FROM zone_updates WHERE last_update_at IS NOT NULL) AS oldest_update_at,
            (
                SELECT ROUND(AVG(interval_sec)::numeric, 2)::double precision
                FROM intervals
                WHERE interval_sec IS NOT NULL AND interval_sec >= 0
            ) AS avg_update_interval_sec,
            (
                SELECT ROUND(MAX(interval_sec)::numeric, 2)::double precision
                FROM intervals
                WHERE interval_sec IS NOT NULL AND interval_sec >= 0
            ) AS max_update_interval_sec,
            (
                SELECT ROUND(AVG(confidence)::numeric, 2)::double precision
                FROM scoped_obs
            ) AS avg_confidence
        FROM scoped_zones sz
        """,
        params,
        expanding_params,
    ).one()
    data = row._mapping

    return AnalyticsSummary(
        active_zones_count=data["active_zones_count"],
        total_capacity=data["total_capacity"],
        current_occupied_count=data["current_occupied_count"],
        current_free_count=data["current_free_count"],
        avg_occupancy_percent=_as_float(data["avg_occupancy_percent"]),
        freshest_update_at=data["freshest_update_at"],
        oldest_update_at=data["oldest_update_at"],
        avg_update_interval_sec=_as_float(data["avg_update_interval_sec"]),
        max_update_interval_sec=_as_float(data["max_update_interval_sec"]),
        avg_confidence=_as_float(data["avg_confidence"]),
    )


# ---------------------------------------------------------------------------
# GET /admin/analytics/occupancy-history
# ---------------------------------------------------------------------------

@router.get("/occupancy-history", response_model=OccupancyHistoryResponse)
def get_occupancy_history(
    current_user: Annotated[User, require("analytics.view")],
    db: Annotated[Session, Depends(get_db)],
    from_: datetime = Query(..., alias="from"),
    to: datetime = Query(...),
    partner_id: int | None = None,
    zone_id: int | None = None,
    camera_id: int | None = None,
    granularity: str | None = None,
):
    from_, to = _validate_period(from_, to)
    _validate_common_filters(db, current_user, partner_id, zone_id, camera_id)
    actual_granularity = _normalize_granularity(granularity, from_, to)

    params: dict[str, Any] = {
        "from_": from_,
        "to": to,
        "bucket_seconds": GRANULARITY_SECONDS[actual_granularity],
    }
    expanding_params: set[str] = set()
    observation_where = _observation_where_sql(
        current_user,
        partner_id,
        zone_id,
        camera_id,
        active_only=False,
        params=params,
        expanding_params=expanding_params,
    )
    bucket_expr = _bucket_sql("oo.observed_at")

    rows = _execute_sql(
        db,
        f"""
        SELECT
            {bucket_expr} AS timestamp,
            oo.zone_id AS zone_id,
            COALESCE(oo.camera_id, pz.camera_id) AS camera_id,
            ROUND(AVG(oo.occupied))::integer AS occupied_count,
            GREATEST(MAX(oo.capacity) - ROUND(AVG(oo.occupied))::integer, 0)::integer AS free_count,
            MAX(oo.capacity)::integer AS capacity,
            ROUND(AVG(
                CASE
                    WHEN oo.capacity > 0 THEN oo.occupied::double precision / oo.capacity * 100.0
                    ELSE NULL
                END
            )::numeric, 2)::double precision AS occupancy_percent,
            ROUND(AVG(oo.confidence)::numeric, 2)::double precision AS confidence_avg,
            COUNT(*)::integer AS observations_count
        FROM occupancy_observations oo
        JOIN parking_zones pz ON pz.parking_zone_id = oo.zone_id
        LEFT JOIN cameras c ON c.camera_id = COALESCE(oo.camera_id, pz.camera_id)
        WHERE {observation_where}
        GROUP BY timestamp, oo.zone_id, COALESCE(oo.camera_id, pz.camera_id)
        ORDER BY timestamp ASC, oo.zone_id ASC, COALESCE(oo.camera_id, pz.camera_id) ASC
        """,
        params,
        expanding_params,
    ).all()

    points = []
    for row in rows:
        data = row._mapping
        points.append(OccupancyHistoryPoint(
            timestamp=data["timestamp"],
            zone_id=data["zone_id"],
            camera_id=data["camera_id"],
            occupied_count=data["occupied_count"],
            free_count=data["free_count"],
            capacity=data["capacity"],
            occupancy_percent=_as_float(data["occupancy_percent"]),
            confidence_avg=_as_float(data["confidence_avg"]),
            observations_count=data["observations_count"],
        ))

    return OccupancyHistoryResponse(granularity=actual_granularity, points=points)


# ---------------------------------------------------------------------------
# GET /admin/analytics/occupancy-forecast
# ---------------------------------------------------------------------------

@router.get("/occupancy-forecast", response_model=OccupancyForecastResponse)
def get_occupancy_forecast(
    current_user: Annotated[User, require("analytics.view")],
    db: Annotated[Session, Depends(get_db)],
    zone_id: int,
    from_: datetime = Query(..., alias="from"),
    to: datetime = Query(...),
    partner_id: int | None = None,
    forecast_created_at: datetime | None = None,
):
    from_, to = _validate_period(from_, to)
    _validate_common_filters(db, current_user, partner_id, zone_id, None)

    params: dict[str, Any] = {"from_": from_, "to": to}
    expanding_params: set[str] = set()
    forecast_where = _forecast_where_sql(
        current_user,
        partner_id,
        zone_id,
        None,
        forecast_created_at,
        active_only=False,
        params=params,
        expanding_params=expanding_params,
    )
    selected_filter = "ranked.rn = 1" if forecast_created_at is None else "TRUE"

    rows = _execute_sql(
        db,
        f"""
        WITH ranked AS (
            SELECT
                f.forecast_id,
                f.zone_id,
                f.predicted_for,
                f.capacity,
                f.predicted_occupied,
                f.model_version,
                f.generated_at,
                ROW_NUMBER() OVER (
                    PARTITION BY f.zone_id, f.predicted_for
                    ORDER BY f.generated_at DESC, f.forecast_id DESC
                ) AS rn
            FROM forecasts f
            JOIN parking_zones pz ON pz.parking_zone_id = f.zone_id
            LEFT JOIN cameras c ON c.camera_id = COALESCE(f.camera_id, pz.camera_id)
            WHERE {forecast_where}
        )
        SELECT
            predicted_for AS timestamp,
            zone_id,
            predicted_occupied::double precision AS predicted_occupied_count,
            GREATEST(capacity - predicted_occupied, 0)::double precision AS predicted_free_count,
            ROUND(
                CASE
                    WHEN capacity > 0 THEN predicted_occupied::double precision / capacity * 100.0
                    ELSE NULL
                END::numeric,
                2
            )::double precision AS predicted_occupancy_percent,
            model_version,
            generated_at AS forecast_created_at
        FROM ranked
        WHERE {selected_filter}
        ORDER BY predicted_for ASC, forecast_id ASC
        """,
        params,
        expanding_params,
    ).all()

    if not rows:
        return OccupancyForecastResponse(
            available=False,
            reason="insufficient_history",
            points=[],
        )

    points = []
    for row in rows:
        data = row._mapping
        points.append(OccupancyForecastPoint(
            timestamp=data["timestamp"],
            zone_id=data["zone_id"],
            predicted_occupied_count=_as_float(data["predicted_occupied_count"]),
            predicted_free_count=_as_float(data["predicted_free_count"]),
            predicted_occupancy_percent=_as_float(data["predicted_occupancy_percent"]),
            model_version=data["model_version"],
            forecast_created_at=data["forecast_created_at"],
        ))

    return OccupancyForecastResponse(available=True, reason=None, points=points)


# ---------------------------------------------------------------------------
# GET /admin/analytics/observations-rate
# ---------------------------------------------------------------------------

@router.get("/observations-rate", response_model=ObservationsRateResponse)
def get_observations_rate(
    current_user: Annotated[User, require("analytics.view")],
    db: Annotated[Session, Depends(get_db)],
    from_: datetime = Query(..., alias="from"),
    to: datetime = Query(...),
    partner_id: int | None = None,
    zone_id: int | None = None,
    camera_id: int | None = None,
    granularity: str | None = None,
):
    from_, to = _validate_period(from_, to)
    _validate_common_filters(db, current_user, partner_id, zone_id, camera_id)
    actual_granularity = _normalize_granularity(granularity, from_, to)

    observations = _observation_query(
        db,
        current_user,
        from_,
        to,
        partner_id=partner_id,
        zone_id=zone_id,
        camera_id=camera_id,
    ).all()

    counts: dict[datetime, int] = defaultdict(int)
    for obs in observations:
        counts[_bucket_start(obs.observed_at, actual_granularity)] += 1

    points = [
        ObservationsRatePoint(timestamp=timestamp, observations_count=count)
        for timestamp, count in sorted(counts.items())
    ]
    return ObservationsRateResponse(granularity=actual_granularity, points=points)


# ---------------------------------------------------------------------------
# GET /admin/analytics/confidence
# ---------------------------------------------------------------------------

@router.get("/confidence", response_model=ConfidenceResponse)
def get_confidence_history(
    current_user: Annotated[User, require("analytics.view")],
    db: Annotated[Session, Depends(get_db)],
    from_: datetime = Query(..., alias="from"),
    to: datetime = Query(...),
    partner_id: int | None = None,
    zone_id: int | None = None,
    camera_id: int | None = None,
    granularity: str | None = None,
):
    from_, to = _validate_period(from_, to)
    _validate_common_filters(db, current_user, partner_id, zone_id, camera_id)
    actual_granularity = _normalize_granularity(granularity, from_, to)

    observations = _observation_query(
        db,
        current_user,
        from_,
        to,
        partner_id=partner_id,
        zone_id=zone_id,
        camera_id=camera_id,
    ).order_by(OccupancyObservation.observed_at.asc()).all()
    zone_camera_ids = _zone_camera_map(db, {obs.zone_id for obs in observations})

    groups: dict[tuple[datetime, int | None, int | None], list[OccupancyObservation]] = defaultdict(list)
    for obs in observations:
        timestamp = _bucket_start(obs.observed_at, actual_granularity)
        if zone_id is not None or camera_id is not None:
            groups[(timestamp, obs.zone_id, obs.camera_id or zone_camera_ids.get(obs.zone_id))].append(obs)
        else:
            groups[(timestamp, None, None)].append(obs)

    points: list[ConfidencePoint] = []
    for (timestamp, grouped_zone_id, grouped_camera_id), grouped_observations in sorted(groups.items()):
        values = [obs.confidence for obs in grouped_observations]
        points.append(ConfidencePoint(
            timestamp=timestamp,
            zone_id=grouped_zone_id,
            camera_id=grouped_camera_id,
            confidence_avg=_avg(values),
            confidence_min=round(min(values), 2) if values else None,
            confidence_max=round(max(values), 2) if values else None,
            observations_count=len(grouped_observations),
        ))

    return ConfidenceResponse(granularity=actual_granularity, points=points)


# ---------------------------------------------------------------------------
# GET /admin/analytics/update-frequency
# ---------------------------------------------------------------------------

@router.get("/update-frequency", response_model=UpdateFrequencyResponse)
def get_update_frequency(
    current_user: Annotated[User, require("analytics.view")],
    db: Annotated[Session, Depends(get_db)],
    from_: datetime = Query(..., alias="from"),
    to: datetime = Query(...),
    partner_id: int | None = None,
    zone_id: int | None = None,
    camera_id: int | None = None,
    granularity: str | None = None,
):
    from_, to = _validate_period(from_, to)
    _validate_common_filters(db, current_user, partner_id, zone_id, camera_id)
    if granularity is not None:
        _normalize_granularity(granularity, from_, to)

    zones = _zone_query(
        db,
        current_user,
        partner_id=partner_id,
        zone_id=zone_id,
        camera_id=camera_id,
        active_only=True,
    ).order_by(ParkingZone.parking_zone_id.asc()).all()
    observations = _observation_query(
        db,
        current_user,
        from_,
        to,
        partner_id=partner_id,
        zone_id=zone_id,
        camera_id=camera_id,
    ).all()
    observations_by_zone = _group_observations_by_zone(observations)

    all_intervals: list[float] = []
    by_zone: list[UpdateFrequencyZone] = []

    for zone in zones:
        zone_observations = observations_by_zone.get(zone.parking_zone_id, [])
        intervals = _intervals_for_observations(zone_observations)
        all_intervals.extend(intervals)

        latest = _latest_observation(zone_observations)
        last_update_at = latest.observed_at if latest is not None else zone.occupancy_updated_at
        by_zone.append(UpdateFrequencyZone(
            zone_id=zone.parking_zone_id,
            camera_id=zone.camera_id,
            avg_update_interval_sec=round(sum(intervals) / len(intervals), 2) if intervals else None,
            max_update_interval_sec=round(max(intervals), 2) if intervals else None,
            last_update_at=_to_utc(last_update_at) if last_update_at else None,
        ))

    update_times = [item.last_update_at for item in by_zone if item.last_update_at is not None]

    return UpdateFrequencyResponse(
        avg_update_interval_sec=round(sum(all_intervals) / len(all_intervals), 2) if all_intervals else None,
        max_update_interval_sec=round(max(all_intervals), 2) if all_intervals else None,
        freshest_update_at=max(update_times) if update_times else None,
        oldest_update_at=min(update_times) if update_times else None,
        by_zone=by_zone,
    )


# ---------------------------------------------------------------------------
# GET /admin/analytics/detector-health
# ---------------------------------------------------------------------------

@router.get("/detector-health", response_model=DetectorHealthResponse)
def get_detector_health(
    current_user: Annotated[User, require("analytics.view")],
    db: Annotated[Session, Depends(get_db)],
    from_: datetime = Query(..., alias="from"),
    to: datetime = Query(...),
    partner_id: int | None = None,
    zone_id: int | None = None,
    camera_id: int | None = None,
    status_: str | None = Query(None, alias="status"),
):
    from_, to = _validate_period(from_, to)
    _validate_common_filters(db, current_user, partner_id, zone_id, camera_id)

    if status_ is not None and status_ not in DETECTOR_HEALTH_STATUSES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_description": "status must be one of: online, stale, offline, no_data, low_confidence, error"},
        )

    zones = _zone_query(
        db,
        current_user,
        partner_id=partner_id,
        zone_id=zone_id,
        camera_id=camera_id,
        active_only=True,
    ).order_by(ParkingZone.parking_zone_id.asc()).all()
    observations = _observation_query(
        db,
        current_user,
        from_,
        to,
        partner_id=partner_id,
        zone_id=zone_id,
        camera_id=camera_id,
    ).all()
    observations_by_zone = _group_observations_by_zone(observations)
    camera_titles = _camera_title_map(db, {zone.camera_id for zone in zones})
    now = datetime.now(timezone.utc)

    items: list[DetectorHealthRow] = []

    for zone in zones:
        zone_observations = observations_by_zone.get(zone.parking_zone_id, [])
        latest = _latest_observation(zone_observations)
        avg_interval, max_interval = _interval_stats(zone_observations)

        occupied_count = latest.occupied if latest is not None else zone.occupied
        capacity = latest.capacity if latest is not None else zone.capacity
        confidence_avg = (
            _avg([obs.confidence for obs in zone_observations])
            if zone_observations
            else zone.confidence
        )
        last_update_at = latest.observed_at if latest is not None else zone.occupancy_updated_at
        latest_metadata = _metadata(latest.metadata_json) if latest is not None else {}
        item_status = _detector_status(
            _to_utc(last_update_at) if last_update_at else None,
            confidence_avg,
            latest_metadata,
            now,
        )

        if status_ is not None and item_status != status_:
            continue

        sec_ago = (
            int(max(0.0, (_to_utc(now) - _to_utc(last_update_at)).total_seconds()))
            if last_update_at is not None
            else None
        )
        items.append(DetectorHealthRow(
            zone_id=zone.parking_zone_id,
            camera_id=zone.camera_id,
            camera_title=camera_titles.get(zone.camera_id, ""),
            capacity=capacity,
            occupied_count=occupied_count,
            free_count=_free_count(capacity, occupied_count),
            occupancy_percent=_occupancy_percent(occupied_count, capacity),
            confidence_avg=confidence_avg,
            last_update_at=_to_utc(last_update_at) if last_update_at else None,
            sec_ago=sec_ago,
            avg_update_interval_sec=avg_interval,
            max_update_interval_sec=max_interval,
            status=item_status,
        ))

    return DetectorHealthResponse(items=items)


# ---------------------------------------------------------------------------
# GET /admin/analytics/cameras/{camera_id}/detections
# ---------------------------------------------------------------------------

@router.get(
    "/cameras/{camera_id}/detections",
    response_model=DetectionRunListResponse,
)
def list_camera_detections(
    camera_id: int,
    current_user: Annotated[User, require("analytics.view")],
    db: Annotated[Session, Depends(get_db)],
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
    limit: int = Query(20, ge=1, le=100),
    status_: str | None = Query(None, alias="status"),
):
    camera = _get_camera_or_404(db, camera_id)
    _ensure_camera_visible(camera, current_user)

    if status_ is not None and status_ not in DETECTION_STATUSES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_description": "status must be one of: success, failed, partial"},
        )

    if from_ is not None and to is not None:
        from_, to = _validate_period(from_, to)
    elif from_ is not None:
        from_ = _to_utc(from_)
    elif to is not None:
        to = _to_utc(to)

    query = db.query(OccupancyObservation).filter(OccupancyObservation.camera_id == camera_id)
    if from_ is not None:
        query = query.filter(OccupancyObservation.observed_at >= from_)
    if to is not None:
        query = query.filter(OccupancyObservation.observed_at <= to)

    observations = query.order_by(
        OccupancyObservation.observed_at.desc(),
        OccupancyObservation.observation_id.desc(),
    ).limit(limit * 3 if status_ else limit).all()

    if status_ is not None:
        observations = [
            obs for obs in observations
            if _detection_status(_metadata(obs.metadata_json)) == status_
        ][:limit]

    feedback_ids = _feedback_run_ids(db, [obs.observation_id for obs in observations])
    zone_camera_ids = _zone_camera_map(db, {obs.zone_id for obs in observations})

    return DetectionRunListResponse(
        items=[
            _serialize_detection_run(
                db,
                obs,
                feedback_ids,
                zone_camera_ids,
            )
            for obs in observations
        ]
    )


# ---------------------------------------------------------------------------
# GET /admin/analytics/detections/{detection_run_id}
# ---------------------------------------------------------------------------

@router.get("/detections/{detection_run_id}", response_model=DetectionRun)
def get_detection_run(
    detection_run_id: int,
    current_user: Annotated[User, require("analytics.view")],
    db: Annotated[Session, Depends(get_db)],
):
    detection = _get_detection_or_404(db, detection_run_id)
    _ensure_detection_visible(db, detection, current_user)

    feedback_ids = _feedback_run_ids(
        db,
        [detection.observation_id],
    )
    zone_camera_ids = _zone_camera_map(
        db,
        {detection.zone_id},
    )

    return _serialize_detection_run(
        db,
        detection,
        feedback_ids,
        zone_camera_ids,
    )


# ---------------------------------------------------------------------------
# POST /admin/analytics/detections/{detection_run_id}/feedback
# ---------------------------------------------------------------------------

@router.post(
    "/detections/{detection_run_id}/feedback",
    response_model=CreateDetectionFeedbackResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_detection_feedback(
    detection_run_id: int,
    body: CreateDetectionFeedbackRequest,
    current_user: Annotated[User, require("analytics.feedback.create")],
    db: Annotated[Session, Depends(get_db)],
):
    detection = _get_detection_or_404(db, detection_run_id)
    _ensure_detection_visible(db, detection, current_user)

    feedback = DBDetectionFeedback(
        detection_run_id=detection_run_id,
        created_by_user_id=current_user.user_id,
        rating=body.rating,
        expected_occupied_count=body.expected_occupied_count,
        expected_free_count=body.expected_free_count,
        error_type=body.error_type,
        comment=body.comment,
    )
    db.add(feedback)
    db.commit()
    db.refresh(feedback)

    return CreateDetectionFeedbackResponse(
        feedback_id=feedback.feedback_id,
        detection_run_id=feedback.detection_run_id,
        created_at=feedback.created_at,
    )


# ---------------------------------------------------------------------------
# GET /admin/analytics/detections/{detection_run_id}/feedback
# ---------------------------------------------------------------------------

@router.get(
    "/detections/{detection_run_id}/feedback",
    response_model=DetectionFeedbackListResponse,
)
def list_detection_feedback(
    detection_run_id: int,
    current_user: Annotated[User, require("analytics.feedback.view")],
    db: Annotated[Session, Depends(get_db)],
):
    _ensure_admin(current_user, "Detection feedback is available only to administrators")
    _get_detection_or_404(db, detection_run_id)

    rows = (
        db.query(DBDetectionFeedback, User.email)
        .outerjoin(User, DBDetectionFeedback.created_by_user_id == User.user_id)
        .filter(DBDetectionFeedback.detection_run_id == detection_run_id)
        .order_by(DBDetectionFeedback.created_at.desc(), DBDetectionFeedback.feedback_id.desc())
        .all()
    )

    return DetectionFeedbackListResponse(
        items=[_serialize_feedback(feedback, email) for feedback, email in rows]
    )


# ---------------------------------------------------------------------------
# GET /admin/analytics/detections/{detection_run_id}/feedback/{feedback_id}
# ---------------------------------------------------------------------------

@router.get(
    "/detections/{detection_run_id}/feedback/{feedback_id}",
    response_model=DetectionFeedback,
)
def get_detection_feedback(
    detection_run_id: int,
    feedback_id: int,
    current_user: Annotated[User, require("analytics.feedback.view")],
    db: Annotated[Session, Depends(get_db)],
):
    _ensure_admin(current_user, "Detection feedback is available only to administrators")
    _get_detection_or_404(db, detection_run_id)

    row = (
        db.query(DBDetectionFeedback, User.email)
        .outerjoin(User, DBDetectionFeedback.created_by_user_id == User.user_id)
        .filter(DBDetectionFeedback.detection_run_id == detection_run_id)
        .filter(DBDetectionFeedback.feedback_id == feedback_id)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error_description": "Detection feedback not found"},
        )

    feedback, email = row
    return _serialize_feedback(feedback, email)


# ---------------------------------------------------------------------------
# GET /admin/analytics/forecast-quality
# ---------------------------------------------------------------------------

@router.get("/forecast-quality", response_model=ForecastQualityResponse)
def get_forecast_quality(
    current_user: Annotated[User, require("admin.forecasts.view")],
    db: Annotated[Session, Depends(get_db)],
    from_: datetime = Query(..., alias="from"),
    to: datetime = Query(...),
    partner_id: int | None = None,
    zone_id: int | None = None,
    camera_id: int | None = None,
    forecast_created_at: datetime | None = None,
    granularity: str | None = None,
):
    _ensure_admin(current_user, "Forecast quality is available only to administrators")
    from_, to = _validate_period(from_, to)
    _validate_common_filters(db, current_user, partner_id, zone_id, camera_id)
    actual_granularity = _normalize_granularity(granularity, from_, to)

    observations = _observation_query(
        db,
        current_user,
        from_,
        to,
        partner_id=partner_id,
        zone_id=zone_id,
        camera_id=camera_id,
    ).all()
    forecasts = _forecast_query(
        db,
        current_user,
        from_,
        to,
        partner_id=partner_id,
        zone_id=zone_id,
        camera_id=camera_id,
        forecast_created_at=forecast_created_at,
    ).all()

    if forecast_created_at is None:
        forecasts = _latest_forecasts(forecasts)

    actual_groups: dict[tuple[datetime, int], list[OccupancyObservation]] = defaultdict(list)
    for obs in observations:
        actual_groups[(_bucket_start(obs.observed_at, actual_granularity), obs.zone_id)].append(obs)

    forecast_groups: dict[tuple[datetime, int], list[Forecast]] = defaultdict(list)
    for forecast in forecasts:
        forecast_groups[(_bucket_start(forecast.predicted_for, actual_granularity), forecast.zone_id)].append(forecast)

    all_keys = sorted(set(actual_groups) | set(forecast_groups))
    points: list[ForecastQualityPoint] = []
    occupied_errors: list[float] = []
    percent_errors: list[float] = []
    percent_biases: list[float] = []

    for timestamp, grouped_zone_id in all_keys:
        actual_group = actual_groups.get((timestamp, grouped_zone_id), [])
        forecast_group = forecast_groups.get((timestamp, grouped_zone_id), [])

        actual_occupied = _avg_int([obs.occupied for obs in actual_group])
        actual_percent = _avg([
            _occupancy_percent(obs.occupied, obs.capacity)
            for obs in actual_group
        ])
        predicted_occupied = _avg([forecast.predicted_occupied for forecast in forecast_group])
        predicted_percent = _avg([
            _occupancy_percent(forecast.predicted_occupied, forecast.capacity)
            for forecast in forecast_group
        ])

        absolute_error_percent = None
        if actual_percent is not None and predicted_percent is not None:
            absolute_error_percent = round(abs(predicted_percent - actual_percent), 2)
            percent_errors.append(absolute_error_percent)
            percent_biases.append(round(predicted_percent - actual_percent, 2))

        if actual_occupied is not None and predicted_occupied is not None:
            occupied_errors.append(abs(predicted_occupied - actual_occupied))

        points.append(ForecastQualityPoint(
            timestamp=timestamp,
            zone_id=grouped_zone_id,
            actual_occupied_count=actual_occupied,
            actual_occupancy_percent=actual_percent,
            predicted_occupied_count=predicted_occupied,
            predicted_occupancy_percent=predicted_percent,
            absolute_error_occupancy_percent=absolute_error_percent,
        ))

    metrics = ForecastQualityMetrics(
        mae_occupied_count=_avg(occupied_errors),
        mae_occupancy_percent=_avg(percent_errors),
        bias_occupancy_percent=_avg(percent_biases),
        points_count=len(percent_errors),
    )

    return ForecastQualityResponse(
        granularity=actual_granularity,
        metrics=metrics,
        points=points,
    )
