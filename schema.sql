CREATE TABLE IF NOT EXISTS paper_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  revision INTEGER NOT NULL DEFAULT 0,
  json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Market-data observations (read-only provider cache)
CREATE TABLE IF NOT EXISTS market_snapshot (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Independent USD market-price virtual account. Never mixed with paper_state.
CREATE TABLE IF NOT EXISTS real_price_paper (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  revision INTEGER NOT NULL,
  json TEXT NOT NULL
);
