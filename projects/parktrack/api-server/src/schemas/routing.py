from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


RoutingProvider = Literal["geoapify", "yandex", "internal", "external"]


# ---------------------------------------------------------------------------
# Вспомогательные типы
# ---------------------------------------------------------------------------

class GeoPoint(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class RankingExplanation(BaseModel):
    tier: int
    tier_label: str

    generalized_cost_seconds: float
    base_drive_seconds: int
    walk_to_destination_seconds: int | None

    current_free_count: int
    predicted_free_count: int | None
    effective_free_count: int

    availability_probability: float
    availability_strength: float

    cluster_strength: float
    nearby_alternative_count: int
    nearby_good_alternative_count: int
    nearby_effective_free_count: int
    nearest_good_alternative_distance_meters: int | None

    price_penalty_seconds: float
    scarcity_penalty_seconds: float
    confidence_penalty_seconds: float
    cluster_bonus_seconds: float
    availability_bonus_seconds: float
    peer_better_availability_penalty_seconds: float
    unreasonable_detour_penalty_seconds: float

    reasons: list[str]


# ---------------------------------------------------------------------------
# RouteCandidate
# ---------------------------------------------------------------------------

class RouteCandidate(BaseModel):
    zone_id: int
    camera_id: int | None
    geometry: Any
    zone_type: str
    location_type: str | None
    is_accessible: bool | None
    pay: int
    capacity: int
    current_occupied: int
    current_free_count: int
    current_confidence: float
    predicted_for_arrival: datetime
    predicted_occupied: int | None
    predicted_free_count: int | None
    probability_free_space: float | None
    forecast_confidence: float | None
    distance_from_origin_meters: int
    duration_from_origin_seconds: int
    distance_to_destination_meters: int | None
    duration_to_destination_seconds: int | None
    score: float
    rank: int

    # Для новых ответов поле всегда заполняется.
    # Optional оставлен только чтобы старые сохранённые маршруты без этого поля не ломали GET.
    ranking_explanation: RankingExplanation | None = None


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

class RouteResponse(BaseModel):
    route_id: int
    user_id: int
    mode: str
    provider: str
    origin: GeoPoint
    destination: GeoPoint | None
    selected_zone_id: int | None
    selected_candidate: RouteCandidate | None
    eta_seconds: int | None
    arrival_time: datetime | None
    polyline: str | None
    deeplink_url: str | None
    status: str
    created_at: datetime
    updated_at: datetime


class RouteListResponse(BaseModel):
    items: list[RouteResponse]
    total: int
    top: int
    offset: int


# ---------------------------------------------------------------------------
# Запросы
# ---------------------------------------------------------------------------

class RoutingRequestBase(BaseModel):
    mode: Literal["find_parking", "route_to_destination"]
    origin: GeoPoint
    destination: GeoPoint | None = None

    max_pay: int | None = Field(None, ge=0)
    min_free_count: int | None = Field(None, ge=0)
    min_confidence: float | None = Field(None, ge=0.0, le=1.0)
    max_distance_to_destination_meters: int | None = Field(None, ge=0)
    max_duration_from_origin_seconds: int | None = Field(None, ge=0)
    include_accessible: bool | None = None

    limit: int = Field(10, ge=1, le=50)
    use_forecast: bool = False

    # Для совместимости с контрактом поле оставляем.
    # Фактически текущая реализация маршрутизации использует Geoapify.
    provider: RoutingProvider = "geoapify"

    @model_validator(mode="after")
    def destination_required_for_route_mode(self) -> "RoutingRequestBase":
        if self.mode == "route_to_destination" and self.destination is None:
            raise ValueError("destination is required for mode=route_to_destination")
        return self


class SearchRoutingRequest(RoutingRequestBase):
    pass


class CreateRouteRequest(RoutingRequestBase):
    selected_zone_id: int | None = None


class SearchRoutingResponse(BaseModel):
    mode: str
    provider: str
    generated_at: datetime
    selected_zone_id: int | None
    total_candidates: int
    candidates: list[RouteCandidate]


class UpdateRouteRequest(BaseModel):
    status: Literal["active", "completed", "cancelled", "replaced"] | None = None
    selected_zone_id: int | None = None
    provider: RoutingProvider | None = None