-- Every row belongs to one company. Natural keys start with tenant_id, and every other table
-- points to its conversation with the composite key, so a row can never cross companies.

CREATE TABLE IF NOT EXISTS conversations (
    tenant_id       TEXT    NOT NULL,
    conversation_id TEXT    NOT NULL,
    mode            TEXT    NOT NULL DEFAULT 'AUTO'
                    CHECK (mode IN ('AUTO', 'SUGGEST', 'HUMAN', 'BLOCKED')),
    control_version INTEGER NOT NULL DEFAULT 0 CHECK (control_version >= 0),
    responsible     TEXT,
    created_at      TEXT    NOT NULL,
    PRIMARY KEY (tenant_id, conversation_id)
);

CREATE TABLE IF NOT EXISTS messages (
    message_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       TEXT    NOT NULL,
    conversation_id TEXT    NOT NULL,
    run_id          TEXT    NOT NULL,
    role            TEXT    NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
    content         TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversations (tenant_id, conversation_id)
);

CREATE INDEX IF NOT EXISTS messages_by_conversation
    ON messages (tenant_id, conversation_id, message_id);

-- What the harness tried to send to the customer. SUPPRESSED means a reply was generated but
-- the control of the conversation changed before it could be delivered.
CREATE TABLE IF NOT EXISTS outbox (
    outbox_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       TEXT    NOT NULL,
    conversation_id TEXT    NOT NULL,
    run_id          TEXT    NOT NULL,
    content         TEXT    NOT NULL,
    status          TEXT    NOT NULL CHECK (status IN ('SENT', 'SUPPRESSED')),
    control_version INTEGER NOT NULL,
    created_at      TEXT    NOT NULL,
    FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversations (tenant_id, conversation_id)
);
