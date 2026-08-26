"""ARIA Lite Phase 3 - Memory & Knowledge System.

SQLite-backed storage for notes, embeddings, semantic chunks, indexed files,
user context and ARIA's own self-state. Standard library only.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "aria_memory.db"


def get_connection():
    """Open a connection to the memory database with dict-like row access.

    Foreign keys are enforced per connection: SQLite parses REFERENCES
    clauses but ignores them unless this pragma is set, so without it the
    semantic_index -> notes constraint would be documentation rather than
    a rule, and ON DELETE CASCADE would never fire.
    """
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    text TEXT NOT NULL,
    tags TEXT,
    source TEXT
);

CREATE TABLE IF NOT EXISTS embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL,
    vector BLOB NOT NULL,
    model TEXT,
    dim INTEGER,
    FOREIGN KEY(note_id) REFERENCES notes(id)
);

CREATE TABLE IF NOT EXISTS semantic_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL,
    chunk TEXT NOT NULL,
    vector BLOB NOT NULL,
    chunk_index INTEGER,
    start_word INTEGER,
    end_word INTEGER,
    created_at TEXT,
    model TEXT,
    dim INTEGER,
    FOREIGN KEY(note_id) REFERENCES notes(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_indexed TEXT,
    tags TEXT,
    name TEXT,
    mime TEXT,
    size_bytes INTEGER,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS file_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    chunk TEXT NOT NULL,
    vector BLOB NOT NULL,
    chunk_index INTEGER,
    start_word INTEGER,
    end_word INTEGER,
    created_at TEXT,
    text TEXT,
    start_offset INTEGER,
    end_offset INTEGER,
    updated_at TEXT,
    model TEXT,
    dim INTEGER,
    section TEXT,
    FOREIGN KEY(file_id) REFERENCES files(id)
);

CREATE TABLE IF NOT EXISTS user_context (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS aria_self (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS aria_self_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    version INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    superseded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notes_tags ON notes(tags);
CREATE INDEX IF NOT EXISTS idx_embeddings_note_id ON embeddings(note_id);
CREATE INDEX IF NOT EXISTS idx_semantic_index_note_id ON semantic_index(note_id);
CREATE INDEX IF NOT EXISTS idx_file_chunks_file_id ON file_chunks(file_id);
CREATE INDEX IF NOT EXISTS idx_user_context_key ON user_context(key);
CREATE INDEX IF NOT EXISTS idx_aria_self_key ON aria_self(key);
CREATE INDEX IF NOT EXISTS idx_aria_self_history_key ON aria_self_history(key, version);
"""


# Columns added to a table after the original Phase 3 schema
# shipped. CREATE TABLE IF NOT EXISTS silently does nothing on a database
# that already has the table, so a database created before this change
# would never gain these columns -- _migrate_chunk_metadata() adds them
# in place instead.
_ADDED_COLUMNS = {
    "semantic_index": (
        ("chunk_index", "INTEGER"),
        ("start_word", "INTEGER"),
        ("end_word", "INTEGER"),
        ("created_at", "TEXT"),
        ("model", "TEXT"),
        ("dim", "INTEGER"),
    ),
    "file_chunks": (
        ("chunk_index", "INTEGER"),
        ("start_word", "INTEGER"),
        ("end_word", "INTEGER"),
        ("created_at", "TEXT"),
        # Phase 4 file ingestion. `text` is the chunk body under the name
        # the ingestion module uses; the older `chunk` column is NOT NULL
        # and is written with the same value so both stay consistent.
        ("text", "TEXT"),
        ("start_offset", "INTEGER"),
        ("end_offset", "INTEGER"),
        ("updated_at", "TEXT"),
        ("model", "TEXT"),
        ("dim", "INTEGER"),
        # The document section a chunk sits under, tracked at
        # chunk time and travelling with the chunk into its vector.
        ("section", "TEXT"),
    ),
    "files": (
        ("name", "TEXT"),
        ("mime", "TEXT"),
        ("size_bytes", "INTEGER"),
        ("updated_at", "TEXT"),
    ),
    "embeddings": (("model", "TEXT"), ("dim", "INTEGER")),
    "user_context": (("expires_at", "TEXT"),),
    "aria_self": (("version", "INTEGER NOT NULL DEFAULT 1"),),
}


def _migrate_added_columns(conn) -> list[str]:
    """Add any column introduced after a table first shipped.

    Returns what it added. Every column here is nullable by necessity
    (SQLite cannot add a NOT NULL column without a default) and honestly
    so: a NULL marks a row written before the column existed.
    """
    added = []
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, declared_type in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declared_type}")
                added.append(f"{table}.{name}")
    return added


# A FOREIGN KEY clause cannot be altered in place -- SQLite's documented
# procedure is to rebuild the table, copy the rows across, and swap it in.
# Only semantic_index needs it (that is the constraint this upgrade is
# about), and only on databases created before the cascade was declared,
# which is what the sqlite_master check below detects.
_SEMANTIC_INDEX_REBUILD = (
    """
    CREATE TABLE semantic_index_migrated (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        note_id INTEGER NOT NULL,
        chunk TEXT NOT NULL,
        vector BLOB NOT NULL,
        chunk_index INTEGER,
        start_word INTEGER,
        end_word INTEGER,
        created_at TEXT,
        model TEXT,
        dim INTEGER,
        FOREIGN KEY(note_id) REFERENCES notes(id) ON DELETE CASCADE
    )
    """,
    """
    INSERT INTO semantic_index_migrated
        (id, note_id, chunk, vector, chunk_index, start_word, end_word,
         created_at, model, dim)
    SELECT id, note_id, chunk, vector, chunk_index, start_word, end_word,
           created_at, model, dim
    FROM semantic_index
    """,
    "DROP TABLE semantic_index",
    "ALTER TABLE semantic_index_migrated RENAME TO semantic_index",
    "CREATE INDEX IF NOT EXISTS idx_semantic_index_note_id ON semantic_index(note_id)",
)


def _semantic_index_has_cascade(conn) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='semantic_index'"
    ).fetchone()
    return bool(row) and "ON DELETE CASCADE" in row["sql"].upper()


def _migrate_semantic_index_fk(conn) -> bool:
    """Rebuild semantic_index with ON DELETE CASCADE if it lacks it.

    Returns True when a rebuild happened. Foreign keys must be off for
    the swap (the pragma is a no-op inside a transaction, hence the
    commit first), and any row whose note no longer exists is dropped on
    the way through -- it could not satisfy the new constraint, and it
    was already unreachable through every read path in this package.
    """
    if _semantic_index_has_cascade(conn):
        return False

    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute(
            "DELETE FROM semantic_index WHERE note_id NOT IN (SELECT id FROM notes)"
        )
        for statement in _SEMANTIC_INDEX_REBUILD:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return True


def init_db():
    """Create every Phase 3 table and index if they do not already exist,
    then bring an older database up to the current column set."""
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        _migrate_added_columns(conn)
        conn.commit()
        _migrate_semantic_index_fk(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"ARIA memory database initialized at {DB_PATH}")
