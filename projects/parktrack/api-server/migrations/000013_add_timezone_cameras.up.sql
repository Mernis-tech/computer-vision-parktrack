ALTER TABLE cameras
    ALTER COLUMN last_snapshot_at TYPE TIMESTAMPTZ
        USING last_snapshot_at AT TIME ZONE 'Europe/Moscow',
    ALTER COLUMN created_at TYPE TIMESTAMPTZ
        USING created_at AT TIME ZONE 'Europe/Moscow',
    ALTER COLUMN updated_at TYPE TIMESTAMPTZ
        USING updated_at AT TIME ZONE 'Europe/Moscow';