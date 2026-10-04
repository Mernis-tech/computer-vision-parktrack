#!/bin/bash

set -euo pipefail

DATABASES_DB_URL="${DATABASE_HOST_URL:="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST:-postgres}:${POSTGRES_PORT:-5432}"}/postgres?sslmode=disable"
MAIN_DB_URL="${DATABASE_HOST_URL}/${POSTGRES_DB}?sslmode=disable"
TEST_DB_URL="${DATABASE_HOST_URL}/${POSTGRES_TEST_DB}?sslmode=disable"

echo "Recreating database"

psql "$DATABASES_DB_URL" \
-v ON_ERROR_STOP=1 <<SQL
DROP DATABASE IF EXISTS ${POSTGRES_TEST_DB} WITH (FORCE);
CREATE DATABASE ${POSTGRES_TEST_DB};
SQL

echo "Starting seqwall staircase test"

seqwall staircase \
--postgres-url "$TEST_DB_URL" \
--migrations-path ./migrations/up \
--upgrade "bash scripts/ci-up-one.sh {current_migration} \"$TEST_DB_URL\"" \
--downgrade "bash scripts/ci-down-one.sh {current_migration} \"$TEST_DB_URL\""

echo "Applying target migration"

if [ -n "${MIGRATION_VERSION:-}" ]; then
	migrate -path ./migrations -database "$MAIN_DB_URL" goto "$MIGRATION_VERSION"
else
	migrate -path ./migrations -database "$MAIN_DB_URL" up
fi
