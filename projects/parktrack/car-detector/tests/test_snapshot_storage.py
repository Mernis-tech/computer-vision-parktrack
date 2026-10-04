import base64
import unittest
from datetime import datetime, timezone

import cv2
import numpy as np
from cryptography.exceptions import InvalidTag

from detection.snapshot_storage import (
    MAGIC,
    S3SnapshotStorage,
    SnapshotStorageConfig,
    decrypt_snapshot,
    encode_yolo_labels,
    encrypt_snapshot,
)


class FakeS3Client:
    def __init__(self):
        self.objects = []

    def put_object(self, **kwargs):
        self.objects.append(kwargs)


class SnapshotEncryptionTest(unittest.TestCase):
    def test_round_trip_and_authenticated_header(self):
        key = bytes(range(32))
        header = {"camera_id": 42, "variant": "raw"}
        plaintext = b"jpeg bytes"
        encrypted = encrypt_snapshot(
            plaintext,
            key,
            header,
            nonce=b"n" * 12,
        )

        self.assertTrue(encrypted.startswith(MAGIC))
        self.assertNotIn(plaintext, encrypted)
        decoded_header, decoded_plaintext = decrypt_snapshot(encrypted, key)
        self.assertEqual(header, decoded_header)
        self.assertEqual(plaintext, decoded_plaintext)

        tampered = bytearray(encrypted)
        tampered[-1] ^= 1
        with self.assertRaises(InvalidTag):
            decrypt_snapshot(bytes(tampered), key)

    def test_configuration_requires_a_256_bit_key(self):
        environment = {
            "SNAPSHOT_S3_BUCKET": "snapshots",
            "SNAPSHOT_ENCRYPTION_KEY_ID": "key-2026-01",
            "SNAPSHOT_ENCRYPTION_KEY_BASE64": base64.b64encode(b"short").decode(),
        }
        with self.assertRaisesRegex(ValueError, "exactly 32 bytes"):
            SnapshotStorageConfig.from_environment(environment)


class YoloLabelsTest(unittest.TestCase):
    def test_encodes_normalized_yolo_detection_labels(self):
        labels = encode_yolo_labels(
            np.array(
                [
                    [10.0, 20.0, 50.0, 60.0],
                    [-5.0, 0.0, 105.0, 50.0],
                ],
                dtype=np.float32,
            ),
            np.array([0, 2], dtype=np.int32),
            image_width=100,
            image_height=80,
        )

        self.assertEqual(
            b"0 0.300000 0.500000 0.400000 0.500000\n"
            b"2 0.500000 0.312500 1.000000 0.625000\n",
            labels,
        )

    def test_encodes_no_detections_as_empty_file(self):
        labels = encode_yolo_labels(
            np.zeros((0, 4), dtype=np.float32),
            np.zeros((0,), dtype=np.int32),
            image_width=100,
            image_height=80,
        )

        self.assertEqual(b"", labels)


class S3SnapshotStorageTest(unittest.TestCase):
    def test_uploads_raw_annotated_and_labels_as_encrypted_objects(self):
        key = bytes(range(32))
        client = FakeS3Client()
        storage = S3SnapshotStorage(
            SnapshotStorageConfig(
                bucket="parktrack-test",
                prefix="snapshots",
                encryption_key=key,
                encryption_key_id="key-2026-01",
            ),
            s3_client=client,
        )
        raw = np.zeros((8, 12, 3), dtype=np.uint8)
        annotated = np.full((8, 12, 3), 255, dtype=np.uint8)
        labels = b"0 0.500000 0.500000 0.250000 0.500000\n"
        captured_at = datetime(2026, 7, 22, 8, 9, 10, tzinfo=timezone.utc)

        result = storage.store_pair(
            camera_id=17,
            captured_at=captured_at,
            raw_frame_bgr=raw,
            annotated_frame_bgr=annotated,
            labels_yolo=labels,
        )

        self.assertEqual({"raw", "annotated", "labels"}, set(result))
        self.assertEqual(3, len(client.objects))
        object_keys = [item["Key"] for item in client.objects]
        self.assertTrue(all("/camera-17/2026/07/22/" in key for key in object_keys))
        self.assertEqual(1, len({key.rsplit("/", 1)[0] for key in object_keys}))
        self.assertTrue(any(key.endswith("/raw.jpg.aesgcm") for key in object_keys))
        self.assertTrue(
            any(key.endswith("/annotated.jpg.aesgcm") for key in object_keys)
        )
        self.assertTrue(any(key.endswith("/labels.txt.aesgcm") for key in object_keys))

        for uploaded in client.objects:
            self.assertEqual("application/octet-stream", uploaded["ContentType"])
            header, plaintext = decrypt_snapshot(uploaded["Body"], key)
            self.assertEqual(17, header["camera_id"])
            self.assertEqual("key-2026-01", header["key_id"])
            if header["variant"] == "labels":
                self.assertEqual("text/plain; charset=utf-8", header["content_type"])
                self.assertEqual("yolo-v12-detection", header["annotation_format"])
                self.assertNotIn(labels, uploaded["Body"])
                self.assertEqual(labels, plaintext)
            else:
                decoded = cv2.imdecode(
                    np.frombuffer(plaintext, np.uint8), cv2.IMREAD_COLOR
                )
                self.assertIsNotNone(decoded)


if __name__ == "__main__":
    unittest.main()
