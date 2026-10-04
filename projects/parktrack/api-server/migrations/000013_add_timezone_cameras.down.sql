ALTER TABLE cameras
    ALTER COLUMN last_snapshot_at TYPE TIMESTAMP
        USING last_snapshot_at AT TIME ZONE 'Europe/Moscow',
    ALTER COLUMN created_at TYPE TIMESTAMP
        USING created_at AT TIME ZONE 'Europe/Moscow',
    ALTER COLUMN updated_at TYPE TIMESTAMP
        USING updated_at AT TIME ZONE 'Europe/Moscow';