"""Family Shopping List - Flask web app for picking recipes and generating
a consolidated shopping list with ad-hoc additions."""
from __future__ import annotations

import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import queue
import threading

from flask import (
    Flask,
    Response,
    flash,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    url_for,
)

from db import DB_PATH, close_db, get_db, init_db
from ingredient import (
    CATEGORIES,
    UNIT_ALIASES,
    format_quantity,
    from_canonical,
    guess_category,
    normalize_name,
    normalize_unit,
    parse_ingredient,
    to_canonical_qty,
)
from ocr import _ocr_image_to_text, _parse_ocr_recipe

APP_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(APP_DIR, "static", "uploads")
ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic"}
MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # 8 MB
# Defensive caps so a fat-fingered "999999" doesn't aggregate into the
# shopping list as 999,999 cups of flour. 100 batches is a generous
# upper bound for any sane family use case.
MAX_MULTIPLIER = 100
MAX_QUANTITY = 1000

RECIPE_CATEGORIES = [
    "Breakfast",
    "Lunch",
    "Dinner",
    "Dessert",
    "Snack",
    "Side",
    "Appetizer",
    "Soup",
    "Salad",
    "Drink",
    "Other",
]


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


@dataclass
class AggregatedItem:
    key: str
    name: str
    quantity: float
    unit: str
    category: str
    sources: list[str]
    note: str = ""
    is_adhoc: bool = False
    adhoc_id: int | None = None
    checked: bool = False

    @property
    def display_quantity(self) -> str:
        return format_quantity(self.quantity)


def build_shopping_list(db: sqlite3.Connection) -> dict[str, list[AggregatedItem]]:
    """Walk the active list_recipe rows, aggregate ingredients, then
    append ad-hoc items.

    Aggregation merges by (normalized name, dimension) when the unit is
    known to belong to a physical dimension (volume/mass), so that
    "1 cup butter" + "8 tbsp butter" become one line. Unknown units
    (sticks, cans, cloves, no-unit) fall back to exact (name, unit)
    matching.
    """
    list_rows = db.execute(
        "SELECT lr.id AS lr_id, lr.multiplier, lr.added_by, "
        "       r.id AS recipe_id, r.name AS recipe_name "
        "FROM list_recipe lr JOIN recipe r ON r.id = lr.recipe_id "
        "ORDER BY r.name"
    ).fetchall()

    # key -> AggregatedItem
    bucket: dict[str, AggregatedItem] = {}
    # For dim-keyed buckets, track the running sum in canonical base
    # units (mL for volume, g for mass) so we can re-derive a sensible
    # display unit at the end after all merging is done.
    canonical_sums: dict[str, list] = {}  # key -> [dimension, base_total]

    for lr in list_rows:
        ings = db.execute(
            "SELECT name, quantity, unit, category, note "
            "FROM ingredient WHERE recipe_id = ?",
            (lr["recipe_id"],),
        ).fetchall()
        for ing in ings:
            n_name = normalize_name(ing["name"])
            n_unit = normalize_unit(ing["unit"])
            qty = float(ing["quantity"]) * float(lr["multiplier"])
            canonical = to_canonical_qty(qty, n_unit)
            if canonical is not None:
                dimension, base_qty = canonical
                key = f"recipe::{n_name}::dim:{dimension}"
            else:
                key = f"recipe::{n_name}::{n_unit}"
            source_label = lr["recipe_name"]
            if lr["multiplier"] != 1:
                source_label += f" (×{format_quantity(lr['multiplier'])})"
            if key in bucket:
                item = bucket[key]
                if canonical is not None:
                    canonical_sums[key][1] += base_qty
                else:
                    item.quantity += qty
                if source_label not in item.sources:
                    item.sources.append(source_label)
            else:
                bucket[key] = AggregatedItem(
                    key=key,
                    name=ing["name"].strip().title(),
                    quantity=qty,
                    unit=ing["unit"] or "",
                    category=ing["category"] or "Other",
                    sources=[source_label],
                    note=ing["note"] or "",
                )
                if canonical is not None:
                    canonical_sums[key] = [canonical[0], canonical[1]]

    # Re-derive display quantity + unit for dim-keyed buckets now that
    # all merging is done.
    for key, (dimension, base_total) in canonical_sums.items():
        item = bucket[key]
        new_qty, new_unit = from_canonical(base_total, dimension)
        item.quantity = new_qty
        item.unit = new_unit

    # Ad-hoc items: each is its own row even if it duplicates a recipe item,
    # so the requester gets the exact thing they asked for.
    adhocs = db.execute(
        "SELECT id, name, quantity, unit, category, note, added_by "
        "FROM adhoc_item ORDER BY id"
    ).fetchall()
    for a in adhocs:
        key = f"adhoc::{a['id']}"
        label = "Ad-hoc"
        if a["added_by"]:
            label = f"Ad-hoc — {a['added_by']}"
        bucket[key] = AggregatedItem(
            key=key,
            name=a["name"].strip().title(),
            quantity=float(a["quantity"]),
            unit=a["unit"] or "",
            category=a["category"] or "Other",
            sources=[label],
            note=a["note"] or "",
            is_adhoc=True,
            adhoc_id=a["id"],
        )

    # Apply checked state.
    checked_keys = {
        row["key"] for row in db.execute("SELECT key FROM checked_item").fetchall()
    }
    for item in bucket.values():
        item.checked = item.key in checked_keys

    # Group by category, preserving the configured order.
    grouped: dict[str, list[AggregatedItem]] = defaultdict(list)
    for item in bucket.values():
        grouped[item.category].append(item)
    for items in grouped.values():
        items.sort(key=lambda i: (i.checked, i.name))

    ordered: dict[str, list[AggregatedItem]] = {}
    for cat in CATEGORIES:
        if cat in grouped:
            ordered[cat] = grouped[cat]
    # Any unknown categories (legacy data) go at the end.
    for cat, items in grouped.items():
        if cat not in ordered:
            ordered[cat] = items
    return ordered


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------


_NOTES_HEADING_RE = re.compile(
    r"^\s*(recipe\s+)?(notes?|tips?|cook'?s\s+notes?|chef'?s\s+notes?)\s*:?\s*$",
    re.I,
)


# Match common recipe-plugin class names: recipe-notes, tasty-recipes-notes-body,
# wprm-recipe-notes, mv-recipe-notes, notes-section, cooks-notes, tips-section, etc.
_NOTES_CLASS_RE = re.compile(r"(?:^|[-_ ])(notes?|tips?)(?:$|[-_ ])", re.I)


def _scrape_notes_section(soup) -> str:
    """Best-effort: walk the recipe page's HTML for a 'Notes' / 'Recipe
    Notes' / 'Tips' section and return its text. Used as a fallback when
    recipe-scrapers' scraper doesn't expose a notes() method."""
    if soup is None:
        return ""

    # 1) Heading-based: <h2>Notes</h2> followed by <p>...</p> blocks.
    headings = soup.find_all(["h2", "h3", "h4", "h5", "strong", "b"])
    for h in headings:
        text = (h.get_text() or "").strip()
        if not _NOTES_HEADING_RE.match(text):
            continue
        parts: list[str] = []
        for sib in h.find_all_next():
            if sib.name in ("h1", "h2", "h3", "h4", "h5") and sib is not h:
                break
            if sib.name == "li":
                t = sib.get_text(" ", strip=True)
                if t:
                    parts.append(f"• {t}")
            elif sib.name == "p":
                t = sib.get_text(" ", strip=True)
                if t:
                    parts.append(t)
            if len(parts) >= 20:
                break
        cleaned = "\n".join(parts).strip()
        if cleaned:
            return cleaned

    # 2) Class-based: <div class="recipe-notes">…</div> as many blogs do.
    for el in soup.find_all(
        ["div", "section", "aside"], class_=_NOTES_CLASS_RE
    ):
        text = el.get_text("\n", strip=True)
        if text and 5 < len(text) < 5000:
            # Compact runs of blank lines.
            return re.sub(r"\n{2,}", "\n", text).strip()

    return ""


def _is_valid_kasa_db(path: str) -> bool:
    """Quick smoke-test that `path` looks like a Kasa SQLite DB."""
    try:
        conn = sqlite3.connect(path)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
    except sqlite3.DatabaseError:
        return False
    return {"recipe", "ingredient", "list_recipe"}.issubset(tables)


def _write_full_backup_zip(zip_path: str) -> tuple[int, int]:
    """Snapshot DB + uploaded photos into a ZIP at `zip_path`.

    Returns (photo_count, total_bytes). Raises on failure.

    The DB is captured via `VACUUM INTO` rather than `Connection.backup()`
    so the snapshot is both consistent AND defragmented. For a database
    that's seen lots of UPDATEs/DELETEs this typically yields a backup
    that's 20–30 % smaller than a raw-page copy. VACUUM INTO requires
    that the destination file does not exist, so we remove the empty
    file mkstemp creates before invoking it.
    """
    fd, tmp_db = tempfile.mkstemp(suffix=".db", prefix="kasa-snap-")
    os.close(fd)
    try:
        os.remove(tmp_db)
    except OSError:
        pass
    photo_count = 0
    total_bytes = 0
    try:
        src = sqlite3.connect(DB_PATH)
        try:
            src.execute("VACUUM INTO ?", (tmp_db,))
        finally:
            src.close()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(tmp_db, arcname="shoppinglist.db")
            if os.path.isdir(UPLOAD_DIR):
                for fname in os.listdir(UPLOAD_DIR):
                    full = os.path.join(UPLOAD_DIR, fname)
                    if os.path.isfile(full):
                        zf.write(full, arcname=f"uploads/{fname}")
                        photo_count += 1
                        total_bytes += os.path.getsize(full)
    finally:
        try:
            os.remove(tmp_db)
        except OSError:
            pass
    return photo_count, total_bytes


def _week_start(d: date) -> date:
    """Return the Sunday on or before `d`. Weeks run Sun → Sat."""
    # weekday(): Mon=0…Sun=6. Days back to Sunday = (weekday + 1) % 7.
    return d - timedelta(days=(d.weekday() + 1) % 7)


def _parse_iso_date(s: str | None) -> date:
    """Parse YYYY-MM-DD; fall back to today on bad input."""
    if not s:
        return date.today()
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return date.today()


def _parse_multiplier_field(
    raw: str | None,
    *,
    allow_float: bool = True,
    floor: float = 1.0,
    cap: float = 100.0,
    default: float = 1.0,
) -> float:
    """Parse a multiplier-style numeric field from a form.

    Used by /list/add-recipe, /list/add-adhoc, and /plan/add — all of
    which historically had their own subtly different parse-and-clamp
    logic (some int-coerced, some kept floats, some had different
    floors). One helper, three call sites.
    """
    s = (raw or "").strip() or str(default)
    try:
        value = float(s) if allow_float else float(int(float(s)))
    except (TypeError, ValueError):
        return default
    return min(cap, max(floor, value))


def _int_field(form, name: str, default: int = 0) -> int:
    """Parse a non-negative integer from a Flask `request.form`-like
    mapping. Returns `default` on bad input. Replaces the inline nested
    helper that used to live inside `_save_recipe`."""
    raw = (form.get(name, str(default)) or str(default)).strip() or str(default)
    try:
        return max(0, int(float(raw)))
    except (TypeError, ValueError):
        return default


# Column list used by both /recipes/<id> and /recipes/<id>/edit. Defining
# it once means a future schema addition only needs to be added here.
_RECIPE_FETCH_COLUMNS = (
    "id, name, description, servings, instructions, source_url, "
    "image_url, prep_time, cook_time, total_time, category, notes, "
    "is_favorite, rating, nutrition, yields_text, cuisine, author, "
    "source_rating, keywords"
)


def _get_recipe(recipe_id: int):
    """Fetch a recipe row by ID, or None. Used by the view and edit
    routes — defines the column list once so schema additions don't
    have to be made in two places."""
    return get_db().execute(
        f"SELECT {_RECIPE_FETCH_COLUMNS} FROM recipe WHERE id = ?",
        (recipe_id,),
    ).fetchone()


# ---------------------------------------------------------------------------
# Server-Sent Events: real-time list sync across family devices
# ---------------------------------------------------------------------------
#
# In-memory pub/sub. Each connected client owns a Queue; mutating routes
# call _broadcast(...) which fans out to every queue.
#
# Caveat: subscribers are per-process. Run gunicorn with a single worker
# (threaded) so all clients share one subscriber pool — see Dockerfile.
# Multi-worker still functionally works (cross-worker events just won't
# arrive), so degradation is graceful.

_sse_subscribers: "set[queue.Queue]" = set()
_sse_lock = threading.Lock()

# Serializes the DELETE+INSERT pair inside _save_recipe so concurrent
# edits from two family devices can't interleave and lose one user's
# ingredient inserts. With gunicorn 1 worker / 8 threads, an in-process
# lock is sufficient. Family-scale write contention is rare; the lock
# is held for ~50-200 ms per save.
_recipe_save_lock = threading.Lock()


def _broadcast(event_type: str, data: str = "1") -> None:
    """Push an event to every subscriber. Best-effort — full queues are
    skipped rather than blocked on, because a slow client must not stall
    a write request."""
    with _sse_lock:
        subscribers = list(_sse_subscribers)
    for q in subscribers:
        try:
            q.put_nowait((event_type, data))
        except queue.Full:
            pass


def _top_predicted_items(db, limit: int = 8) -> list[dict]:
    """Return up to `limit` items the user is likely to want to add next.

    Combines four signals over the user's purchase history:
      * **recency**       – how long since the item was last added
      * **frequency**     – how often it's been added
      * **co-occurrence** – how often it's been added in the same trip
                            as something already in the cart right now
      * **replenishment** – proximity to the user's typical
                            inter-purchase interval (a bell curve around
                            the average gap between past purchases)

    Items already on the active list (recipe-derived ingredients OR
    ad-hoc adds) are skipped so we don't suggest duplicates.

    Pure SQL + Python, no ML. The "feels magical" property comes from
    surfacing patterns the family already has rather than predicting
    new behavior.
    """
    # What's already on the list — exclude these from suggestions.
    on_list: set[str] = set()
    for row in db.execute("SELECT name FROM adhoc_item").fetchall():
        on_list.add(row["name"].strip().lower())
    for row in db.execute(
        "SELECT i.name FROM ingredient i "
        "INNER JOIN list_recipe lr ON lr.recipe_id = i.recipe_id"
    ).fetchall():
        on_list.add(row["name"].strip().lower())

    rows = db.execute(
        "SELECT name, unit, COUNT(*) AS freq, "
        "       MAX(checked_at) AS last_at "
        "FROM purchase_history "
        "WHERE checked_at >= datetime('now', '-90 days') "
        "GROUP BY LOWER(name), unit "
        "ORDER BY freq DESC, last_at DESC "
        "LIMIT 50"
    ).fetchall()
    if not rows:
        return []

    # Co-occurrence: pairs of items added within a 24-hour window of
    # each other. Only keep pairs that have appeared together at least
    # twice — once is noise, twice is a pattern.
    cooccur: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    cooccur_rows = db.execute(
        """
        SELECT LOWER(a.name) AS a_name,
               LOWER(b.name) AS b_name,
               COUNT(*) AS cnt
        FROM purchase_history a
        JOIN purchase_history b
          ON a.id < b.id
         AND ABS(julianday(a.checked_at) - julianday(b.checked_at)) <= 1.0
         AND LOWER(a.name) <> LOWER(b.name)
        WHERE a.checked_at >= datetime('now', '-180 days')
        GROUP BY LOWER(a.name), LOWER(b.name)
        HAVING cnt >= 2
        ORDER BY cnt DESC
        LIMIT 200
        """
    ).fetchall()
    for cr in cooccur_rows:
        cooccur[cr["a_name"]][cr["b_name"]] = cr["cnt"]
        cooccur[cr["b_name"]][cr["a_name"]] = cr["cnt"]

    # Replenishment cycle: average gap between purchases of the same
    # item, plus the most recent purchase date. Requires ≥2 purchases
    # to even compute a gap.
    cycle: dict[str, tuple[float, str]] = {}
    cycle_rows = db.execute(
        """
        WITH recent AS (
            SELECT LOWER(name) AS name,
                   checked_at,
                   LAG(checked_at) OVER (
                       PARTITION BY LOWER(name) ORDER BY checked_at
                   ) AS prev_at
            FROM purchase_history
            WHERE checked_at >= datetime('now', '-365 days')
        )
        SELECT name,
               AVG(julianday(checked_at) - julianday(prev_at)) AS avg_gap,
               MAX(checked_at) AS last_at,
               COUNT(*) AS gaps
        FROM recent
        WHERE prev_at IS NOT NULL
        GROUP BY name
        HAVING gaps >= 1
        """
    ).fetchall()
    for cr in cycle_rows:
        cycle[cr["name"]] = (float(cr["avg_gap"] or 0.0), cr["last_at"])

    now = datetime.utcnow()
    scored: list[tuple[float, dict]] = []
    for r in rows:
        name_lower = r["name"].strip().lower()
        if name_lower in on_list:
            continue
        try:
            last = datetime.fromisoformat(r["last_at"])
        except (TypeError, ValueError):
            continue
        days_ago = max(1, (now - last).days)

        # Component scores in [0, 1].
        recency_score = min(1.0, 1.0 / days_ago)
        frequency_score = min(1.0, r["freq"] / 8.0)

        cooccur_score = 0.0
        partners = cooccur.get(name_lower, {})
        if partners and on_list:
            raw = sum(partners.get(c, 0) for c in on_list)
            cooccur_score = min(1.0, raw / 5.0)

        replenish_score = 0.0
        cyc = cycle.get(name_lower)
        if cyc:
            avg_gap, last_iso = cyc
            if avg_gap > 0:
                try:
                    last_purchase = datetime.fromisoformat(last_iso)
                    days_since = (now - last_purchase).days
                    # Bell-curve-ish around expected interval. Peaks at
                    # ratio = 1 (item is "due"), tails to 0 outside the
                    # 0.5x-2x window.
                    ratio = days_since / avg_gap
                    if 0.5 <= ratio <= 2.0:
                        replenish_score = max(0.0, 1.0 - abs(ratio - 1.0))
                except (TypeError, ValueError):
                    pass

        score = (
            0.30 * recency_score
            + 0.20 * frequency_score
            + 0.30 * cooccur_score
            + 0.20 * replenish_score
        )
        scored.append((score, {
            "name": r["name"],
            "unit": r["unit"],
            "freq": r["freq"],
        }))
    scored.sort(key=lambda x: -x[0])
    return [item for _, item in scored[:limit]]


def _refresh_recipe_embedding(db, recipe_id: int) -> None:
    """Recompute and upsert the semantic embedding for a recipe.

    Best-effort — silently skipped if fastembed/sqlite-vec aren't
    installed or aren't usable on this build. Called from the recipe
    save handler and the URL/photo import flows. The embedding module
    handles input-text hash deduplication internally so editing only
    the rating doesn't burn Pi 4 CPU on every save.
    """
    try:
        import embedding as _embedding
    except ImportError:
        return
    if not _embedding.is_available():
        return
    row = db.execute(
        "SELECT id, name, description, category, cuisine, keywords "
        "FROM recipe WHERE id = ?",
        (recipe_id,),
    ).fetchone()
    if row is None:
        return
    ings = db.execute(
        "SELECT name FROM ingredient WHERE recipe_id = ?", (recipe_id,)
    ).fetchall()
    text = _embedding.build_recipe_text(row, ings)
    _embedding.upsert_recipe_embedding(db, recipe_id, text)


def _delete_uploaded_image_file(
    url: str, except_recipe_id: int | None = None
) -> bool:
    """Remove an uploaded image file from disk if no other recipe needs it.

    Returns True if a file was actually deleted. Skipped silently when:
      - `url` doesn't point at /static/uploads/ (external URLs are left alone)
      - the file's already gone
      - another recipe (other than `except_recipe_id`) still references it

    Path-traversal hardened: rejects any url containing `..` / `/` / `\\`
    in the basename and verifies the resolved path lives under UPLOAD_DIR.
    """
    if not url or not url.startswith("/static/uploads/"):
        return False
    basename = url[len("/static/uploads/"):]
    if not basename or "/" in basename or "\\" in basename or ".." in basename:
        return False
    full = os.path.join(UPLOAD_DIR, basename)
    try:
        real_full = os.path.realpath(full)
        real_dir = os.path.realpath(UPLOAD_DIR)
        if os.path.commonpath([real_full, real_dir]) != real_dir:
            return False
    except (OSError, ValueError):
        return False
    if not os.path.isfile(full):
        return False
    db = get_db()
    sql = "SELECT COUNT(*) FROM recipe WHERE image_url = ?"
    params: list = [url]
    if except_recipe_id is not None:
        sql += " AND id != ?"
        params.append(except_recipe_id)
    if db.execute(sql, params).fetchone()[0] > 0:
        return False
    try:
        os.remove(full)
        return True
    except OSError:
        return False


def _auto_backup_dir() -> str:
    """Where automatic nightly backups land. Override via BACKUP_DIR
    env var — typically a mounted network share so backups land
    off-Pi without an extra cron."""
    return os.environ.get(
        "BACKUP_DIR", os.path.join(os.path.dirname(DB_PATH), "backups")
    )


def _scheduled_auto_backup() -> None:
    """Background-scheduler entrypoint: write a full backup ZIP to
    BACKUP_DIR and prune old auto-backups beyond BACKUP_RETAIN_COUNT.
    Errors are swallowed so the scheduler thread keeps running.
    """
    try:
        backup_dir = _auto_backup_dir()
        os.makedirs(backup_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = os.path.join(backup_dir, f"auto-backup-{ts}.zip")
        _write_full_backup_zip(out_path)
        try:
            keep = max(1, int(os.environ.get("BACKUP_RETAIN_COUNT", "14")))
        except ValueError:
            keep = 14
        _rotate_auto_backups(backup_dir, keep=keep)
    except Exception as exc:
        # Log to stdout — gunicorn's access log captures this.
        print(f"[auto-backup] scheduled backup failed: {exc}", flush=True)


def _rotate_auto_backups(backup_dir: str, keep: int = 14) -> None:
    """Keep at most `keep` newest auto-backup-*.zip files in `backup_dir`."""
    if not os.path.isdir(backup_dir):
        return
    snapshots: list[tuple[float, str]] = []
    for fname in os.listdir(backup_dir):
        if not (fname.startswith("auto-backup-") and fname.endswith(".zip")):
            continue
        full = os.path.join(backup_dir, fname)
        try:
            snapshots.append((os.path.getmtime(full), full))
        except OSError:
            pass
    snapshots.sort(reverse=True)
    for _, full in snapshots[keep:]:
        try:
            os.remove(full)
        except OSError:
            pass


def _list_auto_backups() -> list[dict]:
    """Return metadata about every auto-backup file currently on disk
    (newest first), for display on the /backup page."""
    backup_dir = _auto_backup_dir()
    if not os.path.isdir(backup_dir):
        return []
    out = []
    for fname in os.listdir(backup_dir):
        if not (fname.startswith("auto-backup-") and fname.endswith(".zip")):
            continue
        full = os.path.join(backup_dir, fname)
        try:
            out.append({
                "name": fname,
                "size": os.path.getsize(full),
                "mtime": datetime.fromtimestamp(
                    os.path.getmtime(full)
                ).strftime("%Y-%m-%d %H:%M"),
            })
        except OSError:
            pass
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def _start_auto_backup_scheduler() -> None:
    """Wire up APScheduler for the nightly backup job. Best-effort —
    if APScheduler isn't installed (e.g., on a slimmed-down deploy)
    or BACKUP_ENABLED is "0", the app continues without auto-backups."""
    if os.environ.get("BACKUP_ENABLED", "1") == "0":
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        return
    try:
        hour = int(os.environ.get("BACKUP_HOUR", "3"))
        minute = int(os.environ.get("BACKUP_MINUTE", "0"))
    except ValueError:
        hour, minute = 3, 0
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        _scheduled_auto_backup,
        CronTrigger(hour=hour, minute=minute),
        id="kasa_auto_backup",
        replace_existing=True,
        misfire_grace_time=3600,  # if the Pi was off, run within an hour
    )
    scheduler.start()


def _rotate_pre_restore_snapshots(keep: int = 5) -> None:
    """Keep at most `keep` most-recent pre-restore-*.{db,zip} snapshots
    next to the DB. Older ones are removed so /data doesn't grow forever.
    """
    db_dir = os.path.dirname(DB_PATH)
    if not os.path.isdir(db_dir):
        return
    snapshots: list[tuple[float, str]] = []
    for fname in os.listdir(db_dir):
        if not fname.startswith("pre-restore-"):
            continue
        if not (fname.endswith(".db") or fname.endswith(".zip")):
            continue
        full = os.path.join(db_dir, fname)
        try:
            snapshots.append((os.path.getmtime(full), full))
        except OSError:
            pass
    # Newest first; keep the head, delete the rest.
    snapshots.sort(reverse=True)
    for _, full in snapshots[keep:]:
        try:
            os.remove(full)
        except OSError:
            pass


def _count_remote_image_recipes(db) -> int:
    """How many recipes have an image_url that points OFF the Pi.

    Recipes imported before Stage 9 (image-baking) still have remote
    URLs and their photos won't appear in any /backup ZIP because we
    only include files under /static/uploads/. Surfacing the count
    on the backup page lets the user one-click "rebake" them.
    """
    row = db.execute(
        "SELECT COUNT(*) FROM recipe "
        "WHERE image_url != '' "
        "AND (image_url LIKE 'http://%' OR image_url LIKE 'https://%')"
    ).fetchone()
    return row[0] if row else 0


def _rebake_remote_recipe_images(db) -> tuple[int, int]:
    """Fetch every remote-URL recipe image and replace it with a local
    /static/uploads/ path. Returns (succeeded, failed). Best-effort —
    failures (network, image format mismatch, host blocking) leave the
    recipe's original remote URL in place.
    """
    rows = db.execute(
        "SELECT id, image_url FROM recipe "
        "WHERE image_url LIKE 'http://%' OR image_url LIKE 'https://%'"
    ).fetchall()
    succeeded = 0
    failed = 0
    for row in rows:
        local = _fetch_remote_image(row["image_url"])
        if local:
            db.execute(
                "UPDATE recipe SET image_url = ? WHERE id = ?",
                (local, row["id"]),
            )
            succeeded += 1
        else:
            failed += 1
    if succeeded:
        db.commit()
    return succeeded, failed


def _scan_orphan_images() -> list[str]:
    """Return basenames in UPLOAD_DIR that no recipe.image_url references."""
    if not os.path.isdir(UPLOAD_DIR):
        return []
    db = get_db()
    referenced: set[str] = set()
    for row in db.execute(
        "SELECT image_url FROM recipe WHERE image_url != ''"
    ).fetchall():
        url = row["image_url"]
        if url and url.startswith("/static/uploads/"):
            referenced.add(url[len("/static/uploads/"):])
    on_disk: set[str] = set()
    for fname in os.listdir(UPLOAD_DIR):
        if os.path.isfile(os.path.join(UPLOAD_DIR, fname)):
            on_disk.add(fname)
    return sorted(on_disk - referenced)


# A current desktop-Chrome UA. Used for URL imports where the previous
# `Mozilla/5.0 (compatible; FamilyShoppingList/1.0)` triggered bot-flag
# rules on a few recipe blogs (Wordfence, Cloudflare's default WAF).
# Bump roughly once a year. Don't include "bot" / "scraper" tokens.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/132.0.0.0 Safari/537.36"
)


def _http_get(
    url: str,
    timeout: float = 15.0,
    retries: int = 3,
    extra_headers: dict | None = None,
) -> "tuple[bytes, dict] | None":
    """Polite GET with retry-on-transient-failure. Returns (body, headers)
    or None on terminal failure. Backoff: 1.5s, 3.0s, 6.0s. Honors
    Retry-After when the server sends 429.

    No external dependency — built on stdlib `urllib`. Used for both the
    URL-import HTML fetch (when recipe-scrapers' wild_mode is needed)
    and the image-baker for hotlinked recipe photos.
    """
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen
    import time

    headers = {
        "User-Agent": _BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,image/png,image/*;q=0.8,*/*;q=0.5",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",  # let urlopen handle decoding
    }
    if extra_headers:
        headers.update(extra_headers)

    backoff = 1.5
    for attempt in range(max(1, retries)):
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=timeout) as resp:
                return resp.read(), dict(resp.headers)
        except HTTPError as exc:
            # 4xx other than 429 are terminal — no retry.
            if exc.code in (429, 500, 502, 503, 504):
                if attempt + 1 < retries:
                    wait = backoff * (2 ** attempt)
                    # Honor Retry-After if present and reasonable.
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    if retry_after:
                        try:
                            wait = max(wait, min(30.0, float(retry_after)))
                        except ValueError:
                            pass
                    time.sleep(min(30.0, wait))
                    continue
            return None
        except (URLError, TimeoutError, OSError):
            if attempt + 1 < retries:
                time.sleep(backoff * (2 ** attempt))
                continue
            return None
    return None


def _sniff_image_format(buf: bytes) -> str | None:
    """Return a file extension for `buf` based on magic bytes, or None
    if it doesn't look like a supported image. Pure stdlib — `imghdr`
    was removed in Python 3.13."""
    if len(buf) < 12:
        return None
    if buf[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if buf[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if buf[:4] == b"RIFF" and buf[8:12] == b"WEBP":
        return ".webp"
    if buf[4:12] in (b"ftypavif", b"ftypavis"):
        return ".avif"
    if buf[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if buf[:4] in (b"ftyp",) or buf[4:8] == b"ftyp":
        # HEIC variants — common from iPhone-shot recipe pages.
        if b"heic" in buf[4:32].lower() or b"heif" in buf[4:32].lower():
            return ".heic"
    return None


def _fetch_remote_image(url: str) -> str:
    """Download a remote image to /static/uploads/ and return its local
    path, or "" on any failure. Caller falls back to the original URL.

    Defenses:
      * 8 MB cap (read no more)
      * Magic-byte validation (don't trust Content-Type or URL extension)
      * No Referer header on first try (some hotlink rules whitelist
        empty Referer); retry with the recipe URL as Referer on 403
      * EXIF stripped before storing (privacy + smaller files)
    """
    if not url or not url.startswith(("http://", "https://")):
        return ""
    # First attempt: no Referer.
    result = _http_get(url, timeout=10.0, retries=2)
    if result is None:
        return ""
    body, _headers = result
    # Truncate to the cap if the server sent more.
    body = body[:MAX_UPLOAD_BYTES]
    ext = _sniff_image_format(body)
    if ext is None:
        return ""
    if ext not in ALLOWED_IMAGE_EXTS:
        return ""
    try:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        safe = f"{secrets.token_hex(8)}{ext}"
        dest = os.path.join(UPLOAD_DIR, safe)
        with open(dest, "wb") as f:
            f.write(body)
    except OSError:
        return ""
    _strip_image_metadata(dest)
    return f"/static/uploads/{safe}"


# Tracking-parameter prefixes/names stripped from a source URL when we
# canonicalize it for de-duplication. Conservative — only the obvious
# analytics tags.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term",
    "utm_content", "utm_id", "utm_name", "utm_brand",
    "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid",
    "_ga", "ref", "ref_src", "ref_url", "igshid",
    "yclid", "dclid", "twclid", "wbraid", "gbraid",
}


def _canonical_source_url(url: str) -> str:
    """Strip tracking params + fragment from `url` so re-imports can
    match. Returns "" if the URL doesn't parse."""
    if not url:
        return ""
    try:
        from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
        parsed = urlparse(url.strip())
        if not parsed.scheme or not parsed.netloc:
            return ""
        kept = [
            (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k.lower() not in _TRACKING_PARAMS
        ]
        new_query = urlencode(kept)
        # Drop fragment too — never identifies a different recipe.
        return urlunparse(parsed._replace(query=new_query, fragment=""))
    except Exception:
        return url


def _validate_image_url(url: str) -> str:
    """Allow only http(s) URLs or our own /static/uploads/ paths.

    Anything else (javascript:, data:, file:, blob:, vbscript:, etc.) is
    dropped silently. The recipe form's URL input is browser-validated as
    a URL but the user can paste whatever; this is the server-side guard.
    Defensive against future template changes that might render the URL
    in a more dangerous attribute (e.g. an `<a href>`).
    """
    if not url:
        return ""
    url = url.strip()
    if url.startswith("/static/uploads/"):
        return url
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return ""


def _save_uploaded_image(file_storage) -> str | None:
    """Persist an uploaded image to static/uploads/ and return its public
    URL path. Returns None when no file was provided. Raises ValueError on
    invalid extension or oversized payload."""
    if file_storage is None or not file_storage.filename:
        return None
    name = file_storage.filename
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_IMAGE_EXTS:
        raise ValueError(f"Unsupported image type: {ext}")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    safe = f"{secrets.token_hex(8)}{ext}"
    dest = os.path.join(UPLOAD_DIR, safe)
    file_storage.save(dest)
    if os.path.getsize(dest) > MAX_UPLOAD_BYTES:
        os.remove(dest)
        raise ValueError("Image is too large (max 8 MB).")
    _strip_image_metadata(dest)
    return f"/static/uploads/{safe}"


def _strip_image_metadata(path: str) -> None:
    """Re-encode an uploaded image without EXIF metadata.

    Phone-taken recipe photos carry GPS coordinates of the family's home;
    stripping on upload prevents that data from showing up in any backup
    ZIP, browser cache, or restored copy. Best-effort — non-image files
    or formats Pillow can't re-encode are silently skipped (the file
    stays on disk untouched).
    """
    try:
        from PIL import Image
    except ImportError:
        return
    try:
        with Image.open(path) as img:
            fmt = img.format
            if fmt is None:
                return
            # Decode the pixel data into a fresh Image with no metadata.
            img.load()
            stripped = img.copy()
        # Pillow's save() does not include EXIF unless explicitly passed
        # via the `exif` kwarg, so a plain re-save drops it.
        stripped.save(path, format=fmt)
    except Exception:
        # Anything went wrong — leave the original file in place. The
        # photo still works as a recipe image; we just couldn't strip
        # metadata on this one.
        pass


def _resolve_secret_key() -> str:
    """Pick a stable Flask secret_key without requiring the user to set
    SHOPPINGLIST_SECRET themselves.

    Order of resolution:
      1. SHOPPINGLIST_SECRET env var, if set to something other than the
         shipped placeholder strings.
      2. .secret_key file alongside the DB if it already exists.
      3. Generate a fresh random key, persist it to .secret_key, return.

    Falls back to an ephemeral in-memory key if /data/ isn't writable;
    sessions invalidate on restart in that case, which is acceptable
    degradation — family logs in again.
    """
    placeholders = {
        "",
        "family-shopping-dev-key",
        "change-me-to-a-long-random-string",
    }
    env = os.environ.get("SHOPPINGLIST_SECRET", "").strip()
    if env not in placeholders:
        return env

    secret_dir = os.path.dirname(DB_PATH)
    secret_path = os.path.join(secret_dir, ".secret_key")
    try:
        with open(secret_path, "r", encoding="ascii") as f:
            saved = f.read().strip()
            if len(saved) >= 32:
                return saved
    except (FileNotFoundError, OSError):
        pass

    new_key = secrets.token_urlsafe(48)
    try:
        os.makedirs(secret_dir, exist_ok=True)
        with open(secret_path, "w", encoding="ascii") as f:
            f.write(new_key)
        try:
            os.chmod(secret_path, 0o600)
        except OSError:
            pass  # Windows / overlay FS — best effort
    except OSError:
        pass  # in-memory fallback
    return new_key


def create_app() -> Flask:
    app = Flask(__name__, instance_path=APP_DIR)
    app.secret_key = _resolve_secret_key()
    # Generous upload cap so a full backup ZIP (DB + every uploaded photo)
    # can come back in via /backup/restore. Per-image photos are still
    # bounded to MAX_UPLOAD_BYTES (8 MB) by _save_uploaded_image, and the
    # recipe-image fetcher caps at the same. The global limit only opens
    # the door for legitimately-large backup uploads.
    app.config["MAX_CONTENT_LENGTH"] = 256 * 1024 * 1024  # 256 MB
    app.teardown_appcontext(close_db)

    # ---- PWA assets ----------------------------------------------------

    @app.route("/manifest.json")
    def manifest():
        # Served from the root path so the install banner sees a same-origin
        # manifest scope of "/".
        return send_from_directory(
            os.path.join(APP_DIR, "static"),
            "manifest.json",
            mimetype="application/manifest+json",
        )

    @app.route("/sw.js")
    def service_worker():
        # Service worker MUST be served from the root for its scope to be "/".
        # Adding the Service-Worker-Allowed header is belt-and-suspenders.
        resp = make_response(send_from_directory(
            os.path.join(APP_DIR, "static"), "sw.js", mimetype="application/javascript",
        ))
        resp.headers["Service-Worker-Allowed"] = "/"
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.route("/events")
    def events():
        """Server-Sent Events stream for cross-device list sync.

        One Queue per connected client; mutating routes call
        `_broadcast("list_changed")` which fans out to every queue.
        Heartbeats every 15s keep the connection alive through any
        intervening proxies. Disconnects clean up the queue via
        `try/finally` in the generator.
        """
        def gen():
            q: queue.Queue = queue.Queue(maxsize=20)
            with _sse_lock:
                _sse_subscribers.add(q)
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        event_type, data = q.get(timeout=15)
                    except queue.Empty:
                        # Comment-only line keeps the connection warm
                        # without firing a client-side event.
                        yield ": heartbeat\n\n"
                        continue
                    yield f"event: {event_type}\ndata: {data}\n\n"
            finally:
                with _sse_lock:
                    _sse_subscribers.discard(q)

        response = Response(gen(), mimetype="text/event-stream")
        # Defeat reverse-proxy buffering for SSE.
        response.headers["Cache-Control"] = "no-cache, no-transform"
        response.headers["X-Accel-Buffering"] = "no"
        return response

    # ---- Pages ----------------------------------------------------------

    @app.route("/")
    def index():
        db = get_db()
        recipes = db.execute(
            "SELECT id, name, description, servings, instructions, source_url, image_url, prep_time, cook_time, total_time, category, notes, is_favorite, rating, nutrition, yields_text, cuisine, author, source_rating, keywords FROM recipe ORDER BY name"
        ).fetchall()
        active_recipes = db.execute(
            "SELECT lr.id, lr.multiplier, lr.added_by, r.id AS recipe_id, r.name "
            "FROM list_recipe lr JOIN recipe r ON r.id = lr.recipe_id "
            "ORDER BY lr.added_at"
        ).fetchall()
        grouped = build_shopping_list(db)
        total_items = sum(len(v) for v in grouped.values())
        checked_count = sum(1 for items in grouped.values() for i in items if i.checked)
        quick_add = _top_predicted_items(db, limit=8)
        return render_template(
            "index.html",
            recipes=recipes,
            active_recipes=active_recipes,
            grouped=grouped,
            categories=CATEGORIES,
            total_items=total_items,
            checked_count=checked_count,
            quick_add=quick_add,
        )

    @app.route("/recipes")
    def recipes_page():
        db = get_db()
        q = request.args.get("q", "").strip()
        cat = request.args.get("cat", "").strip()
        favs = request.args.get("favs") == "1"
        # Keyword search via LIKE — exact-match-ish, fast, deterministic.
        sql = (
            "SELECT id, name, description, servings, instructions, source_url, "
            "image_url, prep_time, cook_time, total_time, category, notes, "
            "is_favorite, rating FROM recipe WHERE 1=1"
        )
        params: list = []
        if q:
            sql += " AND (name LIKE ? OR description LIKE ? OR notes LIKE ?)"
            like = f"%{q}%"
            params += [like, like, like]
        if cat:
            sql += " AND category = ?"
            params.append(cat)
        if favs:
            sql += " AND is_favorite = 1"
        sql += " ORDER BY is_favorite DESC, name"
        rows = list(db.execute(sql, params).fetchall())

        # Semantic supplement: when there's a query, also run a vec search
        # and append matches that the keyword pass missed. Best-effort —
        # silently no-ops if fastembed/sqlite-vec aren't installed.
        semantic_extra = 0
        if q:
            try:
                import embedding as _embedding
                if _embedding.is_available():
                    keyword_ids = {r["id"] for r in rows}
                    semantic_ids = _embedding.search(db, q, limit=15)
                    extra_ids = [
                        sid for sid in semantic_ids if sid not in keyword_ids
                    ]
                    if extra_ids:
                        placeholders = ",".join("?" for _ in extra_ids)
                        ext_sql = (
                            "SELECT id, name, description, servings, "
                            "instructions, source_url, image_url, prep_time, "
                            "cook_time, total_time, category, notes, "
                            "is_favorite, rating FROM recipe "
                            f"WHERE id IN ({placeholders})"
                        )
                        ext_params = list(extra_ids)
                        if cat:
                            ext_sql += " AND category = ?"
                            ext_params.append(cat)
                        if favs:
                            ext_sql += " AND is_favorite = 1"
                        ext_rows = db.execute(ext_sql, ext_params).fetchall()
                        # Re-order extras by the semantic ranking that
                        # produced them.
                        row_by_id = {r["id"]: r for r in ext_rows}
                        ordered_extras = [
                            row_by_id[sid] for sid in extra_ids
                            if sid in row_by_id
                        ]
                        rows.extend(ordered_extras)
                        semantic_extra = len(ordered_extras)
            except ImportError:
                pass

        all_categories = [
            row["category"]
            for row in db.execute(
                "SELECT DISTINCT category FROM recipe "
                "WHERE category != '' ORDER BY category"
            ).fetchall()
        ]

        # Fetch all ingredients for the displayed recipes in ONE query
        # rather than firing per-recipe queries (N+1). Significant for the
        # Pi 4 when the recipe library grows past ~30 recipes.
        ings_by_recipe: dict[int, list] = defaultdict(list)
        recipe_ids = [r["id"] for r in rows]
        if recipe_ids:
            placeholders = ",".join("?" * len(recipe_ids))
            ing_rows = db.execute(
                "SELECT id, recipe_id, name, quantity, unit, category, note "
                f"FROM ingredient WHERE recipe_id IN ({placeholders}) "
                "ORDER BY recipe_id, id",
                recipe_ids,
            ).fetchall()
            for ing in ing_rows:
                ings_by_recipe[ing["recipe_id"]].append(ing)
        recipes = [
            {"recipe": r, "ingredients": ings_by_recipe.get(r["id"], [])}
            for r in rows
        ]
        return render_template(
            "recipes.html",
            recipes=recipes,
            categories=CATEGORIES,
            recipe_categories=RECIPE_CATEGORIES,
            existing_categories=all_categories,
            q=q, cat=cat, favs=favs,
            semantic_extra=semantic_extra,
        )

    @app.route("/recipes/<int:recipe_id>/rate", methods=["POST"])
    def recipe_rate(recipe_id: int):
        try:
            value = int(request.form.get("rating", request.json.get("rating", 0)
                                         if request.is_json else 0))
        except (TypeError, ValueError):
            value = 0
        value = max(0, min(5, value))
        db = get_db()
        if not db.execute("SELECT 1 FROM recipe WHERE id = ?", (recipe_id,)).fetchone():
            return jsonify({"ok": False}), 404
        db.execute("UPDATE recipe SET rating = ? WHERE id = ?", (value, recipe_id))
        db.commit()
        if request.is_json or request.headers.get("X-Requested-With") == "fetch":
            return jsonify({"ok": True, "rating": value})
        return redirect(request.referrer or url_for("recipes_page"))

    @app.route("/recipes/<int:recipe_id>/favorite", methods=["POST"])
    def recipe_favorite(recipe_id: int):
        db = get_db()
        row = db.execute(
            "SELECT is_favorite FROM recipe WHERE id = ?", (recipe_id,)
        ).fetchone()
        if row is None:
            return jsonify({"ok": False}), 404
        new_val = 0 if row["is_favorite"] else 1
        db.execute(
            "UPDATE recipe SET is_favorite = ? WHERE id = ?", (new_val, recipe_id)
        )
        db.commit()
        if request.is_json or request.headers.get("X-Requested-With") == "fetch":
            return jsonify({"ok": True, "favorite": bool(new_val)})
        return redirect(request.referrer or url_for("recipes_page"))

    @app.route("/recipes/import", methods=["POST"])
    def recipe_import():
        url = request.form.get("url", "").strip()
        if not url:
            flash("Please paste a recipe URL.", "error")
            return redirect(url_for("recipes_page"))
        try:
            from recipe_scrapers import scrape_me
        except ImportError:
            flash(
                "URL import requires the 'recipe-scrapers' package. "
                "Run: pip install recipe-scrapers",
                "error",
            )
            return redirect(url_for("recipes_page"))

        # De-duplication: if a recipe with the same canonical source URL
        # already exists, redirect the user to its edit page instead of
        # creating a "(2)"-suffixed copy. Conservative — never overwrites
        # silently. The user can delete the existing recipe and re-import
        # to refresh.
        canonical_url = _canonical_source_url(url)
        if canonical_url:
            db_check = get_db()
            existing_rows = db_check.execute(
                "SELECT id, name, source_url FROM recipe "
                "WHERE source_url != '' "
                "ORDER BY id DESC"
            ).fetchall()
            for row in existing_rows:
                if _canonical_source_url(row["source_url"]) == canonical_url:
                    flash(
                        f"That URL is already imported as \"{row['name']}\". "
                        "Edit it directly, or delete it first if you want to "
                        "re-import a fresh copy.",
                        "error",
                    )
                    return redirect(url_for("recipe_edit", recipe_id=row["id"]))

        try:
            from recipe_scrapers import scrape_html, scraper_exists_for
            if scraper_exists_for(url):
                scraper = scrape_me(url)
            else:
                # Unsupported site — fetch HTML and let recipe-scrapers
                # parse schema.org JSON-LD via wild_mode. Use a real
                # browser UA + retry-on-transient-failure so polite
                # 503/429s don't fail user-visibly.
                fetched = _http_get(url, timeout=15.0, retries=3)
                if fetched is None:
                    flash(
                        f"Could not fetch that URL after retries. "
                        "The site may be down or blocking automated requests.",
                        "error",
                    )
                    return redirect(url_for("recipes_page"))
                body, headers = fetched
                charset = "utf-8"
                ctype = headers.get("Content-Type") or headers.get("content-type") or ""
                m = re.search(r"charset=([\w-]+)", ctype, re.I)
                if m:
                    charset = m.group(1)
                html = body.decode(charset, errors="replace")
                scraper = scrape_html(html, org_url=url, wild_mode=True)
            title = (scraper.title() or "").strip() or "Imported Recipe"
            try:
                description = (scraper.description() or "").strip()
            except Exception:
                description = ""
            try:
                yields = scraper.yields() or ""
                # Pull the first integer out of "10 servings", "Serves 4", etc.
                m = re.search(r"\d+", yields)
                servings = int(m.group(0)) if m else 4
            except Exception:
                servings = 4
            ing_lines = scraper.ingredients() or []
            try:
                instructions = (scraper.instructions() or "").strip()
            except Exception:
                instructions = ""

            def _safe(call, default=""):
                try:
                    val = call()
                    return val if val is not None else default
                except Exception:
                    return default

            def _to_int_minutes(val) -> int:
                if val is None:
                    return 0
                if isinstance(val, (int, float)):
                    return int(val)
                # strings like "15", "PT30M", "1 hr 20 min"
                s = str(val)
                m = re.search(r"PT(?:(\d+)H)?(?:(\d+)M)?", s)
                if m and (m.group(1) or m.group(2)):
                    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
                hours = re.search(r"(\d+)\s*(?:h|hour|hr)", s, re.I)
                mins = re.search(r"(\d+)\s*(?:m|min)", s, re.I)
                if hours or mins:
                    return (int(hours.group(1)) if hours else 0) * 60 + (
                        int(mins.group(1)) if mins else 0
                    )
                m = re.search(r"\d+", s)
                return int(m.group(0)) if m else 0

            image_url = _validate_image_url(_safe(scraper.image, ""))
            # Bake the remote image into a local upload so the recipe
            # keeps its photo even if the source site moves it, blocks
            # hotlinks, or goes away. Falls back to the remote URL on
            # any failure (size cap, magic-byte mismatch, network).
            if image_url and image_url.startswith(("http://", "https://")):
                local = _fetch_remote_image(image_url)
                if local:
                    image_url = local
            prep_time = _to_int_minutes(_safe(scraper.prep_time, 0))
            cook_time = _to_int_minutes(_safe(scraper.cook_time, 0))
            total_time = _to_int_minutes(_safe(scraper.total_time, 0))
            if not total_time and (prep_time or cook_time):
                total_time = prep_time + cook_time
            category = (str(_safe(scraper.category, "")) or "").strip()

            # Nutrition: scraper.nutrients() returns a dict like
            # {"calories": "300 kcal", "fatContent": "12 g"}. Render as
            # readable lines for storage/display.
            nutrients = _safe(scraper.nutrients, {}) or {}
            if isinstance(nutrients, dict):
                pretty = []
                for k, v in nutrients.items():
                    if not v:
                        continue
                    label = re.sub(r"Content$", "", str(k))
                    label = re.sub(r"(?<!^)(?=[A-Z])", " ", label).strip().title()
                    pretty.append(f"{label}: {v}")
                nutrition_text = "\n".join(pretty)
            else:
                nutrition_text = str(nutrients).strip()

            yields_text = (str(_safe(scraper.yields, "")) or "").strip()
            cuisine = (str(_safe(scraper.cuisine, "")) or "").strip()
            author = (str(_safe(scraper.author, "")) or "").strip()

            ratings_val = _safe(scraper.ratings, 0)
            try:
                source_rating = float(ratings_val) if ratings_val else 0.0
            except (TypeError, ValueError):
                source_rating = 0.0

            keywords_val = _safe(scraper.keywords, [])
            if isinstance(keywords_val, (list, tuple)):
                keywords_text = ", ".join(str(k).strip() for k in keywords_val if k)
            else:
                keywords_text = str(keywords_val).strip()

            # Recipe-scrapers does not expose a generic notes() method.
            # Some site-specific scrapers add one, and most recipe blogs put
            # a "Notes" / "Recipe Notes" / "Tips" section in their HTML.
            # Try both: the method first, then a soup fallback.
            recipe_notes = ""
            try:
                method = getattr(scraper, "notes", None)
                if callable(method):
                    val = method()
                    if val:
                        if isinstance(val, (list, tuple)):
                            recipe_notes = "\n".join(str(v).strip() for v in val if v)
                        else:
                            recipe_notes = str(val).strip()
            except Exception:
                pass
            if not recipe_notes:
                try:
                    recipe_notes = _scrape_notes_section(scraper.soup)
                except Exception:
                    recipe_notes = ""
        except Exception as exc:  # network, parser, or unsupported site
            flash(f"Could not import that URL: {exc}", "error")
            return redirect(url_for("recipes_page"))

        if not ing_lines:
            flash("No ingredients found at that URL.", "error")
            return redirect(url_for("recipes_page"))

        # Sanity-check the import before committing — flags that have
        # historically meant "the scrape went off the rails." Warnings
        # only; the recipe still saves so the user can fix in edit.
        sanity_warnings: list[str] = []
        if servings < 1 or servings > 50:
            sanity_warnings.append(
                f"servings looked off ({servings}); reset to 4"
            )
            servings = 4
        if len(ing_lines) > 50:
            sanity_warnings.append(
                f"unusually large ingredient list ({len(ing_lines)} rows) — "
                "the scraper may have grabbed too much"
            )
        if instructions and len(instructions) < 50:
            sanity_warnings.append(
                "instructions are very short — site may not have published "
                "the full recipe"
            )

        db = get_db()
        # Make the name unique if it collides with an existing recipe by
        # name (we already deduped on canonical URL above; this catches
        # an edge case where two different sites publish the same title).
        base_title = title
        suffix = 2
        while db.execute(
            "SELECT 1 FROM recipe WHERE name = ?", (title,)
        ).fetchone():
            title = f"{base_title} ({suffix})"
            suffix += 1

        cur = db.execute(
            "INSERT INTO recipe (name, description, servings, instructions, "
            "source_url, image_url, prep_time, cook_time, total_time, category, "
            "nutrition, yields_text, cuisine, author, source_rating, keywords, "
            "notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                title, description, servings, instructions, url,
                image_url, prep_time, cook_time, total_time, category,
                nutrition_text, yields_text, cuisine, author,
                source_rating, keywords_text, recipe_notes,
            ),
        )
        recipe_id = cur.lastrowid
        for line in ing_lines:
            parsed = parse_ingredient(line)
            if not parsed["name"]:
                continue
            db.execute(
                "INSERT INTO ingredient "
                "(recipe_id, name, quantity, unit, category, note) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    recipe_id,
                    parsed["name"],
                    parsed["quantity"],
                    parsed["unit"],
                    guess_category(parsed["name"]),
                    parsed["note"],
                ),
            )
        db.commit()
        _refresh_recipe_embedding(db, recipe_id)
        db.commit()
        flash(
            f"Imported \"{title}\" — review categories and units, then save.",
            "success",
        )
        for w in sanity_warnings:
            flash(f"Heads up: {w}.", "error")
        return redirect(url_for("recipe_edit", recipe_id=recipe_id))

    @app.post("/recipes/import-photo")
    def recipe_import_photo():
        file_storage = request.files.get("photo")
        if file_storage is None or not file_storage.filename:
            flash("Please choose a photo of the recipe card.", "error")
            return redirect(url_for("recipes_page"))
        try:
            image_url = _save_uploaded_image(file_storage)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("recipes_page"))
        if not image_url:
            flash("Could not read that photo.", "error")
            return redirect(url_for("recipes_page"))

        disk_path = os.path.join(UPLOAD_DIR, os.path.basename(image_url))

        try:
            raw_text = _ocr_image_to_text(disk_path)
        except ImportError:
            # Tesseract / pytesseract / Pillow missing — clean up and tell
            # the user without leaving an orphan upload behind.
            try:
                os.remove(disk_path)
            except OSError:
                pass
            flash(
                "Photo import requires tesseract-ocr and pytesseract on the server.",
                "error",
            )
            return redirect(url_for("recipes_page"))
        except Exception as exc:
            try:
                os.remove(disk_path)
            except OSError:
                pass
            flash(f"OCR failed on that photo: {exc}", "error")
            return redirect(url_for("recipes_page"))

        # If we got essentially nothing, don't pollute the recipe list with
        # an empty card — clean up the upload too.
        if len(raw_text.strip()) < 20:
            try:
                os.remove(disk_path)
            except OSError:
                pass
            flash(
                "Couldn't read enough text from that photo. Try better light, "
                "fill the frame with the card, and avoid glare.",
                "error",
            )
            return redirect(url_for("recipes_page"))

        parsed_recipe = _parse_ocr_recipe(raw_text)
        title = parsed_recipe["title"] or "Imported Recipe"
        ing_lines = parsed_recipe["ingredients"]
        instructions = parsed_recipe["instructions"]
        servings = parsed_recipe["servings"] or 4
        prep_time = parsed_recipe["prep_time"]
        cook_time = parsed_recipe["cook_time"]
        total_time = parsed_recipe["total_time"]

        # Stash the unprocessed OCR output in `notes` so the user can spot
        # anything the parser missed and copy it into the right field.
        review_notes = (
            "[OCR scan output — review and remove this block when you're "
            "happy with the rest]\n\n" + raw_text.strip()
        )

        db = get_db()
        base_title = title
        suffix = 2
        while db.execute(
            "SELECT 1 FROM recipe WHERE name = ?", (title,)
        ).fetchone():
            title = f"{base_title} ({suffix})"
            suffix += 1

        cur = db.execute(
            "INSERT INTO recipe "
            "(name, servings, instructions, image_url, prep_time, cook_time, "
            " total_time, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                title, servings, instructions, image_url,
                prep_time, cook_time, total_time, review_notes,
            ),
        )
        recipe_id = cur.lastrowid
        for line in ing_lines:
            parsed = parse_ingredient(line)
            if not parsed["name"]:
                continue
            db.execute(
                "INSERT INTO ingredient "
                "(recipe_id, name, quantity, unit, category, note) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    recipe_id,
                    parsed["name"],
                    parsed["quantity"],
                    parsed["unit"],
                    guess_category(parsed["name"]),
                    parsed["note"],
                ),
            )
        db.commit()
        _refresh_recipe_embedding(db, recipe_id)
        db.commit()
        if not ing_lines and not instructions.strip():
            flash(
                "Photo scanned but the parser couldn't split ingredients — "
                "the raw text is in the Notes field; copy what you need into "
                "the right fields.",
                "error",
            )
        else:
            flash(
                f"Imported \"{title}\" from photo — review and clean up before saving.",
                "success",
            )
        return redirect(url_for("recipe_edit", recipe_id=recipe_id))

    @app.route("/recipes/<int:recipe_id>")
    def recipe_view(recipe_id: int):
        recipe = _get_recipe(recipe_id)
        if recipe is None:
            flash("Recipe not found.", "error")
            return redirect(url_for("recipes_page"))
        db = get_db()
        ingredients = db.execute(
            "SELECT id, name, quantity, unit, category, note "
            "FROM ingredient WHERE recipe_id = ? ORDER BY id",
            (recipe_id,),
        ).fetchall()
        # Split instructions into a list of steps. Recipe-scrapers joins
        # them with newlines; some sites use double-newlines.
        raw = (recipe["instructions"] or "").strip()
        if raw:
            steps = [s.strip() for s in re.split(r"\n+", raw) if s.strip()]
        else:
            steps = []
        return render_template(
            "recipe_view.html",
            recipe=recipe,
            ingredients=ingredients,
            steps=steps,
        )

    @app.route("/recipes/<int:recipe_id>/cook")
    def recipe_cook(recipe_id: int):
        """Full-screen step-by-step cooking view with screen Wake Lock.

        Pulls out the instructions as a JSON-friendly list so the
        client-side JS can navigate between steps without hitting the
        server. Wake Lock keeps the iPad/phone screen on while cooking.
        """
        recipe = _get_recipe(recipe_id)
        if recipe is None:
            flash("Recipe not found.", "error")
            return redirect(url_for("recipes_page"))
        db = get_db()
        ingredients = db.execute(
            "SELECT id, name, quantity, unit, category, note "
            "FROM ingredient WHERE recipe_id = ? ORDER BY id",
            (recipe_id,),
        ).fetchall()
        raw = (recipe["instructions"] or "").strip()
        steps = [s.strip() for s in re.split(r"\n+", raw) if s.strip()] if raw else []
        return render_template(
            "cook_mode.html",
            recipe=recipe,
            ingredients=ingredients,
            steps=steps,
        )

    @app.route("/recipes/new", methods=["GET", "POST"])
    def recipe_new():
        if request.method == "POST":
            return _save_recipe(None)
        return render_template(
            "recipe_form.html",
            recipe=None,
            ingredients=[],
            categories=CATEGORIES,
            recipe_categories=RECIPE_CATEGORIES,
        )

    @app.route("/recipes/<int:recipe_id>/edit", methods=["GET", "POST"])
    def recipe_edit(recipe_id: int):
        recipe = _get_recipe(recipe_id)
        if recipe is None:
            flash("Recipe not found.", "error")
            return redirect(url_for("recipes_page"))
        if request.method == "POST":
            return _save_recipe(recipe_id)
        db = get_db()
        ings = db.execute(
            "SELECT id, name, quantity, unit, category, note "
            "FROM ingredient WHERE recipe_id = ? ORDER BY id",
            (recipe_id,),
        ).fetchall()
        return render_template(
            "recipe_form.html",
            recipe=recipe,
            ingredients=ings,
            categories=CATEGORIES,
            recipe_categories=RECIPE_CATEGORIES,
        )

    @app.route("/recipes/<int:recipe_id>/delete", methods=["POST"])
    def recipe_delete(recipe_id: int):
        db = get_db()
        # Capture image_url before delete so we can clean up the file.
        row = db.execute(
            "SELECT image_url FROM recipe WHERE id = ?", (recipe_id,)
        ).fetchone()
        image_url = row["image_url"] if row else ""
        # vec0 virtual tables don't honor FK cascades — clean up the
        # embedding row explicitly. OperationalError = extension not
        # loaded, table doesn't exist; safe to ignore.
        try:
            db.execute(
                "DELETE FROM recipe_embedding WHERE recipe_id = ?", (recipe_id,)
            )
        except sqlite3.OperationalError:
            pass
        db.execute("DELETE FROM recipe WHERE id = ?", (recipe_id,))
        db.commit()
        if image_url:
            _delete_uploaded_image_file(image_url, except_recipe_id=recipe_id)
        flash("Recipe deleted.", "success")
        return redirect(url_for("recipes_page"))

    # ---- Shopping list actions -----------------------------------------

    @app.route("/plan")
    def plan_page():
        week_param = request.args.get("week", "")
        today = date.today()
        week_start = _week_start(_parse_iso_date(week_param) if week_param else today)
        days = [week_start + timedelta(days=i) for i in range(7)]

        db = get_db()
        rows = db.execute(
            "SELECT mp.id, mp.plan_date, mp.slot, mp.recipe_id, mp.text_plan, "
            "       mp.multiplier, mp.sort_order, r.name AS recipe_name "
            "FROM meal_plan mp "
            "LEFT JOIN recipe r ON r.id = mp.recipe_id "
            "WHERE mp.plan_date >= ? AND mp.plan_date <= ? "
            "ORDER BY mp.plan_date, mp.sort_order, mp.id",
            (days[0].isoformat(), days[6].isoformat()),
        ).fetchall()

        by_date = defaultdict(list)
        for r in rows:
            by_date[r["plan_date"]].append(r)

        recipes = db.execute(
            "SELECT id, name FROM recipe ORDER BY name COLLATE NOCASE"
        ).fetchall()

        prev_week = week_start - timedelta(days=7)
        next_week = week_start + timedelta(days=7)
        week_end = week_start + timedelta(days=6)

        return render_template(
            "plan.html",
            days=days,
            by_date=by_date,
            recipes=recipes,
            today=today,
            week_start=week_start,
            week_end=week_end,
            prev_week=prev_week.isoformat(),
            next_week=next_week.isoformat(),
            this_week_iso=_week_start(today).isoformat(),
        )

    @app.post("/plan/add")
    def plan_add():
        plan_date_raw = request.form.get("plan_date", "").strip()
        slot = request.form.get("slot", "").strip()[:40]
        recipe_id_raw = request.form.get("recipe_id", "").strip()
        text_plan = request.form.get("text_plan", "").strip()[:200]
        multiplier_raw = request.form.get("multiplier", "1").strip() or "1"

        try:
            datetime.strptime(plan_date_raw, "%Y-%m-%d")
        except ValueError:
            flash("Invalid date.", "error")
            return redirect(url_for("plan_page"))

        multiplier = _parse_multiplier_field(
            multiplier_raw, floor=0.1, cap=MAX_MULTIPLIER
        )

        recipe_id = None
        if recipe_id_raw:
            try:
                recipe_id = int(recipe_id_raw)
            except ValueError:
                recipe_id = None

        if not recipe_id and not text_plan:
            flash("Pick a recipe or type a quick plan.", "error")
            return redirect(url_for("plan_page", week=plan_date_raw))

        db = get_db()
        db.execute(
            "INSERT INTO meal_plan (plan_date, slot, recipe_id, text_plan, multiplier) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                plan_date_raw,
                slot,
                recipe_id,
                text_plan if not recipe_id else "",
                multiplier,
            ),
        )
        db.commit()
        return redirect(url_for("plan_page", week=plan_date_raw))

    @app.post("/plan/<int:plan_id>/remove")
    def plan_remove(plan_id: int):
        db = get_db()
        row = db.execute(
            "SELECT plan_date FROM meal_plan WHERE id = ?", (plan_id,)
        ).fetchone()
        week = row["plan_date"] if row else ""
        db.execute("DELETE FROM meal_plan WHERE id = ?", (plan_id,))
        db.commit()
        return redirect(url_for("plan_page", week=week))

    @app.post("/plan/add-week-to-list")
    def plan_add_week_to_list():
        week_raw = request.form.get("week", "").strip()
        week_start = _week_start(_parse_iso_date(week_raw))
        week_end = week_start + timedelta(days=6)

        db = get_db()
        rows = db.execute(
            "SELECT recipe_id, multiplier FROM meal_plan "
            "WHERE plan_date BETWEEN ? AND ? AND recipe_id IS NOT NULL",
            (week_start.isoformat(), week_end.isoformat()),
        ).fetchall()

        if not rows:
            flash("No recipes planned for this week.", "error")
            return redirect(url_for("plan_page", week=week_start.isoformat()))

        for r in rows:
            db.execute(
                "INSERT INTO list_recipe (recipe_id, multiplier, added_by) "
                "VALUES (?, ?, ?)",
                (r["recipe_id"], r["multiplier"], "Plan"),
            )
        db.commit()
        flash(
            f"Added {len(rows)} planned recipe(s) to the shopping list.",
            "success",
        )
        return redirect(url_for("index", _anchor="list"))

    @app.route("/backup")
    def backup_page():
        db_size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
        photo_count = 0
        photo_bytes = 0
        if os.path.isdir(UPLOAD_DIR):
            for fname in os.listdir(UPLOAD_DIR):
                full = os.path.join(UPLOAD_DIR, fname)
                if os.path.isfile(full):
                    photo_count += 1
                    photo_bytes += os.path.getsize(full)
        db_dir = os.path.dirname(DB_PATH)
        snapshots = []
        if os.path.isdir(db_dir):
            for fname in sorted(os.listdir(db_dir), reverse=True):
                if not fname.startswith("pre-restore-"):
                    continue
                if not (fname.endswith(".db") or fname.endswith(".zip")):
                    continue
                full = os.path.join(db_dir, fname)
                try:
                    snapshots.append({
                        "name": fname,
                        "size": os.path.getsize(full),
                        "mtime": datetime.fromtimestamp(
                            os.path.getmtime(full)
                        ).strftime("%Y-%m-%d %H:%M"),
                        "kind": "Full (DB + photos)" if fname.endswith(".zip")
                                else "DB only (legacy)",
                    })
                except OSError:
                    pass
        orphans = _scan_orphan_images()
        orphan_bytes = 0
        for fname in orphans:
            full = os.path.join(UPLOAD_DIR, fname)
            try:
                orphan_bytes += os.path.getsize(full)
            except OSError:
                pass
        auto_backups = _list_auto_backups()
        auto_backup_dir = _auto_backup_dir()
        remote_image_count = _count_remote_image_recipes(get_db())
        return render_template(
            "backup.html",
            db_size=db_size,
            photo_count=photo_count,
            photo_bytes=photo_bytes,
            snapshots=snapshots,
            orphan_count=len(orphans),
            orphan_bytes=orphan_bytes,
            auto_backups=auto_backups,
            auto_backup_dir=auto_backup_dir,
            backup_enabled=os.environ.get("BACKUP_ENABLED", "1") != "0",
            remote_image_count=remote_image_count,
        )

    @app.post("/backup/rebake-images")
    def backup_rebake_images():
        """Pull every remote recipe image down to /static/uploads/ so
        they actually land in the backup ZIP. One-shot migration for
        recipes imported before Stage 9 added the image-baker."""
        succeeded, failed = _rebake_remote_recipe_images(get_db())
        if succeeded == 0 and failed == 0:
            flash("No remote-URL recipe images to rebake.", "success")
        elif failed == 0:
            flash(
                f"Baked {succeeded} remote recipe image"
                f"{'' if succeeded == 1 else 's'} into local uploads.",
                "success",
            )
        else:
            flash(
                f"Baked {succeeded} image(s); {failed} failed (host blocked, "
                "image moved, or network error). The failed ones still have "
                "their original URL — try again later or replace manually.",
                "error",
            )
        return redirect(url_for("backup_page"))

    @app.post("/backup/auto/run-now")
    def backup_auto_run_now():
        """Trigger an immediate auto-backup outside the scheduled window —
        useful for "I'm about to do something risky, snapshot now."""
        try:
            _scheduled_auto_backup()
            flash("Backup written.", "success")
        except Exception as exc:
            flash(f"Backup failed: {exc}", "error")
        return redirect(url_for("backup_page"))

    _AUTO_BACKUP_NAME_RE = re.compile(
        r"auto-backup-[0-9]{8}-[0-9]{6}\.zip"
    )

    @app.get("/backup/auto/<name>")
    def backup_auto_download(name: str):
        if not _AUTO_BACKUP_NAME_RE.fullmatch(name):
            return ("Not found", 404)
        full = os.path.join(_auto_backup_dir(), name)
        if not os.path.isfile(full):
            return ("Not found", 404)
        return send_file(
            full,
            as_attachment=True,
            download_name=name,
            mimetype="application/zip",
        )

    @app.post("/backup/auto/<name>/delete")
    def backup_auto_delete(name: str):
        if not _AUTO_BACKUP_NAME_RE.fullmatch(name):
            flash("Invalid backup name.", "error")
            return redirect(url_for("backup_page"))
        full = os.path.join(_auto_backup_dir(), name)
        if os.path.isfile(full):
            try:
                os.remove(full)
                flash(f"Deleted {name}.", "success")
            except OSError as exc:
                flash(f"Could not delete: {exc}", "error")
        return redirect(url_for("backup_page"))

    @app.post("/backup/cleanup-orphans")
    def backup_cleanup_orphans():
        orphans = _scan_orphan_images()
        deleted = 0
        for fname in orphans:
            full = os.path.join(UPLOAD_DIR, fname)
            try:
                os.remove(full)
                deleted += 1
            except OSError:
                pass
        if deleted:
            flash(
                f"Removed {deleted} orphaned photo file"
                f"{'' if deleted == 1 else 's'}.",
                "success",
            )
        else:
            flash("No orphaned photos to clean up.", "success")
        return redirect(url_for("backup_page"))

    @app.get("/backup/download")
    def backup_download():
        # Build a ZIP of DB + every uploaded photo so a restore on a
        # fresh Pi truly recreates the family's state.
        fd, tmp_zip = tempfile.mkstemp(suffix=".zip", prefix="kasa-")
        os.close(fd)
        try:
            _write_full_backup_zip(tmp_zip)
        except Exception as exc:
            try:
                os.remove(tmp_zip)
            except OSError:
                pass
            flash(f"Could not generate backup: {exc}", "error")
            return redirect(url_for("backup_page"))

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        response = send_file(
            tmp_zip,
            as_attachment=True,
            download_name=f"kasa-backup-{timestamp}.zip",
            mimetype="application/zip",
        )

        @response.call_on_close
        def _cleanup():
            try:
                os.remove(tmp_zip)
            except OSError:
                pass
        return response

    @app.post("/backup/restore")
    def backup_restore():
        file = request.files.get("backup")
        confirm = request.form.get("confirm", "").strip().lower()
        if confirm != "restore":
            flash(
                "Type the word RESTORE to confirm — this overwrites all "
                "current recipes, lists, meal plans, and uploaded photos.",
                "error",
            )
            return redirect(url_for("backup_page"))
        if not file or not file.filename:
            flash("Choose a backup file to restore.", "error")
            return redirect(url_for("backup_page"))

        db_dir = os.path.dirname(DB_PATH)
        os.makedirs(db_dir, exist_ok=True)
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in (".zip", ".db"):
            flash("Backup file must be .zip (full) or .db (legacy).", "error")
            return redirect(url_for("backup_page"))

        incoming = os.path.join(
            db_dir, f"_incoming-{secrets.token_hex(8)}{ext}"
        )
        file.save(incoming)

        # Always snapshot current state before touching anything. Include
        # microseconds in the filename so two same-second restores don't
        # overwrite each other's pre-restore snapshots.
        ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        pre_path = os.path.join(db_dir, f"pre-restore-{ts}.zip")
        try:
            _write_full_backup_zip(pre_path)
        except Exception as exc:
            try:
                os.remove(incoming)
            except OSError:
                pass
            flash(f"Couldn't snapshot current state; aborting: {exc}", "error")
            return redirect(url_for("backup_page"))
        _rotate_pre_restore_snapshots(keep=5)

        # Branch on file type.
        if ext == ".zip":
            ok, msg = _restore_from_zip(incoming)
        else:
            ok, msg = _restore_from_db_only(incoming)

        try:
            os.remove(incoming)
        except OSError:
            pass

        if ok:
            # Re-run schema migrations so an older backup (made before
            # newer tables/columns existed) lands in a state the running
            # app can actually query. init_db is idempotent — CREATE
            # TABLE IF NOT EXISTS + ALTER on missing columns + classifier
            # version check. Cheap to call.
            try:
                init_db()
            except Exception as exc:
                # Don't fail the restore if migration hits an edge case;
                # the next container restart will run init_db again.
                print(
                    f"[restore] post-restore init_db failed (will retry "
                    f"on next boot): {exc}",
                    flush=True,
                )
            flash(
                f"{msg} Pre-restore snapshot saved as "
                f"{os.path.basename(pre_path)}.",
                "success",
            )
        else:
            flash(msg, "error")
        return redirect(url_for("backup_page"))

    def _restore_from_zip(zip_path: str) -> tuple[bool, str]:
        if not zipfile.is_zipfile(zip_path):
            return False, "That .zip file is not a valid archive."
        with tempfile.TemporaryDirectory() as tmp:
            try:
                with zipfile.ZipFile(zip_path) as zf:
                    # Defend against ZIP slip — reject any path component
                    # that escapes the temp dir.
                    for member in zf.namelist():
                        if member.startswith("/") or ".." in member.split("/"):
                            return False, "Backup archive contains unsafe paths."
                    zf.extractall(tmp)
            except Exception as exc:
                return False, f"Couldn't read backup archive: {exc}"
            db_path = os.path.join(tmp, "shoppinglist.db")
            if not os.path.isfile(db_path):
                return False, "Backup archive doesn't contain shoppinglist.db."
            if not _is_valid_kasa_db(db_path):
                return False, "Backup DB is missing required tables."
            try:
                src = sqlite3.connect(db_path)
                dst = sqlite3.connect(DB_PATH)
                src.backup(dst)
                dst.close()
                src.close()
            except Exception as exc:
                return False, f"DB restore failed: {exc}"
            # Replace uploads — wipe everything in UPLOAD_DIR, then copy
            # whatever was in the ZIP's uploads/ folder.
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            for fname in os.listdir(UPLOAD_DIR):
                full = os.path.join(UPLOAD_DIR, fname)
                if os.path.isfile(full):
                    try:
                        os.remove(full)
                    except OSError:
                        pass
            uploads_src = os.path.join(tmp, "uploads")
            photo_count = 0
            if os.path.isdir(uploads_src):
                for fname in os.listdir(uploads_src):
                    full = os.path.join(uploads_src, fname)
                    if os.path.isfile(full):
                        shutil.copy2(full, os.path.join(UPLOAD_DIR, fname))
                        photo_count += 1
        return True, f"Restored DB and {photo_count} photo(s) from backup."

    def _restore_from_db_only(db_path: str) -> tuple[bool, str]:
        if not _is_valid_kasa_db(db_path):
            return False, "That .db file is not a Kasa backup."
        try:
            src = sqlite3.connect(db_path)
            dst = sqlite3.connect(DB_PATH)
            src.backup(dst)
            dst.close()
            src.close()
        except Exception as exc:
            return False, f"Restore failed: {exc}"
        return True, (
            "Restored DB only — uploaded photos are unchanged "
            "(use a .zip backup to also restore photos)."
        )

    # Optional `-NNNNNN` microsecond suffix accepted for newer snapshots
    # while still allowing the older second-precision filenames.
    _SNAPSHOT_NAME_RE = re.compile(
        r"pre-restore-[0-9]{8}-[0-9]{6}(?:-[0-9]{6})?\.(?:db|zip)"
    )

    @app.get("/backup/snapshot/<name>")
    def backup_snapshot_download(name: str):
        # Only allow filenames matching the pre-restore-*.{db,zip}
        # convention so an attacker can't path-traverse.
        if not _SNAPSHOT_NAME_RE.fullmatch(name):
            return ("Not found", 404)
        full = os.path.join(os.path.dirname(DB_PATH), name)
        if not os.path.isfile(full):
            return ("Not found", 404)
        mime = "application/zip" if name.endswith(".zip") else "application/octet-stream"
        return send_file(
            full,
            as_attachment=True,
            download_name=name,
            mimetype=mime,
        )

    @app.post("/backup/snapshot/<name>/delete")
    def backup_snapshot_delete(name: str):
        if not _SNAPSHOT_NAME_RE.fullmatch(name):
            flash("Invalid snapshot name.", "error")
            return redirect(url_for("backup_page"))
        full = os.path.join(os.path.dirname(DB_PATH), name)
        if os.path.isfile(full):
            try:
                os.remove(full)
                flash(f"Deleted {name}.", "success")
            except OSError as exc:
                flash(f"Could not delete: {exc}", "error")
        return redirect(url_for("backup_page"))

    @app.route("/list/add-recipe", methods=["POST"])
    def list_add_recipe():
        recipe_id = request.form.get("recipe_id", type=int)
        multiplier_raw = request.form.get("multiplier", "1")
        added_by = request.form.get("added_by", "").strip()
        # Float multiplier preserves "1.5×" / "0.5×" intent. The
        # list_recipe.multiplier column is REAL; floats are stored
        # as-given and used downstream when aggregating ingredient
        # quantities for the shopping list.
        multiplier = _parse_multiplier_field(
            multiplier_raw, allow_float=True, floor=0.25, cap=MAX_MULTIPLIER
        )
        db = get_db()
        recipe = db.execute(
            "SELECT name FROM recipe WHERE id = ?", (recipe_id,)
        ).fetchone()
        if not recipe:
            flash("Recipe not found.", "error")
            return redirect(url_for("index"))
        db.execute(
            "INSERT INTO list_recipe (recipe_id, multiplier, added_by) "
            "VALUES (?, ?, ?)",
            (recipe_id, multiplier, added_by),
        )
        db.commit()
        _broadcast("list_changed")
        flash(f"Added {recipe['name']} to the shopping list.", "success")
        return redirect(url_for("index", _anchor="list"))

    @app.route("/list/remove-recipe/<int:list_recipe_id>", methods=["POST"])
    def list_remove_recipe(list_recipe_id: int):
        db = get_db()
        db.execute("DELETE FROM list_recipe WHERE id = ?", (list_recipe_id,))
        db.commit()
        _broadcast("list_changed")
        flash("Recipe removed from list.", "success")
        return redirect(url_for("index", _anchor="list"))

    @app.route("/list/add-adhoc", methods=["POST"])
    def list_add_adhoc():
        name = request.form.get("name", "").strip()
        if not name:
            flash("Item name is required.", "error")
            return redirect(url_for("index", _anchor="list"))
        # Explicitly reject 0 / negative quantities instead of silently
        # clamping to 1 — otherwise the user thinks they typed something
        # and the system disagreed.
        quantity_raw = (request.form.get("quantity", "1") or "1").strip()
        try:
            qty_int = int(float(quantity_raw))
        except ValueError:
            qty_int = 1
        if qty_int <= 0:
            flash("Quantity must be 1 or more.", "error")
            return redirect(url_for("index", _anchor="list"))
        qty = min(MAX_QUANTITY, qty_int)
        unit = request.form.get("unit", "").strip()
        # Aisle is auto-classified from the item name. Falls back to "Other"
        # for things the keyword classifier doesn't recognize.
        category = guess_category(name)
        note = request.form.get("note", "").strip()
        added_by = request.form.get("added_by", "").strip()
        db = get_db()
        db.execute(
            "INSERT INTO adhoc_item (name, quantity, unit, category, note, added_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, qty, unit, category, note, added_by),
        )
        # Record the add as a "purchase event" — drives the Quick Add
        # predictor on the home page (recency + frequency scoring).
        db.execute(
            "INSERT INTO purchase_history (name, unit) VALUES (?, ?)",
            (name, unit),
        )
        db.commit()
        _broadcast("list_changed")
        flash(f"Added {name} to the shopping list.", "success")
        return redirect(url_for("index", _anchor="list"))

    @app.route("/list/remove-adhoc/<int:adhoc_id>", methods=["POST"])
    def list_remove_adhoc(adhoc_id: int):
        db = get_db()
        db.execute("DELETE FROM adhoc_item WHERE id = ?", (adhoc_id,))
        db.execute("DELETE FROM checked_item WHERE key = ?", (f"adhoc::{adhoc_id}",))
        db.commit()
        _broadcast("list_changed")
        flash("Item removed.", "success")
        return redirect(url_for("index", _anchor="list"))

    @app.route("/list/toggle", methods=["POST"])
    def list_toggle():
        key = request.json.get("key") if request.is_json else request.form.get("key")
        checked = (
            request.json.get("checked") if request.is_json else request.form.get("checked")
        )
        if not key:
            return jsonify({"ok": False, "error": "missing key"}), 400
        db = get_db()
        if checked in (True, "true", "1", "on"):
            db.execute(
                "INSERT OR IGNORE INTO checked_item (key) VALUES (?)", (key,)
            )
        else:
            db.execute("DELETE FROM checked_item WHERE key = ?", (key,))
        db.commit()
        _broadcast("list_changed")
        return jsonify({"ok": True})

    @app.route("/list/clear", methods=["POST"])
    def list_clear():
        scope = request.form.get("scope", "all")
        db = get_db()
        if scope in ("all", "recipes"):
            db.execute("DELETE FROM list_recipe")
        if scope in ("all", "adhoc"):
            db.execute("DELETE FROM adhoc_item")
        if scope == "checks":
            db.execute("DELETE FROM checked_item")
        else:
            db.execute("DELETE FROM checked_item")
        db.commit()
        _broadcast("list_changed")
        flash("Shopping list cleared.", "success")
        return redirect(url_for("index"))

    # ---- Helpers --------------------------------------------------------

    def _save_recipe(recipe_id: int | None):
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        instructions = request.form.get("instructions", "").strip()
        source_url = request.form.get("source_url", "").strip()
        image_url = _validate_image_url(request.form.get("image_url", ""))
        # An uploaded file (e.g., taken on phone) takes precedence over a
        # pasted URL, since the user explicitly chose a new image.
        try:
            uploaded = _save_uploaded_image(request.files.get("image_file"))
            if uploaded:
                image_url = uploaded
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(request.referrer or url_for("recipes_page"))
        category_field = request.form.get("category", "").strip()
        notes = request.form.get("notes", "").strip()
        rating_raw = request.form.get("rating", "0").strip() or "0"
        servings_raw = request.form.get("servings", "4").strip() or "4"

        prep_time = _int_field(request.form, "prep_time")
        cook_time = _int_field(request.form, "cook_time")
        total_time = _int_field(request.form, "total_time") or (
            prep_time + cook_time
        )
        try:
            rating = max(0, min(5, int(float(rating_raw))))
        except ValueError:
            rating = 0
        if not name:
            flash("Recipe name is required.", "error")
            return redirect(request.referrer or url_for("recipes_page"))
        try:
            servings = max(1, int(float(servings_raw)))
        except ValueError:
            servings = 4

        db = get_db()
        # Hold the lock across the recipe UPDATE + ingredient DELETE +
        # ingredient re-INSERT so a second request from another family
        # device can't interleave and lose ingredients. The lock is
        # in-process; safe because gunicorn runs 1 worker / 8 threads.
        old_image_url = ""
        with _recipe_save_lock:
            if recipe_id is not None:
                old_row = db.execute(
                    "SELECT image_url FROM recipe WHERE id = ?", (recipe_id,)
                ).fetchone()
                if old_row:
                    old_image_url = old_row["image_url"] or ""
            try:
                if recipe_id is None:
                    cur = db.execute(
                        "INSERT INTO recipe (name, description, servings, instructions, "
                        "source_url, image_url, prep_time, cook_time, total_time, "
                        "category, notes, rating) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            name, description, servings, instructions, source_url,
                            image_url, prep_time, cook_time, total_time,
                            category_field, notes, rating,
                        ),
                    )
                    recipe_id = cur.lastrowid
                else:
                    db.execute(
                        "UPDATE recipe SET name = ?, description = ?, servings = ?, "
                        "instructions = ?, source_url = ?, image_url = ?, "
                        "prep_time = ?, cook_time = ?, total_time = ?, "
                        "category = ?, notes = ?, rating = ? WHERE id = ?",
                        (
                            name, description, servings, instructions, source_url,
                            image_url, prep_time, cook_time, total_time,
                            category_field, notes, rating, recipe_id,
                        ),
                    )
                    db.execute("DELETE FROM ingredient WHERE recipe_id = ?", (recipe_id,))
            except sqlite3.IntegrityError:
                db.rollback()
                flash("A recipe with that name already exists.", "error")
                return redirect(request.referrer or url_for("recipes_page"))

            names = request.form.getlist("ing_name[]")
            qtys = request.form.getlist("ing_qty[]")
            units = request.form.getlist("ing_unit[]")
            cats = request.form.getlist("ing_cat[]")
            notes = request.form.getlist("ing_note[]")
            for i, n in enumerate(names):
                n = (n or "").strip()
                if not n:
                    continue
                try:
                    q = float((qtys[i] if i < len(qtys) else "1").strip() or 1)
                    if q <= 0:
                        q = 1.0
                except ValueError:
                    q = 1.0
                u = (units[i] if i < len(units) else "").strip()
                # Aisle is auto-derived from the ingredient name unless the form
                # explicitly supplies one (e.g., legacy data, future overrides).
                submitted_cat = (cats[i] if i < len(cats) else "").strip()
                c = submitted_cat or guess_category(n)
                note = (notes[i] if i < len(notes) else "").strip()
                db.execute(
                    "INSERT INTO ingredient (recipe_id, name, quantity, unit, category, note) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (recipe_id, n, q, u, c, note),
                )
            db.commit()
            _refresh_recipe_embedding(db, recipe_id)
            db.commit()

        # If the user replaced the image (or removed it), unlink the old
        # uploaded file from disk so we don't accumulate orphans.
        if old_image_url and old_image_url != image_url:
            _delete_uploaded_image_file(
                old_image_url, except_recipe_id=recipe_id
            )
        flash(f"Saved recipe: {name}.", "success")
        return redirect(url_for("recipes_page"))

    # Jinja filter for friendly quantities.
    app.jinja_env.filters["fmtqty"] = format_quantity

    # When a recipe has no image, fall back to a single generic stock graphic
    # so it's instantly obvious which recipes still need a photo.
    def recipe_image(recipe) -> str:
        return recipe["image_url"] or url_for("static", filename="stock-recipe.svg")

    app.jinja_env.globals["recipe_image"] = recipe_image

    # Make sure DB exists.
    init_db()
    # Kick off the nightly auto-backup scheduler. No-op if APScheduler
    # isn't installed or BACKUP_ENABLED=0.
    _start_auto_backup_scheduler()
    return app


app = create_app()


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    app.run(host=host, port=port, debug=bool(os.environ.get("DEBUG")))
