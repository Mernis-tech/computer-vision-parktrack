CREATE TABLE IF NOT EXISTS routes (
    route_id                  SERIAL PRIMARY KEY,
    user_id                   INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    mode                      VARCHAR(30) NOT NULL CHECK (mode IN ('find_parking', 'route_to_destination')),
    provider                  VARCHAR(50) NOT NULL DEFAULT 'internal',
    origin_latitude           DOUBLE PRECISION NOT NULL,
    origin_longitude          DOUBLE PRECISION NOT NULL,
    destination_latitude      DOUBLE PRECISION,
    destination_longitude     DOUBLE PRECISION,
    selected_zone_id          INTEGER REFERENCES parking_zones(parking_zone_id) ON DELETE SET NULL,
    selected_candidate        JSONB,
    eta_seconds               INTEGER,
    arrival_time              TIMESTAMP WITH TIME ZONE,
    polyline                  TEXT,
    deeplink_url              TEXT,
    status                    VARCHAR(20) NOT NULL DEFAULT 'active'
                                  CHECK (status IN ('active', 'completed', 'cancelled', 'replaced')),
    created_at                TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at                TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_routes_user_id        ON routes(user_id);
CREATE INDEX idx_routes_status         ON routes(status);
CREATE INDEX idx_routes_selected_zone  ON routes(selected_zone_id);
CREATE INDEX idx_routes_created_at     ON routes(created_at DESC);
