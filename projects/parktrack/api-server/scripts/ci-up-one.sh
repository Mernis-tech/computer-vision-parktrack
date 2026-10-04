#!/bin/bash
set -euo pipefail

MIGRATION_FILE="$1"

VERSION=$(basename "$MIGRATION_FILE" | sed 's/^0*//' | cut -d'_' -f1)

if [ $# -ge 2 ]; then
  TEST_DB_URL="$2"
else
  TEST_DB_URL="${DATABASE_HOST_URL:="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST:-postgres}:${POSTGRES_PORT:-5432}"}/${POSTGRES_TEST_DB}?sslmode=disable"
fi

migrate -path ./migrations -database "$TEST_DB_URL" goto "$VERSION"

#migrate -path ./migrations -database "$TEST_DB_URL" up 1
