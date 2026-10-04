ALTER TABLE parking_zones
    ALTER COLUMN occupancy_updated_at TYPE TIMESTAMP
        USING occupancy_updated_at AT TIME ZONE 'Europe/Moscow',
    ALTER COLUMN created_at TYPE TIMESTAMP
        USING created_at AT TIME ZONE 'Europe/Moscow',
    ALTER COLUMN updated_at TYPE TIMESTAMP
        USING updated_at AT TIME ZONE 'Europe/Moscow';