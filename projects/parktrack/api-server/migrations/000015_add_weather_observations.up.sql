CREATE TABLE weather_observations (
    camera_id      BIGINT NOT NULL REFERENCES cameras(camera_id),
    observed_at    TIMESTAMPTZ NOT NULL,
    temperature    DOUBLE PRECISION NOT NULL,
    precipitation  DOUBLE PRECISION NOT NULL CHECK (precipitation >= 0),

    PRIMARY KEY (camera_id, observed_at)
);