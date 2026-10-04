from __future__ import annotations

from datetime import datetime, timezone
import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite://")

from src.db_models import OccupancyObservation  # noqa: E402
from src.routers import analytics, cameras  # noqa: E402


class SnapshotRouteContractTests(unittest.TestCase):
    def test_separate_detection_artifact_endpoints_remain_removed(self):
        analytics_paths = {route.path for route in analytics.router.routes}
        camera_paths = {route.path for route in cameras.router.routes}

        self.assertNotIn(
            "/admin/analytics/detections/{detection_run_id}/snapshot",
            analytics_paths,
        )
        self.assertNotIn(
            "/admin/analytics/detections/{detection_run_id}/labels",
            analytics_paths,
        )
        self.assertNotIn("/cameras/{camera_id}/snapshot", camera_paths)

    def test_detection_response_keeps_public_s3_artifact_urls(self):
        raw_url = "https://s3.example.test/detections/42/raw.jpg"
        annotated_url = "https://s3.example.test/detections/42/annotated.jpg"
        labels_url = "https://s3.example.test/detections/42/labels.txt"
        observed_at = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
        detection = OccupancyObservation(
            observation_id=42,
            zone_id=9,
            camera_id=17,
            source_type="detector",
            capacity=8,
            occupied=3,
            confidence=0.9,
            observed_at=observed_at,
            ingested_at=observed_at,
            metadata_json={
                "snapshots": {
                    "raw": {"url": raw_url},
                    "annotated": {"url": annotated_url},
                    "labels": {"url": labels_url},
                }
            },
        )

        result = analytics._serialize_detection_run(
            db=None,  # type: ignore[arg-type]
            observation=detection,
            feedback_ids=set(),
        )

        self.assertEqual(result.raw_snapshot_url, raw_url)
        self.assertEqual(result.annotated_snapshot_url, annotated_url)
        self.assertEqual(result.yolo_labels_url, labels_url)

    def test_invalid_or_missing_s3_artifact_urls_are_null(self):
        metadata = {
            "snapshots": {
                "raw": {"url": "   "},
                "annotated": {"object_key": "annotated.jpg"},
                "labels": "https://s3.example.test/labels.txt",
            }
        }

        self.assertIsNone(analytics._available_artifact_url(metadata, "raw"))
        self.assertIsNone(analytics._available_artifact_url(metadata, "annotated"))
        self.assertIsNone(analytics._available_artifact_url(metadata, "labels"))


if __name__ == "__main__":
    unittest.main()
