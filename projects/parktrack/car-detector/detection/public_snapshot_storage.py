"""Plaintext public storage for camera snapshots in S3-compatible storage."""

from __future__ import annotations

import os
import secrets
import string
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import quote

import boto3
import cv2
import numpy as np

_ALPHANUMERIC = string.ascii_letters + string.digits
_VARIANT_SUFFIXES = {
    "raw": ".jpg",
    "annotated": ".jpg",
    "labels": ".txt",
}


@dataclass(frozen=True)
class SnapshotStorageConfig:
    bucket: str
    public_base_url: str
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

        public_base_url = env.get("SNAPSHOT_PUBLIC_BASE_URL", "").strip().rstrip("/")
        if not public_base_url:
            raise ValueError("SNAPSHOT_PUBLIC_BASE_URL is required")

        prefix = env.get("SNAPSHOT_S3_PREFIX", "camera-snapshots").strip("/")
        if not prefix:
            raise ValueError("SNAPSHOT_S3_PREFIX must not be empty")

        return cls(
            bucket=bucket,
            public_base_url=public_base_url,
            prefix=prefix,
            endpoint_url=env.get("SNAPSHOT_S3_ENDPOINT_URL") or None,
            region_name=env.get("SNAPSHOT_S3_REGION") or None,
        )


@dataclass(frozen=True)
class StoredSnapshot:
    bucket: str
    object_key: str
    url: str
    variant: str
    captured_at: str
    content_type: str

    def as_dict(self) -> dict[str, str]:
        return {
            "bucket": self.bucket,
            "object_key": self.object_key,
            "url": self.url,
            "variant": self.variant,
            "captured_at": self.captured_at,
            "content_type": self.content_type,
        }


def _random_alphanumeric(length: int) -> str:
    if length <= 0:
        raise ValueError("random token length must be positive")
    return "".join(secrets.choice(_ALPHANUMERIC) for _ in range(length))


def encode_jpeg(frame_bgr: np.ndarray, quality: int = 95) -> bytes:
    """Encode an OpenCV BGR frame without creating a local file."""
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

    def _object_key(self, variant: str) -> str:
        try:
            suffix = _VARIANT_SUFFIXES[variant]
        except KeyError as exception:
            raise ValueError(f"unsupported snapshot variant: {variant}") from exception

        # Each object receives independent random directory components and a
        # random file name. A URL to one artifact does not reveal the location
        # or name of the other artifacts from the same detection run.
        directory_1 = _random_alphanumeric(32)
        directory_2 = _random_alphanumeric(32)
        filename = _random_alphanumeric(48)
        return f"{self.config.prefix}/{directory_1}/{directory_2}/{filename}{suffix}"

    def _public_url(self, object_key: str) -> str:
        encoded_key = quote(object_key, safe="/")
        return f"{self.config.public_base_url}/{encoded_key}"

    def store_pair(
        self,
        *,
        camera_id: int,
        captured_at: datetime,
        raw_frame_bgr: np.ndarray,
        annotated_frame_bgr: np.ndarray,
        labels_yolo: bytes,
    ) -> dict[str, StoredSnapshot]:
        """Upload raw, annotated and labels artifacts without encryption."""
        if captured_at.tzinfo is None:
            raise ValueError("captured_at must be timezone-aware")

        captured_at_iso = (
            captured_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        stored: dict[str, StoredSnapshot] = {}

        for variant, plaintext, content_type in (
            ("raw", encode_jpeg(raw_frame_bgr), "image/jpeg"),
            ("annotated", encode_jpeg(annotated_frame_bgr), "image/jpeg"),
            ("labels", labels_yolo, "text/plain; charset=utf-8"),
        ):
            object_key = self._object_key(variant)
            self.s3_client.put_object(
                Bucket=self.config.bucket,
                Key=object_key,
                Body=plaintext,
                ContentType=content_type,
                CacheControl="public, max-age=31536000, immutable",
                Metadata={
                    "camera-id": str(camera_id),
                    "variant": variant,
                    "captured-at": captured_at_iso,
                },
            )
            stored[variant] = StoredSnapshot(
                bucket=self.config.bucket,
                object_key=object_key,
                url=self._public_url(object_key),
                variant=variant,
                captured_at=captured_at_iso,
                content_type=content_type,
            )

        return stored
