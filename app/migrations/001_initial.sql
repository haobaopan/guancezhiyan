-- Application data only. The public knowledge database is deliberately separate.
CREATE TABLE users (
    user_id TEXT PRIMARY KEY,
    email TEXT NOT NULL COLLATE NOCASE UNIQUE,
    email_verified_at TEXT,
    display_name TEXT NOT NULL DEFAULT '',
    memory_extraction_enabled INTEGER NOT NULL DEFAULT 1 CHECK (memory_extraction_enabled IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled', 'deleted')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE email_verification_codes (
    code_id TEXT PRIMARY KEY,
    email TEXT NOT NULL COLLATE NOCASE,
    purpose TEXT NOT NULL CHECK (purpose IN ('register', 'login')),
    code_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
    consumed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_email_codes_lookup ON email_verification_codes(email, purpose, created_at DESC);

CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    last_seen_at TEXT NOT NULL
);
CREATE INDEX idx_sessions_user_expiry ON sessions(user_id, expires_at);

CREATE TABLE projects (
    project_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived', 'deleted')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, project_id)
);
CREATE INDEX idx_projects_owner_updated ON projects(user_id, updated_at DESC);

CREATE TABLE conversations (
    conversation_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived', 'deleted')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id, project_id) REFERENCES projects(user_id, project_id) ON DELETE CASCADE,
    UNIQUE(user_id, project_id, conversation_id)
);
CREATE INDEX idx_conversations_project_updated ON conversations(user_id, project_id, updated_at DESC);

CREATE TABLE messages (
    message_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id, project_id, conversation_id)
        REFERENCES conversations(user_id, project_id, conversation_id) ON DELETE CASCADE
);
CREATE INDEX idx_messages_conversation_time ON messages(user_id, project_id, conversation_id, created_at);

CREATE TABLE conversation_summaries (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(conversation_id) ON DELETE CASCADE,
    summary TEXT NOT NULL DEFAULT '',
    source_message_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE project_summaries (
    project_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id, project_id) REFERENCES projects(user_id, project_id) ON DELETE CASCADE
);

CREATE TABLE memories (
    memory_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    project_id TEXT,
    category TEXT NOT NULL,
    content TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.5 CHECK (importance >= 0.0 AND importance <= 1.0),
    confidence REAL NOT NULL DEFAULT 0.5 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    source_conversation_id TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded', 'deleted')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id, project_id) REFERENCES projects(user_id, project_id) ON DELETE CASCADE,
    FOREIGN KEY (source_conversation_id) REFERENCES conversations(conversation_id) ON DELETE SET NULL
);
CREATE INDEX idx_memories_owner_scope ON memories(user_id, project_id, status, category);

CREATE TABLE model_profiles (
    profile_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    purpose TEXT NOT NULL CHECK (purpose IN ('chat', 'embedding')),
    provider TEXT NOT NULL DEFAULT 'custom',
    base_url TEXT NOT NULL,
    model_name TEXT NOT NULL,
    dimensions INTEGER CHECK (dimensions IS NULL OR dimensions > 0),
    parameters_json TEXT NOT NULL DEFAULT '{}',
    is_active INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, profile_id)
);
CREATE UNIQUE INDEX idx_model_profiles_active_purpose
    ON model_profiles(user_id, purpose) WHERE is_active = 1;

CREATE TABLE private_documents (
    document_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_path TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'ready', 'failed', 'deleted')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, document_id)
);
CREATE INDEX idx_private_documents_owner_state ON private_documents(user_id, status, updated_at DESC);

CREATE TABLE project_files (
    file_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    source_name TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready' CHECK (status IN ('ready', 'failed', 'deleted')),
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id, project_id) REFERENCES projects(user_id, project_id) ON DELETE CASCADE
);
CREATE INDEX idx_project_files_owner_scope ON project_files(user_id, project_id, created_at DESC);

CREATE TABLE memory_jobs (
    job_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    project_id TEXT,
    conversation_id TEXT,
    job_type TEXT NOT NULL CHECK (job_type IN ('extract_memory', 'summarize_conversation', 'summarize_project')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    payload_json TEXT NOT NULL DEFAULT '{}',
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id, project_id) REFERENCES projects(user_id, project_id) ON DELETE CASCADE,
    FOREIGN KEY (user_id, project_id, conversation_id)
        REFERENCES conversations(user_id, project_id, conversation_id) ON DELETE CASCADE
);
CREATE INDEX idx_memory_jobs_status_created ON memory_jobs(status, created_at);
