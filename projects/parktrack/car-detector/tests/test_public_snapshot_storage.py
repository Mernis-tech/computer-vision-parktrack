import re
import unittest
from datetime import datetime, timezone

import cv2
import numpy as np

from detection.public_snapshot_storage import (
    S3SnapshotStorage,
    SnapshotStorageConfig,
)


class FakeS3Client:
    def __init__(self) -> None:
        self.calls = []

    def put_object(self, **kwargs) -> None:
        self.calls.append(kwargs)


class PublicSnapshotStorageTests(unittest.TestCase):
    def test_config_requires_public_base_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "SNAPSHOT_PUBLIC_BASE_URL"):
            SnapshotStorageConfig.from_environment(
                {"SNAPSHOT_S3_BUCKET": "public-bucket"}
            )

    def test_uploads_plaintext_with_independent_random_keys_and_urls(self) -> None:
        client = FakeS3Client()
        storage = S3SnapshotStorage(
            SnapshotStorageConfig(
                bucket="public-bucket",
                public_base_url="https://cdn.example.test/public-bucket",
                prefix="camera-snapshots",
            ),
            s3_client=client,
        )
        raw = np.zeros((16, 24, 3), dtype=np.uint8)
        annotated = np.full((16, 24, 3), 127, dtype=np.uint8)
        labels = b"0 0.500000 0.500000 0.250000 0.250000\n"

        result = storage.store_pair(
            camera_id=23,
            captured_at=datetime(2026, 7, 22, 12, 30, tzinfo=timezone.utc),
            raw_frame_bgr=raw,
            annotated_frame_bgr=annotated,
            labels_yolo=labels,
        )

        self.assertEqual(set(result), {"raw", "annotated", "labels"})
        self.assertEqual(len(client.calls), 3)

        key_pattern = re.compile(
            r"^camera-snapshots/[A-Za-z0-9]{32}/[A-Za-z0-9]{32}/"
            r"[A-Za-z0-9]{48}\.(?:jpg|txt)$"
        )
        keys = [call["Key"] for call in client.calls]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len({key.rsplit("/", 1)[0] for key in keys}), 3)
        self.assertTrue(all(key_pattern.fullmatch(key) for key in keys))

        by_variant = {call["Metadata"]["variant"]: call for call in client.calls}
        self.assertEqual(by_variant["labels"]["Body"], labels)
        self.assertEqual(
            by_variant["labels"]["ContentType"],
            "text/plain; charset=utf-8",
        )
        self.assertEqual(by_variant["raw"]["ContentType"], "image/jpeg")
        self.assertEqual(by_variant["annotated"]["ContentType"], "image/jpeg")

        raw_decoded = cv2.imdecode(
            np.frombuffer(by_variant["raw"]["Body"], dtype=np.uint8),
            cv2.IMREAD_COLOR,
        )
        self.assertIsNotNone(raw_decoded)

        for snapshot in result.values():
            payload = snapshot.as_dict()
            self.assertEqual(payload["object_key"], snapshot.object_key)
            self.assertEqual(
                payload["url"],
                f"https://cdn.example.test/public-bucket/{snapshot.object_key}",
            )
            self.assertNotIn("encryption_key_id", payload)


if __name__ == "__main__":
    unittest.main()
