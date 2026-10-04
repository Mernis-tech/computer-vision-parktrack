"""PostgreSQL benchmark for the forecast map query.

The benchmark creates transaction-local tables, fills them with synthetic
forecast history, compares the former global ranking with the optimized query,
and rolls everything back. It does not modify application data.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import URL, create_engine, text
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

database_url = URL.create(
    "postgresql+psycopg2",
    username=os.environ["POSTGRES_USER"],
    password=os.environ["POSTGRES_PASSWORD"],
    host=os.environ.get("FORECAST_BENCHMARK_HOST", "127.0.0.1"),
    port=int(os.environ["POSTGRES_PORT"]),
    database=os.environ["POSTGRES_DB"],
)
os.environ["DATABASE_URL"] = database_url.render_as_string(hide_password=False)

from src.routers import forecasts  # noqa: E402


AT = datetime(2026, 7, 23, 16, 33, 5, tzinfo=timezone.utc)
BBOX = (34.3471, 61.78765, 34.36887, 61.79272)


def _plan_nodes(plan: dict[str, Any]):
    yield plan
    for child in plan.get("Plans", []):
        yield from _plan_nodes(child)


def _explain_ms(connection, statement, params: dict[str, object]) -> tuple[float, set[str]]:
    explain = text(
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
        + statement.text
    )
    document = connection.execute(explain, params).scalar_one()
    root = document[0]
    indexes = {
        node["Index Name"]
        for node in _plan_nodes(root["Plan"])
        if "Index Name" in node
    }
    return float(root["Execution Time"]), indexes


def _explain_select_ms(connection, statement) -> tuple[float, set[str]]:
    compiled = statement.compile(
        dialect=connection.dialect,
        compile_kwargs={"literal_binds": True},
    )
    document = connection.execute(
        text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + str(compiled))
    ).scalar_one()
    root = document[0]
    indexes = {
        node["Index Name"]
        for node in _plan_nodes(root["Plan"])
        if "Index Name" in node
    }
    return float(root["Execution Time"]), indexes


def main() -> None:
    row_count = int(os.environ.get("FORECAST_BENCHMARK_ROWS", "1000000"))
    engine = create_engine(database_url)

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(
                text(
                    """
                    CREATE TEMP TABLE parking_zones (
                        parking_zone_id INTEGER PRIMARY KEY,
                        geometry JSONB NOT NULL,
                        centroid_latitude DOUBLE PRECISION,
                        centroid_longitude DOUBLE PRECISION,
                        pay INTEGER NOT NULL,
                        zone_type TEXT NOT NULL,
                        location_type TEXT,
                        is_accessible BOOLEAN,
                        is_active BOOLEAN NOT NULL
                    ) ON COMMIT DROP
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TEMP TABLE forecasts (
                        forecast_id BIGSERIAL PRIMARY KEY,
                        zone_id INTEGER NOT NULL,
                        camera_id INTEGER,
                        partner_id INTEGER,
                        model_type TEXT NOT NULL,
                        model_version TEXT,
                        generated_at TIMESTAMPTZ NOT NULL,
                        predicted_for TIMESTAMPTZ NOT NULL,
                        capacity INTEGER NOT NULL,
                        predicted_occupied INTEGER NOT NULL,
                        predicted_free_count INTEGER GENERATED ALWAYS AS (
                            capacity - predicted_occupied
                        ) STORED,
                        probability_free_space DOUBLE PRECISION NOT NULL,
                        confidence DOUBLE PRECISION NOT NULL,
                        confidence_level TEXT,
                        metadata JSONB,
                        created_by_user_id INTEGER
                    ) ON COMMIT DROP
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO parking_zones (
                        parking_zone_id,
                        geometry,
                        centroid_latitude,
                        centroid_longitude,
                        pay,
                        zone_type,
                        location_type,
                        is_accessible,
                        is_active
                    )
                    SELECT
                        zone_id,
                        jsonb_build_object(
                            'type', 'Polygon',
                            'coordinates', jsonb_build_array(jsonb_build_array(
                                jsonb_build_array(longitude, latitude)
                            ))
                        ),
                        latitude,
                        longitude,
                        0,
                        'parallel',
                        'street',
                        TRUE,
                        zone_id <> 27
                    FROM (
                        SELECT
                            zone_id,
                            CASE
                                WHEN zone_id <= 27 THEN 61.788 + zone_id * 0.0001
                                ELSE 62.5 + zone_id * 0.0001
                            END AS latitude,
                            CASE
                                WHEN zone_id <= 27 THEN 34.348 + zone_id * 0.0005
                                ELSE 36.0 + zone_id * 0.0005
                            END AS longitude
                        FROM generate_series(1, 75) AS zone_id
                    ) AS zones
                    """
                )
            )

            started = time.perf_counter()
            connection.execute(
                text(
                    """
                    INSERT INTO forecasts (
                        zone_id,
                        camera_id,
                        partner_id,
                        model_type,
                        generated_at,
                        predicted_for,
                        capacity,
                        predicted_occupied,
                        probability_free_space,
                        confidence,
                        confidence_level
                    )
                    SELECT
                        zone_id,
                        1,
                        1,
                        'baseline',
                        TIMESTAMPTZ '2026-07-01 00:00:00+00'
                            + (sequence_number / 288) * INTERVAL '6 hours',
                        TIMESTAMPTZ '2026-07-23 00:00:00+00'
                            + (sequence_number % 288) * INTERVAL '5 minutes',
                        10,
                        item % 10,
                        0.8,
                        0.9,
                        'high'
                    FROM (
                        SELECT
                            item,
                            ((item - 1) % 75) + 1 AS zone_id,
                            (item - 1) / 75 AS sequence_number
                        FROM generate_series(1, :row_count) AS item
                    ) AS generated_points
                    """
                ),
                {"row_count": row_count},
            )
            connection.execute(
                text(
                    """
                    CREATE UNIQUE INDEX uq_forecast_point
                    ON forecasts (zone_id, generated_at, predicted_for)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE INDEX idx_forecasts_routing_lookup
                    ON forecasts (
                        zone_id,
                        predicted_for,
                        generated_at DESC,
                        forecast_id DESC
                    )
                    """
                )
            )
            connection.execute(text("ANALYZE forecasts"))
            connection.execute(text("ANALYZE parking_zones"))
            seed_seconds = time.perf_counter() - started

            old_statement = text(
                """
                SELECT f.forecast_id
                FROM forecasts AS f
                JOIN (
                    SELECT
                        ranked_source.forecast_id,
                        ROW_NUMBER() OVER (
                            PARTITION BY ranked_source.zone_id
                            ORDER BY
                                ABS(EXTRACT(
                                    EPOCH FROM ranked_source.predicted_for - :at
                                )) ASC,
                                ranked_source.generated_at DESC,
                                ranked_source.forecast_id DESC
                        ) AS rn
                    FROM forecasts AS ranked_source
                ) AS ranked ON ranked.forecast_id = f.forecast_id
                WHERE ranked.rn = 1
                  AND f.zone_id <= 27
                ORDER BY f.predicted_for ASC
                """
            )
            new_statement, params = forecasts._map_forecasts_statement(
                at=AT,
                zone_id=None,
                camera_id=None,
                partner_id=None,
                model_type=None,
                generated_from=None,
                generated_to=None,
                from_=None,
                to=None,
                bbox=BBOX,
                is_active=True,
                latest_model_only=False,
            )
            latest_statement, latest_params = forecasts._map_forecasts_statement(
                at=AT,
                zone_id=None,
                camera_id=None,
                partner_id=None,
                model_type=None,
                generated_from=None,
                generated_to=None,
                from_=None,
                to=None,
                bbox=BBOX,
                is_active=True,
                latest_model_only=True,
            )

            old_ms, _ = _explain_ms(connection, old_statement, {"at": AT})
            new_ms, indexes = _explain_ms(connection, new_statement, params)
            latest_ms, latest_indexes = _explain_ms(
                connection,
                latest_statement,
                latest_params,
            )
            latest_series_query = forecasts._latest_generation_query(
                db=Session(bind=connection),
                zone_id=None,
                camera_id=None,
                partner_id=None,
                model_type=None,
                generated_from=None,
                generated_to=None,
                from_=None,
                to=None,
                bbox=BBOX,
                is_active=True,
            )
            latest_series_ms, latest_series_indexes = _explain_select_ms(
                connection,
                latest_series_query.statement,
            )
            rows = connection.execute(new_statement, params).mappings().all()
            latest_rows = (
                connection.execute(latest_statement, latest_params)
                .mappings()
                .all()
            )
            latest_series_rows = latest_series_query.all()

            if len(rows) != 26:
                raise AssertionError(f"expected 26 active map zones, got {len(rows)}")
            if len(latest_rows) != 26:
                raise AssertionError(
                    f"expected 26 latest-model map zones, got {len(latest_rows)}"
                )
            expected_time = datetime(
                2026,
                7,
                23,
                16,
                35,
                tzinfo=timezone.utc,
            )
            if any(row["predicted_for"] != expected_time for row in rows):
                raise AssertionError("optimized query selected a non-nearest forecast")
            if "idx_forecasts_routing_lookup" not in indexes:
                raise AssertionError(f"forecast lookup index was not used: {sorted(indexes)}")
            if new_ms >= 2_000:
                raise AssertionError(f"optimized query exceeded 2 seconds: {new_ms:.3f} ms")
            if "uq_forecast_point" not in latest_indexes:
                raise AssertionError(
                    "latest-generation index was not used: "
                    f"{sorted(latest_indexes)}"
                )
            if latest_ms >= 2_000:
                raise AssertionError(
                    "latest-model query exceeded 2 seconds: "
                    f"{latest_ms:.3f} ms"
                )
            generations_by_zone: dict[int, set[datetime]] = {}
            for forecast in latest_series_rows:
                generations_by_zone.setdefault(forecast.zone_id, set()).add(
                    forecast.generated_at
                )
            if len(generations_by_zone) != 26:
                raise AssertionError(
                    "expected latest series for 26 active zones, got "
                    f"{len(generations_by_zone)}"
                )
            if any(len(generations) != 1 for generations in generations_by_zone.values()):
                raise AssertionError("latest series contains multiple generations")
            if len(latest_series_rows) > 26 * 288:
                raise AssertionError("latest series returned more than one horizon per zone")
            if "uq_forecast_point" not in latest_series_indexes:
                raise AssertionError(
                    "latest-series index was not used: "
                    f"{sorted(latest_series_indexes)}"
                )
            if latest_series_ms >= 2_000:
                raise AssertionError(
                    "latest-series query exceeded 2 seconds: "
                    f"{latest_series_ms:.3f} ms"
                )

            print(
                "forecast_map "
                f"rows={row_count} result_zones={len(rows)} "
                f"seed_seconds={seed_seconds:.3f} "
                f"old_ms={old_ms:.3f} new_ms={new_ms:.3f} "
                f"latest_ms={latest_ms:.3f} "
                f"latest_series_rows={len(latest_series_rows)} "
                f"latest_series_ms={latest_series_ms:.3f} "
                f"speedup={old_ms / new_ms:.1f}x"
            )
        finally:
            transaction.rollback()
            engine.dispose()


if __name__ == "__main__":
    main()
