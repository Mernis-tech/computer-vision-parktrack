"""Client-side encrypted storage for camera snapshots in S3."""

from __future__ import annotations

import base64
import json
import os
import struct
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

import boto3
import cv2
import numpy as np
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


MAGIC = b"PTSNAP01"
FORMAT_VERSION = 1
NONCE_SIZE = 12
TAG_SIZE = 16
HEADER_LENGTH_STRUCT = struct.Struct(">I")


@dataclass(frozen=True)
class SnapshotStorageConfig:
    bucket: str
    encryption_key: bytes = field(repr=False)
    encryption_key_id: str
    prefix: str = "camera-snapshots"
    endpoint_url: str | None = None
    region_name: str | None = None

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "SnapshotStorageConfig":
        env = environment if environment is not None else os.environ

        bucket = env.get("SNAPSHOT_S3_BUCKET", "").strip()
        if not bucket:
            raise ValueError("SNAPSHOT_S3_BUCKET is required")

        encoded_key = env.get("SNAPSHOT_ENCRYPTION_KEY_BASE64", "").strip()
        encryption_key = decode_encryption_key(encoded_key)

        key_id = env.get("SNAPSHOT_ENCRYPTION_KEY_ID", "").strip()
        if not key_id:
            raise ValueError("SNAPSHOT_ENCRYPTION_KEY_ID is required")

        prefix = env.get("SNAPSHOT_S3_PREFIX", "camera-snapshots").strip("/")
        if not prefix:
            raise ValueError("SNAPSHOT_S3_PREFIX must not be empty")

        return cls(
            bucket=bucket,
            encryption_key=encryption_key,
            encryption_key_id=key_id,
            prefix=prefix,
            endpoint_url=env.get("SNAPSHOT_S3_ENDPOINT_URL") or None,
            region_name=env.get("SNAPSHOT_S3_REGION") or None,
        )


def decode_encryption_key(encoded_key: str) -> bytes:
    if not encoded_key:
        raise ValueError("SNAPSHOT_ENCRYPTION_KEY_BASE64 is required")
    try:
        encryption_key = base64.b64decode(encoded_key, validate=True)
    except ValueError as exception:
        raise ValueError(
            "SNAPSHOT_ENCRYPTION_KEY_BASE64 must be valid Base64"
        ) from exception
    if len(encryption_key) != 32:
        raise ValueError(
            "SNAPSHOT_ENCRYPTION_KEY_BASE64 must decode to exactly 32 bytes"
        )
    return encryption_key


@dataclass(frozen=True)
class StoredSnapshot:
    bucket: str
    object_key: str
    variant: str
    captured_at: str
    encryption_key_id: str

    def as_dict(self) -> dict[str, str]:
        return {
            "bucket": self.bucket,
            "object_key": self.object_key,
            "variant": self.variant,
            "captured_at": self.captured_at,
            "encryption_key_id": self.encryption_key_id,
        }


def encode_jpeg(frame_bgr: np.ndarray, quality: int = 95) -> bytes:
    """Encode an OpenCV BGR frame without creating a plaintext file."""
    success, encoded = cv2.imencode(
        ".jpg",
        frame_bgr,
        [cv2.IMWRITE_JPEG_QUALITY, quality],
    )
    if not success:
        raise RuntimeError("failed to encode snapshot as JPEG")
    return encoded.tobytes()


def encode_yolo_labels(
    bounding_boxes_xyxy: np.ndarray,
    class_ids: np.ndarray,
    *,
    image_width: int,
    image_height: int,
) -> bytes:
    """Encode detections as UTF-8 YOLO detection labels."""
    if image_width <= 0 or image_height <= 0:
        raise ValueError("image dimensions must be positive")

    boxes = np.asarray(bounding_boxes_xyxy, dtype=np.float64)
    if boxes.size == 0:
        boxes = boxes.reshape(0, 4)
    if boxes.ndim != 2 or boxes.shape[1] != 4:
        raise ValueError("bounding_boxes_xyxy must have shape (N, 4)")

    classes = np.asarray(class_ids).reshape(-1)
    if len(boxes) != len(classes):
        raise ValueError("bounding boxes and class IDs must have the same length")

    lines: list[str] = []
    for box, class_id in zip(boxes, classes):
        if not np.all(np.isfinite(box)):
            raise ValueError("bounding boxes must contain only finite values")

        x_min, y_min, x_max, y_max = box
        x_min = float(np.clip(x_min, 0.0, image_width))
        x_max = float(np.clip(x_max, 0.0, image_width))
        y_min = float(np.clip(y_min, 0.0, image_height))
        y_max = float(np.clip(y_max, 0.0, image_height))
        if x_max < x_min or y_max < y_min:
            raise ValueError("bounding boxes must use x_min, y_min, x_max, y_max order")

        x_center = ((x_min + x_max) / 2.0) / image_width
        y_center = ((y_min + y_max) / 2.0) / image_height
        width = (x_max - x_min) / image_width
        height = (y_max - y_min) / image_height
        lines.append(
            f"{int(class_id)} {x_center:.6f} {y_center:.6f} "
            f"{width:.6f} {height:.6f}"
        )

    return (("\n".join(lines) + "\n") if lines else "").encode("utf-8")


def _serialize_header(header: Mapping[str, Any]) -> bytes:
    return json.dumps(
        header,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def encrypt_snapshot(
    plaintext: bytes,
    encryption_key: bytes,
    header: Mapping[str, Any],
    *,
    nonce: bytes | None = None,
) -> bytes:
    """Build a PTSNAP01 container authenticated with AES-256-GCM."""
    if len(encryption_key) != 32:
        raise ValueError("AES-256-GCM key must contain exactly 32 bytes")

    nonce = nonce if nonce is not None else os.urandom(NONCE_SIZE)
    if len(nonce) != NONCE_SIZE:
        raise ValueError(f"AES-GCM nonce must contain exactly {NONCE_SIZE} bytes")

    header_bytes = _serialize_header(header)
    header_prefix = MAGIC + HEADER_LENGTH_STRUCT.pack(len(header_bytes)) + header_bytes
    ciphertext_and_tag = AESGCM(encryption_key).encrypt(
        nonce,
        plaintext,
        header_prefix,
    )
    return header_prefix + nonce + ciphertext_and_tag


def decrypt_snapshot(
    container: bytes, encryption_key: bytes
) -> tuple[dict[str, Any], bytes]:
    """Parse and decrypt a PTSNAP01 container."""
    minimum_size = len(MAGIC) + HEADER_LENGTH_STRUCT.size + NONCE_SIZE + TAG_SIZE
    if len(container) < minimum_size or not container.startswith(MAGIC):
        raise ValueError("not a PTSNAP01 encrypted snapshot")

    header_length_offset = len(MAGIC)
    header_offset = header_length_offset + HEADER_LENGTH_STRUCT.size
    (header_length,) = HEADER_LENGTH_STRUCT.unpack_from(container, header_length_offset)
    nonce_offset = header_offset + header_length
    ciphertext_offset = nonce_offset + NONCE_SIZE
    if ciphertext_offset + TAG_SIZE > len(container):
        raise ValueError("truncated PTSNAP01 encrypted snapshot")

    header_prefix = container[:nonce_offset]
    try:
        header = json.loads(container[header_offset:nonce_offset].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exception:
        raise ValueError("invalid PTSNAP01 JSON header") from exception

    nonce = container[nonce_offset:ciphertext_offset]
    ciphertext_and_tag = container[ciphertext_offset:]
    plaintext = AESGCM(encryption_key).decrypt(
        nonce,
        ciphertext_and_tag,
        header_prefix,
    )
    return header, plaintext


class S3SnapshotStorage:
    def __init__(
        self,
        config: SnapshotStorageConfig,
        *,
        s3_client: Any | None = None,
    ) -> None:
        self.config = config
        self.s3_client = s3_client or boto3.client(
            "s3",
            endpoint_url=config.endpoint_url,
            region_name=config.region_name,
        )

    def _object_key(
        self,
        camera_id: int,
        captured_at: datetime,
        snapshot_id: str,
        variant: str,
    ) -> str:
        utc_time = captured_at.astimezone(timezone.utc)
        timestamp = utc_time.strftime("%Y%m%dT%H%M%S.%fZ")
        filenames = {
            "raw": "raw.jpg.aesgcm",
            "annotated": "annotated.jpg.aesgcm",
            "labels": "labels.txt.aesgcm",
        }
        try:
            filename = filenames[variant]
        except KeyError as exception:
            raise ValueError(f"unsupported snapshot variant: {variant}") from exception
        return (
            f"{self.config.prefix}/camera-{camera_id}/"
            f"{utc_time:%Y/%m/%d}/{timestamp}_{snapshot_id}/{filename}"
        )

    def store_pair(
        self,
        *,
        camera_id: int,
        captured_at: datetime,
        raw_frame_bgr: np.ndarray,
        annotated_frame_bgr: np.ndarray,
        labels_yolo: bytes,
    ) -> dict[str, StoredSnapshot]:
        """Encrypt and upload the raw frame, annotation image, and YOLO labels."""
        if captured_at.tzinfo is None:
            raise ValueError("captured_at must be timezone-aware")

        captured_at_utc = captured_at.astimezone(timezone.utc)
        captured_at_iso = captured_at_utc.isoformat().replace("+00:00", "Z")
        snapshot_id = uuid.uuid4().hex
        stored: dict[str, StoredSnapshot] = {}

        for variant, plaintext, content_type in (
            ("raw", encode_jpeg(raw_frame_bgr), "image/jpeg"),
            ("annotated", encode_jpeg(annotated_frame_bgr), "image/jpeg"),
            ("labels", labels_yolo, "text/plain; charset=utf-8"),
        ):
            object_key = self._object_key(
                camera_id,
                captured_at_utc,
                snapshot_id,
                variant,
            )
            header = {
                "algorithm": "AES-256-GCM",
                "camera_id": int(camera_id),
                "captured_at": captured_at_iso,
                "content_type": content_type,
                "format_version": FORMAT_VERSION,
                "key_id": self.config.encryption_key_id,
                "variant": variant,
            }
            if variant == "labels":
                header["annotation_format"] = "yolo-v12-detection"
            encrypted = encrypt_snapshot(
                plaintext,
                self.config.encryption_key,
                header,
            )
            self.s3_client.put_object(
                Bucket=self.config.bucket,
                Key=object_key,
                Body=encrypted,
                ContentType="application/octet-stream",
                Metadata={
                    "snapshot-format": "PTSNAP01",
                    "encryption-key-id": self.config.encryption_key_id,
                    "camera-id": str(camera_id),
                    "variant": variant,
                    "captured-at": captured_at_iso,
                },
            )
            stored[variant] = StoredSnapshot(
                bucket=self.config.bucket,
                object_key=object_key,
                variant=variant,
                captured_at=captured_at_iso,
                encryption_key_id=self.config.encryption_key_id,
            )

        return stored
