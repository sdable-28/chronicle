import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

CURRENT_VERSION = 3

DEFAULT_DB = Path.home() / ".chronicle" / "chronicle.db"


def find_db() -> Path:
    if env := os.environ.get("CHRONICLE_DB"):
        return Path(env)
    return DEFAULT_DB


@contextmanager
def get_db(db_path: Path | str | None = None):
    path = Path(db_path) if db_path else find_db()
    if not path.exists():
        raise RuntimeError(
            f"Chronicle DB not found at {path}.\nRun 'chronicle init' first."
        )
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS entities (
    id                    TEXT PRIMARY KEY,
    name                  TEXT NOT NULL,
    type                  TEXT NOT NULL DEFAULT 'HYPOTHESIS'
                            CHECK(type IN (
                                'STRATEGY','EDGE','HYPOTHESIS',
                                'FEATURE','OBSERVATION','SYSTEM'
                            )),
    status                TEXT NOT NULL DEFAULT 'PROPOSED'
                            CHECK(status IN (
                                'PROPOSED','IN_RESEARCH','VALIDATED','LIVE',
                                'FALSIFIED','DEPRECATED','SUPERSEDED'
                            )),
    parent_id             TEXT REFERENCES entities(id),
    guard_tags            TEXT,
    history_complete      INTEGER NOT NULL DEFAULT 1,
    pre_chronicle_context TEXT,
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now')),
    notes                 TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id           TEXT    NOT NULL REFERENCES entities(id),
    event_type          TEXT    NOT NULL CHECK(event_type IN (
                            'PROPOSED','VALIDATED','FALSIFIED','WIRED',
                            'REVERTED','SUPERSEDED','DEPRECATED',
                            'RESULT','PARTIAL_RESULT','EXPLORED','TUNED',
                            'PARAMETER_CHANGE','COMBINED','OBSERVATION',
                            'DECISION','NOTE',
                            'WIRING_VERIFIED','WIRING_BROKEN'
                        )),
    event_at            TEXT    NOT NULL,
    logged_at           TEXT    NOT NULL DEFAULT (datetime('now')),
    description         TEXT    NOT NULL,
    finding             TEXT,
    why                 TEXT,
    gap                 TEXT,
    next_step           TEXT,
    result_primary      REAL,
    validation_primary  REAL,
    validation_holdout  REAL,
    metric_pct          REAL,
    n_samples           INTEGER,
    baseline            REAL,
    script              TEXT,
    git_commit          TEXT,
    external_run_id     TEXT,
    conditions          TEXT,
    expires_at          TEXT,
    embedding           BLOB
);

CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    entity_id  UNINDEXED,
    description,
    finding,
    why,
    gap,
    content='events',
    content_rowid='id'
);

CREATE TABLE IF NOT EXISTS entity_metrics (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id        TEXT    NOT NULL REFERENCES entities(id),
    event_id         INTEGER REFERENCES events(id),
    metric_name      TEXT    NOT NULL,
    metric_value_num REAL,
    metric_value_str TEXT,
    logged_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS entity_aliases (
    alias        TEXT PRIMARY KEY,
    canonical_id TEXT NOT NULL REFERENCES entities(id),
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    note         TEXT
);

CREATE TABLE IF NOT EXISTS wiring (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id     TEXT    NOT NULL REFERENCES entities(id),
    file          TEXT    NOT NULL,
    line_start    INTEGER,
    line_end      INTEGER,
    description   TEXT,
    status        TEXT    NOT NULL DEFAULT 'ACTIVE'
                    CHECK(status IN ('ACTIVE','STALE','BROKEN','REMOVED')),
    git_commit    TEXT,
    wired_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    verified_at      TEXT,
    last_check_at    TEXT,
    content_snippet  TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT    PRIMARY KEY,
    started_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    ended_at      TEXT,
    summary       TEXT,
    events_logged INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS guard_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    proposed        TEXT    NOT NULL,
    matched_entity  TEXT    NOT NULL,
    matched_status  TEXT    NOT NULL,
    similarity      REAL,
    match_type      TEXT,
    triggered_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_entity   ON events(entity_id);
CREATE INDEX IF NOT EXISTS idx_events_type     ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_event_at ON events(event_at);
CREATE INDEX IF NOT EXISTS idx_events_expires  ON events(expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_wiring_entity   ON wiring(entity_id);
CREATE INDEX IF NOT EXISTS idx_wiring_status   ON wiring(entity_id, status);
CREATE INDEX IF NOT EXISTS idx_metrics_entity  ON entity_metrics(entity_id);
CREATE INDEX IF NOT EXISTS idx_entities_status ON entities(status);

CREATE TRIGGER IF NOT EXISTS events_fts_ai AFTER INSERT ON events BEGIN
    INSERT INTO events_fts(rowid, entity_id, description, finding, why, gap)
    VALUES (new.id, new.entity_id, new.description, new.finding, new.why, new.gap);
END;

CREATE TRIGGER IF NOT EXISTS events_fts_ad AFTER DELETE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, entity_id, description, finding, why, gap)
    VALUES ('delete', old.id, old.entity_id, old.description, old.finding, old.why, old.gap);
END;

CREATE TRIGGER IF NOT EXISTS events_fts_au AFTER UPDATE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, entity_id, description, finding, why, gap)
    VALUES ('delete', old.id, old.entity_id, old.description, old.finding, old.why, old.gap);
    INSERT INTO events_fts(rowid, entity_id, description, finding, why, gap)
    VALUES (new.id, new.entity_id, new.description, new.finding, new.why, new.gap);
END;

CREATE TRIGGER IF NOT EXISTS entities_updated_at AFTER UPDATE ON entities BEGIN
    UPDATE entities SET updated_at = datetime('now') WHERE id = new.id;
END;
"""

# Status transitions triggered by lifecycle events
STATUS_TRANSITIONS: dict[str, str] = {
    "PROPOSED":   "PROPOSED",
    "VALIDATED":  "VALIDATED",
    "FALSIFIED":  "FALSIFIED",
    "WIRED":      "LIVE",
    "REVERTED":   "IN_RESEARCH",
    "SUPERSEDED": "SUPERSEDED",
    "DEPRECATED": "DEPRECATED",
}

RESEARCH_EVENTS = {
    "RESULT", "PARTIAL_RESULT", "EXPLORED", "TUNED",
    "PARAMETER_CHANGE", "COMBINED", "OBSERVATION",
    "DECISION", "NOTE", "WIRING_VERIFIED", "WIRING_BROKEN",
}

# Version-gated schema migrations.
# Add entries here when CURRENT_VERSION is bumped — each value is a SQL
# script applied exactly once when the DB is behind that version.
MIGRATIONS: dict[int, str] = {
    2: """
CREATE TABLE IF NOT EXISTS guard_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    proposed        TEXT    NOT NULL,
    matched_entity  TEXT    NOT NULL,
    matched_status  TEXT    NOT NULL,
    similarity      REAL,
    match_type      TEXT,
    triggered_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);
""",
    3: "ALTER TABLE wiring ADD COLUMN content_snippet TEXT;",
}


def init_db(db_path: Path | None = None) -> Path:
    path = Path(db_path) if db_path else find_db()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_V1)
    conn.execute(
        "INSERT OR REPLACE INTO schema_version(version) VALUES (?)",
        (CURRENT_VERSION,),
    )
    conn.commit()
    conn.close()
    return path


def get_version(db_path: Path | None = None) -> int:
    path = Path(db_path) if db_path else find_db()
    if not path.exists():
        return 0
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return row[0] or 0
    except Exception:
        return 0
    finally:
        conn.close()


def apply_migrations(conn: sqlite3.Connection) -> list[int]:
    """Apply any pending schema migrations in version order.

    Returns list of version numbers that were applied (empty = already current).
    Each migration is applied exactly once — safe to call on every startup.
    """
    current = conn.execute(
        "SELECT MAX(version) FROM schema_version"
    ).fetchone()[0] or 0
    applied: list[int] = []
    for version in sorted(MIGRATIONS):
        if version > current:
            conn.executescript(MIGRATIONS[version])
            conn.execute(
                "INSERT OR REPLACE INTO schema_version(version) VALUES (?)",
                (version,),
            )
            applied.append(version)
    return applied


def resolve_entity(entity_id: str, conn: sqlite3.Connection) -> str | None:
    """Resolve entity ID following aliases. Returns canonical ID or None."""
    row = conn.execute("SELECT id FROM entities WHERE id = ?", (entity_id,)).fetchone()
    if row:
        return row[0]
    row = conn.execute(
        "SELECT canonical_id FROM entity_aliases WHERE alias = ?", (entity_id,)
    ).fetchone()
    if row:
        return row[0]
    return None
