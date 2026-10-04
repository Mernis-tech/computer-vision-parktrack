DROP INDEX IF EXISTS idx_parking_zones_active_centroid_lat;
DROP INDEX IF EXISTS idx_parking_zones_active_centroid_lon;

ALTER TABLE parking_zones
DROP COLUMN IF EXISTS centroid_latitude,
DROP COLUMN IF EXISTS centroid_longitude;