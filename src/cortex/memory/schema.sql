-- Every row belongs to one company. Natural keys start with tenant_id, and every other table
-- points to its conversation with the composite key, so a row can never cross companies.

CREATE TABLE IF NOT EXISTS conversations (
    tenant_id       TEXT    NOT NULL,
    conversation_id TEXT    NOT NULL,
    mode            TEXT    NOT NULL DEFAULT 'AUTO'
                    CHECK (mode IN ('AUTO', 'SUGGEST', 'HUMAN', 'BLOCKED')),
    control_version INTEGER NOT NULL DEFAULT 0 CHECK (control_version >= 0),
    responsible     TEXT,
    -- The run that holds the conversation right now: one at a time.
    active_run_id   TEXT,
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

-- A write the model proposed, waiting for (or holding) the exact confirmation of its values.
-- `arguments` and `fingerprint` may be NULL for a pending action recorded without its values.
CREATE TABLE IF NOT EXISTS pending_actions (
    tenant_id       TEXT    NOT NULL,
    operation_id    TEXT    NOT NULL,
    conversation_id TEXT    NOT NULL,
    tool            TEXT    NOT NULL,
    arguments       TEXT,
    fingerprint     TEXT,
    control_version INTEGER NOT NULL,
    status          TEXT    NOT NULL
                    CHECK (status IN ('PENDING', 'CONFIRMED', 'SUSPENDED', 'CONSUMED', 'CANCELLED')),
    updated_at      TEXT    NOT NULL,
    PRIMARY KEY (tenant_id, operation_id),
    FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversations (tenant_id, conversation_id)
);

-- The idempotency ledger: one row per write command, whatever happens to it.
CREATE TABLE IF NOT EXISTS operations (
    tenant_id    TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    tool         TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('STARTED', 'COMMITTED', 'UNCERTAIN', 'FAILED')),
    result       TEXT,
    run_id       TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (tenant_id, operation_id)
);

-- One row per command the runner accepted. A repeated run_id replays the stored result.
CREATE TABLE IF NOT EXISTS runs (
    tenant_id       TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    request_digest  TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('RUNNING', 'FINISHED', 'FAILED')),
    result          TEXT,
    started_at      TEXT NOT NULL,
    PRIMARY KEY (tenant_id, run_id)
);

-- What happened in a conversation, in order. One sequence for every kind, so "after the
-- takeover" can be measured: a delivery or an effect with a higher seq happened later.
CREATE TABLE IF NOT EXISTS journal (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       TEXT    NOT NULL,
    conversation_id TEXT    NOT NULL,
    kind            TEXT    NOT NULL CHECK (kind IN ('takeover', 'delivery', 'effect')),
    reference       TEXT,
    created_at      TEXT    NOT NULL,
    FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversations (tenant_id, conversation_id)
);

-- What someone owes and until when: an internal task for Zoco, or a date the customer chose.
CREATE TABLE IF NOT EXISTS commitments (
    commitment_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       TEXT    NOT NULL,
    conversation_id TEXT    NOT NULL,
    kind            TEXT    NOT NULL CHECK (kind IN ('internal_task', 'contact_date')),
    responsible     TEXT,
    due_date        TEXT,
    status          TEXT    NOT NULL CHECK (status IN ('OPEN', 'CANCELLED')),
    created_at      TEXT    NOT NULL,
    FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversations (tenant_id, conversation_id)
);

CREATE TABLE IF NOT EXISTS followup_decisions (
    decision_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       TEXT    NOT NULL,
    conversation_id TEXT    NOT NULL,
    run_id          TEXT    NOT NULL,
    decision        TEXT    NOT NULL
                    CHECK (decision IN ('CONTACTAR', 'TAREA_INTERNA', 'REPROGRAMAR', 'CANCELAR')),
    reasons         TEXT    NOT NULL,
    decided_at      TEXT    NOT NULL,
    FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversations (tenant_id, conversation_id)
);
