-- Family Tree schema v1
-- Dates are stored as optional parts (day/month/year) so unknown years are
-- representable without inventing values. All timestamps are UTC epoch seconds.

CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    email           TEXT NOT NULL,
    email_norm      TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin','user')),
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','active','disabled')),
    person_id       TEXT REFERENCES persons(id) ON DELETE SET NULL,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until    INTEGER,
    token_version   INTEGER NOT NULL DEFAULT 1,
    created_at      INTEGER NOT NULL,
    approved_by     TEXT,
    approved_at     INTEGER,
    last_login_at   INTEGER
);

CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash   TEXT NOT NULL UNIQUE,
    csrf_token   TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL,
    revoked_at   INTEGER,
    ip           TEXT,
    user_agent   TEXT
);

CREATE TABLE IF NOT EXISTS invites (
    id         TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL,
    person_id  TEXT,
    note       TEXT NOT NULL DEFAULT '',
    role       TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin','user')),
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    used_at    INTEGER,
    used_by    TEXT
);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS persons (
    id                 TEXT PRIMARY KEY,
    given_name         TEXT NOT NULL DEFAULT '',
    middle_names       TEXT NOT NULL DEFAULT '',
    surname            TEXT NOT NULL DEFAULT '',
    maiden_name        TEXT NOT NULL DEFAULT '',
    nickname           TEXT NOT NULL DEFAULT '',
    gender             TEXT NOT NULL DEFAULT 'other' CHECK (gender IN ('male','female','other')),
    is_living          INTEGER NOT NULL DEFAULT 1 CHECK (is_living IN (0,1)),

    birth_day          INTEGER CHECK (birth_day IS NULL OR birth_day BETWEEN 1 AND 31),
    birth_month        INTEGER CHECK (birth_month IS NULL OR birth_month BETWEEN 1 AND 12),
    birth_year         INTEGER CHECK (birth_year IS NULL OR birth_year BETWEEN 1000 AND 2200),
    birth_qualifier    TEXT NOT NULL DEFAULT 'exact' CHECK (birth_qualifier IN ('exact','about','before','after')),

    death_day          INTEGER CHECK (death_day IS NULL OR death_day BETWEEN 1 AND 31),
    death_month        INTEGER CHECK (death_month IS NULL OR death_month BETWEEN 1 AND 12),
    death_year         INTEGER CHECK (death_year IS NULL OR death_year BETWEEN 1000 AND 2200),
    death_qualifier    TEXT NOT NULL DEFAULT 'exact' CHECK (death_qualifier IN ('exact','about','before','after')),
    resting_place      TEXT NOT NULL DEFAULT '',
    memorial_note      TEXT NOT NULL DEFAULT '',

    notes              TEXT NOT NULL DEFAULT '',
    email              TEXT NOT NULL DEFAULT '',
    phone              TEXT NOT NULL DEFAULT '',
    phone_alt          TEXT NOT NULL DEFAULT '',
    address_line1      TEXT NOT NULL DEFAULT '',
    address_line2      TEXT NOT NULL DEFAULT '',
    city               TEXT NOT NULL DEFAULT '',
    region             TEXT NOT NULL DEFAULT '',
    postal_code        TEXT NOT NULL DEFAULT '',
    country            TEXT NOT NULL DEFAULT '',

    avatar_key         TEXT,
    avatar_updated_at  INTEGER,
    contact_visibility TEXT NOT NULL DEFAULT 'all' CHECK (contact_visibility IN ('all','family','private')),
    private_fields     TEXT NOT NULL DEFAULT '[]',
    search_text        TEXT NOT NULL DEFAULT '',

    version            INTEGER NOT NULL DEFAULT 1,
    created_by         TEXT,
    updated_by         TEXT,
    created_at         INTEGER NOT NULL,
    updated_at         INTEGER NOT NULL,
    deleted_at         INTEGER
);

CREATE TABLE IF NOT EXISTS unions (
    id             TEXT PRIMARY KEY,
    partner1_id    TEXT NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    partner2_id    TEXT REFERENCES persons(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL DEFAULT 'unknown' CHECK (kind IN ('marriage','civil_union','partnership','unknown')),
    status         TEXT NOT NULL DEFAULT 'unknown' CHECK (status IN ('ongoing','divorced','widowed','separated','unknown')),
    start_day      INTEGER, start_month INTEGER, start_year INTEGER,
    start_qualifier TEXT NOT NULL DEFAULT 'exact' CHECK (start_qualifier IN ('exact','about','before','after')),
    end_day        INTEGER, end_month INTEGER, end_year INTEGER,
    end_qualifier  TEXT NOT NULL DEFAULT 'exact' CHECK (end_qualifier IN ('exact','about','before','after')),
    notes          TEXT NOT NULL DEFAULT '',
    created_by     TEXT,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL,
    deleted_at     INTEGER,
    CHECK (partner2_id IS NULL OR partner1_id <> partner2_id)
);

CREATE TABLE IF NOT EXISTS union_children (
    union_id          TEXT NOT NULL REFERENCES unions(id) ON DELETE CASCADE,
    child_id          TEXT NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    relationship_type TEXT NOT NULL DEFAULT 'birth' CHECK (relationship_type IN ('birth','adopted','step','foster')),
    sort_order        INTEGER NOT NULL DEFAULT 0,
    created_at        INTEGER NOT NULL,
    PRIMARY KEY (union_id, child_id)
);

-- Derived from union_children + unions, maintained transactionally.
-- The union tables remain the source of truth; this table makes ancestor
-- queries, cycle checks and scope checks fast in both directions.
CREATE TABLE IF NOT EXISTS parent_child (
    parent_id         TEXT NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    child_id          TEXT NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    union_id          TEXT NOT NULL REFERENCES unions(id) ON DELETE CASCADE,
    relationship_type TEXT NOT NULL DEFAULT 'birth',
    PRIMARY KEY (parent_id, child_id, union_id)
);

CREATE TABLE IF NOT EXISTS change_sets (
    id              TEXT PRIMARY KEY,
    submitted_by    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title           TEXT NOT NULL DEFAULT '',
    note            TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','needs_rebase','withdrawn')),
    submitted_at    INTEGER NOT NULL,
    reviewed_by     TEXT,
    reviewed_at     INTEGER,
    decision_reason TEXT NOT NULL DEFAULT '',
    conflict_json   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS change_items (
    id           TEXT PRIMARY KEY,
    set_id       TEXT NOT NULL REFERENCES change_sets(id) ON DELETE CASCADE,
    op           TEXT NOT NULL CHECK (op IN ('person_create','person_update','union_create','union_update','union_child_add')),
    entity_type  TEXT NOT NULL CHECK (entity_type IN ('person','union','union_child')),
    entity_id    TEXT,
    base_version INTEGER,
    payload_json TEXT NOT NULL DEFAULT '{}',
    diff_json    TEXT NOT NULL DEFAULT '[]',
    sort_order   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_events (
    id          TEXT PRIMARY KEY,
    actor_id    TEXT,
    actor_name  TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT,
    before_json TEXT NOT NULL DEFAULT '',
    after_json  TEXT NOT NULL DEFAULT '',
    ip          TEXT NOT NULL DEFAULT '',
    user_agent  TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id, revoked_at);
CREATE INDEX IF NOT EXISTS ix_sessions_expiry ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS ix_persons_name ON persons(surname, given_name);
CREATE INDEX IF NOT EXISTS ix_persons_search ON persons(search_text);
CREATE INDEX IF NOT EXISTS ix_persons_deleted ON persons(deleted_at);
CREATE INDEX IF NOT EXISTS ix_unions_p1 ON unions(partner1_id);
CREATE INDEX IF NOT EXISTS ix_unions_p2 ON unions(partner2_id);
CREATE INDEX IF NOT EXISTS ix_uc_child ON union_children(child_id);
CREATE INDEX IF NOT EXISTS ix_pc_parent ON parent_child(parent_id);
CREATE INDEX IF NOT EXISTS ix_pc_child ON parent_child(child_id);
CREATE INDEX IF NOT EXISTS ix_change_sets_status ON change_sets(status, submitted_at);
CREATE INDEX IF NOT EXISTS ix_change_items_set ON change_items(set_id, sort_order);
CREATE INDEX IF NOT EXISTS ix_change_items_entity ON change_items(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS ix_audit_entity ON audit_events(entity_type, entity_id, created_at);
CREATE INDEX IF NOT EXISTS ix_invites_hash ON invites(token_hash);

-- A person may have only one birth family; adoptive/step/foster links may
-- coexist in separate unions.
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_birth_union
    ON union_children(child_id) WHERE relationship_type = 'birth';
