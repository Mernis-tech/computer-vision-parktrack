CREATE TABLE IF NOT EXISTS detection_feedback (
    feedback_id             SERIAL PRIMARY KEY,
    detection_run_id        INTEGER NOT NULL REFERENCES occupancy_observations(observation_id) ON DELETE CASCADE,
    created_by_user_id      INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    rating                  VARCHAR(32) NOT NULL CHECK (rating IN ('correct', 'partially_correct', 'incorrect')),
    expected_occupied_count INTEGER CHECK (expected_occupied_count IS NULL OR expected_occupied_count >= 0),
    expected_free_count     INTEGER CHECK (expected_free_count IS NULL OR expected_free_count >= 0),
    error_type              VARCHAR(64) CHECK (
        error_type IS NULL OR error_type IN (
            'false_positive_car',
            'false_negative_car',
            'wrong_zone_assignment',
            'bad_lighting',
            'bad_camera_angle',
            'calibration_problem',
            'other'
        )
    ),
    comment                 TEXT,
    created_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_detection_feedback_detection_run_id
ON detection_feedback(detection_run_id);

CREATE INDEX IF NOT EXISTS idx_detection_feedback_created_by_user_id
ON detection_feedback(created_by_user_id);

CREATE INDEX IF NOT EXISTS idx_detection_feedback_created_at
ON detection_feedback(created_at DESC);
