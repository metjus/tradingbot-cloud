-- ARGUS sync D1 schema (idempotent)
CREATE TABLE IF NOT EXISTS config (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  ts INTEGER NOT NULL,
  json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commands (
  id TEXT PRIMARY KEY,
  action TEXT NOT NULL,
  target TEXT,
  ts INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',   -- pending | done
  result TEXT,
  acked_ts INTEGER
);
CREATE INDEX IF NOT EXISTS ix_commands_status ON commands(status, ts);

CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  category TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT,
  data TEXT
);
CREATE INDEX IF NOT EXISTS ix_notifications_ts ON notifications(ts DESC);

CREATE TABLE IF NOT EXISTS push_subscriptions (
  endpoint TEXT PRIMARY KEY,
  json TEXT NOT NULL,
  categories TEXT,                          -- JSON array; NULL = all
  created_ts INTEGER NOT NULL
);
