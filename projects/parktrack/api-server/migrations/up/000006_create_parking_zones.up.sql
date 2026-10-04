CREATE TYPE zone_types AS ENUM ('parallel', 'standard');
CREATE TYPE confidence_level_types AS ENUM ('very_low', 'low', 'medium', 'high')
CREATE TYPE location_types AS ENUM ('street', 'yard', 'open_lot', 'underground', 'multilevel')

CREATE TABLE IF NOT EXISTS parking_zones (
    parking_zone_id SERIAL PRIMARY KEY,
    camera_id INTEGER NOT NULL REFERENCES cameras(camera_id) ON DELETE CASCADE,
    zone_type zone_types NOT NULL,
    capacity INTEGER NOT NULL CHECK (capacity >= 0),
    occupied INTEGER NOT NULL DEFAULT 0 CHECK (occupied >= 0),
    free_count INTEGER GENERATED ALWAYS AS (capacity - occupied) STORED,
    confidence DOUBLE PRECISION DEFAULT 0.0 CHECK (confidence BETWEEN 0.0 AND 1.0),
    confidence_level confidence_level_types,
    pay INTEGER NOT NULL DEFAULT 0 CHECK (pay >= 0),
    geometry JSONB NOT NULL,
    image_polygon JSONB NOT NULL,
    partner_id INTEGER REFERENCES partners(partner_id) ON DELETE SET NULL,
    created_by_user_id INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    is_active BOOLEAN DEFAULT TRUE,
    location_type location_types,
    is_private BOOLEAN,
    is_accessible BOOLEAN,
    occupancy_updated_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
);