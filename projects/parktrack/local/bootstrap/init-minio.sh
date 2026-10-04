#!/bin/sh
set -eu

until mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1; do
  echo "MinIO ещё запускается..."
  sleep 2
done

mc mb --ignore-existing "local/$SNAPSHOT_S3_BUCKET"
mc anonymous set download "local/$SNAPSHOT_S3_BUCKET"
echo "MinIO готов: бакет $SNAPSHOT_S3_BUCKET создан."
