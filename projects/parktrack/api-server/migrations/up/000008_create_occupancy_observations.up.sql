CREATE TABLE IF NOT EXISTS occupancy_observations (
    observation_id      SERIAL PRIMARY KEY,
    zone_id             INTEGER NOT NULL REFERENCES parking_zones(parking_zone_id) ON DELETE CASCADE,
    camera_id           INTEGER REFERENCES cameras(camera_id)   ON DELETE SET NULL,
    partner_id          INTEGER REFERENCES partners(partner_id) ON DELETE SET NULL,
    source_type         VARCHAR(50) NOT NULL,
    source_ref          VARCHAR(255),
    capacity            INTEGER NOT NULL CHECK (capacity >= 0),
    occupied            INTEGER NOT NULL CHECK (occupied >= 0),
    free_count          INTEGER GENERATED ALWAYS AS (capacity - occupied) STORED,
    confidence          DOUBLE PRECISION NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    confidence_level    VARCHAR(10) CHECK (confidence_level IN ('very_low', 'low', 'medium', 'high')),
    observed_at         TIMESTAMP WITH TIME ZONE NOT NULL,
    ingested_at         TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    metadata            JSONB,
    created_by_user_id  INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    UNIQUE (source_type, source_ref)
);

CREATE INDEX idx_occupancy_obs_zone_id     ON occupancy_observations(zone_id);
CREATE INDEX idx_occupancy_obs_camera_id   ON occupancy_observations(camera_id);
CREATE INDEX idx_occupancy_obs_partner_id  ON occupancy_observations(partner_id);
CREATE INDEX idx_occupancy_obs_observed_at ON occupancy_observations(observed_at DESC);
CREATE INDEX idx_occupancy_obs_source_type ON occupancy_observations(source_type);
