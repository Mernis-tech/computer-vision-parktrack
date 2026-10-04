ALTER TABLE parking_zones
    ALTER COLUMN occupancy_updated_at TYPE TIMESTAMPTZ
        USING occupancy_updated_at AT TIME ZONE 'Europe/Moscow',
    ALTER COLUMN created_at TYPE TIMESTAMPTZ
        USING created_at AT TIME ZONE 'Europe/Moscow',
    ALTER COLUMN updated_at TYPE TIMESTAMPTZ
        USING updated_at AT TIME ZONE 'Europe/Moscow';