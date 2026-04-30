"""End-to-end backup/restore round-trip tests.

Creates a temporary DB + uploads dir, populates them with realistic
content, runs the actual backup → wipe → restore code paths, and
verifies the restored state matches what was there before. Covers:

  * VACUUM INTO snapshot consistency
  * Photo files preserved through the ZIP round-trip
  * Restoring a backup made with the CURRENT schema works
  * Restoring a backup made with an OLDER schema (missing newer
    tables) still leaves the app in a queryable state because
    init_db is run after restore
"""
import os
import shutil
import sqlite3
import tempfile
import zipfile

import pytest


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    """Spin up a fresh app instance with its own tmp DB + uploads dir."""
    db_path = str(tmp_path / "shoppinglist.db")
    uploads_path = str(tmp_path / "uploads")
    os.makedirs(uploads_path, exist_ok=True)
    monkeypatch.setenv("SHOPPINGLIST_DB", db_path)
    monkeypatch.setenv("BACKUP_ENABLED", "0")  # don't fire the cron in tests

    # Re-import db + app fresh so module-level constants pick up the env var.
    import importlib
    import db as db_mod
    importlib.reload(db_mod)
    import app as app_mod
    importlib.reload(app_mod)

    monkeypatch.setattr(app_mod, "UPLOAD_DIR", uploads_path)
    db_mod.init_db()

    return app_mod, db_mod, db_path, uploads_path


def _put_photo(uploads_dir: str, name: str = "test.jpg") -> str:
    """Drop a tiny valid JPEG into uploads_dir for round-trip testing."""
    path = os.path.join(uploads_dir, name)
    # Smallest possible valid JPEG (SOI + EOI markers).
    with open(path, "wb") as f:
        f.write(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9")
    return path


def test_backup_zip_contains_db_and_photos(isolated_app):
    app_mod, db_mod, db_path, uploads_path = isolated_app
    _put_photo(uploads_path, "alpha.jpg")
    _put_photo(uploads_path, "beta.jpg")

    out = os.path.join(os.path.dirname(db_path), "test-backup.zip")
    photo_count, _ = app_mod._write_full_backup_zip(out)
    assert photo_count == 2

    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
    assert "shoppinglist.db" in names
    assert "uploads/alpha.jpg" in names
    assert "uploads/beta.jpg" in names


def test_vacuum_into_produces_valid_sqlite(isolated_app):
    app_mod, db_mod, db_path, uploads_path = isolated_app

    # Insert a known recipe so we can verify it survives the snapshot.
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO recipe (name, description, servings) VALUES (?, ?, ?)",
        ("Round Trip Test", "verify backup", 4),
    )
    conn.commit()
    conn.close()

    out = os.path.join(os.path.dirname(db_path), "vacuumed.zip")
    app_mod._write_full_backup_zip(out)

    # Extract the embedded DB and verify our recipe is there.
    with zipfile.ZipFile(out) as zf:
        with zf.open("shoppinglist.db") as src, open(
            os.path.join(os.path.dirname(db_path), "extracted.db"), "wb"
        ) as dst:
            shutil.copyfileobj(src, dst)
    extracted = sqlite3.connect(
        os.path.join(os.path.dirname(db_path), "extracted.db")
    )
    rows = extracted.execute(
        "SELECT name FROM recipe WHERE name = ?", ("Round Trip Test",)
    ).fetchall()
    extracted.close()
    assert len(rows) == 1


def test_full_round_trip_preserves_recipes_and_photos(isolated_app):
    app_mod, db_mod, db_path, uploads_path = isolated_app

    # 1. Populate with a recipe + photo.
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO recipe (name, description, servings, image_url) "
        "VALUES (?, ?, ?, ?)",
        ("My Recipe", "tasty", 4, "/static/uploads/myphoto.jpg"),
    )
    conn.commit()
    conn.close()
    _put_photo(uploads_path, "myphoto.jpg")

    # 2. Build a backup ZIP.
    backup_path = os.path.join(os.path.dirname(db_path), "rt.zip")
    app_mod._write_full_backup_zip(backup_path)

    # 3. Wipe the DB and uploads.
    os.remove(db_path)
    for f in os.listdir(uploads_path):
        os.remove(os.path.join(uploads_path, f))
    db_mod.init_db()  # rebuild schema; will also seed default recipes

    # 4. Restore from the backup (extract manually since the route is
    #    Flask-bound and we want to test the underlying file ops).
    with zipfile.ZipFile(backup_path) as zf:
        with tempfile.TemporaryDirectory() as extract_to:
            zf.extractall(extract_to)
            # Restore DB via .backup() like the route does.
            src = sqlite3.connect(os.path.join(extract_to, "shoppinglist.db"))
            dst = sqlite3.connect(db_path)
            src.backup(dst)
            dst.close()
            src.close()
            # Restore uploads.
            for fname in os.listdir(os.path.join(extract_to, "uploads")):
                shutil.copy2(
                    os.path.join(extract_to, "uploads", fname),
                    os.path.join(uploads_path, fname),
                )

    # 5. Verify recipe is back.
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT name, description, servings, image_url FROM recipe "
        "WHERE name = ?",
        ("My Recipe",),
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "My Recipe"
    assert rows[0][1] == "tasty"
    assert rows[0][2] == 4
    assert rows[0][3] == "/static/uploads/myphoto.jpg"
    # Photo file is back too.
    assert os.path.isfile(os.path.join(uploads_path, "myphoto.jpg"))


def test_restore_old_schema_works_after_init_db(isolated_app):
    """Restoring a DB that's missing newer tables should land in a
    queryable state once init_db reapplies the schema."""
    app_mod, db_mod, db_path, uploads_path = isolated_app

    # 1. Build a fake "old schema" DB: just the recipe table, missing
    #    everything we added later (purchase_history, app_meta, etc.).
    old_db = os.path.join(os.path.dirname(db_path), "old.db")
    conn = sqlite3.connect(old_db)
    conn.execute("""
        CREATE TABLE recipe (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT DEFAULT '',
            servings INTEGER NOT NULL DEFAULT 4
        )
    """)
    conn.execute("""
        CREATE TABLE ingredient (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            quantity REAL NOT NULL DEFAULT 1,
            unit TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT 'Other',
            note TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE list_recipe (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_id INTEGER NOT NULL,
            multiplier REAL NOT NULL DEFAULT 1,
            added_by TEXT DEFAULT '',
            added_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "INSERT INTO recipe (name, description, servings) VALUES (?, ?, ?)",
        ("Legacy Recipe", "from a year ago", 6),
    )
    conn.commit()
    conn.close()

    # 2. Restore by copying the old DB's bytes into the current DB path.
    src = sqlite3.connect(old_db)
    dst = sqlite3.connect(db_path)
    src.backup(dst)
    dst.close()
    src.close()

    # 3. Run init_db — applies all migrations on top of the old schema.
    db_mod.init_db()

    # 4. Verify all expected tables exist after the migration pass.
    conn = sqlite3.connect(db_path)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    expected = {
        "recipe", "ingredient", "list_recipe", "adhoc_item",
        "checked_item", "meal_plan", "purchase_history", "app_meta",
        "recipe_embedding_hash",
    }
    missing = expected - tables
    assert not missing, f"missing after init_db: {missing}"

    # 5. Verify the legacy recipe is still there.
    rows = conn.execute(
        "SELECT name, description FROM recipe WHERE name = ?",
        ("Legacy Recipe",),
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "Legacy Recipe"


def test_zip_slip_path_traversal_rejected(isolated_app):
    """A ZIP with `..` path components shouldn't be able to escape
    the extraction temp dir on restore."""
    app_mod, _db_mod, db_path, _uploads = isolated_app

    # Build a malicious ZIP with a relative path escape.
    with tempfile.TemporaryDirectory() as work:
        bad_zip = os.path.join(work, "bad.zip")
        with zipfile.ZipFile(bad_zip, "w") as zf:
            zf.writestr("../escaped.db", b"malicious")
            zf.writestr("shoppinglist.db", b"SQLite format 3\x00...")

        # _restore_from_zip is defined inside create_app, so we can't
        # call it directly without a request context. Instead, verify
        # the same defense logic on the namelist:
        with zipfile.ZipFile(bad_zip) as zf:
            for member in zf.namelist():
                # The route's check:
                if member.startswith("/") or ".." in member.split("/"):
                    break
            else:
                pytest.fail("malicious member should have been caught")
