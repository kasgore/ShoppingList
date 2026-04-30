"""Semantic recipe search via fastembed + sqlite-vec.

Loads MiniLM-L6 embeddings lazily on first use and stores 384-dim
vectors in a sqlite-vec virtual table parallel to the recipe table.
Used by the /recipes search to broaden matches beyond keyword LIKE
patterns ("comfort dish with chicken" → matches Chicken Pot Pie even
without the word "comfort" in the recipe).

Both dependencies are optional. If fastembed or sqlite-vec aren't
installed (or fail to load on this Pi build of arm64), every public
function returns a sentinel and the app continues with keyword-only
search. Check `is_available()` before assuming embeddings work.
"""
from __future__ import annotations

import sqlite3
import struct
import threading

EMBEDDING_DIM = 384
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

_model = None
_model_load_failed = False
# Guard the singleton init: with gunicorn 1 worker / 8 threads, two
# concurrent requests on a cold start can both see _model=None and both
# try to instantiate the 22 MB model — wasted RAM on the Pi 4.
_model_lock = threading.Lock()


def _load_model():
    """Lazily instantiate the fastembed TextEmbedding model. Cached for
    the lifetime of the process — model load is ~2-5 s on Pi 5,
    longer on Pi 4."""
    global _model, _model_load_failed
    # Fast path: read without lock.
    if _model is not None or _model_load_failed:
        return _model
    with _model_lock:
        # Double-checked locking: re-read after acquiring the lock so
        # only the first thread does the actual instantiation.
        if _model is not None or _model_load_failed:
            return _model
        try:
            from fastembed import TextEmbedding
            _model = TextEmbedding(EMBEDDING_MODEL)
        except Exception:
            _model_load_failed = True
        return _model


def is_available() -> bool:
    """True if both fastembed and sqlite-vec are usable in this env."""
    if _load_model() is None:
        return False
    try:
        import sqlite_vec  # noqa: F401
        return True
    except ImportError:
        return False


def setup_extension(conn: sqlite3.Connection) -> bool:
    """Load the sqlite-vec extension on this connection. No-op on
    sqlite builds without extension support, which is rare."""
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:
        return False


def init_schema(conn: sqlite3.Connection) -> bool:
    """Create the recipe_embedding virtual table if not exists."""
    if not setup_extension(conn):
        return False
    try:
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS recipe_embedding "
            f"USING vec0(recipe_id INTEGER PRIMARY KEY, "
            f"embedding FLOAT[{EMBEDDING_DIM}])"
        )
        return True
    except Exception:
        return False


def build_recipe_text(recipe_row, ingredient_rows) -> str:
    """Combine the parts of a recipe that matter for retrieval into
    one string the embedder can encode."""
    parts: list[str] = []
    name = recipe_row["name"] if "name" in recipe_row.keys() else ""
    if name:
        parts.append(name)
    description = (
        recipe_row["description"] if "description" in recipe_row.keys() else ""
    )
    if description:
        parts.append(description)
    category = (
        recipe_row["category"] if "category" in recipe_row.keys() else ""
    )
    if category:
        parts.append(category)
    cuisine = (
        recipe_row["cuisine"] if "cuisine" in recipe_row.keys() else ""
    )
    if cuisine:
        parts.append(cuisine)
    keywords = (
        recipe_row["keywords"] if "keywords" in recipe_row.keys() else ""
    )
    if keywords:
        parts.append(keywords)
    for ing in ingredient_rows:
        parts.append(ing["name"])
    return ". ".join(p for p in parts if p)


def _serialize(vec) -> bytes:
    """Pack a Python list/numpy array into the float32 blob sqlite-vec wants."""
    return struct.pack(f"{EMBEDDING_DIM}f", *vec)


def encode(text: str) -> bytes | None:
    """Encode `text` to a 384-dim float32 blob, ready for INSERT."""
    model = _load_model()
    if model is None or not text:
        return None
    try:
        vec = next(iter(model.embed([text])))
        return _serialize(vec)
    except Exception:
        return None


def upsert_recipe_embedding(
    conn: sqlite3.Connection, recipe_id: int, text: str
) -> bool:
    """Compute and store/replace an embedding row for the recipe.

    Skips the (relatively expensive) re-encode when the input text hash
    matches the row already stored in `recipe_embedding_hash` — common
    case after a recipe edit that only changed the rating or favorite
    flag. Falls back to a plain upsert if the hash table doesn't exist
    yet (older deploys, mid-migration).
    """
    if not text:
        return False
    import hashlib
    text_hash = hashlib.sha1(text.encode("utf-8")).hexdigest()
    try:
        existing = conn.execute(
            "SELECT text_hash FROM recipe_embedding_hash WHERE recipe_id = ?",
            (recipe_id,),
        ).fetchone()
        if existing is not None and existing[0] == text_hash:
            return True  # text unchanged — keep the existing embedding
    except sqlite3.OperationalError:
        # Hash table not present — proceed with a fresh encode.
        pass

    blob = encode(text)
    if blob is None:
        return False
    try:
        # vec0 virtual tables require DELETE+INSERT to update an existing
        # row by primary key — UPSERT/INSERT OR REPLACE behaves as expected.
        conn.execute(
            "DELETE FROM recipe_embedding WHERE recipe_id = ?", (recipe_id,)
        )
        conn.execute(
            "INSERT INTO recipe_embedding (recipe_id, embedding) VALUES (?, ?)",
            (recipe_id, blob),
        )
        try:
            conn.execute(
                "INSERT OR REPLACE INTO recipe_embedding_hash "
                "(recipe_id, text_hash) VALUES (?, ?)",
                (recipe_id, text_hash),
            )
        except sqlite3.OperationalError:
            pass  # hash table missing on older builds — non-fatal
        return True
    except Exception:
        return False


def search(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[int]:
    """Return recipe_ids ordered by semantic similarity to `query`.
    Empty list if the model or extension isn't available."""
    blob = encode(query)
    if blob is None:
        return []
    try:
        rows = conn.execute(
            "SELECT recipe_id FROM recipe_embedding "
            "WHERE embedding MATCH ? AND k = ? "
            "ORDER BY distance",
            (blob, limit),
        ).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []
