from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from typing import Any

os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_mock_engine  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.db_models import Forecast  # noqa: E402
from src.routers import forecasts  # noqa: E402


AT = datetime(2026, 7, 23, 16, 33, 5, tzinfo=timezone.utc)


def _map_row() -> dict[str, Any]:
    return {
        "zone_id": 9,
        "camera_id": 2,
        "capacity": 8,
        "predicted_occupied": 3,
        "predicted_free_count": 5,
        "probability_free_space": 0.95,
        "confidence": 0.9,
        "confidence_level": "high",
        "predicted_for": datetime(
            2026,
            7,
            23,
            16,
            30,
            tzinfo=timezone.utc,
        ),
        "generated_at": datetime(
            2026,
            7,
            23,
            15,
            0,
            tzinfo=timezone.utc,
        ),
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[34.35, 61.79], [34.36, 61.79]]],
        },
        "pay": 0,
        "zone_type": "parallel",
        "location_type": "street",
        "is_accessible": True,
        "is_active": True,
    }


def _statement(
    **overrides: Any,
):
    arguments = {
        "at": AT,
        "zone_id": None,
        "camera_id": None,
        "partner_id": None,
        "model_type": None,
        "generated_from": None,
        "generated_to": None,
        "from_": None,
        "to": None,
        "bbox": None,
        "is_active": None,
        "latest_model_only": False,
    }
    arguments.update(overrides)
    return forecasts._map_forecasts_statement(**arguments)


class ForecastMapStatementTests(unittest.TestCase):
    def test_uses_two_bounded_lateral_index_lookups(self):
        statement, params = _statement(
            bbox=(34.3471, 61.78765, 34.36887, 61.79272),
            is_active=True,
            latest_model_only=False,
        )
        sql = " ".join(statement.text.lower().split())

        self.assertIn("cross join lateral", sql)
        self.assertNotIn("row_number", sql)
        self.assertEqual(sql.count("f.zone_id = z.parking_zone_id"), 2)
        self.assertIn("f.predicted_for >= :at", sql)
        self.assertIn("f.predicted_for < :at", sql)
        self.assertIn("z.is_active is true", sql)
        self.assertIn("z.centroid_longitude >= :min_lon", sql)
        self.assertIn("z.centroid_longitude <= :max_lon", sql)
        self.assertIn("z.centroid_latitude >= :min_lat", sql)
        self.assertIn("z.centroid_latitude <= :max_lat", sql)
        self.assertEqual(params["at"], AT)
        self.assertEqual(params["min_lon"], 34.3471)
        self.assertEqual(params["max_lat"], 61.79272)

    def test_applies_forecast_filters_to_both_candidates(self):
        generated_from = datetime(2026, 7, 22, tzinfo=timezone.utc)
        generated_to = datetime(2026, 7, 23, tzinfo=timezone.utc)
        from_ = datetime(2026, 7, 23, 15, tzinfo=timezone.utc)
        to = datetime(2026, 7, 23, 18, tzinfo=timezone.utc)

        statement, params = _statement(
            zone_id=9,
            camera_id=3,
            partner_id=4,
            model_type="baseline",
            generated_from=generated_from,
            generated_to=generated_to,
            from_=from_,
            to=to,
            is_active=False,
        )
        sql = " ".join(statement.text.lower().split())

        for clause in (
            "f.camera_id = :camera_id",
            "f.partner_id = :partner_id",
            "f.model_type = :model_type",
            "f.generated_at >= :generated_from",
            "f.generated_at <= :generated_to",
            "f.predicted_for >= :from_",
            "f.predicted_for <= :to",
        ):
            self.assertEqual(sql.count(clause), 2)

        self.assertIn("z.parking_zone_id = :zone_id", sql)
        self.assertIn("z.is_active is false", sql)
        self.assertEqual(
            params,
            {
                "at": AT,
                "zone_id": 9,
                "camera_id": 3,
                "partner_id": 4,
                "model_type": "baseline",
                "generated_from": generated_from,
                "generated_to": generated_to,
                "from_": from_,
                "to": to,
            },
        )

    def test_latest_model_limits_candidates_to_latest_zone_generation(self):
        statement, _ = _statement(
            latest_model_only=True,
            model_type="baseline",
        )
        sql = " ".join(statement.text.lower().split())

        self.assertIn(
            "select generation_candidate.generated_at",
            sql,
        )
        self.assertIn(
            "order by generation_candidate.generated_at desc limit 1",
            sql,
        )
        self.assertEqual(
            sql.count("f.generated_at = latest_generation.generated_at"),
            2,
        )
        self.assertNotIn("group by", sql)


class ForecastLatestGenerationQueryTests(unittest.TestCase):
    def setUp(self):
        engine = create_mock_engine(
            "postgresql+psycopg2://",
            lambda *args, **kwargs: None,
        )
        self.db = Session(bind=engine)

    def tearDown(self):
        self.db.close()

    def test_uses_one_bounded_lateral_lookup_per_matching_zone(self):
        query = forecasts._latest_generation_query(
            db=self.db,
            zone_id=9,
            camera_id=3,
            partner_id=4,
            model_type="baseline",
            generated_from=None,
            generated_to=None,
            from_=None,
            to=None,
            bbox=(34.3471, 61.78765, 34.36887, 61.79272),
            is_active=True,
        )
        sql = " ".join(
            str(
                query.statement.compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            ).lower().split()
        )

        self.assertIn("join lateral", sql)
        self.assertIn(
            "order by generation_candidate.generated_at desc limit 1",
            sql,
        )
        self.assertIn(
            "forecasts.generated_at = latest_generation.generated_at",
            sql,
        )
        self.assertIn("parking_zones.parking_zone_id = 9", sql)
        self.assertIn("parking_zones.centroid_longitude >=", sql)
        self.assertNotIn("group by", sql)
        self.assertNotIn("row_number", sql)


class _FakeMappings:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def mappings(self) -> _FakeMappings:
        return _FakeMappings(self._rows)


class _FakeSession:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows
        self.calls: list[tuple[object, dict[str, object]]] = []

    def execute(
        self,
        statement: object,
        params: dict[str, object],
    ) -> _FakeResult:
        self.calls.append((statement, params))
        return _FakeResult(self._rows)


class ForecastMapExecutionTests(unittest.TestCase):
    def test_builds_map_response_with_one_database_round_trip(self):
        row = _map_row()
        db = _FakeSession([row])

        result = forecasts._list_map_forecasts(
            db=db,  # type: ignore[arg-type]
            at=AT,
            zone_id=None,
            camera_id=None,
            partner_id=None,
            model_type=None,
            generated_from=None,
            generated_to=None,
            from_=None,
            to=None,
            bbox="34.3471,61.78765,34.36887,61.79272",
            is_active=True,
            latest_model_only=False,
        )

        self.assertEqual(len(db.calls), 1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].zone_id, 9)
        self.assertEqual(result[0].predicted_free_count, 5)
        self.assertEqual(result[0].predicted_for, row["predicted_for"])

    def test_free_count_does_not_query_database(self):
        item = Forecast(capacity=12, predicted_occupied=7)

        self.assertEqual(forecasts._predicted_free_count(item), 5)


class ForecastEndpointTests(unittest.TestCase):
    def setUp(self):
        self.db = _FakeSession([_map_row()])

    def test_exact_map_request_uses_optimized_query(self):
        request_at = datetime(
            2026,
            7,
            23,
            16,
            33,
            5,
            669000,
            tzinfo=timezone.utc,
        )
        result = forecasts.list_forecasts(
            db=self.db,  # type: ignore[arg-type]
            zone_id=None,
            camera_id=None,
            partner_id=None,
            model_type=None,
            generated_from=None,
            generated_to=None,
            from_=None,
            to=None,
            at=request_at,
            latest_model_only=False,
            bbox="34.3471,61.78765,34.36887,61.79272",
            is_active=True,
            view="map",
        )

        self.assertEqual(result[0].zone_id, 9)
        self.assertEqual(len(self.db.calls), 1)

        statement, params = self.db.calls[0]
        sql = " ".join(statement.text.lower().split())
        self.assertIn("cross join lateral", sql)
        self.assertIn("z.is_active is true", sql)
        self.assertEqual(params["min_lon"], 34.3471)
        self.assertEqual(params["max_lat"], 61.79272)
        self.assertEqual(params["at"], request_at)

    def test_map_request_without_at_does_not_query_database(self):
        with self.assertRaises(forecasts.HTTPException) as raised:
            forecasts.list_forecasts(
                db=self.db,  # type: ignore[arg-type]
                zone_id=None,
                camera_id=None,
                partner_id=None,
                model_type=None,
                generated_from=None,
                generated_to=None,
                from_=None,
                to=None,
                at=None,
                latest_model_only=False,
                bbox="34.3471,61.78765,34.36887,61.79272",
                is_active=True,
                view="map",
            )

        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(len(self.db.calls), 0)

    def test_fastapi_route_exposes_is_active_filter(self):
        route = next(
            route
            for route in forecasts.router.routes
            if route.path == "/forecasts"
        )
        parameter_names = {
            parameter.name
            for parameter in route.dependant.query_params
        }

        self.assertIn("is_active", parameter_names)


if __name__ == "__main__":
    unittest.main()
