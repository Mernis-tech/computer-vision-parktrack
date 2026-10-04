from __future__ import annotations

import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import requests
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("GEOAPIFY_API_KEY", "test-key")

from src.db_models import Forecast  # noqa: E402
from src.routers import routing  # noqa: E402
from src.schemas.routing import GeoPoint  # noqa: E402


class _Response:
    def __init__(
        self,
        status_code: int = 200,
        payload: object | None = None,
        text: str = "provider response",
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, ValueError):
            raise self._payload
        return self._payload


def _target(
    zone_id: int,
    distance: int | None = None,
    free: int = 4,
    capacity: int | None = None,
):
    point = GeoPoint(latitude=55.75 + zone_id * 0.00005, longitude=37.61)
    capacity = capacity if capacity is not None else max(10, free)
    zone = SimpleNamespace(
        parking_zone_id=zone_id,
        camera_id=None,
        geometry={"type": "Point", "coordinates": [point.longitude, point.latitude]},
        zone_type="public",
        location_type=None,
        is_accessible=False,
        pay=0,
        capacity=capacity,
        occupied=capacity - free,
        confidence=0.9,
        occupancy_updated_at=datetime.now(timezone.utc),
    )
    return routing._ZoneTarget(
        zone=zone,
        point=point,
        anchor_distance_meters=distance if distance is not None else zone_id * 10,
        current_occupied=capacity - free,
        current_free_count=free,
        current_confidence=0.9,
    )


def _matrix_payload(target_count: int) -> dict[str, object]:
    return {
        "sources_to_targets": [[
            {"distance": 100 + index, "time": 60 + index}
            for index in range(target_count)
        ]]
    }


def _search(pool, **overrides):
    arguments = dict(
        db=SimpleNamespace(),
        origin=GeoPoint(latitude=55.75, longitude=37.61),
        destination=None,
        mode="find_parking",
        max_pay=None,
        min_free_count=None,
        min_confidence=None,
        max_distance_to_destination_meters=None,
        max_duration_from_origin_seconds=None,
        include_accessible=None,
        use_forecast=False,
        limit=10,
    )
    arguments.update(overrides)
    with patch.object(routing, "_query_zone_targets_near_anchor", return_value=pool):
        return routing._search_candidates(**arguments)


class RoutingProviderTests(unittest.TestCase):
    def test_fast_provider_response_and_timeout_tuple(self) -> None:
        pool = [_target(index) for index in range(1, 5)]
        with patch.object(
            routing.requests,
            "post",
            return_value=_Response(payload=_matrix_payload(len(pool))),
        ) as post:
            result = _search(pool)

        self.assertEqual(result.provider, "geoapify")
        self.assertEqual(len(result.candidates), len(pool))
        timeout = post.call_args.kwargs["timeout"]
        self.assertIsInstance(timeout, tuple)
        self.assertLessEqual(timeout[0], 0.25)
        self.assertLessEqual(timeout[1], 0.9)
        self.assertNotEqual(timeout, 15)

    def test_timeout_uses_local_fallback_within_budget(self) -> None:
        pool = [_target(index) for index in range(1, 5)]

        def slow_timeout(*args, **kwargs):
            connect, read = kwargs["timeout"]
            time.sleep(connect + read + 0.01)
            raise requests.Timeout("simulated timeout")

        started = time.monotonic()
        with patch.dict(
            os.environ,
            {
                "ROUTING_SEARCH_BUDGET_SECONDS": "0.18",
                "ROUTING_PROVIDER_CONNECT_TIMEOUT_SECONDS": "0.03",
                "ROUTING_PROVIDER_READ_TIMEOUT_SECONDS": "0.05",
            },
        ), patch.object(routing.requests, "post", side_effect=slow_timeout):
            result = _search(pool)
        elapsed = time.monotonic() - started

        self.assertEqual(result.provider, "internal")
        self.assertEqual(result.fallback_reason, "provider_timeout")
        self.assertTrue(result.candidates)
        self.assertLess(elapsed, 0.28)

    def test_5xx_and_invalid_response_fall_back(self) -> None:
        pool = [_target(1)]
        matrix_distance_error = (
            '{"statusCode":400,"error":"Bad Request","message":'
            '"Too long sum distance. Estimated sum distance is 428229726 meter(s)."}'
        )
        responses = (
            (_Response(status_code=503, payload={}), "provider_5xx"),
            (_Response(status_code=429, payload={}), "provider_429"),
            (
                _Response(status_code=400, payload={}, text=matrix_distance_error),
                "provider_matrix_distance_limit",
            ),
            (_Response(payload=ValueError("bad json")), "invalid_provider_response"),
            (_Response(payload={"unexpected": []}), "invalid_provider_response"),
        )
        for response, expected_reason in responses:
            with self.subTest(expected_reason=expected_reason), patch.object(
                routing.requests, "post", return_value=response
            ):
                result = _search(pool)
                self.assertEqual(result.provider, "internal")
                self.assertEqual(result.fallback_reason, expected_reason)
                self.assertEqual(len(result.candidates), 1)

    def test_oversized_matrix_falls_back_without_provider_call(self) -> None:
        pool = [_target(index) for index in range(1, 33)]
        far_point = GeoPoint(latitude=-55.0, longitude=-140.0)
        pool = [
            routing._ZoneTarget(
                zone=item.zone,
                point=far_point,
                anchor_distance_meters=18_000_000 + index,
                current_occupied=item.current_occupied,
                current_free_count=item.current_free_count,
                current_confidence=item.current_confidence,
            )
            for index, item in enumerate(pool)
        ]
        with patch.object(routing.requests, "post") as post:
            result = _search(pool)

        post.assert_not_called()
        self.assertEqual(result.provider, "internal")
        self.assertEqual(result.fallback_reason, "provider_matrix_distance_limit")
        self.assertTrue(result.candidates)


class RoutingSelectionTests(unittest.TestCase):
    @staticmethod
    def _settings(max_matrix_targets: int) -> routing._RoutingSettings:
        return routing._RoutingSettings(
            search_budget_seconds=1.8,
            provider_connect_timeout_seconds=0.25,
            provider_read_timeout_seconds=0.9,
            provider_max_estimated_matrix_distance_meters=280_000_000,
            max_matrix_targets=max_matrix_targets,
            road_detour_factor=1.25,
            average_driving_speed_kph=30.0,
        )

    def test_dynamic_matrix_limit(self) -> None:
        settings = self._settings(32)
        pool = [_target(index) for index in range(1, 81)]
        self.assertEqual(len(routing._select_matrix_targets(pool, 2, settings)), 12)
        self.assertEqual(len(routing._select_matrix_targets(pool, 10, settings)), 32)

    def test_explicit_zone_is_always_included(self) -> None:
        settings = self._settings(12)
        pool = [_target(index, distance=index * 100) for index in range(1, 31)]
        selected = routing._select_matrix_targets(
            pool, requested_limit=1, settings=settings, selected_zone_id=30
        )
        self.assertIn(30, {item.zone.parking_zone_id for item in selected})
        self.assertEqual(len(selected), 12)

    def test_candidate_search_has_at_most_two_radii(self) -> None:
        self.assertLessEqual(len(routing._candidate_query_radii("find_parking", None)), 2)
        self.assertLessEqual(
            len(routing._candidate_query_radii("route_to_destination", 20_000)), 2
        )

    def test_filters_are_preserved(self) -> None:
        query = routing._base_zone_query(
            Session(), max_pay=100, include_accessible=False, selected_zone_id=None
        )
        sql = str(query.statement.compile(dialect=postgresql.dialect()))
        self.assertIn("parking_zones.is_active IS true", sql)
        self.assertIn("parking_zones.pay <=", sql)
        self.assertIn("parking_zones.is_accessible IS false", sql)

        pool = [_target(1, free=0), _target(2, free=3)]
        with patch.object(
            routing.requests, "post", return_value=_Response(payload=_matrix_payload(2))
        ):
            result = _search(pool, min_free_count=1)
        self.assertEqual([item.zone_id for item in result.candidates], [2])


class ForecastSelectionTests(unittest.TestCase):
    @staticmethod
    def _forecast(forecast_id: int, predicted_for: datetime, generated_at: datetime):
        return Forecast(
            forecast_id=forecast_id,
            zone_id=1,
            predicted_for=predicted_for,
            generated_at=generated_at,
            capacity=10,
            predicted_occupied=5,
            probability_free_space=0.5,
            confidence=0.8,
            model_type="test",
        )

    def test_binary_selection_matches_previous_boundary_semantics(self) -> None:
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        forecasts = [
            self._forecast(1, base, base - timedelta(minutes=5)),
            self._forecast(2, base + timedelta(minutes=10), base - timedelta(minutes=4)),
            self._forecast(3, base + timedelta(minutes=20), base - timedelta(minutes=3)),
        ]
        series = routing._ForecastSeries(
            forecasts=forecasts,
            predicted_timestamps=[
                routing._datetime_timestamp(f.predicted_for) for f in forecasts
            ],
        )
        for arrival in (
            base - timedelta(seconds=1),
            base,
            base + timedelta(minutes=5),
            base + timedelta(minutes=10),
            base + timedelta(minutes=25),
        ):
            old = min(
                forecasts,
                key=lambda forecast: (
                    routing._seconds_between(forecast.predicted_for, arrival),
                    -routing._datetime_timestamp(forecast.generated_at),
                    -forecast.forecast_id,
                ),
            )
            new = routing._pick_forecast_for_arrival(series, arrival)
            self.assertIsNotNone(new)
            self.assertEqual(new.forecast_id, old.forecast_id)

    def test_latest_generation_is_selected_in_sql(self) -> None:
        statement = routing._latest_forecasts_statement(
            [1, 2],
            datetime(2026, 1, 1),
            datetime(2026, 1, 2),
        )
        sql = str(statement.compile(dialect=postgresql.dialect())).lower()
        self.assertIn("row_number() over", sql)
        self.assertIn("partition by forecasts.zone_id, forecasts.predicted_for", sql)
        self.assertIn("forecasts.generated_at desc, forecasts.forecast_id desc", sql)
        self.assertIn("generation_rank", sql)


class DestinationRankingTests(unittest.TestCase):
    @staticmethod
    def _context(
        target,
        *,
        drive_seconds: int,
        walk_seconds: int | None,
        request_context: routing._RankingRequestContext,
        use_forecast: bool = False,
    ) -> routing._CandidateContext:
        routed = routing._RoutedCandidate(
            zone_target=target,
            distance_from_origin_meters=drive_seconds * 8,
            duration_from_origin_seconds=drive_seconds,
            distance_to_destination_meters=walk_seconds,
            duration_to_destination_seconds=walk_seconds,
            arrival_time=datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
        )
        return routing._build_ranking_context(
            routed=routed,
            request_context=request_context,
            use_forecast=use_forecast,
        )

    @staticmethod
    def _request_context(targets) -> routing._RankingRequestContext:
        return routing._RankingRequestContext(
            forecasts_by_zone={},
            cluster_neighbors=routing._build_cluster_neighbors(targets, targets),
            effective_state_cache={},
        )

    def test_four_spaces_at_destination_beat_twenty_spaces_five_minutes_away(self) -> None:
        near = _target(1, free=4, capacity=10)
        far = _target(2, free=20, capacity=30)
        request_context = self._request_context([near, far])

        ranked = routing._finalize_contexts([
            self._context(
                near,
                drive_seconds=600,
                walk_seconds=20,
                request_context=request_context,
            ),
            self._context(
                far,
                drive_seconds=600,
                walk_seconds=300,
                request_context=request_context,
            ),
        ])

        self.assertEqual([candidate.zone_id for candidate in ranked], [1, 2])
        self.assertEqual(
            ranked[0].ranking_explanation.peer_better_availability_penalty_seconds,
            0.0,
        )

    def test_two_spaces_at_destination_beat_large_excess_supply_farther_away(self) -> None:
        near = _target(1, free=2, capacity=10)
        far = _target(2, free=100, capacity=120)
        request_context = self._request_context([near, far])

        ranked = routing._finalize_contexts([
            self._context(
                near,
                drive_seconds=600,
                walk_seconds=20,
                request_context=request_context,
            ),
            self._context(
                far,
                drive_seconds=600,
                walk_seconds=300,
                request_context=request_context,
            ),
        ])

        self.assertEqual([candidate.zone_id for candidate in ranked], [1, 2])

    def test_single_space_at_destination_keeps_scarcity_risk(self) -> None:
        near = _target(1, free=1, capacity=10)
        far = _target(2, free=20, capacity=30)
        request_context = routing._RankingRequestContext(
            forecasts_by_zone={},
            cluster_neighbors={},
            effective_state_cache={},
        )

        ranked = routing._finalize_contexts([
            self._context(
                near,
                drive_seconds=600,
                walk_seconds=20,
                request_context=request_context,
            ),
            self._context(
                far,
                drive_seconds=600,
                walk_seconds=300,
                request_context=request_context,
            ),
        ])

        self.assertEqual([candidate.zone_id for candidate in ranked], [2, 1])


    def test_dominated_candidate_cannot_win_after_bonus_saturation(self) -> None:
        arrival = datetime(2026, 7, 22, 17, 21, tzinfo=timezone.utc)
        worse = _target(75, free=10, capacity=11)
        better = _target(66, free=20, capacity=24)

        forecasts = {
            75: Forecast(
                forecast_id=75,
                zone_id=75,
                predicted_for=arrival,
                generated_at=arrival - timedelta(minutes=5),
                capacity=11,
                predicted_occupied=3,
                probability_free_space=0.9888,
                confidence=0.7776,
                model_type="test",
            ),
            66: Forecast(
                forecast_id=66,
                zone_id=66,
                predicted_for=arrival,
                generated_at=arrival - timedelta(minutes=5),
                capacity=24,
                predicted_occupied=8,
                probability_free_space=0.9965,
                confidence=0.5452,
                model_type="test",
            ),
        }
        request_context = routing._RankingRequestContext(
            forecasts_by_zone={
                zone_id: routing._ForecastSeries(
                    forecasts=[forecast],
                    predicted_timestamps=[routing._datetime_timestamp(arrival)],
                )
                for zone_id, forecast in forecasts.items()
            },
            cluster_neighbors=routing._build_cluster_neighbors(
                [worse, better], [worse, better]
            ),
            effective_state_cache={},
        )

        ranked = routing._finalize_contexts([
            self._context(
                worse,
                drive_seconds=92,
                walk_seconds=276,
                request_context=request_context,
                use_forecast=True,
            ),
            self._context(
                better,
                drive_seconds=59,
                walk_seconds=86,
                request_context=request_context,
                use_forecast=True,
            ),
        ])

        self.assertEqual([candidate.zone_id for candidate in ranked], [66, 75])
        self.assertGreater(
            ranked[1].ranking_explanation.generalized_cost_seconds,
            ranked[0].ranking_explanation.generalized_cost_seconds,
        )
        self.assertNotEqual(ranked[0].score, ranked[1].score)

    def test_destination_priority_uses_availability_at_arrival(self) -> None:
        arrival = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        near = _target(1, free=4, capacity=10)
        far = _target(2, free=100, capacity=120)

        near_forecast = Forecast(
            forecast_id=1,
            zone_id=1,
            predicted_for=arrival,
            generated_at=arrival - timedelta(minutes=5),
            capacity=10,
            predicted_occupied=8,
            probability_free_space=0.7,
            confidence=0.9,
            model_type="test",
        )
        far_forecast = Forecast(
            forecast_id=2,
            zone_id=2,
            predicted_for=arrival,
            generated_at=arrival - timedelta(minutes=5),
            capacity=120,
            predicted_occupied=20,
            probability_free_space=0.99,
            confidence=0.9,
            model_type="test",
        )

        request_context = routing._RankingRequestContext(
            forecasts_by_zone={
                1: routing._ForecastSeries(
                    forecasts=[near_forecast],
                    predicted_timestamps=[routing._datetime_timestamp(arrival)],
                ),
                2: routing._ForecastSeries(
                    forecasts=[far_forecast],
                    predicted_timestamps=[routing._datetime_timestamp(arrival)],
                ),
            },
            cluster_neighbors=routing._build_cluster_neighbors([near, far], [near, far]),
            effective_state_cache={},
        )

        ranked = routing._finalize_contexts([
            self._context(
                near,
                drive_seconds=600,
                walk_seconds=20,
                request_context=request_context,
                use_forecast=True,
            ),
            self._context(
                far,
                drive_seconds=600,
                walk_seconds=300,
                request_context=request_context,
                use_forecast=True,
            ),
        ])

        self.assertEqual(ranked[0].zone_id, 1)
        self.assertEqual(ranked[0].ranking_explanation.effective_free_count, 2)


if __name__ == "__main__":
    unittest.main()
