from __future__ import annotations

import bisect
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, cast

import requests
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..database import get_db
from ..db_models import (
    Forecast,
    GlobalRole,
    ParkingZone,
    Route,
    RouteMode,
    RouteStatus,
    User,
)
from ..dependencies import require
from ..schemas.routing import (
    CreateRouteRequest,
    GeoPoint,
    RankingExplanation,
    RouteCandidate,
    RouteListResponse,
    RouteResponse,
    SearchRoutingRequest,
    SearchRoutingResponse,
    UpdateRouteRequest,
)

router = APIRouter(prefix="/routing", tags=["Routing"])


# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

GEOAPIFY_ROUTEMATRIX_URL = "https://api.geoapify.com/v1/routematrix"
GEOAPIFY_PROVIDER_NAME = "geoapify"
GEOAPIFY_MODE = "drive"

EARTH_RADIUS_METERS = 6_371_000
METERS_PER_LATITUDE_DEGREE = 111_320

MAX_CLUSTER_CONTEXT_TARGETS = 160
MIN_CLUSTER_CONTEXT_FOR_COMPARE = 48
PRIMARY_SEARCH_RADIUS_METERS = 5_000

CLUSTER_RADIUS_METERS = 500
GOOD_ALTERNATIVE_MIN_PROBABILITY = 0.35

WALKING_SPEED_METERS_PER_SECOND = 1.35
WALKING_DETOUR_FACTOR = 1.35

# После того как у парковки есть достаточный запас (два места), лишние места
# дают быстро убывающую пользу. Для маршрута к точке назначения важнее не
# заставлять человека идти несколько кварталов ради избыточного запаса мест.
SUFFICIENT_FREE_SPACES = 2
DESTINATION_WALK_COST_MULTIPLIER = 2.5
AVAILABILITY_BASE_COST_DISCOUNT = 0.20
CLUSTER_BASE_COST_DISCOUNT = 0.10
PROTECTED_DESTINATION_WALK_SECONDS = 2 * 60

FORECAST_LOOKAROUND = timedelta(hours=2)

PUBLIC_ROUTING_USER_ID_ENV = "PUBLIC_ROUTING_USER_ID"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _RoutingSettings:
    search_budget_seconds: float
    provider_connect_timeout_seconds: float
    provider_read_timeout_seconds: float
    provider_max_estimated_matrix_distance_meters: int
    max_matrix_targets: int
    road_detour_factor: float
    average_driving_speed_kph: float


def _env_float(name: str, default: float, minimum: float) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except ValueError:
        return default


def _env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _routing_settings() -> _RoutingSettings:
    return _RoutingSettings(
        search_budget_seconds=_env_float("ROUTING_SEARCH_BUDGET_SECONDS", 1.8, 0.1),
        provider_connect_timeout_seconds=_env_float(
            "ROUTING_PROVIDER_CONNECT_TIMEOUT_SECONDS", 0.25, 0.01
        ),
        provider_read_timeout_seconds=_env_float(
            "ROUTING_PROVIDER_READ_TIMEOUT_SECONDS", 0.9, 0.01
        ),
        provider_max_estimated_matrix_distance_meters=_env_int(
            "ROUTING_PROVIDER_MAX_ESTIMATED_MATRIX_DISTANCE_METERS",
            280_000_000,
            1,
        ),
        max_matrix_targets=_env_int("ROUTING_MAX_MATRIX_TARGETS", 32, 1),
        road_detour_factor=_env_float("ROUTING_ROAD_DETOUR_FACTOR", 1.25, 1.0),
        average_driving_speed_kph=_env_float(
            "ROUTING_AVERAGE_DRIVING_SPEED_KPH", 30.0, 1.0
        ),
    )


# ---------------------------------------------------------------------------
# Внутренние типы
# ---------------------------------------------------------------------------

class RoutingProviderError(Exception):
    def __init__(self, message: str, reason: str = "provider_error") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class _ZoneTarget:
    zone: ParkingZone
    point: GeoPoint
    anchor_distance_meters: int
    current_occupied: int
    current_free_count: int
    current_confidence: float


@dataclass(frozen=True)
class _RoutedCandidate:
    zone_target: _ZoneTarget
    distance_from_origin_meters: int
    duration_from_origin_seconds: int
    distance_to_destination_meters: int | None
    duration_to_destination_seconds: int | None
    arrival_time: datetime


@dataclass(frozen=True)
class _ForecastView:
    predicted_occupied: int | None
    predicted_free_count: int | None
    probability_free_space: float | None
    forecast_confidence: float | None


@dataclass(frozen=True)
class _ClusterMetrics:
    cluster_strength: float
    nearby_alternative_count: int
    nearby_good_alternative_count: int
    nearby_effective_free_count: int
    best_nearby_probability: float
    nearest_good_alternative_distance_meters: int | None


@dataclass
class _CandidateContext:
    candidate: RouteCandidate
    tier: int
    tier_label: str

    effective_free_count: int
    effective_confidence: float
    availability_probability: float
    availability_strength: float
    cluster_metrics: _ClusterMetrics

    base_cost_seconds: float
    generalized_cost_seconds: float

    price_penalty_seconds: float
    scarcity_penalty_seconds: float
    confidence_penalty_seconds: float
    cluster_bonus_seconds: float
    availability_bonus_seconds: float

    peer_better_availability_penalty_seconds: float = 0.0
    unreasonable_detour_penalty_seconds: float = 0.0


@dataclass(frozen=True)
class _CandidateSearchResult:
    candidates: list[RouteCandidate]
    total_candidates: int
    provider: str
    fallback_reason: str | None = None


@dataclass(frozen=True)
class _ForecastSeries:
    forecasts: list[Forecast]
    predicted_timestamps: list[float]


@dataclass
class _RankingRequestContext:
    forecasts_by_zone: dict[int, _ForecastSeries]
    cluster_neighbors: dict[int, list[tuple[_ZoneTarget, int]]]
    effective_state_cache: dict[
        tuple[int, int], tuple[int, float, float, _ForecastView]
    ]


# ---------------------------------------------------------------------------
# Общие helpers
# ---------------------------------------------------------------------------

def _enum_value(value: Any) -> str | None:
    if value is None:
        return None

    enum_value = getattr(value, "value", None)

    if enum_value is not None:
        return str(enum_value)

    return str(value)


def _to_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value

    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _datetime_timestamp(value: datetime | None) -> float:
    if value is None:
        return 0.0

    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        value = value.replace(tzinfo=timezone.utc)

    return value.timestamp()


def _seconds_between(a: datetime, b: datetime) -> float:
    return abs((_to_utc_naive(a) - _to_utc_naive(b)).total_seconds())


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp_float(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(value, max_value))


def _remaining_seconds(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _serialize_route(route: Route) -> RouteResponse:
    candidate: RouteCandidate | None = None

    if route.selected_candidate:
        candidate = RouteCandidate.model_validate(route.selected_candidate)

    destination: GeoPoint | None = None

    if route.destination_latitude is not None and route.destination_longitude is not None:
        destination = GeoPoint(
            latitude=route.destination_latitude,
            longitude=route.destination_longitude,
        )

    return RouteResponse(
        route_id=route.route_id,
        user_id=route.user_id,
        mode=_enum_value(route.mode) or str(route.mode),
        provider=route.provider,
        origin=GeoPoint(
            latitude=route.origin_latitude,
            longitude=route.origin_longitude,
        ),
        destination=destination,
        selected_zone_id=route.selected_zone_id,
        selected_candidate=candidate,
        eta_seconds=route.eta_seconds,
        arrival_time=route.arrival_time,
        polyline=route.polyline,
        deeplink_url=route.deeplink_url,
        status=_enum_value(route.status) or str(route.status),
        created_at=route.created_at,
        updated_at=route.updated_at,
    )


def _get_route_or_404(db: Session, route_id: int) -> Route:
    route = db.query(Route).filter(Route.route_id == route_id).one_or_none()

    if route is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error_description": "Route not found"},
        )

    return route


def _assert_owner_or_admin(route: Route, current_user: User) -> None:
    if current_user.global_role != GlobalRole.admin and route.user_id != current_user.user_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"error_description": "Access denied: not your route"},
        )


def _provider_unavailable(exc: RoutingProviderError) -> HTTPException:
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error_description": str(exc)},
    )


def _get_public_routing_user_id(db: Session) -> int:
    raw_user_id = os.getenv(PUBLIC_ROUTING_USER_ID_ENV)

    if raw_user_id:
        try:
            return int(raw_user_id)
        except ValueError:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error_description": f"{PUBLIC_ROUTING_USER_ID_ENV} must be integer"
                },
            )

    row = db.query(User.user_id).order_by(User.user_id.asc()).first()

    if row is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_description": (
                    "Cannot create public route: no users exist. "
                    f"Create a technical user or set {PUBLIC_ROUTING_USER_ID_ENV}."
                )
            },
        )

    return int(row[0])


def _haversine_meters(a: GeoPoint, b: GeoPoint) -> int:
    lat1 = math.radians(a.latitude)
    lat2 = math.radians(b.latitude)
    delta_lat = math.radians(b.latitude - a.latitude)
    delta_lon = math.radians(b.longitude - a.longitude)

    h = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )

    return int(2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(h)))


def _estimated_walking_seconds(distance_meters: int) -> int:
    return int(distance_meters * WALKING_DETOUR_FACTOR / WALKING_SPEED_METERS_PER_SECOND)


def _build_map_deeplink(destination: GeoPoint) -> str:
    return (
        "https://www.google.com/maps/dir/?api=1"
        f"&destination={destination.latitude},{destination.longitude}"
    )


# ---------------------------------------------------------------------------
# Центроид зоны
# ---------------------------------------------------------------------------

def _zone_centroid_from_geometry(zone: ParkingZone) -> GeoPoint | None:
    geometry = zone.geometry

    if not isinstance(geometry, dict):
        return None

    try:
        coords = list(geometry["coordinates"][0])

        if len(coords) > 1 and coords[0] == coords[-1]:
            coords = coords[:-1]

        if not coords:
            return None

        latitude = sum(float(point[1]) for point in coords) / len(coords)
        longitude = sum(float(point[0]) for point in coords) / len(coords)

        return GeoPoint(latitude=latitude, longitude=longitude)

    except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError):
        try:
            return GeoPoint(
                latitude=float(geometry["lat"]),
                longitude=float(geometry["lon"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _zone_point_from_centroid_columns(zone: ParkingZone) -> GeoPoint | None:
    latitude = getattr(zone, "centroid_latitude", None)
    longitude = getattr(zone, "centroid_longitude", None)

    if latitude is None or longitude is None:
        return _zone_centroid_from_geometry(zone)

    try:
        return GeoPoint(
            latitude=float(latitude),
            longitude=float(longitude),
        )
    except (TypeError, ValueError):
        return _zone_centroid_from_geometry(zone)


# ---------------------------------------------------------------------------
# Geoapify
# ---------------------------------------------------------------------------

def _geoapify_api_key() -> str:
    api_key = os.getenv("GEOAPIFY_API_KEY")

    if not api_key:
        raise RoutingProviderError("Geoapify API key is not configured")

    return api_key


def _geoapify_matrix(
    sources: list[GeoPoint],
    targets: list[GeoPoint],
    deadline: float,
    settings: _RoutingSettings,
) -> list[list[dict[str, Any]]]:
    if not sources or not targets:
        return []

    estimated_distance_sum = sum(
        _haversine_meters(source, target) * settings.road_detour_factor
        for source in sources
        for target in targets
    )
    if (
        estimated_distance_sum
        > settings.provider_max_estimated_matrix_distance_meters
    ):
        raise RoutingProviderError(
            "Estimated route matrix distance exceeds provider limit",
            reason="provider_matrix_distance_limit",
        )

    payload = {
        "mode": GEOAPIFY_MODE,
        "sources": [
            {"location": [point.longitude, point.latitude]}
            for point in sources
        ],
        "targets": [
            {"location": [point.longitude, point.latitude]}
            for point in targets
        ],
    }

    remaining = _remaining_seconds(deadline)
    if remaining <= 0.05:
        raise RoutingProviderError(
            "Routing budget exhausted before provider call",
            reason="insufficient_remaining_time",
        )

    connect_timeout = min(settings.provider_connect_timeout_seconds, remaining)
    read_budget = remaining - connect_timeout
    if read_budget <= 0.01:
        raise RoutingProviderError(
            "Routing budget is insufficient for provider response",
            reason="insufficient_remaining_time",
        )
    read_timeout = min(settings.provider_read_timeout_seconds, read_budget)

    try:
        response = requests.post(
            GEOAPIFY_ROUTEMATRIX_URL,
            params={"apiKey": _geoapify_api_key()},
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=(connect_timeout, read_timeout),
        )
    except requests.Timeout as exc:
        raise RoutingProviderError(
            "Geoapify Route Matrix API timed out",
            reason="provider_timeout",
        ) from exc
    except requests.RequestException as exc:
        raise RoutingProviderError(
            "Geoapify Route Matrix API is unavailable",
            reason="provider_network_error",
        ) from exc

    if response.status_code >= 500:
        raise RoutingProviderError(
            f"Geoapify Route Matrix API is unavailable: HTTP {response.status_code}",
            reason="provider_5xx",
        )

    if response.status_code >= 400:
        response_preview = response.text[:300]
        reason = "provider_429" if response.status_code == 429 else "provider_4xx"
        if (
            response.status_code == 400
            and "too long sum distance" in response_preview.lower()
        ):
            reason = "provider_matrix_distance_limit"
        raise RoutingProviderError(
            f"Geoapify Route Matrix API rejected request: "
            f"HTTP {response.status_code}: {response_preview}",
            reason=reason,
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise RoutingProviderError(
            "Geoapify returned invalid JSON",
            reason="invalid_provider_response",
        ) from exc

    matrix = data.get("sources_to_targets")

    if not isinstance(matrix, list):
        raise RoutingProviderError(
            "Geoapify response does not contain sources_to_targets",
            reason="invalid_provider_response",
        )

    return matrix


def _matrix_cell(
    matrix: list[list[dict[str, Any]]],
    source_index: int,
    target_index: int,
) -> tuple[int, int] | None:
    try:
        cell = matrix[source_index][target_index]
    except (IndexError, TypeError):
        return None

    if not isinstance(cell, dict):
        return None

    distance = cell.get("distance")
    duration = cell.get("time", cell.get("duration"))

    if distance is None or duration is None:
        return None

    try:
        return int(round(float(distance))), int(round(float(duration)))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# SQL radius search
# ---------------------------------------------------------------------------

def _longitude_bounds(longitude: float, delta: float) -> tuple[float, float, bool]:
    min_lon = longitude - delta
    max_lon = longitude + delta

    if min_lon < -180:
        return min_lon + 360, max_lon, True

    if max_lon > 180:
        return min_lon, max_lon - 360, True

    return min_lon, max_lon, False


def _radius_bounding_box(
    center: GeoPoint,
    radius_meters: int,
) -> tuple[float, float, float, float, bool]:
    lat_delta = math.degrees(radius_meters / EARTH_RADIUS_METERS)

    min_lat = max(-90.0, center.latitude - lat_delta)
    max_lat = min(90.0, center.latitude + lat_delta)

    cos_lat = abs(math.cos(math.radians(center.latitude)))

    if cos_lat < 1e-6:
        lon_delta = 180.0
    else:
        lon_delta = min(
            180.0,
            math.degrees(radius_meters / (EARTH_RADIUS_METERS * cos_lat)),
        )

    min_lon, max_lon, wraps = _longitude_bounds(center.longitude, lon_delta)

    return min_lat, max_lat, min_lon, max_lon, wraps


def _base_zone_query(
    db: Session,
    max_pay: int | None,
    include_accessible: bool | None,
    selected_zone_id: int | None,
):
    query = db.query(ParkingZone).filter(ParkingZone.is_active.is_(True))

    if selected_zone_id is not None:
        return query.filter(ParkingZone.parking_zone_id == selected_zone_id)

    query = query.filter(ParkingZone.centroid_latitude.is_not(None))
    query = query.filter(ParkingZone.centroid_longitude.is_not(None))

    if max_pay is not None:
        query = query.filter(ParkingZone.pay <= max_pay)

    if include_accessible is False:
        query = query.filter(
            or_(
                ParkingZone.is_accessible.is_(False),
                ParkingZone.is_accessible.is_(None),
            )
        )

    return query


def _apply_bounding_box_filter(
    query,
    center: GeoPoint,
    radius_meters: int,
):
    min_lat, max_lat, min_lon, max_lon, wraps = _radius_bounding_box(
        center=center,
        radius_meters=radius_meters,
    )

    query = query.filter(ParkingZone.centroid_latitude >= min_lat)
    query = query.filter(ParkingZone.centroid_latitude <= max_lat)

    if wraps:
        query = query.filter(
            or_(
                ParkingZone.centroid_longitude >= min_lon,
                ParkingZone.centroid_longitude <= max_lon,
            )
        )
    else:
        query = query.filter(ParkingZone.centroid_longitude >= min_lon)
        query = query.filter(ParkingZone.centroid_longitude <= max_lon)

    return query


def _approx_distance_expr(center: GeoPoint):
    cos_lat = abs(math.cos(math.radians(center.latitude)))

    latitude_meters = (
        (ParkingZone.centroid_latitude - center.latitude)
        * METERS_PER_LATITUDE_DEGREE
    )

    longitude_meters = (
        (ParkingZone.centroid_longitude - center.longitude)
        * METERS_PER_LATITUDE_DEGREE
        * cos_lat
    )

    return func.sqrt(
        func.power(latitude_meters, 2)
        + func.power(longitude_meters, 2)
    )


def _zone_to_target(zone: ParkingZone, anchor: GeoPoint) -> _ZoneTarget | None:
    point = _zone_point_from_centroid_columns(zone)

    if point is None:
        return None

    capacity = max(_safe_int(zone.capacity), 0)
    occupied = max(_safe_int(zone.occupied), 0)
    occupied = min(occupied, capacity)

    free_count = max(capacity - occupied, 0)
    confidence = _clamp_float(_safe_float(zone.confidence), 0.0, 1.0)

    return _ZoneTarget(
        zone=zone,
        point=point,
        anchor_distance_meters=_haversine_meters(anchor, point),
        current_occupied=occupied,
        current_free_count=free_count,
        current_confidence=confidence,
    )


def _matrix_target_count(limit: int, settings: _RoutingSettings) -> int:
    return min(settings.max_matrix_targets, max(12, limit * 4))


def _required_cluster_pool_size(limit: int, settings: _RoutingSettings) -> int:
    return min(
        MAX_CLUSTER_CONTEXT_TARGETS,
        max(
            MIN_CLUSTER_CONTEXT_FOR_COMPARE,
            _matrix_target_count(limit, settings) * 3,
        ),
    )


def _candidate_query_radii(
    mode: str,
    max_distance_to_destination_meters: int | None,
) -> list[int | None]:
    if mode == "route_to_destination" and max_distance_to_destination_meters is not None:
        primary = min(PRIMARY_SEARCH_RADIUS_METERS, max_distance_to_destination_meters)
        if primary == max_distance_to_destination_meters:
            return [primary]
        return [primary, max_distance_to_destination_meters]

    # Второй запрос — единственный расширенный fallback без искусственного
    # ограничения дальности, сохраняющий прежнюю семантику поиска.
    return [PRIMARY_SEARCH_RADIUS_METERS, None]


def _query_zone_targets_near_anchor(
    db: Session,
    anchor: GeoPoint,
    mode: str,
    max_pay: int | None,
    include_accessible: bool | None,
    max_distance_to_destination_meters: int | None,
    limit: int,
    selected_zone_id: int | None,
    deadline: float,
    settings: _RoutingSettings,
) -> list[_ZoneTarget]:
    required_count = _required_cluster_pool_size(limit, settings)

    if selected_zone_id is not None:
        zone = (
            _base_zone_query(
                db=db,
                max_pay=max_pay,
                include_accessible=include_accessible,
                selected_zone_id=selected_zone_id,
            )
            .one_or_none()
        )

        if zone is None:
            return []

        target = _zone_to_target(zone, anchor)

        return [target] if target is not None else []

    last_non_empty: list[_ZoneTarget] = []

    radii = _candidate_query_radii(
        mode=mode,
        max_distance_to_destination_meters=max_distance_to_destination_meters,
    )
    for query_number, radius in enumerate(radii, start=1):
        if query_number > 1 and _remaining_seconds(deadline) <= 0.05:
            break

        query = _base_zone_query(
            db=db,
            max_pay=max_pay,
            include_accessible=include_accessible,
            selected_zone_id=None,
        )

        if radius is not None:
            query = _apply_bounding_box_filter(
                query=query,
                center=anchor,
                radius_meters=radius,
            )

        zones = (
            query
            .order_by(_approx_distance_expr(anchor).asc())
            .limit(required_count)
            .all()
        )

        targets: list[_ZoneTarget] = []

        for zone in zones:
            target = _zone_to_target(zone, anchor)

            if target is None:
                continue

            if radius is None or target.anchor_distance_meters <= radius:
                targets.append(target)

        targets.sort(key=lambda item: item.anchor_distance_meters)

        if targets:
            last_non_empty = targets

        if len(targets) >= required_count:
            return targets[:required_count]

    return last_non_empty[:required_count]


def _matrix_preselection_key(target: _ZoneTarget) -> tuple[Any, ...]:
    distance_band = target.anchor_distance_meters // 500

    if target.current_free_count >= 3:
        availability_band = 0
    elif target.current_free_count >= 1:
        availability_band = 1
    else:
        availability_band = 2

    pay = max(_safe_int(target.zone.pay), 0)

    freshness = _datetime_timestamp(
        cast(datetime | None, getattr(target.zone, "occupancy_updated_at", None))
    )

    return (
        distance_band,
        availability_band,
        -target.current_confidence,
        -freshness,
        target.anchor_distance_meters,
        pay,
        int(target.zone.parking_zone_id),
    )


def _select_matrix_targets(
    cluster_pool: list[_ZoneTarget],
    requested_limit: int,
    settings: _RoutingSettings,
    selected_zone_id: int | None = None,
) -> list[_ZoneTarget]:
    target_count = _matrix_target_count(requested_limit, settings)
    selected = sorted(cluster_pool, key=_matrix_preselection_key)[:target_count]

    if selected_zone_id is None or any(
        int(item.zone.parking_zone_id) == selected_zone_id for item in selected
    ):
        return selected

    explicit = next(
        (
            item
            for item in cluster_pool
            if int(item.zone.parking_zone_id) == selected_zone_id
        ),
        None,
    )
    if explicit is None:
        return selected
    if len(selected) >= target_count:
        selected[-1] = explicit
    else:
        selected.append(explicit)
    return selected


# ---------------------------------------------------------------------------
# Forecast helpers
# ---------------------------------------------------------------------------

def _latest_forecasts_statement(
    zone_ids: list[int],
    from_time: datetime,
    to_time: datetime,
):
    ranked = (
        select(
            Forecast.forecast_id.label("forecast_id"),
            func.row_number().over(
                partition_by=(Forecast.zone_id, Forecast.predicted_for),
                order_by=(Forecast.generated_at.desc(), Forecast.forecast_id.desc()),
            ).label("generation_rank"),
        )
        .where(Forecast.zone_id.in_(zone_ids))
        .where(Forecast.predicted_for >= from_time)
        .where(Forecast.predicted_for <= to_time)
        .subquery()
    )

    return (
        select(Forecast)
        .join(ranked, Forecast.forecast_id == ranked.c.forecast_id)
        .where(ranked.c.generation_rank == 1)
        .order_by(Forecast.zone_id.asc(), Forecast.predicted_for.asc())
    )


def _load_forecasts_for_zones(
    db: Session,
    zone_ids: list[int],
    min_arrival: datetime,
    max_arrival: datetime,
) -> dict[int, _ForecastSeries]:
    if not zone_ids:
        return {}

    from_time = _to_utc_naive(min_arrival - FORECAST_LOOKAROUND)
    to_time = _to_utc_naive(max_arrival + FORECAST_LOOKAROUND)
    statement = _latest_forecasts_statement(zone_ids, from_time, to_time)
    rows = db.execute(statement).scalars().all()

    grouped: dict[int, list[Forecast]] = {}

    for forecast in rows:
        grouped.setdefault(int(forecast.zone_id), []).append(forecast)

    return {
        zone_id: _ForecastSeries(
            forecasts=forecasts,
            predicted_timestamps=[
                _datetime_timestamp(item.predicted_for) for item in forecasts
            ],
        )
        for zone_id, forecasts in grouped.items()
    }


def _pick_forecast_for_arrival(
    series: _ForecastSeries | None,
    arrival_time: datetime,
) -> Forecast | None:
    if series is None or not series.forecasts:
        return None

    arrival_timestamp = _datetime_timestamp(arrival_time)
    index = bisect.bisect_left(series.predicted_timestamps, arrival_timestamp)
    candidate_indexes = range(max(0, index - 1), min(len(series.forecasts), index + 1))

    return min(
        (series.forecasts[candidate_index] for candidate_index in candidate_indexes),
        key=lambda forecast: (
            _seconds_between(forecast.predicted_for, arrival_time),
            -_datetime_timestamp(forecast.generated_at),
            -int(forecast.forecast_id),
        ),
    )


def _forecast_view(
    zone_capacity: int,
    forecast: Forecast | None,
) -> _ForecastView:
    if forecast is None:
        return _ForecastView(
            predicted_occupied=None,
            predicted_free_count=None,
            probability_free_space=None,
            forecast_confidence=None,
        )

    forecast_capacity = max(_safe_int(forecast.capacity, zone_capacity), 0)
    predicted_occupied = max(_safe_int(forecast.predicted_occupied), 0)
    predicted_occupied = min(predicted_occupied, forecast_capacity)
    predicted_free_count = max(forecast_capacity - predicted_occupied, 0)

    probability_free_space = (
        _clamp_float(float(forecast.probability_free_space), 0.0, 1.0)
        if forecast.probability_free_space is not None
        else None
    )

    forecast_confidence = (
        _clamp_float(float(forecast.confidence), 0.0, 1.0)
        if forecast.confidence is not None
        else None
    )

    return _ForecastView(
        predicted_occupied=predicted_occupied,
        predicted_free_count=predicted_free_count,
        probability_free_space=probability_free_space,
        forecast_confidence=forecast_confidence,
    )


def _effective_free_count(
    current_free_count: int,
    forecast_view: _ForecastView,
    use_forecast: bool,
) -> int:
    if use_forecast and forecast_view.predicted_free_count is not None:
        return forecast_view.predicted_free_count

    return current_free_count


def _effective_confidence(
    current_confidence: float,
    forecast_view: _ForecastView,
    use_forecast: bool,
) -> float:
    if use_forecast and forecast_view.forecast_confidence is not None:
        return forecast_view.forecast_confidence

    return current_confidence


def _availability_probability(
    effective_free_count: int,
    forecast_view: _ForecastView,
    use_forecast: bool,
) -> float:
    if use_forecast and forecast_view.probability_free_space is not None:
        return forecast_view.probability_free_space

    if effective_free_count >= 5:
        return 0.92

    if effective_free_count >= 3:
        return 0.82

    if effective_free_count == 2:
        return 0.65

    if effective_free_count == 1:
        return 0.42

    return 0.04


# ---------------------------------------------------------------------------
# Cluster / fallback alternatives
# ---------------------------------------------------------------------------

def _build_cluster_neighbors(
    candidate_targets: list[_ZoneTarget],
    cluster_pool: list[_ZoneTarget],
) -> dict[int, list[tuple[_ZoneTarget, int]]]:
    if not candidate_targets or not cluster_pool:
        return {}

    reference_latitude = (
        sum(item.point.latitude for item in cluster_pool) / len(cluster_pool)
    )
    reference_longitude = cluster_pool[0].point.longitude
    longitude_scale = max(abs(math.cos(math.radians(reference_latitude))), 1e-6)

    def cell_for(target: _ZoneTarget) -> tuple[int, int]:
        longitude_delta = (
            (target.point.longitude - reference_longitude + 180.0) % 360.0
        ) - 180.0
        x = math.radians(longitude_delta) * EARTH_RADIUS_METERS * longitude_scale
        y = math.radians(target.point.latitude) * EARTH_RADIUS_METERS
        return (
            math.floor(x / CLUSTER_RADIUS_METERS),
            math.floor(y / CLUSTER_RADIUS_METERS),
        )

    grid: dict[tuple[int, int], list[_ZoneTarget]] = {}
    for target in cluster_pool:
        grid.setdefault(cell_for(target), []).append(target)

    result: dict[int, list[tuple[_ZoneTarget, int]]] = {}
    for candidate in candidate_targets:
        candidate_zone_id = int(candidate.zone.parking_zone_id)
        cell_x, cell_y = cell_for(candidate)
        neighbors: list[tuple[_ZoneTarget, int]] = []
        for delta_x in (-1, 0, 1):
            for delta_y in (-1, 0, 1):
                for alternative in grid.get((cell_x + delta_x, cell_y + delta_y), []):
                    if int(alternative.zone.parking_zone_id) == candidate_zone_id:
                        continue
                    distance = _haversine_meters(candidate.point, alternative.point)
                    if distance <= CLUSTER_RADIUS_METERS:
                        neighbors.append((alternative, distance))
        result[candidate_zone_id] = neighbors

    return result

def _effective_state_for_target_at_arrival(
    target: _ZoneTarget,
    arrival_time: datetime,
    request_context: _RankingRequestContext,
    use_forecast: bool,
) -> tuple[int, float, float, _ForecastView]:
    zone_id = int(target.zone.parking_zone_id)
    cache_key = (zone_id, int(_datetime_timestamp(arrival_time)))
    cached = request_context.effective_state_cache.get(cache_key)
    if cached is not None:
        return cached

    capacity = max(_safe_int(target.zone.capacity), 0)

    forecast = None

    if use_forecast:
        forecast = _pick_forecast_for_arrival(
            request_context.forecasts_by_zone.get(zone_id),
            arrival_time,
        )

    view = _forecast_view(
        zone_capacity=capacity,
        forecast=forecast,
    )

    effective_free = _effective_free_count(
        current_free_count=target.current_free_count,
        forecast_view=view,
        use_forecast=use_forecast,
    )

    effective_confidence = _effective_confidence(
        current_confidence=target.current_confidence,
        forecast_view=view,
        use_forecast=use_forecast,
    )

    probability = _availability_probability(
        effective_free_count=effective_free,
        forecast_view=view,
        use_forecast=use_forecast,
    )

    state = effective_free, effective_confidence, probability, view
    request_context.effective_state_cache[cache_key] = state
    return state


def _cluster_metrics(
    candidate_target: _ZoneTarget,
    arrival_time: datetime,
    request_context: _RankingRequestContext,
    use_forecast: bool,
) -> _ClusterMetrics:
    alternative_count = 0
    good_alternative_count = 0
    total_effective_free = 0
    best_probability = 0.0
    nearest_good_distance: int | None = None

    zone_id = int(candidate_target.zone.parking_zone_id)
    for alternative, distance in request_context.cluster_neighbors.get(zone_id, []):

        alternative_count += 1

        effective_free, _, probability, _ = _effective_state_for_target_at_arrival(
            target=alternative,
            arrival_time=arrival_time,
            request_context=request_context,
            use_forecast=use_forecast,
        )

        total_effective_free += max(effective_free, 0)
        best_probability = max(best_probability, probability)

        is_good = effective_free >= 1 or probability >= GOOD_ALTERNATIVE_MIN_PROBABILITY

        if is_good:
            good_alternative_count += 1

            if nearest_good_distance is None or distance < nearest_good_distance:
                nearest_good_distance = distance

    cluster_strength = (
        0.45 * min(good_alternative_count / 3.0, 1.0)
        + 0.40 * min(total_effective_free / 10.0, 1.0)
        + 0.15 * best_probability
    )

    return _ClusterMetrics(
        cluster_strength=round(_clamp_float(cluster_strength, 0.0, 1.0), 6),
        nearby_alternative_count=alternative_count,
        nearby_good_alternative_count=good_alternative_count,
        nearby_effective_free_count=total_effective_free,
        best_nearby_probability=round(best_probability, 6),
        nearest_good_alternative_distance_meters=nearest_good_distance,
    )


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def _availability_strength(
    effective_free_count: int,
    capacity: int,
    probability_free_space: float,
) -> float:
    if effective_free_count <= 0:
        return round(probability_free_space * 0.20, 6)

    free_count_score = min(effective_free_count / 4.0, 1.0)
    free_ratio_score = min(effective_free_count / max(capacity, 1), 1.0)

    strength = (
        0.55 * free_count_score
        + 0.30 * probability_free_space
        + 0.15 * free_ratio_score
    )

    return round(_clamp_float(strength, 0.0, 1.0), 6)


def _tier_for_candidate(
    current_free_count: int,
    effective_free_count: int,
    probability_free_space: float,
    effective_confidence: float,
    duration_from_origin_seconds: int,
    cluster_strength: float,
    use_forecast: bool,
    forecast_view: _ForecastView,
) -> tuple[int, str]:
    if effective_free_count >= 3 and probability_free_space >= 0.55:
        return 0, "excellent"

    if effective_free_count >= 2 and probability_free_space >= 0.40:
        return 1, "good"

    if effective_free_count == 1 and (probability_free_space >= 0.45 or cluster_strength >= 0.45):
        return 2, "scarce_but_usable"

    if (
        use_forecast
        and current_free_count == 0
        and forecast_view.predicted_free_count is not None
        and forecast_view.predicted_free_count >= 2
        and duration_from_origin_seconds >= 8 * 60
    ):
        return 2, "forecast_opportunity"

    if effective_free_count >= 1:
        return 3, "risky"

    if cluster_strength >= 0.50:
        return 4, "fallback_cluster_only"

    if effective_confidence < 0.35:
        return 5, "poor_low_confidence"

    return 5, "poor_no_spaces"


def _scarcity_penalty_seconds(
    effective_free_count: int,
    probability_free_space: float,
    duration_from_origin_seconds: int,
    cluster_strength: float,
) -> float:
    cluster_relief = cluster_strength * 450.0

    if effective_free_count >= 4:
        return 0.0

    if effective_free_count == 3:
        return max(0.0, 120.0 - cluster_relief * 0.25)

    if effective_free_count == 2:
        return max(0.0, 360.0 - cluster_relief * 0.50)

    if effective_free_count == 1:
        if duration_from_origin_seconds <= 10 * 60:
            base = 600.0
        elif duration_from_origin_seconds <= 30 * 60:
            base = 1_050.0
        else:
            base = 1_500.0

        probability_relief = probability_free_space * 350.0

        return max(180.0, base - probability_relief - cluster_relief)

    base = 4_200.0

    if duration_from_origin_seconds <= 10 * 60:
        base = 5_400.0

    probability_relief = probability_free_space * 1_200.0

    return max(1_200.0, base - probability_relief - cluster_relief)


def _price_penalty_seconds(pay: int) -> float:
    if pay <= 0:
        return 0.0

    if pay <= 50:
        return 90.0

    if pay <= 150:
        return 240.0

    if pay <= 300:
        return 480.0

    return 720.0


def _confidence_penalty_seconds(confidence: float) -> float:
    return (1.0 - _clamp_float(confidence, 0.0, 1.0)) * 300.0


def _availability_bonus_seconds(
    base_cost_seconds: float,
    availability_strength: float,
) -> float:
    # Доступность может уменьшить стоимость пути, но не должна полностью
    # перекрывать время поездки и пешего участка.
    return (
        max(base_cost_seconds, 0.0)
        * AVAILABILITY_BASE_COST_DISCOUNT
        * _clamp_float(availability_strength, 0.0, 1.0)
    )


def _cluster_bonus_seconds(
    base_cost_seconds: float,
    cluster_strength: float,
) -> float:
    # Наличие альтернатив рядом — страховка, а не замена основного маршрута.
    return (
        max(base_cost_seconds, 0.0)
        * CLUSTER_BASE_COST_DISCOUNT
        * _clamp_float(cluster_strength, 0.0, 1.0)
    )


def _candidate_reasons(
    tier_label: str,
    effective_free_count: int,
    current_free_count: int,
    forecast_view: _ForecastView,
    probability_free_space: float,
    duration_from_origin_seconds: int,
    duration_to_destination_seconds: int | None,
    pay: int,
    cluster_metrics: _ClusterMetrics,
    peer_penalty: float,
    detour_penalty: float,
) -> list[str]:
    reasons: list[str] = []

    reasons.append(f"quality_tier={tier_label}")

    if duration_from_origin_seconds <= 10 * 60:
        reasons.append("very_close_to_origin")
    elif duration_from_origin_seconds <= 30 * 60:
        reasons.append("reasonable_drive_time")
    else:
        reasons.append("long_drive_time")

    if duration_to_destination_seconds is not None:
        if duration_to_destination_seconds <= 2 * 60:
            reasons.append("very_close_to_destination_after_parking")
        elif duration_to_destination_seconds <= 8 * 60:
            reasons.append("acceptable_distance_to_destination_after_parking")
        else:
            reasons.append("far_from_destination_after_parking")

    if effective_free_count >= 3:
        reasons.append("good_free_space_buffer")
    elif effective_free_count == 2:
        reasons.append("moderate_free_space_buffer")
    elif effective_free_count == 1:
        reasons.append("only_one_effective_free_space")
    else:
        reasons.append("no_effective_free_spaces")

    if forecast_view.predicted_free_count is not None:
        if forecast_view.predicted_free_count > current_free_count:
            reasons.append("forecast_expects_spaces_to_free_up")
        elif forecast_view.predicted_free_count < current_free_count:
            reasons.append("forecast_expects_spaces_to_be_taken")

    if probability_free_space >= 0.75:
        reasons.append("high_probability_of_free_space")
    elif probability_free_space < 0.30:
        reasons.append("low_probability_of_free_space")

    if pay <= 0:
        reasons.append("free_parking")
    elif pay >= 300:
        reasons.append("expensive_parking")

    if cluster_metrics.cluster_strength >= 0.60:
        reasons.append("strong_nearby_fallback_cluster")
    elif cluster_metrics.nearby_good_alternative_count == 0:
        reasons.append("no_good_nearby_alternatives")

    if peer_penalty > 0:
        reasons.append("similar_nearby_candidate_has_better_availability")

    if detour_penalty > 0:
        reasons.append("penalized_as_unreasonable_detour")

    return reasons


def _build_ranking_context(
    routed: _RoutedCandidate,
    request_context: _RankingRequestContext,
    use_forecast: bool,
) -> _CandidateContext:
    target = routed.zone_target
    zone = target.zone
    zone_id = int(zone.parking_zone_id)

    capacity = max(_safe_int(zone.capacity), 0)
    pay = max(_safe_int(zone.pay), 0)

    effective_free, effective_confidence, probability, forecast_view = (
        _effective_state_for_target_at_arrival(
            target=target,
            arrival_time=routed.arrival_time,
            request_context=request_context,
            use_forecast=use_forecast,
        )
    )

    cluster = _cluster_metrics(
        candidate_target=target,
        arrival_time=routed.arrival_time,
        request_context=request_context,
        use_forecast=use_forecast,
    )

    availability_strength = _availability_strength(
        effective_free_count=effective_free,
        capacity=capacity,
        probability_free_space=probability,
    )

    tier, tier_label = _tier_for_candidate(
        current_free_count=target.current_free_count,
        effective_free_count=effective_free,
        probability_free_space=probability,
        effective_confidence=effective_confidence,
        duration_from_origin_seconds=routed.duration_from_origin_seconds,
        cluster_strength=cluster.cluster_strength,
        use_forecast=use_forecast,
        forecast_view=forecast_view,
    )

    scarcity_penalty = _scarcity_penalty_seconds(
        effective_free_count=effective_free,
        probability_free_space=probability,
        duration_from_origin_seconds=routed.duration_from_origin_seconds,
        cluster_strength=cluster.cluster_strength,
    )

    price_penalty = _price_penalty_seconds(pay)
    confidence_penalty = _confidence_penalty_seconds(effective_confidence)

    walk_seconds = routed.duration_to_destination_seconds or 0

    # В режиме маршрута к точке назначения пешая часть имеет повышенную цену:
    # парковка в нескольких кварталах не должна выигрывать у парковки прямо у
    # цели только благодаря десяткам избыточных свободных мест.
    base_cost = (
        float(routed.duration_from_origin_seconds)
        + DESTINATION_WALK_COST_MULTIPLIER * float(walk_seconds)
    )

    availability_bonus = _availability_bonus_seconds(
        base_cost_seconds=base_cost,
        availability_strength=availability_strength,
    )
    cluster_bonus = _cluster_bonus_seconds(
        base_cost_seconds=base_cost,
        cluster_strength=cluster.cluster_strength,
    )

    generalized_cost = (
        base_cost
        + scarcity_penalty
        + price_penalty
        + confidence_penalty
        - availability_bonus
        - cluster_bonus
    )

    generalized_cost = max(0.0, generalized_cost)

    explanation = RankingExplanation(
        tier=tier,
        tier_label=tier_label,
        generalized_cost_seconds=round(generalized_cost, 3),
        base_drive_seconds=routed.duration_from_origin_seconds,
        walk_to_destination_seconds=routed.duration_to_destination_seconds,
        current_free_count=target.current_free_count,
        predicted_free_count=forecast_view.predicted_free_count,
        effective_free_count=effective_free,
        availability_probability=round(probability, 6),
        availability_strength=availability_strength,
        cluster_strength=cluster.cluster_strength,
        nearby_alternative_count=cluster.nearby_alternative_count,
        nearby_good_alternative_count=cluster.nearby_good_alternative_count,
        nearby_effective_free_count=cluster.nearby_effective_free_count,
        nearest_good_alternative_distance_meters=cluster.nearest_good_alternative_distance_meters,
        price_penalty_seconds=round(price_penalty, 3),
        scarcity_penalty_seconds=round(scarcity_penalty, 3),
        confidence_penalty_seconds=round(confidence_penalty, 3),
        cluster_bonus_seconds=round(cluster_bonus, 3),
        availability_bonus_seconds=round(availability_bonus, 3),
        peer_better_availability_penalty_seconds=0.0,
        unreasonable_detour_penalty_seconds=0.0,
        reasons=[],
    )

    score = round(1.0 / (1.0 + generalized_cost / 1_800.0), 6)

    candidate = RouteCandidate(
        zone_id=zone_id,
        camera_id=cast(int | None, zone.camera_id),
        geometry=zone.geometry,
        zone_type=_enum_value(zone.zone_type) or "unknown",
        location_type=_enum_value(zone.location_type),
        is_accessible=cast(bool | None, zone.is_accessible),
        pay=pay,
        capacity=capacity,
        current_occupied=target.current_occupied,
        current_free_count=target.current_free_count,
        current_confidence=target.current_confidence,
        predicted_for_arrival=routed.arrival_time,
        predicted_occupied=forecast_view.predicted_occupied,
        predicted_free_count=forecast_view.predicted_free_count,
        probability_free_space=forecast_view.probability_free_space,
        forecast_confidence=forecast_view.forecast_confidence,
        distance_from_origin_meters=routed.distance_from_origin_meters,
        duration_from_origin_seconds=routed.duration_from_origin_seconds,
        distance_to_destination_meters=routed.distance_to_destination_meters,
        duration_to_destination_seconds=routed.duration_to_destination_seconds,
        score=score,
        rank=0,
        ranking_explanation=explanation,
    )

    return _CandidateContext(
        candidate=candidate,
        tier=tier,
        tier_label=tier_label,
        effective_free_count=effective_free,
        effective_confidence=effective_confidence,
        availability_probability=probability,
        availability_strength=availability_strength,
        cluster_metrics=cluster,
        base_cost_seconds=base_cost,
        generalized_cost_seconds=generalized_cost,
        price_penalty_seconds=price_penalty,
        scarcity_penalty_seconds=scarcity_penalty,
        confidence_penalty_seconds=confidence_penalty,
        cluster_bonus_seconds=cluster_bonus,
        availability_bonus_seconds=availability_bonus,
    )


def _apply_peer_availability_penalties(contexts: list[_CandidateContext]) -> None:
    for context in contexts:
        candidate = context.candidate
        penalty = 0.0

        # Два надёжно доступных места уже образуют достаточный запас. Не
        # штрафуем такую парковку у самой точки назначения только потому, что
        # в нескольких кварталах есть гораздо более крупная парковка.
        has_sufficient_destination_availability = (
            candidate.duration_to_destination_seconds is not None
            and candidate.duration_to_destination_seconds
            <= PROTECTED_DESTINATION_WALK_SECONDS
            and candidate.current_free_count >= SUFFICIENT_FREE_SPACES
            and context.effective_free_count >= SUFFICIENT_FREE_SPACES
            and context.availability_probability >= 0.40
        )

        if has_sufficient_destination_availability:
            continue

        for other in contexts:
            if other is context:
                continue

            other_candidate = other.candidate

            similar_drive_time = (
                other_candidate.duration_from_origin_seconds
                <= candidate.duration_from_origin_seconds + 5 * 60
            )

            current_walk = candidate.duration_to_destination_seconds or 0
            other_walk = other_candidate.duration_to_destination_seconds or 0

            similar_walk = other_walk <= current_walk + 5 * 60
            similar_price = other_candidate.pay <= candidate.pay + 100

            much_better_availability = (
                other.effective_free_count >= context.effective_free_count + 2
                or other.availability_strength >= context.availability_strength + 0.20
            )

            not_weaker_cluster = (
                other.cluster_metrics.cluster_strength
                >= context.cluster_metrics.cluster_strength - 0.15
            )

            if (
                similar_drive_time
                and similar_walk
                and similar_price
                and much_better_availability
                and not_weaker_cluster
            ):
                penalty = max(penalty, 900.0)

        if penalty > 0:
            context.peer_better_availability_penalty_seconds = penalty
            context.generalized_cost_seconds += penalty


def _apply_unreasonable_detour_penalties(contexts: list[_CandidateContext]) -> None:
    good_contexts = [
        context
        for context in contexts
        if context.tier <= 3
    ]

    if len(good_contexts) < 3:
        return

    best_duration = min(
        context.candidate.duration_from_origin_seconds
        for context in good_contexts
    )

    max_reasonable_duration = max(
        best_duration + 30 * 60,
        int(best_duration * 2.5),
    )

    max_reasonable_duration = min(
        max_reasonable_duration,
        best_duration + 2 * 60 * 60,
    )

    for context in contexts:
        if (
            context.tier >= 3
            and context.candidate.duration_from_origin_seconds > max_reasonable_duration
        ):
            context.unreasonable_detour_penalty_seconds = 3_600.0
            context.generalized_cost_seconds += 3_600.0


def _ranking_sort_key(context: _CandidateContext) -> tuple[Any, ...]:
    candidate = context.candidate

    poor_group = 1 if context.tier >= 5 else 0

    return (
        poor_group,
        context.generalized_cost_seconds,
        context.base_cost_seconds,
        -context.availability_strength,
        -context.cluster_metrics.cluster_strength,
        -context.effective_free_count,
        candidate.pay,
        candidate.duration_from_origin_seconds,
        candidate.distance_from_origin_meters,
        candidate.zone_id,
    )


def _finalize_contexts(contexts: list[_CandidateContext]) -> list[RouteCandidate]:
    _apply_peer_availability_penalties(contexts)
    _apply_unreasonable_detour_penalties(contexts)

    finalized: list[_CandidateContext] = []

    for context in contexts:
        candidate = context.candidate
        explanation = candidate.ranking_explanation

        if explanation is not None:
            reasons = _candidate_reasons(
                tier_label=context.tier_label,
                effective_free_count=context.effective_free_count,
                current_free_count=candidate.current_free_count,
                forecast_view=_ForecastView(
                    predicted_occupied=candidate.predicted_occupied,
                    predicted_free_count=candidate.predicted_free_count,
                    probability_free_space=candidate.probability_free_space,
                    forecast_confidence=candidate.forecast_confidence,
                ),
                probability_free_space=context.availability_probability,
                duration_from_origin_seconds=candidate.duration_from_origin_seconds,
                duration_to_destination_seconds=candidate.duration_to_destination_seconds,
                pay=candidate.pay,
                cluster_metrics=context.cluster_metrics,
                peer_penalty=context.peer_better_availability_penalty_seconds,
                detour_penalty=context.unreasonable_detour_penalty_seconds,
            )

            explanation = explanation.model_copy(
                update={
                    "generalized_cost_seconds": round(context.generalized_cost_seconds, 3),
                    "peer_better_availability_penalty_seconds": round(
                        context.peer_better_availability_penalty_seconds,
                        3,
                    ),
                    "unreasonable_detour_penalty_seconds": round(
                        context.unreasonable_detour_penalty_seconds,
                        3,
                    ),
                    "reasons": reasons,
                }
            )

        score = round(1.0 / (1.0 + context.generalized_cost_seconds / 1_800.0), 6)

        context.candidate = candidate.model_copy(
            update={
                "score": score,
                "ranking_explanation": explanation,
            }
        )

        finalized.append(context)

    finalized.sort(key=_ranking_sort_key)

    return [
        context.candidate.model_copy(update={"rank": rank})
        for rank, context in enumerate(finalized, start=1)
    ]


# ---------------------------------------------------------------------------
# Route matrix
# ---------------------------------------------------------------------------

def _route_zone_pool_with_provider(
    origin: GeoPoint,
    destination: GeoPoint | None,
    zone_targets: list[_ZoneTarget],
    deadline: float,
    settings: _RoutingSettings,
) -> list[_RoutedCandidate]:
    if not zone_targets:
        return []

    matrix = _geoapify_matrix(
        sources=[origin],
        targets=[item.point for item in zone_targets],
        deadline=deadline,
        settings=settings,
    )

    now = datetime.now(timezone.utc)
    routed: list[_RoutedCandidate] = []

    for index, item in enumerate(zone_targets):
        from_origin = _matrix_cell(matrix, 0, index)

        if from_origin is None:
            raise RoutingProviderError(
                "Geoapify returned an incomplete route matrix",
                reason="invalid_provider_response",
            )

        distance_from_origin, duration_from_origin = from_origin
        arrival_time = now + timedelta(seconds=duration_from_origin)

        distance_to_destination: int | None = None
        duration_to_destination: int | None = None

        if destination is not None:
            direct_distance = _haversine_meters(item.point, destination)
            distance_to_destination = int(direct_distance * WALKING_DETOUR_FACTOR)
            duration_to_destination = _estimated_walking_seconds(direct_distance)

        routed.append(
            _RoutedCandidate(
                zone_target=item,
                distance_from_origin_meters=distance_from_origin,
                duration_from_origin_seconds=duration_from_origin,
                distance_to_destination_meters=distance_to_destination,
                duration_to_destination_seconds=duration_to_destination,
                arrival_time=arrival_time,
            )
        )

    return routed


def _route_zone_pool_locally(
    origin: GeoPoint,
    destination: GeoPoint | None,
    zone_targets: list[_ZoneTarget],
    settings: _RoutingSettings,
) -> list[_RoutedCandidate]:
    now = datetime.now(timezone.utc)
    driving_speed_mps = settings.average_driving_speed_kph / 3.6
    routed: list[_RoutedCandidate] = []

    for item in zone_targets:
        direct_from_origin = _haversine_meters(origin, item.point)
        driving_distance = int(round(direct_from_origin * settings.road_detour_factor))
        driving_duration = max(30, int(round(driving_distance / driving_speed_mps)))

        distance_to_destination: int | None = None
        duration_to_destination: int | None = None
        if destination is not None:
            direct_to_destination = _haversine_meters(item.point, destination)
            distance_to_destination = int(
                round(direct_to_destination * WALKING_DETOUR_FACTOR)
            )
            duration_to_destination = _estimated_walking_seconds(
                direct_to_destination
            )

        routed.append(
            _RoutedCandidate(
                zone_target=item,
                distance_from_origin_meters=driving_distance,
                duration_from_origin_seconds=driving_duration,
                distance_to_destination_meters=distance_to_destination,
                duration_to_destination_seconds=duration_to_destination,
                arrival_time=now + timedelta(seconds=driving_duration),
            )
        )

    return routed


# ---------------------------------------------------------------------------
# Основной поиск
# ---------------------------------------------------------------------------

def _search_candidates(
    db: Session,
    origin: GeoPoint,
    destination: GeoPoint | None,
    mode: str,
    max_pay: int | None,
    min_free_count: int | None,
    min_confidence: float | None,
    max_distance_to_destination_meters: int | None,
    max_duration_from_origin_seconds: int | None,
    include_accessible: bool | None,
    use_forecast: bool,
    limit: int,
    selected_zone_id: int | None = None,
) -> _CandidateSearchResult:
    started = time.monotonic()
    settings = _routing_settings()
    deadline = started + settings.search_budget_seconds
    candidate_query_ms = 0.0
    provider_ms = 0.0
    forecast_query_ms = 0.0
    ranking_ms = 0.0
    provider_used = GEOAPIFY_PROVIDER_NAME
    fallback_reason: str | None = None
    candidate_count = 0
    matrix_target_count = 0

    def finish(
        candidates: list[RouteCandidate],
        total_candidates: int,
    ) -> _CandidateSearchResult:
        total_ms = (time.monotonic() - started) * 1_000
        logger.info(
            "routing_search candidate_query_ms=%.1f provider_ms=%.1f "
            "forecast_query_ms=%.1f ranking_ms=%.1f total_ms=%.1f "
            "provider_used=%s candidate_count=%d matrix_target_count=%d "
            "fallback_reason=%s",
            candidate_query_ms,
            provider_ms,
            forecast_query_ms,
            ranking_ms,
            total_ms,
            provider_used,
            candidate_count,
            matrix_target_count,
            fallback_reason or "none",
        )
        return _CandidateSearchResult(
            candidates=candidates,
            total_candidates=total_candidates,
            provider=provider_used,
            fallback_reason=fallback_reason,
        )

    if mode == "route_to_destination" and destination is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_description": "destination is required for mode=route_to_destination"},
        )

    anchor = destination if mode == "route_to_destination" and destination is not None else origin

    stage_started = time.monotonic()
    cluster_pool = _query_zone_targets_near_anchor(
        db=db,
        anchor=anchor,
        mode=mode,
        max_pay=max_pay,
        include_accessible=include_accessible,
        max_distance_to_destination_meters=max_distance_to_destination_meters,
        limit=limit,
        selected_zone_id=selected_zone_id,
        deadline=deadline,
        settings=settings,
    )
    candidate_query_ms = (time.monotonic() - stage_started) * 1_000
    candidate_count = len(cluster_pool)

    matrix_targets = _select_matrix_targets(
        cluster_pool=cluster_pool,
        requested_limit=limit,
        settings=settings,
        selected_zone_id=selected_zone_id,
    )
    matrix_target_count = len(matrix_targets)

    stage_started = time.monotonic()
    try:
        routed_candidates = _route_zone_pool_with_provider(
            origin=origin,
            destination=destination,
            zone_targets=matrix_targets,
            deadline=deadline,
            settings=settings,
        )
    except RoutingProviderError as exc:
        provider_used = "internal"
        fallback_reason = exc.reason
        routed_candidates = _route_zone_pool_locally(
            origin=origin,
            destination=destination,
            zone_targets=matrix_targets,
            settings=settings,
        )
    provider_ms = (time.monotonic() - stage_started) * 1_000

    if not routed_candidates:
        return finish([], 0)

    filtered_by_explicit_constraints: list[_RoutedCandidate] = []

    for routed in routed_candidates:
        if (
            max_duration_from_origin_seconds is not None
            and routed.duration_from_origin_seconds > max_duration_from_origin_seconds
        ):
            continue

        if (
            max_distance_to_destination_meters is not None
            and routed.distance_to_destination_meters is not None
            and routed.distance_to_destination_meters > max_distance_to_destination_meters
        ):
            continue

        filtered_by_explicit_constraints.append(routed)

    if not filtered_by_explicit_constraints:
        return finish([], 0)

    min_arrival = min(item.arrival_time for item in filtered_by_explicit_constraints)
    max_arrival = max(item.arrival_time for item in filtered_by_explicit_constraints)

    cluster_neighbors = _build_cluster_neighbors(
        candidate_targets=[item.zone_target for item in filtered_by_explicit_constraints],
        cluster_pool=cluster_pool,
    )
    forecast_zone_ids = {
        int(item.zone_target.zone.parking_zone_id)
        for item in filtered_by_explicit_constraints
    }
    for neighbors in cluster_neighbors.values():
        forecast_zone_ids.update(
            int(target.zone.parking_zone_id) for target, _ in neighbors
        )

    forecasts_by_zone: dict[int, _ForecastSeries] = {}
    if use_forecast and _remaining_seconds(deadline) > 0.05:
        stage_started = time.monotonic()
        forecasts_by_zone = _load_forecasts_for_zones(
            db=db,
            zone_ids=sorted(forecast_zone_ids),
            min_arrival=min_arrival,
            max_arrival=max_arrival,
        )
        forecast_query_ms = (time.monotonic() - stage_started) * 1_000
    elif use_forecast:
        fallback_reason = fallback_reason or "insufficient_time_for_forecasts"

    request_context = _RankingRequestContext(
        forecasts_by_zone=forecasts_by_zone,
        cluster_neighbors=cluster_neighbors,
        effective_state_cache={},
    )

    contexts: list[_CandidateContext] = []

    stage_started = time.monotonic()
    for routed in filtered_by_explicit_constraints:
        context = _build_ranking_context(
            routed=routed,
            request_context=request_context,
            use_forecast=use_forecast,
        )

        if min_free_count is not None and context.effective_free_count < min_free_count:
            continue

        if min_confidence is not None and context.effective_confidence < min_confidence:
            continue

        contexts.append(context)

    if not contexts:
        ranking_ms = (time.monotonic() - stage_started) * 1_000
        return finish([], 0)

    ranked_candidates = _finalize_contexts(contexts)
    ranking_ms = (time.monotonic() - stage_started) * 1_000
    total_candidates = len(ranked_candidates)

    return finish(ranked_candidates[:limit], total_candidates)


def _selected_candidate_or_422(
    db: Session,
    origin: GeoPoint,
    destination: GeoPoint | None,
    mode: str,
    use_forecast: bool,
    selected_zone_id: int,
) -> tuple[RouteCandidate, str]:
    result = _search_candidates(
        db=db,
        origin=origin,
        destination=destination,
        mode=mode,
        max_pay=None,
        min_free_count=None,
        min_confidence=None,
        max_distance_to_destination_meters=None,
        max_duration_from_origin_seconds=None,
        include_accessible=None,
        use_forecast=use_forecast,
        limit=1,
        selected_zone_id=selected_zone_id,
    )

    if not result.candidates:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_description": "Cannot build route to the selected zone"},
        )

    return result.candidates[0], result.provider


# ---------------------------------------------------------------------------
# POST /routing/search — публичный
# ---------------------------------------------------------------------------

@router.post("/search", response_model=SearchRoutingResponse)
def search_routing(
    body: SearchRoutingRequest,
    db: Annotated[Session, Depends(get_db)],
):
    try:
        result = _search_candidates(
            db=db,
            origin=body.origin,
            destination=body.destination,
            mode=body.mode,
            max_pay=body.max_pay,
            min_free_count=body.min_free_count,
            min_confidence=body.min_confidence,
            max_distance_to_destination_meters=body.max_distance_to_destination_meters,
            max_duration_from_origin_seconds=body.max_duration_from_origin_seconds,
            include_accessible=body.include_accessible,
            use_forecast=body.use_forecast,
            limit=body.limit,
        )
    except RoutingProviderError as exc:
        raise _provider_unavailable(exc) from exc

    selected_zone_id = result.candidates[0].zone_id if result.candidates else None

    return SearchRoutingResponse(
        mode=body.mode,
        provider=result.provider,
        generated_at=datetime.now(timezone.utc),
        selected_zone_id=selected_zone_id,
        total_candidates=result.total_candidates,
        candidates=result.candidates,
    )


# ---------------------------------------------------------------------------
# POST /routing/new — публичный
# ---------------------------------------------------------------------------

@router.post("/new", status_code=status.HTTP_201_CREATED, response_model=RouteResponse)
def create_route(
    body: CreateRouteRequest,
    db: Annotated[Session, Depends(get_db)],
):
    if body.selected_zone_id is not None:
        zone_exists = (
            db.query(ParkingZone.parking_zone_id)
            .filter(ParkingZone.parking_zone_id == body.selected_zone_id)
            .one_or_none()
        )

        if zone_exists is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail={"error_description": f"Zone {body.selected_zone_id} not found"},
            )

    try:
        result = _search_candidates(
            db=db,
            origin=body.origin,
            destination=body.destination,
            mode=body.mode,
            max_pay=body.max_pay,
            min_free_count=body.min_free_count,
            min_confidence=body.min_confidence,
            max_distance_to_destination_meters=body.max_distance_to_destination_meters,
            max_duration_from_origin_seconds=body.max_duration_from_origin_seconds,
            include_accessible=body.include_accessible,
            use_forecast=body.use_forecast,
            limit=body.limit,
            selected_zone_id=body.selected_zone_id,
        )
    except RoutingProviderError as exc:
        raise _provider_unavailable(exc) from exc

    if not result.candidates:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_description": "No suitable parking zones found"},
        )

    best = result.candidates[0]
    now = datetime.now(timezone.utc)
    public_user_id = _get_public_routing_user_id(db)

    selected_zone = (
        db.query(ParkingZone)
        .filter(ParkingZone.parking_zone_id == best.zone_id)
        .one_or_none()
    )

    deeplink_target = body.origin

    if selected_zone is not None:
        centroid = _zone_point_from_centroid_columns(selected_zone)

        if centroid is not None:
            deeplink_target = centroid

    route = Route(
        user_id=public_user_id,
        mode=RouteMode(body.mode),
        provider=result.provider,
        origin_latitude=body.origin.latitude,
        origin_longitude=body.origin.longitude,
        destination_latitude=body.destination.latitude if body.destination else None,
        destination_longitude=body.destination.longitude if body.destination else None,
        selected_zone_id=best.zone_id,
        selected_candidate=best.model_dump(mode="json"),
        eta_seconds=best.duration_from_origin_seconds,
        arrival_time=best.predicted_for_arrival,
        polyline=None,
        deeplink_url=_build_map_deeplink(deeplink_target),
        status=RouteStatus.active,
        created_at=now,
        updated_at=now,
    )

    try:
        db.add(route)
        db.commit()
        db.refresh(route)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_description": "Route could not be saved",
                "error_type": exc.__class__.__name__,
            },
        )

    return _serialize_route(route)


# ---------------------------------------------------------------------------
# GET /routing
# ---------------------------------------------------------------------------

@router.get("", response_model=RouteListResponse)
def list_routes(
    current_user: Annotated[User, require("routing.view")],
    db: Annotated[Session, Depends(get_db)],
    route_status: Annotated[str | None, Query(alias="status")] = None,
    mode: Annotated[str | None, Query()] = None,
    top: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    query = db.query(Route)

    if current_user.global_role != GlobalRole.admin:
        query = query.filter(Route.user_id == current_user.user_id)

    if route_status is not None:
        try:
            query = query.filter(Route.status == RouteStatus(route_status))
        except ValueError:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error_description": f"Unknown status: {route_status}"},
            )

    if mode is not None:
        try:
            query = query.filter(Route.mode == RouteMode(mode))
        except ValueError:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error_description": f"Unknown mode: {mode}"},
            )

    total = query.count()

    routes = (
        query
        .order_by(Route.created_at.desc())
        .offset(offset)
        .limit(top)
        .all()
    )

    return RouteListResponse(
        items=[_serialize_route(route) for route in routes],
        total=total,
        top=top,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# GET /routing/{route_id}
# ---------------------------------------------------------------------------

@router.get("/{route_id}", response_model=RouteResponse)
def get_route(
    route_id: int,
    current_user: Annotated[User, require("routing.view")],
    db: Annotated[Session, Depends(get_db)],
):
    route = _get_route_or_404(db, route_id)
    _assert_owner_or_admin(route, current_user)

    return _serialize_route(route)


# ---------------------------------------------------------------------------
# PUT /routing/{route_id}
# ---------------------------------------------------------------------------

@router.put("/{route_id}", response_model=RouteResponse)
def update_route(
    route_id: int,
    body: UpdateRouteRequest,
    current_user: Annotated[User, require("routing.create")],
    db: Annotated[Session, Depends(get_db)],
):
    route = _get_route_or_404(db, route_id)
    _assert_owner_or_admin(route, current_user)

    if body.status is not None:
        route.status = RouteStatus(body.status)

    if body.provider is not None:
        route.provider = GEOAPIFY_PROVIDER_NAME

    if body.selected_zone_id is not None:
        zone = (
            db.query(ParkingZone)
            .filter(ParkingZone.parking_zone_id == body.selected_zone_id)
            .one_or_none()
        )

        if zone is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail={"error_description": f"Zone {body.selected_zone_id} not found"},
            )

        origin = GeoPoint(
            latitude=route.origin_latitude,
            longitude=route.origin_longitude,
        )

        destination: GeoPoint | None = None

        if route.destination_latitude is not None and route.destination_longitude is not None:
            destination = GeoPoint(
                latitude=route.destination_latitude,
                longitude=route.destination_longitude,
            )

        try:
            candidate, provider_used = _selected_candidate_or_422(
                db=db,
                origin=origin,
                destination=destination,
                mode=_enum_value(route.mode) or "find_parking",
                use_forecast=True,
                selected_zone_id=body.selected_zone_id,
            )
        except RoutingProviderError as exc:
            raise _provider_unavailable(exc) from exc

        centroid = _zone_point_from_centroid_columns(zone)

        route.provider = provider_used
        route.selected_zone_id = body.selected_zone_id
        route.selected_candidate = candidate.model_dump(mode="json")
        route.eta_seconds = candidate.duration_from_origin_seconds
        route.arrival_time = candidate.predicted_for_arrival
        route.polyline = None

        if centroid is not None:
            route.deeplink_url = _build_map_deeplink(centroid)

    route.updated_at = datetime.now(timezone.utc)

    try:
        db.commit()
        db.refresh(route)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_description": "Route could not be updated",
                "error_type": exc.__class__.__name__,
            },
        )

    return _serialize_route(route)


# ---------------------------------------------------------------------------
# DELETE /routing/{route_id}
# ---------------------------------------------------------------------------

@router.delete("/{route_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_route(
    route_id: int,
    current_user: Annotated[User, require("routing.delete")],
    db: Annotated[Session, Depends(get_db)],
):
    route = _get_route_or_404(db, route_id)
    _assert_owner_or_admin(route, current_user)

    route.status = RouteStatus.cancelled
    route.updated_at = datetime.now(timezone.utc)

    db.commit()

    return None
