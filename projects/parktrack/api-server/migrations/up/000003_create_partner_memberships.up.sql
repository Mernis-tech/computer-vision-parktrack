CREATE TABLE IF NOT EXISTS partner_memberships (
    partner_membership_id SERIAL PRIMARY KEY,
    uesr_id INTEGER REFERENCES users(user_id) ON DELETE CASCADE,
    partner_id INTEGER REFERENCES partners(partner_id) ON DELETE CASCADE,
    user_role VARCHAR(50) NOT NULL,
    read_scope VARCHAR(50) DEFAULT 'own',
    write_scope VARCHAR(50) DEFAULT 'own',
    delete_scope VARCHAR(50) DEFAULT 'own',
    created_at TIMESTAMP DEFAULT NOW(),
    
    UNIQUE(user_id, partner_id)
);