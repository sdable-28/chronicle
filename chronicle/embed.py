"""
Optional sqlite-vec embeddings for semantic search.
Requires: pip install sqlite-vec sentence-transformers

Usage:
    chronicle embed all            # embed all un-embedded events
    chronicle search "..." --semantic
"""

import os
import sqlite3
from pathlib import Path

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

_model = None
_vec_loaded = False


def check_deps() -> tuple[bool, str]:
    try:
        import sqlite_vec  # noqa: F401
    except ImportError:
        return False, "sqlite-vec not installed. Run: pip install sqlite-vec"
    try:
        from sentence_transformers import SentenceTransformer  # noqa: F401
    except ImportError:
        return False, "sentence-transformers not installed. Run: pip install sentence-transformers"
    return True, "ok"


def _load_model():
    global _model
    if _model is None:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer("all-MiniLM-L6-v2", local_files_only=True)
    return _model


def _load_vec(conn: sqlite3.Connection):
    global _vec_loaded
    if not _vec_loaded:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _vec_loaded = True


def embed_text(text: str) -> bytes | None:
    ok, msg = check_deps()
    if not ok:
        return None
    import struct
    model = _load_model()
    vec = model.encode(text, normalize_embeddings=True)
    return struct.pack(f"{len(vec)}f", *vec)


def embed_event(event_id: int, conn: sqlite3.Connection) -> bool:
    ok, msg = check_deps()
    if not ok:
        return False
    row = conn.execute(
        "SELECT description, finding, why, gap FROM events WHERE id=?", (event_id,)
    ).fetchone()
    if not row:
        return False
    text = " ".join(filter(None, [row[0], row[1], row[2], row[3]]))
    blob = embed_text(text)
    if blob:
        conn.execute("UPDATE events SET embedding=? WHERE id=?", (blob, event_id))
    return blob is not None


def embed_all(conn: sqlite3.Connection) -> int:
    ok, msg = check_deps()
    if not ok:
        raise RuntimeError(msg)
    rows = conn.execute(
        "SELECT id FROM events WHERE embedding IS NULL"
    ).fetchall()
    count = 0
    for row in rows:
        if embed_event(row[0], conn):
            count += 1
    return count


def search_semantic(
    query: str, conn: sqlite3.Connection, limit: int = 10
) -> list[dict]:
    ok, msg = check_deps()
    if not ok:
        raise RuntimeError(msg)

    import struct
    _load_vec(conn)

    q_blob = embed_text(query)
    if not q_blob:
        return []

    dim = len(struct.unpack(f"{len(q_blob)//4}f", q_blob))

    rows = conn.execute(
        f"""
        SELECT e.id, ev.id as event_id, ev.event_type, ev.event_at,
               ev.description, ev.entity_id,
               vec_distance_cosine(ev.embedding, ?) as dist
        FROM events ev
        JOIN entities e ON e.id = ev.entity_id
        WHERE ev.embedding IS NOT NULL
        ORDER BY dist ASC
        LIMIT ?
        """,
        (q_blob, limit),
    ).fetchall()

    return [dict(r) for r in rows]
