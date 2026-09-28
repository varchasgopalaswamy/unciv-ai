-- Current accounting schema, independent of SQLAlchemy metadata for restart tests.
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, payload TEXT NOT NULL, sent INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY, job TEXT NOT NULL, keys TEXT NOT NULL,
    started REAL NOT NULL, outcome TEXT, replayed INTEGER NOT NULL DEFAULT 0, elapsed INTEGER);
