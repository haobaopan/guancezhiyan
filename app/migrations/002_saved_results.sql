CREATE TABLE saved_results (
 result_id TEXT PRIMARY KEY,
 user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
 conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
 message_id TEXT NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
 payload_json TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE INDEX idx_results_owner ON saved_results(user_id,conversation_id);
