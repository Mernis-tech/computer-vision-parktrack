CREATE TABLE IF NOT EXISTS forecasts (
    forecast_id             SERIAL PRIMARY KEY,
    zone_id                 INTEGER NOT NULL REFERENCES parking_zones(parking_zone_id) ON DELETE CASCADE,
    camera_id               INTEGER REFERENCES cameras(camera_id)   ON DELETE SET NULL,
    partner_id              INTEGER REFERENCES partners(partner_id) ON DELETE SET NULL,
    model_type              VARCHAR(50) NOT NULL,
    model_version           VARCHAR(100),
    generated_at            TIMESTAMP WITH TIME ZONE NOT NULL,
    predicted_for           TIMESTAMP WITH TIME ZONE NOT NULL,
    capacity                INTEGER NOT NULL CHECK (capacity >= 0),
    predicted_occupied      INTEGER NOT NULL CHECK (predicted_occupied >= 0),
    predicted_free_count    INTEGER GENERATED ALWAYS AS (capacity - predicted_occupied) STORED,
    probability_free_space  DOUBLE PRECISION NOT NULL CHECK (probability_free_space BETWEEN 0 AND 1),
    confidence              DOUBLE PRECISION NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    confidence_level        VARCHAR(10) CHECK (confidence_level IN ('very_low', 'low', 'medium', 'high')),
    metadata                JSONB,
    created_by_user_id      INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    UNIQUE (zone_id, generated_at, predicted_for)
);

CREATE INDEX idx_forecasts_zone_id       ON forecasts(zone_id);
CREATE INDEX idx_forecasts_camera_id     ON forecasts(camera_id);
CREATE INDEX idx_forecasts_partner_id    ON forecasts(partner_id);
CREATE INDEX idx_forecasts_predicted_for ON forecasts(predicted_for);
CREATE INDEX idx_forecasts_generated_at  ON forecasts(generated_at DESC);
CREATE INDEX idx_forecasts_model_type    ON forecasts(model_type);
