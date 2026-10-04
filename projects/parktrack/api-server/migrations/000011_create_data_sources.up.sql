CREATE TABLE IF NOT EXISTS data_sources (
    source_id           SERIAL PRIMARY KEY,
    partner_id          INTEGER REFERENCES partners(partner_id) ON DELETE SET NULL,
    created_by_user_id  INTEGER REFERENCES users(user_id)       ON DELETE SET NULL,
    source_type         VARCHAR(50)  NOT NULL,
    entity_type         VARCHAR(50)  NOT NULL,
    entity_id           INTEGER      NOT NULL,
    title               VARCHAR(255) NOT NULL,
    status              VARCHAR(20)  NOT NULL DEFAULT 'unknown'
                            CHECK (status IN ('active', 'paused', 'error', 'deleted', 'unknown')),
    last_data_at        TIMESTAMP WITH TIME ZONE,
    last_error          TEXT,
    is_active           BOOLEAN DEFAULT TRUE,
    created_at          TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at          TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE (entity_type, entity_id)
);

CREATE INDEX idx_data_sources_partner_id  ON data_sources(partner_id);
CREATE INDEX idx_data_sources_entity      ON data_sources(entity_type, entity_id);
CREATE INDEX idx_data_sources_source_type ON data_sources(source_type);
CREATE INDEX idx_data_sources_status      ON data_sources(status);
