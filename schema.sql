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

-- DEMO-only manual order request journal, separate from synthetic and USD sandbox accounts.
CREATE TABLE IF NOT EXISTS demo_order_intents (
 intent_id TEXT PRIMARY KEY,
 side TEXT NOT NULL,
 ticker TEXT NOT NULL,
 quantity REAL NOT NULL,
 limit_price REAL NOT NULL,
 notional REAL NOT NULL,
 state TEXT NOT NULL,
 broker_id TEXT,
 error_code TEXT,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS demo_instrument_catalog (
 id INTEGER PRIMARY KEY CHECK(id=1),
 json TEXT NOT NULL,
 fetched_at TEXT NOT NULL
);
