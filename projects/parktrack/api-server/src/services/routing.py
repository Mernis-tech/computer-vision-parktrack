"""
Сервис поиска кандидатов парковки.

Для provider="yandex" расстояния и ETA считаются через Yandex Distance Matrix.
Для provider="internal" используется локальная Haversine-оценка как fallback.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence, TypeVar
from urllib import error, parse, request

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..db_models import Forecast, ParkingZone
from ..schemas.routing import GeoPoint, RouteCandidate

# ---------------------------------------------------------------------------
# Геометрия и локальный fallback
# ---------------------------------------------------------------------------

_EARTH_RADIUS_M = 6_371_000.0
_WALKING_SPEED_MPS = 1.4        # м/с (~5 км/ч)
_DRIVING_SPEED_MPS = 8.33       # м/с (~30 км/ч в городе)

_PROVIDER_INTERNAL = "internal"
_PROVIDER_YANDEX = "yandex"
_SUPPORTED_PROVIDERS = {_PROVIDER_INTERNAL, _PROVIDER_YANDEX}
_YANDEX_DISTANCE_MATRIX_URL = "https://api.routing.yandex.net/v2/distancematrix"
_YANDEX_MATRIX_MAX_ITEMS = 100
_T = TypeVar("_T")


@dataclass(frozen=True)
class RouteMetrics:
    distance_meters: int
    duration_seconds: int


@dataclass(frozen=True)
class CandidateSearchResult:
    candidates: list[RouteCandidate]
    total_candidates: int


class RoutingProviderError(RuntimeError):
    """Внешний провайдер маршрутизации не смог рассчитать маршрут."""


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Возвращает расстояние в метрах между двумя точками."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _duration_seconds(distance_m: float) -> int:
    """Грубая оценка времени поездки на автомобиле."""
    return max(30, int(distance_m / _DRIVING_SPEED_MPS))


def _duration_on_foot(distance_m: float) -> int:
    """Грубая оценка времени пешей прогулки."""
    return max(10, int(distance_m / _WALKING_SPEED_MPS))


def _zone_centroid(zone: ParkingZone) -> tuple[float, float]:
    """Возвращает (lat, lon) центра зоны из GeoJSON Polygon."""
    try:
        coords = list(zone.geometry["coordinates"][0])
        if len(coords) > 1 and coords[0] == coords[-1]:
            coords = coords[:-1]
        lons = [float(c[0]) for c in coords]
        lats = [float(c[1]) for c in coords]
        return sum(lats) / len(lats), sum(lons) / len(lons)
    except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError):
        return float(zone.geometry.get("lat", 0.0)), float(zone.geometry.get("lon", 0.0))


def _enum_value(value: object) -> str | None:
    if value is None:
        return None
    return getattr(value, "value", value)


# ---------------------------------------------------------------------------
# Провайдеры маршрутизации
# ---------------------------------------------------------------------------

def _normalize_provider(provider: str | None) -> str:
    return (provider or _PROVIDER_INTERNAL).strip().lower() or _PROVIDER_INTERNAL


def _use_yandex(provider: str | None) -> bool:
    return _normalize_provider(provider) == _PROVIDER_YANDEX


def _yandex_api_key() -> str | None:
    return (
        os.getenv("YANDEX_ROUTING_API_KEY")
        or os.getenv("YANDEX_MAPS_API_KEY")
        or os.getenv("YANDEX_API_KEY")
    )


def _yandex_timeout_seconds() -> float:
    raw = os.getenv("YANDEX_ROUTING_TIMEOUT_SECONDS", "5")
    try:
        return max(0.1, float(raw))
    except ValueError:
        return 5.0


def _format_point(point: tuple[float, float]) -> str:
    lat, lon = point
    return f"{lat:.7f},{lon:.7f}"


def _chunked(items: Sequence[_T], size: int) -> list[Sequence[_T]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _yandex_error_detail(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        return "; ".join(str(item) for item in errors)
    if isinstance(errors, str):
        return errors
    return None


def _request_yandex_matrix(
    origins: Sequence[tuple[float, float]],
    destinations: Sequence[tuple[float, float]],
    mode: str,
) -> list[list[RouteMetrics | None]]:
    api_key = _yandex_api_key()
    if not api_key:
        raise RoutingProviderError("Yandex routing provider is not configured")

    params = {
        "apikey": api_key,
        "origins": "|".join(_format_point(point) for point in origins),
        "destinations": "|".join(_format_point(point) for point in destinations),
        "mode": mode,
    }
    if mode == "driving":
        params["departure_time"] = str(int(datetime.now(timezone.utc).timestamp()))

    base_url = os.getenv("YANDEX_DISTANCE_MATRIX_URL", _YANDEX_DISTANCE_MATRIX_URL)
    url = f"{base_url}?{parse.urlencode(params, safe=',|')}"
    req = request.Request(url, headers={"Accept": "application/json"})

    try:
        with request.urlopen(req, timeout=_yandex_timeout_seconds()) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        raw_body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = _yandex_error_detail(json.loads(raw_body))
        except json.JSONDecodeError:
            detail = raw_body.strip() or None
        message = f"Yandex routing provider returned HTTP {exc.code}"
        if detail:
            message = f"{message}: {detail}"
        raise RoutingProviderError(message) from exc
    except (error.URLError, TimeoutError) as exc:
        raise RoutingProviderError(f"Yandex routing provider is unavailable: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RoutingProviderError("Yandex routing provider returned invalid JSON") from exc

    detail = _yandex_error_detail(payload)
    if detail:
        raise RoutingProviderError(f"Yandex routing provider returned an error: {detail}")

    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RoutingProviderError("Yandex routing provider returned an unexpected response")

    matrix: list[list[RouteMetrics | None]] = []
    for origin_idx in range(len(origins)):
        row = rows[origin_idx] if origin_idx < len(rows) and isinstance(rows[origin_idx], dict) else {}
        elements = row.get("elements") if isinstance(row, dict) else None
        elements = elements if isinstance(elements, list) else []

        parsed_row: list[RouteMetrics | None] = []
        for dest_idx in range(len(destinations)):
            element = elements[dest_idx] if dest_idx < len(elements) and isinstance(elements[dest_idx], dict) else {}
            if element.get("status") != "OK":
                parsed_row.append(None)
                continue

            distance = element.get("distance")
            duration = element.get("duration")
            if not isinstance(distance, dict) or not isinstance(duration, dict):
                parsed_row.append(None)
                continue

            try:
                parsed_row.append(
                    RouteMetrics(
                        distance_meters=max(0, int(round(float(distance["value"])))),
                        duration_seconds=max(0, int(round(float(duration["value"])))),
                    )
                )
            except (KeyError, TypeError, ValueError):
                parsed_row.append(None)
        matrix.append(parsed_row)

    return matrix


def _metrics_from_origin(
    provider: str,
    origin: GeoPoint,
    zone_points: Sequence[tuple[float, float]],
) -> list[RouteMetrics | None]:
    if not zone_points:
        return []

    if _use_yandex(provider):
        results: list[RouteMetrics | None] = []
        origin_point = (origin.latitude, origin.longitude)
        for chunk in _chunked(zone_points, _YANDEX_MATRIX_MAX_ITEMS):
            matrix = _request_yandex_matrix([origin_point], chunk, mode="driving")
            results.extend(matrix[0] if matrix else [None] * len(chunk))
        return results

    return [
        RouteMetrics(
            distance_meters=int(distance),
            duration_seconds=_duration_seconds(distance),
        )
        for distance in (
            _haversine(origin.latitude, origin.longitude, lat, lon)
            for lat, lon in zone_points
        )
    ]


def _metrics_to_destination(
    provider: str,
    zone_points: Sequence[tuple[float, float]],
    destination: GeoPoint | None,
) -> list[RouteMetrics | None]:
    if destination is None:
        return [None] * len(zone_points)

    if _use_yandex(provider):
        results: list[RouteMetrics | None] = []
        destination_point = (destination.latitude, destination.longitude)
        for chunk in _chunked(zone_points, _YANDEX_MATRIX_MAX_ITEMS):
            matrix = _request_yandex_matrix(chunk, [destination_point], mode="walking")
            results.extend(row[0] if row else None for row in matrix)
        return results

    return [
        RouteMetrics(
            distance_meters=int(distance),
            duration_seconds=_duration_on_foot(distance),
        )
        for distance in (
            _haversine(lat, lon, destination.latitude, destination.longitude)
            for lat, lon in zone_points
        )
    ]


# ---------------------------------------------------------------------------
# Deeplink
# ---------------------------------------------------------------------------

def build_deeplink(provider: str, lat: float, lon: float) -> str | None:
    if _use_yandex(provider):
        return f"yandexnavi://build_route_on_map?lat_to={lat}&lon_to={lon}"
    return None


# ---------------------------------------------------------------------------
# Прогноз к моменту прибытия
# ---------------------------------------------------------------------------

def _get_forecast_for_arrival(
    db: Session,
    zone_id: int,
    arrival_time: datetime,
) -> Forecast | None:
    """
    Выбираем прогноз с predicted_for ближайшим к arrival_time (не позже чем +30 мин),
    из самой последней генерации.
    """
    window_start = arrival_time - timedelta(minutes=30)
    window_end = arrival_time + timedelta(minutes=30)

    return (
        db.query(Forecast)
        .filter(
            Forecast.zone_id == zone_id,
            Forecast.predicted_for >= window_start,
            Forecast.predicted_for <= window_end,
        )
        .order_by(Forecast.generated_at.desc(), Forecast.predicted_for.asc())
        .first()
    )


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


def _score(
    free_count: int,
    capacity: int,
    confidence: float,
    duration_from_origin_seconds: int,
    pay: int,
    probability_free_space: float | None,
    duration_to_destination_seconds: int | None,
) -> float:
    """
    Итоговый score в диапазоне 0..1.

    Время в пути имеет высокий вес: парковка в 10 часах езды не должна
    конкурировать с парковкой в 10 минутах только из-за цены или ёмкости.
    """
    availability_score = _clamp(max(free_count, 0) / max(capacity, 1)) * 0.20
    confidence_score = _clamp(confidence) * 0.08
    duration_score = math.exp(-duration_from_origin_seconds / 1800.0) * 0.60
    destination_score = (
        math.exp(-duration_to_destination_seconds / 600.0)
        if duration_to_destination_seconds is not None
        else 1.0
    ) * 0.05
    pay_score = (1.0 / (1.0 + max(pay, 0) / 100.0)) * 0.02
    forecast_score = _clamp(probability_free_space or 0.0) * 0.05

    return round(
        availability_score
        + confidence_score
        + duration_score
        + destination_score
        + pay_score
        + forecast_score,
        4,
    )


def _candidate_sort_key(candidate: RouteCandidate) -> tuple[float, int, int, int, int]:
    return (
        -candidate.score,
        candidate.duration_from_origin_seconds,
        candidate.duration_to_destination_seconds if candidate.duration_to_destination_seconds is not None else 0,
        candidate.pay,
        -candidate.current_free_count,
    )


# ---------------------------------------------------------------------------
# Публичный интерфейс
# ---------------------------------------------------------------------------

def search_candidates(
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
    provider: str = _PROVIDER_INTERNAL,
    selected_zone_id: int | None = None,
) -> CandidateSearchResult:
    """
    Возвращает отсортированный список кандидатов и общий размер выдачи до limit.

    Если selected_zone_id указан, выборка ограничивается этой зоной; она всё
    равно должна пройти фильтры доступности и маршрутизируемости.
    """
    now = datetime.now(timezone.utc)

    query = db.query(ParkingZone).filter(ParkingZone.is_active.is_(True))

    if max_pay is not None:
        query = query.filter(ParkingZone.pay <= max_pay)
    if min_free_count is not None:
        query = query.filter((ParkingZone.capacity - ParkingZone.occupied) >= min_free_count)
    if min_confidence is not None:
        query = query.filter(ParkingZone.confidence >= min_confidence)
    if include_accessible is False:
        query = query.filter(or_(ParkingZone.is_accessible.is_(False), ParkingZone.is_accessible.is_(None)))
    if selected_zone_id is not None:
        query = query.filter(ParkingZone.parking_zone_id == selected_zone_id)

    zones: list[ParkingZone] = query.all()
    if not zones:
        return CandidateSearchResult(candidates=[], total_candidates=0)

    provider_key = _normalize_provider(provider)
    if provider_key not in _SUPPORTED_PROVIDERS:
        raise RoutingProviderError(f"Routing provider '{provider_key}' is not configured")

    zone_points = [_zone_centroid(zone) for zone in zones]
    origin_metrics = _metrics_from_origin(provider_key, origin, zone_points)
    destination_metrics = _metrics_to_destination(provider_key, zone_points, destination)

    candidates: list[RouteCandidate] = []

    for zone, metrics_from_origin, metrics_to_destination in zip(zones, origin_metrics, destination_metrics):
        if metrics_from_origin is None:
            continue

        if (
            max_duration_from_origin_seconds is not None
            and metrics_from_origin.duration_seconds > max_duration_from_origin_seconds
        ):
            continue

        if destination is not None and metrics_to_destination is None:
            continue

        if (
            metrics_to_destination is not None
            and max_distance_to_destination_meters is not None
            and metrics_to_destination.distance_meters > max_distance_to_destination_meters
        ):
            continue

        arrival_time = now + timedelta(seconds=metrics_from_origin.duration_seconds)
        forecast = _get_forecast_for_arrival(db, zone.parking_zone_id, arrival_time) if use_forecast else None
        pred_occupied = forecast.predicted_occupied if forecast else None
        pred_free = (zone.capacity - forecast.predicted_occupied) if forecast else None
        prob_free = forecast.probability_free_space if forecast else None
        forecast_conf = forecast.confidence if forecast else None

        current_free = max(zone.capacity - zone.occupied, 0)
        effective_free = pred_free if pred_free is not None else current_free
        effective_conf = forecast_conf if forecast_conf is not None else (zone.confidence or 0.0)

        score = _score(
            free_count=effective_free,
            capacity=zone.capacity,
            confidence=effective_conf,
            duration_from_origin_seconds=metrics_from_origin.duration_seconds,
            pay=zone.pay,
            probability_free_space=prob_free,
            duration_to_destination_seconds=metrics_to_destination.duration_seconds if metrics_to_destination else None,
        )

        candidates.append(
            RouteCandidate(
                zone_id=zone.parking_zone_id,
                camera_id=zone.camera_id,
                geometry=zone.geometry,
                zone_type=str(_enum_value(zone.zone_type)),
                location_type=_enum_value(zone.location_type),
                is_accessible=zone.is_accessible,
                pay=zone.pay,
                capacity=zone.capacity,
                current_occupied=zone.occupied,
                current_free_count=current_free,
                current_confidence=zone.confidence or 0.0,
                predicted_for_arrival=arrival_time,
                predicted_occupied=pred_occupied,
                predicted_free_count=pred_free,
                probability_free_space=prob_free,
                forecast_confidence=forecast_conf,
                distance_from_origin_meters=metrics_from_origin.distance_meters,
                duration_from_origin_seconds=metrics_from_origin.duration_seconds,
                distance_to_destination_meters=metrics_to_destination.distance_meters if metrics_to_destination else None,
                duration_to_destination_seconds=metrics_to_destination.duration_seconds if metrics_to_destination else None,
                score=score,
                rank=0,
            )
        )

    candidates.sort(key=_candidate_sort_key)

    for rank, candidate in enumerate(candidates, start=1):
        candidate.rank = rank

    return CandidateSearchResult(
        candidates=candidates[:limit],
        total_candidates=len(candidates),
    )


def find_candidates(
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
    provider: str = _PROVIDER_INTERNAL,
) -> list[RouteCandidate]:
    """Обратная совместимость для старых вызовов: возвращает только список."""
    return search_candidates(
        db=db,
        origin=origin,
        destination=destination,
        mode=mode,
        max_pay=max_pay,
        min_free_count=min_free_count,
        min_confidence=min_confidence,
        max_distance_to_destination_meters=max_distance_to_destination_meters,
        max_duration_from_origin_seconds=max_duration_from_origin_seconds,
        include_accessible=include_accessible,
        use_forecast=use_forecast,
        limit=limit,
        provider=provider,
        selected_zone_id=selected_zone_id,
    ).candidates
