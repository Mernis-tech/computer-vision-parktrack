from __future__ import annotations

import base64
import io
import json
import struct
import unittest

from botocore.exceptions import ClientError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from src.services.snapshot_storage import (
    MAGIC,
    MAX_HEADER_SIZE_BYTES,
    S3SnapshotReader,
    SnapshotInvalidError,
    SnapshotReference,
    SnapshotStorageSettings,
    SnapshotUnavailableError,
    snapshot_reference_from_metadata,
)


TEST_KEY = bytes(range(32))
TEST_KEY_ID = "snapshot-key-test"
TEST_BUCKET = "snapshots"
TEST_PREFIX = "camera-snapshots"
TEST_OBJECT_KEY = (
    "camera-snapshots/camera-17/2026/07/22/"
    "20260722T080910.123456Z_test/raw.jpg.aesgcm"
)


def build_container(
    plaintext: bytes,
    *,
    camera_id: int = 17,
    variant: str = "raw",
    content_type: str = "image/jpeg",
) -> bytes:
    header = {
        "algorithm": "AES-256-GCM",
        "camera_id": camera_id,
        "captured_at": "2026-07-22T08:09:10.123456Z",
        "content_type": content_type,
        "format_version": 1,
        "key_id": TEST_KEY_ID,
        "variant": variant,
    }
    if variant == "labels":
        header["annotation_format"] = "yolo-v12-detection"
    header_bytes = json.dumps(
        header,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    prefix = MAGIC + struct.pack(">I", len(header_bytes)) + header_bytes
    nonce = bytes(range(12))
    return prefix + nonce + AESGCM(TEST_KEY).encrypt(nonce, plaintext, prefix)


class TrackingBody(io.BytesIO):
    def __init__(self, content: bytes):
        super().__init__(content)
        self.was_closed = False

    def close(self):
        self.was_closed = True
        super().close()


class FakeS3Client:
    def __init__(self, content: bytes):
        self.content = content
        self.calls: list[dict[str, str]] = []
        self.body: TrackingBody | None = None

    def get_object(self, **kwargs):
        self.calls.append(kwargs)
        self.body = TrackingBody(self.content)
        return {"Body": self.body, "ContentLength": len(self.content)}


class ErrorS3Client:
    def __init__(self, code: str):
        self.code = code

    def get_object(self, **kwargs):
        raise ClientError(
            {"Error": {"Code": self.code, "Message": self.code}},
            "GetObject",
        )


class SnapshotStorageSettingsTests(unittest.TestCase):
    def test_loads_current_and_rotated_keys(self):
        old_key = bytes(reversed(range(32)))
        settings = SnapshotStorageSettings.from_environment({
            "SNAPSHOT_S3_BUCKET": TEST_BUCKET,
            "SNAPSHOT_ENCRYPTION_KEY_ID": TEST_KEY_ID,
            "SNAPSHOT_ENCRYPTION_KEY_BASE64": base64.b64encode(TEST_KEY).decode(),
            "SNAPSHOT_DECRYPTION_KEYS_JSON": json.dumps({
                "snapshot-key-old": base64.b64encode(old_key).decode(),
            }),
        })

        self.assertEqual(TEST_KEY, settings.encryption_keys[TEST_KEY_ID])
        self.assertEqual(old_key, settings.encryption_keys["snapshot-key-old"])

    def test_extracts_nested_snapshot_reference(self):
        metadata = {
            "snapshots": {
                "raw": {
                    "bucket": TEST_BUCKET,
                    "object_key": TEST_OBJECT_KEY,
                    "variant": "raw",
                    "encryption_key_id": TEST_KEY_ID,
                }
            }
        }

        reference = snapshot_reference_from_metadata(metadata, "raw")

        self.assertEqual(
            SnapshotReference(TEST_BUCKET, TEST_OBJECT_KEY, "raw", TEST_KEY_ID),
            reference,
        )
        self.assertIsNone(snapshot_reference_from_metadata(metadata, "annotated"))


class S3SnapshotReaderTests(unittest.TestCase):
    def make_reader(self, container: bytes) -> tuple[S3SnapshotReader, FakeS3Client]:
        client = FakeS3Client(container)
        settings = SnapshotStorageSettings(
            bucket=TEST_BUCKET,
            encryption_keys={TEST_KEY_ID: TEST_KEY},
            prefix=TEST_PREFIX,
        )
        return S3SnapshotReader(settings, s3_client=client), client

    def test_downloads_authenticates_and_decrypts_image(self):
        plaintext = b"jpeg bytes"
        reader, client = self.make_reader(build_container(plaintext))
        reference = SnapshotReference(
            TEST_BUCKET,
            TEST_OBJECT_KEY,
            "raw",
            TEST_KEY_ID,
        )

        result = reader.read(reference, expected_camera_id=17, expected_variant="raw")

        self.assertEqual(plaintext, result.content)
        self.assertEqual("image/jpeg", result.content_type)
        self.assertEqual("raw.jpg", result.filename)
        self.assertEqual(
            [{"Bucket": TEST_BUCKET, "Key": TEST_OBJECT_KEY}],
            client.calls,
        )
        self.assertIsNotNone(client.body)
        self.assertTrue(client.body.was_closed)

    def test_downloads_yolov12_labels(self):
        object_key = TEST_OBJECT_KEY.replace("raw.jpg", "labels.txt")
        plaintext = b"0 0.500000 0.500000 0.250000 0.500000\n"
        reader, _ = self.make_reader(build_container(
            plaintext,
            variant="labels",
            content_type="text/plain; charset=utf-8",
        ))
        reference = SnapshotReference(TEST_BUCKET, object_key, "labels", TEST_KEY_ID)

        result = reader.read(reference, expected_camera_id=17, expected_variant="labels")

        self.assertEqual(plaintext, result.content)
        self.assertEqual("text/plain; charset=utf-8", result.content_type)
        self.assertEqual("labels.txt", result.filename)

    def test_rejects_tampered_ciphertext(self):
        container = bytearray(build_container(b"jpeg bytes"))
        container[-1] ^= 1
        reader, _ = self.make_reader(bytes(container))
        reference = SnapshotReference(
            TEST_BUCKET,
            TEST_OBJECT_KEY,
            "raw",
            TEST_KEY_ID,
        )

        with self.assertRaises(SnapshotInvalidError):
            reader.read(reference, expected_camera_id=17, expected_variant="raw")

    def test_rejects_oversized_header_before_json_parsing(self):
        container = (
            MAGIC
            + struct.pack(">I", MAX_HEADER_SIZE_BYTES + 1)
            + bytes(12 + 16)
        )

        with self.assertRaises(SnapshotInvalidError):
            S3SnapshotReader._parse_container(container)

    def test_rejects_non_utf8_labels(self):
        object_key = TEST_OBJECT_KEY.replace("raw.jpg", "labels.txt")
        reader, _ = self.make_reader(build_container(
            b"\xff\xfe",
            variant="labels",
            content_type="text/plain; charset=utf-8",
        ))
        reference = SnapshotReference(TEST_BUCKET, object_key, "labels", TEST_KEY_ID)

        with self.assertRaises(SnapshotInvalidError):
            reader.read(reference, expected_camera_id=17, expected_variant="labels")

    def test_maps_missing_bucket_to_storage_unavailable(self):
        settings = SnapshotStorageSettings(
            bucket=TEST_BUCKET,
            encryption_keys={TEST_KEY_ID: TEST_KEY},
            prefix=TEST_PREFIX,
        )
        reader = S3SnapshotReader(settings, s3_client=ErrorS3Client("NoSuchBucket"))
        reference = SnapshotReference(
            TEST_BUCKET,
            TEST_OBJECT_KEY,
            "raw",
            TEST_KEY_ID,
        )

        with self.assertRaises(SnapshotUnavailableError):
            reader.read(reference, expected_camera_id=17, expected_variant="raw")

    def test_rejects_object_key_from_another_camera_before_s3_request(self):
        reader, client = self.make_reader(build_container(b"jpeg bytes"))
        reference = SnapshotReference(
            TEST_BUCKET,
            TEST_OBJECT_KEY.replace("camera-17", "camera-99"),
            "raw",
            TEST_KEY_ID,
        )

        with self.assertRaises(SnapshotInvalidError):
            reader.read(reference, expected_camera_id=17, expected_variant="raw")
        self.assertEqual([], client.calls)


if __name__ == "__main__":
    unittest.main()
