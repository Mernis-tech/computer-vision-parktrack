"""Read and decrypt detection artifacts stored as PTSNAP01 objects in S3."""

from __future__ import annotations

import base64
import binascii
import json
import os
import struct
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Mapping

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


MAGIC = b"PTSNAP01"
FORMAT_VERSION = 1
HEADER_LENGTH_STRUCT = struct.Struct(">I")
NONCE_SIZE = 12
TAG_SIZE = 16
DEFAULT_MAX_OBJECT_SIZE_BYTES = 25 * 1024 * 1024
MAX_HEADER_SIZE_BYTES = 64 * 1024
SUPPORTED_VARIANTS = {"raw", "annotated", "labels"}


class SnapshotStorageError(Exception):
    """Base class for safe, user-facing storage error mapping."""


class SnapshotNotConfiguredError(SnapshotStorageError):
    pass


class SnapshotNotFoundError(SnapshotStorageError):
    pass


class SnapshotUnavailableError(SnapshotStorageError):
    pass


class SnapshotInvalidError(SnapshotStorageError):
    pass


def _decode_key(encoded_key: str, variable_name: str) -> bytes:
    try:
        key = base64.b64decode(encoded_key, validate=True)
    except (ValueError, binascii.Error) as exception:
        raise ValueError(f"{variable_name} must be valid Base64") from exception
    if len(key) != 32:
        raise ValueError(f"{variable_name} must decode to exactly 32 bytes")
    return key


@dataclass(frozen=True)
class SnapshotStorageSettings:
    bucket: str
    encryption_keys: Mapping[str, bytes] = field(repr=False)
    prefix: str = "camera-snapshots"
    endpoint_url: str | None = None
    region_name: str | None = None
    max_object_size_bytes: int = DEFAULT_MAX_OBJECT_SIZE_BYTES

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "SnapshotStorageSettings":
        env = environment if environment is not None else os.environ
        bucket = env.get("SNAPSHOT_S3_BUCKET", "").strip()
        if not bucket:
            raise ValueError("SNAPSHOT_S3_BUCKET is required")

        keys: dict[str, bytes] = {}
        current_key_id = env.get("SNAPSHOT_ENCRYPTION_KEY_ID", "").strip()
        current_key_base64 = env.get("SNAPSHOT_ENCRYPTION_KEY_BASE64", "").strip()
        if current_key_id and current_key_base64:
            keys[current_key_id] = _decode_key(
                current_key_base64,
                "SNAPSHOT_ENCRYPTION_KEY_BASE64",
            )
        elif current_key_id or current_key_base64:
            raise ValueError(
                "SNAPSHOT_ENCRYPTION_KEY_ID and SNAPSHOT_ENCRYPTION_KEY_BASE64 "
                "must be configured together"
            )

        keyring_json = env.get("SNAPSHOT_DECRYPTION_KEYS_JSON", "").strip()
        if keyring_json:
            try:
                keyring = json.loads(keyring_json)
            except json.JSONDecodeError as exception:
                raise ValueError("SNAPSHOT_DECRYPTION_KEYS_JSON must be valid JSON") from exception
            if not isinstance(keyring, dict):
                raise ValueError("SNAPSHOT_DECRYPTION_KEYS_JSON must be a JSON object")
            for key_id, encoded_key in keyring.items():
                if not isinstance(key_id, str) or not key_id or not isinstance(encoded_key, str):
                    raise ValueError(
                        "SNAPSHOT_DECRYPTION_KEYS_JSON must map non-empty key IDs to Base64 strings"
                    )
                keys[key_id] = _decode_key(encoded_key, "SNAPSHOT_DECRYPTION_KEYS_JSON value")

        if not keys:
            raise ValueError(
                "At least one snapshot encryption key must be configured"
            )

        prefix = env.get("SNAPSHOT_S3_PREFIX", "camera-snapshots").strip("/")
        if not prefix:
            raise ValueError("SNAPSHOT_S3_PREFIX must not be empty")

        raw_max_size = env.get("SNAPSHOT_MAX_OBJECT_SIZE_BYTES", "").strip()
        try:
            max_size = int(raw_max_size) if raw_max_size else DEFAULT_MAX_OBJECT_SIZE_BYTES
        except ValueError as exception:
            raise ValueError("SNAPSHOT_MAX_OBJECT_SIZE_BYTES must be an integer") from exception
        if max_size <= 0:
            raise ValueError("SNAPSHOT_MAX_OBJECT_SIZE_BYTES must be positive")

        return cls(
            bucket=bucket,
            encryption_keys=keys,
            prefix=prefix,
            endpoint_url=env.get("SNAPSHOT_S3_ENDPOINT_URL") or None,
            region_name=env.get("SNAPSHOT_S3_REGION") or None,
            max_object_size_bytes=max_size,
        )


@dataclass(frozen=True)
class SnapshotReference:
    bucket: str
    object_key: str
    variant: str
    encryption_key_id: str | None = None


@dataclass(frozen=True)
class DecryptedSnapshot:
    content: bytes = field(repr=False)
    content_type: str
    filename: str
    captured_at: str


def snapshot_reference_from_metadata(
    metadata: Any,
    variant: str,
) -> SnapshotReference | None:
    if variant not in SUPPORTED_VARIANTS or not isinstance(metadata, dict):
        return None
    snapshots = metadata.get("snapshots")
    if not isinstance(snapshots, dict):
        return None
    value = snapshots.get(variant)
    if not isinstance(value, dict):
        return None

    bucket = value.get("bucket")
    object_key = value.get("object_key") or value.get("key")
    stored_variant = value.get("variant", variant)
    key_id = value.get("encryption_key_id") or value.get("key_id")
    if not isinstance(bucket, str) or not bucket:
        return None
    if not isinstance(object_key, str) or not object_key:
        return None
    if stored_variant != variant:
        return None
    if key_id is not None and (not isinstance(key_id, str) or not key_id):
        return None

    return SnapshotReference(
        bucket=bucket,
        object_key=object_key,
        variant=variant,
        encryption_key_id=key_id,
    )


class S3SnapshotReader:
    def __init__(
        self,
        settings: SnapshotStorageSettings,
        *,
        s3_client: Any | None = None,
    ) -> None:
        self.settings = settings
        self.s3_client = s3_client or boto3.client(
            "s3",
            endpoint_url=settings.endpoint_url,
            region_name=settings.region_name,
        )

    def read(
        self,
        reference: SnapshotReference,
        *,
        expected_camera_id: int,
        expected_variant: str,
    ) -> DecryptedSnapshot:
        self._validate_reference(reference, expected_camera_id, expected_variant)
        container = self._download(reference)
        header_prefix, header, nonce, ciphertext = self._parse_container(container)

        key_id = reference.encryption_key_id or header.get("key_id")
        if not isinstance(key_id, str) or key_id not in self.settings.encryption_keys:
            raise SnapshotNotConfiguredError("Snapshot encryption key is unavailable")

        try:
            plaintext = AESGCM(self.settings.encryption_keys[key_id]).decrypt(
                nonce,
                ciphertext,
                header_prefix,
            )
        except InvalidTag as exception:
            raise SnapshotInvalidError("Snapshot integrity check failed") from exception

        content_type, captured_at = self._validate_header(
            header,
            key_id=key_id,
            expected_camera_id=expected_camera_id,
            expected_variant=expected_variant,
        )
        if expected_variant == "labels":
            try:
                plaintext.decode("utf-8")
            except UnicodeDecodeError as exception:
                raise SnapshotInvalidError("Snapshot labels are not valid UTF-8") from exception

        filename = "labels.txt" if expected_variant == "labels" else f"{expected_variant}.jpg"
        return DecryptedSnapshot(
            content=plaintext,
            content_type=content_type,
            filename=filename,
            captured_at=captured_at,
        )

    def _validate_reference(
        self,
        reference: SnapshotReference,
        expected_camera_id: int,
        expected_variant: str,
    ) -> None:
        if expected_variant not in SUPPORTED_VARIANTS or reference.variant != expected_variant:
            raise SnapshotInvalidError("Snapshot variant does not match the request")
        if reference.bucket != self.settings.bucket:
            raise SnapshotInvalidError("Snapshot bucket does not match API configuration")
        expected_prefix = f"{self.settings.prefix}/camera-{expected_camera_id}/"
        if not reference.object_key.startswith(expected_prefix):
            raise SnapshotInvalidError("Snapshot object key does not match the detection camera")

    def _download(self, reference: SnapshotReference) -> bytes:
        try:
            response = self.s3_client.get_object(
                Bucket=reference.bucket,
                Key=reference.object_key,
            )
            content_length = response.get("ContentLength")
            if (
                isinstance(content_length, int)
                and content_length > self.settings.max_object_size_bytes
            ):
                raise SnapshotInvalidError("Encrypted snapshot is too large")
            body = response["Body"]
            try:
                container = body.read(self.settings.max_object_size_bytes + 1)
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()
        except ClientError as exception:
            error_code = str(exception.response.get("Error", {}).get("Code", ""))
            if error_code in {"404", "NoSuchKey", "NotFound"}:
                raise SnapshotNotFoundError("Snapshot object not found") from exception
            raise SnapshotUnavailableError("S3 snapshot storage is unavailable") from exception
        except (BotoCoreError, KeyError, OSError) as exception:
            raise SnapshotUnavailableError("S3 snapshot storage is unavailable") from exception

        if not isinstance(container, bytes):
            container = bytes(container)
        if len(container) > self.settings.max_object_size_bytes:
            raise SnapshotInvalidError("Encrypted snapshot is too large")
        return container

    @staticmethod
    def _parse_container(
        container: bytes,
    ) -> tuple[bytes, dict[str, Any], bytes, bytes]:
        minimum_size = len(MAGIC) + HEADER_LENGTH_STRUCT.size + NONCE_SIZE + TAG_SIZE
        if len(container) < minimum_size or not container.startswith(MAGIC):
            raise SnapshotInvalidError("Invalid encrypted snapshot format")

        header_length_offset = len(MAGIC)
        header_offset = header_length_offset + HEADER_LENGTH_STRUCT.size
        (header_length,) = HEADER_LENGTH_STRUCT.unpack_from(container, header_length_offset)
        if header_length <= 0 or header_length > MAX_HEADER_SIZE_BYTES:
            raise SnapshotInvalidError("Invalid encrypted snapshot header length")
        nonce_offset = header_offset + header_length
        ciphertext_offset = nonce_offset + NONCE_SIZE
        if ciphertext_offset + TAG_SIZE > len(container):
            raise SnapshotInvalidError("Encrypted snapshot is truncated")

        try:
            header = json.loads(container[header_offset:nonce_offset].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exception:
            raise SnapshotInvalidError("Invalid encrypted snapshot header") from exception
        if not isinstance(header, dict):
            raise SnapshotInvalidError("Invalid encrypted snapshot header")

        return (
            container[:nonce_offset],
            header,
            container[nonce_offset:ciphertext_offset],
            container[ciphertext_offset:],
        )

    @staticmethod
    def _validate_header(
        header: Mapping[str, Any],
        *,
        key_id: str,
        expected_camera_id: int,
        expected_variant: str,
    ) -> tuple[str, str]:
        if header.get("format_version") != FORMAT_VERSION:
            raise SnapshotInvalidError("Unsupported snapshot format version")
        if header.get("algorithm") != "AES-256-GCM":
            raise SnapshotInvalidError("Unsupported snapshot encryption algorithm")
        if header.get("key_id") != key_id:
            raise SnapshotInvalidError("Snapshot key ID does not match metadata")
        if header.get("camera_id") != expected_camera_id:
            raise SnapshotInvalidError("Snapshot camera ID does not match the detection")
        if header.get("variant") != expected_variant:
            raise SnapshotInvalidError("Snapshot variant does not match the request")

        captured_at = header.get("captured_at")
        content_type = header.get("content_type")
        if not isinstance(captured_at, str) or not captured_at:
            raise SnapshotInvalidError("Snapshot capture time is missing")
        if not isinstance(content_type, str):
            raise SnapshotInvalidError("Snapshot content type is missing")

        if expected_variant in {"raw", "annotated"}:
            if content_type != "image/jpeg":
                raise SnapshotInvalidError("Snapshot image content type is invalid")
        else:
            if not content_type.startswith("text/plain"):
                raise SnapshotInvalidError("Snapshot labels content type is invalid")
            if header.get("annotation_format") != "yolo-v12-detection":
                raise SnapshotInvalidError("Snapshot labels format is invalid")

        return content_type, captured_at


@lru_cache(maxsize=1)
def get_snapshot_reader() -> S3SnapshotReader:
    try:
        settings = SnapshotStorageSettings.from_environment()
    except ValueError as exception:
        raise SnapshotNotConfiguredError(str(exception)) from exception
    return S3SnapshotReader(settings)


def read_snapshot_from_metadata(
    metadata: Any,
    *,
    camera_id: int,
    variant: str,
) -> DecryptedSnapshot:
    reference = snapshot_reference_from_metadata(metadata, variant)
    if reference is None:
        raise SnapshotNotFoundError("Detection artifact metadata is missing")
    return get_snapshot_reader().read(
        reference,
        expected_camera_id=camera_id,
        expected_variant=variant,
    )
