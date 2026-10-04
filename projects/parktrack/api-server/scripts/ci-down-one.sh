#!/bin/bash

set -eo

if [ $# -ge 2 ]; then
  TEST_DB_URL="$2"
else
  TEST_DB_URL="${DATABASE_HOST_URL:="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST:-postgres}:${POSTGRES_PORT:-5432}"}/${POSTGRES_TEST_DB}?sslmode=disable"
fi

migrate -path ./migrations -database "$TEST_DB_URL" down 1
