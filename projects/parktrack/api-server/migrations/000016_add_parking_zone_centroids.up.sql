ALTER TABLE parking_zones
ADD COLUMN IF NOT EXISTS centroid_latitude DOUBLE PRECISION,
ADD COLUMN IF NOT EXISTS centroid_longitude DOUBLE PRECISION;

-- Заполняем центроиды для уже существующих зон.
-- Берём среднее по точкам внешнего кольца GeoJSON Polygon.
WITH points AS (
    SELECT
        pz.parking_zone_id,
        (point.value ->> 0)::DOUBLE PRECISION AS longitude,
        (point.value ->> 1)::DOUBLE PRECISION AS latitude
    FROM parking_zones pz
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE
            WHEN jsonb_typeof(pz.geometry) = 'object'
             AND pz.geometry ->> 'type' = 'Polygon'
             AND jsonb_typeof(pz.geometry -> 'coordinates') = 'array'
             AND jsonb_array_length(pz.geometry -> 'coordinates') > 0
             AND jsonb_typeof(pz.geometry -> 'coordinates' -> 0) = 'array'
            THEN pz.geometry -> 'coordinates' -> 0
            ELSE '[]'::jsonb
        END
    ) AS point(value)
    WHERE jsonb_typeof(point.value) = 'array'
      AND jsonb_array_length(point.value) >= 2
      AND (point.value ->> 0) ~ '^-?[0-9]+(\.[0-9]+)?$'
      AND (point.value ->> 1) ~ '^-?[0-9]+(\.[0-9]+)?$'
),
centroids AS (
    SELECT
        parking_zone_id,
        AVG(latitude) AS centroid_latitude,
        AVG(longitude) AS centroid_longitude
    FROM points
    WHERE latitude BETWEEN -90 AND 90
      AND longitude BETWEEN -180 AND 180
    GROUP BY parking_zone_id
)
UPDATE parking_zones pz
SET
    centroid_latitude = c.centroid_latitude,
    centroid_longitude = c.centroid_longitude
FROM centroids c
WHERE pz.parking_zone_id = c.parking_zone_id;

-- Индексы для быстрого радиусного поиска через bounding box.
CREATE INDEX IF NOT EXISTS idx_parking_zones_active_centroid_lat
ON parking_zones (centroid_latitude)
WHERE is_active = TRUE
  AND centroid_latitude IS NOT NULL
  AND centroid_longitude IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_parking_zones_active_centroid_lon
ON parking_zones (centroid_longitude)
WHERE is_active = TRUE
  AND centroid_latitude IS NOT NULL
  AND centroid_longitude IS NOT NULL;