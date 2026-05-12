"""Database layer: connection management, schema, idempotent migrations,
and seed data. Owns no Flask-specific behavior beyond `g`-scoped pooling
of the per-request connection.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import closing

from flask import g

from ingredient import guess_category


DB_PATH = os.environ.get(
    "SHOPPINGLIST_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "shoppinglist.db"),
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS recipe (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL UNIQUE,
    description  TEXT    DEFAULT '',
    servings     INTEGER NOT NULL DEFAULT 4,
    instructions TEXT    DEFAULT '',
    source_url   TEXT    DEFAULT '',
    image_url    TEXT    DEFAULT '',
    prep_time    INTEGER NOT NULL DEFAULT 0,
    cook_time    INTEGER NOT NULL DEFAULT 0,
    total_time   INTEGER NOT NULL DEFAULT 0,
    category     TEXT    DEFAULT '',
    notes        TEXT    DEFAULT '',
    is_favorite  INTEGER NOT NULL DEFAULT 0,
    rating       INTEGER NOT NULL DEFAULT 0,
    nutrition    TEXT    DEFAULT '',
    yields_text  TEXT    DEFAULT '',
    cuisine      TEXT    DEFAULT '',
    author       TEXT    DEFAULT '',
    source_rating REAL   NOT NULL DEFAULT 0,
    keywords     TEXT    DEFAULT ''
);

CREATE TABLE IF NOT EXISTS ingredient (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id INTEGER NOT NULL REFERENCES recipe(id) ON DELETE CASCADE,
    name      TEXT    NOT NULL,
    quantity  REAL    NOT NULL DEFAULT 1,
    unit      TEXT    NOT NULL DEFAULT '',
    category  TEXT    NOT NULL DEFAULT 'Other',
    note      TEXT    DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ingredient_recipe_id ON ingredient(recipe_id);

CREATE TABLE IF NOT EXISTS list_recipe (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id  INTEGER NOT NULL REFERENCES recipe(id) ON DELETE CASCADE,
    multiplier REAL    NOT NULL DEFAULT 1,
    added_by   TEXT    DEFAULT '',
    added_at   TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS adhoc_item (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT    NOT NULL,
    quantity  REAL    NOT NULL DEFAULT 1,
    unit      TEXT    NOT NULL DEFAULT '',
    category  TEXT    NOT NULL DEFAULT 'Other',
    note      TEXT    DEFAULT '',
    added_by  TEXT    DEFAULT '',
    added_at  TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS checked_item (
    -- Stores which aggregated keys have been checked off.
    -- Two key formats coexist:
    --   "recipe::<normalized_name>::<unit>"  → derived rows from recipes
    --   "adhoc::<id>"                        → ad-hoc one-off items
    -- Both formats are produced by build_shopping_list() in app.py.
    key TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS meal_plan (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_date  TEXT    NOT NULL,                       -- YYYY-MM-DD
    slot       TEXT    NOT NULL DEFAULT '',            -- "Dinner", etc. (free-form)
    recipe_id  INTEGER REFERENCES recipe(id) ON DELETE CASCADE,
    text_plan  TEXT    NOT NULL DEFAULT '',            -- ad-hoc text when no recipe
    multiplier REAL    NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_meal_plan_date ON meal_plan(plan_date);
CREATE INDEX IF NOT EXISTS idx_meal_plan_recipe_id ON meal_plan(recipe_id);

CREATE TABLE IF NOT EXISTS purchase_history (
    -- One row per item checked off the shopping list — the "I bought
    -- it" signal, recipe-derived staples included, not just one-off
    -- ad-hoc adds. Drives the "Quick Add" predictor on the home page
    -- (recency + frequency + co-occurrence + replenishment-cycle
    -- scoring). Written from app.list_toggle on a fresh check-off.
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    unit       TEXT    NOT NULL DEFAULT '',
    checked_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_purchase_history_name ON purchase_history(name);
CREATE INDEX IF NOT EXISTS idx_purchase_history_checked_at
    ON purchase_history(checked_at);

CREATE TABLE IF NOT EXISTS pantry_item (
    -- Staples the family normally keeps stocked. When a recipe pushes
    -- one of these onto the shopping list, build_shopping_list flags it
    -- "probably have it" so the UI can collapse it — keeps salt/oil/
    -- flour from cluttering the list on every recipe. `normalized` is
    -- ingredient.normalize_name(name), matched against the normalized
    -- recipe-ingredient names.
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    normalized  TEXT    NOT NULL,
    added_at    TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pantry_normalized ON pantry_item(normalized);

CREATE TABLE IF NOT EXISTS recipe_embedding_hash (
    -- SHA-1 of the text we last embedded for each recipe. Lets us skip
    -- the (relatively expensive on Pi 4) re-encode when nothing relevant
    -- to the embedding changed (e.g. user only updated the rating).
    recipe_id  INTEGER PRIMARY KEY REFERENCES recipe(id) ON DELETE CASCADE,
    text_hash  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_meta (
    -- Tiny key/value store for app-internal flags. Currently used to
    -- gate the init_db() reclassification pass on classifier-rule
    -- version, so a 1000-row DB doesn't pay the SELECT+UPDATE cost on
    -- every container restart.
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""

# Bump this whenever the rules in ingredient.guess_category() change.
# init_db() will re-run the "Other" reclassification on the next start
# only if the persisted value disagrees with this one.
CLASSIFIER_VERSION = "1"


def _apply_connection_pragmas(conn: sqlite3.Connection) -> None:
    """SQLite resets these to defaults on every new connection regardless
    of file-header settings, so we re-apply on each `get_db()`.

      foreign_keys=ON    enforce ON DELETE CASCADE on recipe→ingredient
      busy_timeout=5000  wait up to 5s for a lock instead of erroring out
      synchronous=NORMAL WAL-safe; halves fsyncs vs FULL — kinder to SD card
      temp_store=MEMORY  keep transient indexes/sorts off disk
    """
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        conn.row_factory = sqlite3.Row
        _apply_connection_pragmas(conn)
        # Best-effort load of sqlite-vec for semantic search. If the
        # extension or fastembed isn't installed/available, vec queries
        # fall back to empty and keyword search keeps working.
        try:
            import embedding as _embedding
            _embedding.setup_extension(conn)
        except ImportError:
            pass
        g.db = conn
    return g.db


def close_db(_exc=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    """Create schema, run idempotent migrations, seed if empty, and
    re-classify ingredient/adhoc rows still in 'Other' aisle.

    Safe to call repeatedly. WAL mode is set once here (persisted in the
    DB file header). Per-connection pragmas live in get_db().
    """
    with closing(sqlite3.connect(DB_PATH)) as conn:
        _apply_connection_pragmas(conn)
        # WAL mode persists in the file header — set once here on first
        # boot. Concurrent readers + a writer don't block each other,
        # which matters when multiple family phones hit the app at once.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        # Idempotent migrations for older DBs created before these columns.
        existing = {
            row[1] for row in conn.execute("PRAGMA table_info(recipe)").fetchall()
        }
        migrations = [
            ("instructions", "ALTER TABLE recipe ADD COLUMN instructions TEXT DEFAULT ''"),
            ("source_url", "ALTER TABLE recipe ADD COLUMN source_url TEXT DEFAULT ''"),
            ("image_url", "ALTER TABLE recipe ADD COLUMN image_url TEXT DEFAULT ''"),
            ("prep_time", "ALTER TABLE recipe ADD COLUMN prep_time INTEGER NOT NULL DEFAULT 0"),
            ("cook_time", "ALTER TABLE recipe ADD COLUMN cook_time INTEGER NOT NULL DEFAULT 0"),
            ("total_time", "ALTER TABLE recipe ADD COLUMN total_time INTEGER NOT NULL DEFAULT 0"),
            ("category", "ALTER TABLE recipe ADD COLUMN category TEXT DEFAULT ''"),
            ("notes", "ALTER TABLE recipe ADD COLUMN notes TEXT DEFAULT ''"),
            ("is_favorite", "ALTER TABLE recipe ADD COLUMN is_favorite INTEGER NOT NULL DEFAULT 0"),
            ("rating", "ALTER TABLE recipe ADD COLUMN rating INTEGER NOT NULL DEFAULT 0"),
            ("nutrition", "ALTER TABLE recipe ADD COLUMN nutrition TEXT DEFAULT ''"),
            ("yields_text", "ALTER TABLE recipe ADD COLUMN yields_text TEXT DEFAULT ''"),
            ("cuisine", "ALTER TABLE recipe ADD COLUMN cuisine TEXT DEFAULT ''"),
            ("author", "ALTER TABLE recipe ADD COLUMN author TEXT DEFAULT ''"),
            ("source_rating", "ALTER TABLE recipe ADD COLUMN source_rating REAL NOT NULL DEFAULT 0"),
            ("keywords", "ALTER TABLE recipe ADD COLUMN keywords TEXT DEFAULT ''"),
        ]
        for col, ddl in migrations:
            if col not in existing:
                conn.execute(ddl)
        conn.commit()
        # Seed if empty.
        count = conn.execute("SELECT COUNT(*) FROM recipe").fetchone()[0]
        if count == 0:
            seed_recipes(conn)
            conn.commit()

        # Re-classify any ingredient or ad-hoc item still sitting in 'Other'
        # using the latest keyword rules. Skipped on subsequent boots when
        # the persisted classifier_version matches CLASSIFIER_VERSION, so
        # a 1000-row DB doesn't pay this cost on every container restart.
        stored_version_row = conn.execute(
            "SELECT value FROM app_meta WHERE key = 'classifier_version'"
        ).fetchone()
        stored_version = stored_version_row[0] if stored_version_row else ""
        if stored_version != CLASSIFIER_VERSION:
            for table in ("ingredient", "adhoc_item"):
                rows = conn.execute(
                    f"SELECT id, name FROM {table} WHERE category = 'Other'"
                ).fetchall()
                for row in rows:
                    guessed = guess_category(row[1])
                    if guessed != "Other":
                        conn.execute(
                            f"UPDATE {table} SET category = ? WHERE id = ?",
                            (guessed, row[0]),
                        )
            conn.execute(
                "INSERT OR REPLACE INTO app_meta (key, value) "
                "VALUES ('classifier_version', ?)",
                (CLASSIFIER_VERSION,),
            )
            conn.commit()

        # Best-effort: create the vec0 virtual table for semantic search.
        # The table itself must exist before any /recipes search runs, so
        # do that synchronously here — but the actual embedding backfill
        # (which has to load the ~22 MB MiniLM model and encode every
        # recipe — several seconds on a Pi) is handed off to a daemon
        # thread so it never blocks app startup. Skipped silently if
        # fastembed/sqlite-vec aren't installed — keyword search keeps
        # working in that case.
        try:
            import embedding as _embedding
        except ImportError:
            return
        if not _embedding.init_schema(conn):
            return
    threading.Thread(
        target=_backfill_embeddings, name="embedding-backfill", daemon=True
    ).start()


def _backfill_embeddings() -> None:
    """Encode any recipes that don't yet have a semantic embedding.

    Runs off the app-startup path in a daemon thread — the MiniLM model
    load alone is several seconds on a Pi 4. Best-effort: if
    fastembed/sqlite-vec aren't usable, search just stays keyword-only.
    Uses its own short-lived connection (WAL mode, set in init_db, lets
    this coexist with request traffic; busy_timeout covers write
    contention with a concurrent recipe save).
    """
    try:
        import embedding as _embedding
    except ImportError:
        return
    if not _embedding.is_available():
        return
    with closing(sqlite3.connect(DB_PATH)) as conn:
        _apply_connection_pragmas(conn)
        if not _embedding.setup_extension(conn):
            return
        conn.row_factory = sqlite3.Row
        try:
            existing_ids = {
                r[0] for r in conn.execute(
                    "SELECT recipe_id FROM recipe_embedding"
                ).fetchall()
            }
            all_ids = {
                r[0] for r in conn.execute("SELECT id FROM recipe").fetchall()
            }
        except sqlite3.OperationalError:
            # vec0 table not present on this build — nothing to backfill.
            return
        missing = sorted(all_ids - existing_ids)
        for rid in missing:
            row = conn.execute(
                "SELECT id, name, description, category, cuisine, keywords "
                "FROM recipe WHERE id = ?",
                (rid,),
            ).fetchone()
            if row is None:
                continue
            ings = conn.execute(
                "SELECT name FROM ingredient WHERE recipe_id = ?", (rid,)
            ).fetchall()
            text = _embedding.build_recipe_text(row, ings)
            _embedding.upsert_recipe_embedding(conn, rid, text)
        if missing:
            conn.commit()


def seed_recipes(conn: sqlite3.Connection) -> None:
    """Pre-populate a handful of family-friendly recipes so the app is
    immediately useful out of the box."""
    seeds = [
        {
            "name": "Spaghetti Bolognese",
            "description": "Classic weeknight pasta with meat sauce.",
            "servings": 4,
            "ingredients": [
                ("Spaghetti", 1, "lb", "Pantry"),
                ("Ground beef", 1, "lb", "Meat & Seafood"),
                ("Yellow onion", 1, "", "Produce"),
                ("Garlic", 3, "clove", "Produce"),
                ("Crushed tomatoes", 28, "oz", "Pantry"),
                ("Tomato paste", 2, "tbsp", "Pantry"),
                ("Olive oil", 2, "tbsp", "Pantry"),
                ("Parmesan cheese", 4, "oz", "Dairy & Eggs"),
                ("Salt", 1, "tsp", "Pantry"),
                ("Black pepper", 1, "tsp", "Pantry"),
            ],
        },
        {
            "name": "Taco Tuesday",
            "description": "Family taco night, hard or soft shells.",
            "servings": 4,
            "ingredients": [
                ("Ground beef", 1, "lb", "Meat & Seafood"),
                ("Taco seasoning", 1, "pkg", "Pantry"),
                ("Taco shells", 12, "", "Pantry"),
                ("Shredded cheddar", 8, "oz", "Dairy & Eggs"),
                ("Lettuce", 1, "head", "Produce"),
                ("Tomato", 2, "", "Produce"),
                ("Sour cream", 8, "oz", "Dairy & Eggs"),
                ("Salsa", 16, "oz", "Pantry"),
            ],
        },
        {
            "name": "Chicken Alfredo",
            "description": "Creamy fettuccine alfredo with grilled chicken.",
            "servings": 4,
            "ingredients": [
                ("Fettuccine", 1, "lb", "Pantry"),
                ("Chicken breast", 1.5, "lb", "Meat & Seafood"),
                ("Heavy cream", 2, "cup", "Dairy & Eggs"),
                ("Butter", 4, "tbsp", "Dairy & Eggs"),
                ("Parmesan cheese", 6, "oz", "Dairy & Eggs"),
                ("Garlic", 4, "clove", "Produce"),
                ("Olive oil", 2, "tbsp", "Pantry"),
                ("Salt", 1, "tsp", "Pantry"),
                ("Black pepper", 1, "tsp", "Pantry"),
                ("Parsley", 1, "bunch", "Produce"),
            ],
        },
        {
            "name": "Sheet Pan Fajitas",
            "description": "Easy oven-baked chicken fajitas.",
            "servings": 4,
            "ingredients": [
                ("Chicken breast", 1.5, "lb", "Meat & Seafood"),
                ("Bell pepper", 3, "", "Produce"),
                ("Yellow onion", 1, "", "Produce"),
                ("Flour tortillas", 8, "", "Bakery"),
                ("Fajita seasoning", 1, "pkg", "Pantry"),
                ("Olive oil", 2, "tbsp", "Pantry"),
                ("Lime", 2, "", "Produce"),
                ("Cilantro", 1, "bunch", "Produce"),
            ],
        },
        {
            "name": "Sunday Pancakes",
            "description": "Fluffy buttermilk pancakes for the whole family.",
            "servings": 4,
            "ingredients": [
                ("All-purpose flour", 2, "cup", "Pantry"),
                ("Sugar", 2, "tbsp", "Pantry"),
                ("Baking powder", 2, "tsp", "Pantry"),
                ("Salt", 0.5, "tsp", "Pantry"),
                ("Buttermilk", 2, "cup", "Dairy & Eggs"),
                ("Eggs", 2, "", "Dairy & Eggs"),
                ("Butter", 4, "tbsp", "Dairy & Eggs"),
                ("Maple syrup", 8, "oz", "Pantry"),
            ],
        },
        {
            "name": "Garden Salad",
            "description": "Fresh side salad.",
            "servings": 4,
            "ingredients": [
                ("Romaine lettuce", 1, "head", "Produce"),
                ("Cucumber", 1, "", "Produce"),
                ("Cherry tomatoes", 1, "pkg", "Produce"),
                ("Carrot", 2, "", "Produce"),
                ("Red onion", 0.5, "", "Produce"),
                ("Salad dressing", 1, "bottle", "Pantry"),
            ],
        },
        {
            "name": "BBQ Chicken Sandwiches",
            "description": "Pulled BBQ chicken on brioche buns.",
            "servings": 4,
            "ingredients": [
                ("Chicken breast", 2, "lb", "Meat & Seafood"),
                ("BBQ sauce", 18, "oz", "Pantry"),
                ("Brioche buns", 4, "", "Bakery"),
                ("Coleslaw mix", 14, "oz", "Produce"),
                ("Mayonnaise", 0.5, "cup", "Pantry"),
                ("Apple cider vinegar", 2, "tbsp", "Pantry"),
            ],
        },
    ]

    for r in seeds:
        cur = conn.execute(
            "INSERT INTO recipe (name, description, servings) VALUES (?, ?, ?)",
            (r["name"], r["description"], r["servings"]),
        )
        rid = cur.lastrowid
        for name, qty, unit, category in r["ingredients"]:
            conn.execute(
                "INSERT INTO ingredient (recipe_id, name, quantity, unit, category) "
                "VALUES (?, ?, ?, ?, ?)",
                (rid, name, qty, unit, category),
            )
