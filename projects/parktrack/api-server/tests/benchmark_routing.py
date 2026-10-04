"""Нестрогий локальный benchmark подготовки кластерных соседств."""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.routers import routing
from src.schemas.routing import GeoPoint


def target(zone_id: int):
    point = GeoPoint(
        latitude=55.75 + (zone_id % 20) * 0.0008,
        longitude=37.61 + (zone_id // 20) * 0.0008,
    )
    return routing._ZoneTarget(
        zone=SimpleNamespace(parking_zone_id=zone_id),
        point=point,
        anchor_distance_meters=zone_id,
        current_occupied=2,
        current_free_count=8,
        current_confidence=0.9,
    )


def main() -> None:
    pool = [target(index) for index in range(160)]
    candidates = pool[:32]

    started = time.perf_counter()
    old_count = 0
    for candidate in candidates:
        for alternative in pool:
            if candidate.zone.parking_zone_id != alternative.zone.parking_zone_id:
                distance = routing._haversine_meters(candidate.point, alternative.point)
                old_count += distance <= routing.CLUSTER_RADIUS_METERS
    old_ms = (time.perf_counter() - started) * 1_000

    started = time.perf_counter()
    neighbors = routing._build_cluster_neighbors(candidates, pool)
    new_count = sum(len(items) for items in neighbors.values())
    new_ms = (time.perf_counter() - started) * 1_000

    if new_count != old_count:
        raise AssertionError(f"neighbor mismatch: old={old_count}, new={new_count}")
    print(f"cluster_neighbors old_ms={old_ms:.3f} new_ms={new_ms:.3f} pairs={new_count}")

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    forecasts = [
        SimpleNamespace(
            forecast_id=index,
            predicted_for=base + timedelta(minutes=5 * index),
            generated_at=base,
        )
        for index in range(96)
    ]
    series = routing._ForecastSeries(
        forecasts=forecasts,
        predicted_timestamps=[
            routing._datetime_timestamp(item.predicted_for) for item in forecasts
        ],
    )
    arrivals = [
        base + timedelta(seconds=(index * 137) % (8 * 60 * 60))
        for index in range(5_000)
    ]

    started = time.perf_counter()
    old_ids = [
        min(
            forecasts,
            key=lambda forecast: (
                routing._seconds_between(forecast.predicted_for, arrival),
                -routing._datetime_timestamp(forecast.generated_at),
                -forecast.forecast_id,
            ),
        ).forecast_id
        for arrival in arrivals
    ]
    forecast_old_ms = (time.perf_counter() - started) * 1_000

    started = time.perf_counter()
    new_ids = [
        routing._pick_forecast_for_arrival(series, arrival).forecast_id
        for arrival in arrivals
    ]
    forecast_new_ms = (time.perf_counter() - started) * 1_000
    if new_ids != old_ids:
        raise AssertionError("binary forecast lookup changed selection semantics")
    print(
        "forecast_lookup "
        f"old_ms={forecast_old_ms:.3f} new_ms={forecast_new_ms:.3f} "
        f"lookups={len(arrivals)} points={len(forecasts)}"
    )


if __name__ == "__main__":
    main()
