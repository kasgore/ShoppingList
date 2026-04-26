"""Family Shopping List - Flask web app for picking recipes and generating
a consolidated shopping list with ad-hoc additions."""
from __future__ import annotations

import os
import re
import secrets
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from fractions import Fraction
from typing import Iterable

from flask import (
    Flask,
    flash,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

# Register HEIC/HEIF support so iPhone photos (the default camera format
# on iOS 11+) open through Pillow without conversion. Optional dep; the
# rest of the app still works for JPEG/PNG/WebP if it's not installed.
try:
    import pillow_heif  # type: ignore[import-untyped]

    pillow_heif.register_heif_opener()
except Exception:
    pass

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("SHOPPINGLIST_DB", os.path.join(APP_DIR, "shoppinglist.db"))
UPLOAD_DIR = os.path.join(APP_DIR, "static", "uploads")
ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic"}
MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # 8 MB

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

CATEGORIES = [
    "Produce",
    "Meat & Seafood",
    "Dairy & Eggs",
    "Bakery",
    "Pantry",
    "Frozen",
    "Beverages",
    "Snacks",
    "Household",
    "Other",
]

# Unit normalization for nicer aggregation. Keys are lower-case.
UNIT_ALIASES = {
    "": "",
    "ea": "",
    "each": "",
    "ct": "",
    "count": "",
    "tsp": "tsp",
    "teaspoon": "tsp",
    "teaspoons": "tsp",
    "tbsp": "tbsp",
    "tablespoon": "tbsp",
    "tablespoons": "tbsp",
    "c": "cup",
    "cup": "cup",
    "cups": "cup",
    "oz": "oz",
    "ounce": "oz",
    "ounces": "oz",
    "lb": "lb",
    "lbs": "lb",
    "pound": "lb",
    "pounds": "lb",
    "g": "g",
    "gram": "g",
    "grams": "g",
    "kg": "kg",
    "ml": "ml",
    "l": "l",
    "liter": "l",
    "liters": "l",
    "pinch": "pinch",
    "dash": "dash",
    "clove": "clove",
    "cloves": "clove",
    "can": "can",
    "cans": "can",
    "jar": "jar",
    "jars": "jar",
    "pkg": "pkg",
    "package": "pkg",
    "packages": "pkg",
    "bottle": "bottle",
    "bottles": "bottle",
    "bunch": "bunch",
    "bunches": "bunch",
    "head": "head",
    "heads": "head",
    "slice": "slice",
    "slices": "slice",
    "stick": "stick",
    "sticks": "stick",
}


def normalize_unit(unit: str | None) -> str:
    if not unit:
        return ""
    return UNIT_ALIASES.get(unit.strip().lower(), unit.strip().lower())


def normalize_name(name: str) -> str:
    return name.strip().lower()


def format_quantity(qty: float) -> str:
    """Format a float quantity as a friendly fraction-aware string.

    Snaps to common cooking fractions in order of simplicity: halves,
    thirds, quarters, sixths, eighths. The first denominator whose
    nearest fraction is within tolerance wins so 0.5 stays "1/2" and
    doesn't become "4/8". 0.333 → "1/3", 0.667 → "2/3", etc.
    """
    if qty <= 0:
        return ""
    whole = int(qty)
    frac = qty - whole
    if abs(frac) < 0.01:
        return str(whole)
    for denom in (2, 3, 4, 6, 8):
        num = round(frac * denom)
        if 1 <= num < denom and abs(frac - num / denom) < 0.02:
            f = Fraction(num, denom)
            if whole:
                return f"{whole} {f.numerator}/{f.denominator}"
            return f"{f.numerator}/{f.denominator}"
    # Fall back to a tidy decimal.
    text = f"{qty:.2f}".rstrip("0").rstrip(".")
    return text


# ---------------------------------------------------------------------------
# Ingredient string parsing (for URL import)
# ---------------------------------------------------------------------------

# Unicode vulgar fractions → decimal value.
VULGAR_FRACTIONS = {
    "½": 0.5, "⅓": 1 / 3, "⅔": 2 / 3, "¼": 0.25, "¾": 0.75,
    "⅕": 0.2, "⅖": 0.4, "⅗": 0.6, "⅘": 0.8,
    "⅙": 1 / 6, "⅚": 5 / 6, "⅛": 0.125, "⅜": 0.375, "⅝": 0.625, "⅞": 0.875,
}

_QTY_TOKEN = re.compile(
    r"^\s*("
    r"\d+\s+\d+/\d+"      # mixed: "1 1/2"
    r"|\d+/\d+"           # fraction: "1/2"
    r"|\d+(?:\.\d+)?"     # decimal: "1" or "1.5"
    r")"
)


def _parse_quantity_token(text: str) -> tuple[float | None, str]:
    """Pull a leading quantity off `text`. Returns (qty, remainder)."""
    text = text.lstrip()
    # Vulgar fraction at start (possibly preceded by integer, e.g., "1 ½").
    m = re.match(r"^(\d+)\s*([" + "".join(VULGAR_FRACTIONS) + r"])", text)
    if m:
        qty = int(m.group(1)) + VULGAR_FRACTIONS[m.group(2)]
        return qty, text[m.end():]
    if text and text[0] in VULGAR_FRACTIONS:
        return VULGAR_FRACTIONS[text[0]], text[1:]
    m = _QTY_TOKEN.match(text)
    if not m:
        return None, text
    raw = m.group(1)
    rest = text[m.end():]
    try:
        if " " in raw:  # mixed
            whole, frac = raw.split()
            n, d = frac.split("/")
            return float(int(whole) + int(n) / int(d)), rest
        if "/" in raw:
            n, d = raw.split("/")
            return int(n) / int(d), rest
        return float(raw), rest
    except (ValueError, ZeroDivisionError):
        return None, text


def parse_ingredient(line: str) -> dict:
    """Parse a free-form ingredient string like '1 1/2 cups flour, sifted'
    into {name, quantity, unit, note}. Best-effort; the user can fix up
    anything that looks off in the edit form."""
    original = line.strip()
    if not original:
        return {"name": "", "quantity": 1.0, "unit": "", "note": ""}
    # Strip a leading bullet/dash decoration from photo OCR or pasted lists.
    # Must happen before quantity parsing — otherwise "- 1 pound" yields no qty.
    original = re.sub(r"^[-*•·–—]+\s*", "", original)

    qty, rest = _parse_quantity_token(original)
    # Sometimes recipes use a range like "1-2 cups" — take the larger.
    if qty is not None:
        m = re.match(r"^\s*[-–to]+\s*", rest)
        if m:
            rest2 = rest[m.end():]
            qty2, rest3 = _parse_quantity_token(rest2)
            if qty2 is not None:
                qty = qty2
                rest = rest3

    rest = rest.strip()

    # Note after a comma is preserved separately.
    note = ""
    if "," in rest:
        head, _, tail = rest.partition(",")
        rest, note = head.strip(), tail.strip()

    # Drop a leading "of " ("1 cup of flour").
    rest = re.sub(r"^of\s+", "", rest, flags=re.I)

    # First word might be a unit.
    unit = ""
    parts = rest.split(None, 1)
    if parts:
        raw_first = parts[0].rstrip(".")
        # Recipe cards use "T" for tablespoon and "t" for teaspoon — the
        # case is the only distinguishing signal, so treat these before
        # lowercasing.
        case_aliases = {"T": "tbsp", "t": "tsp", "Tb": "tbsp", "Tbs": "tbsp"}
        if raw_first in case_aliases:
            unit = case_aliases[raw_first]
            rest = parts[1] if len(parts) > 1 else ""
        else:
            candidate = raw_first.lower()
            if candidate in UNIT_ALIASES:
                unit = UNIT_ALIASES[candidate]
                rest = parts[1] if len(parts) > 1 else ""

    name = rest.strip()
    if not name:
        # Couldn't separate — fall back to the whole line as the name.
        name = original

    return {
        "name": name,
        "quantity": qty if qty is not None else 1.0,
        "unit": unit,
        "note": note,
    }


def guess_category(name: str) -> str:
    """Lightweight keyword classifier so imported items don't all default
    to 'Other'. Users can correct anything wrong on the edit page."""
    n = name.lower()
    rules = [
        ("Produce", [
            # Greens & lettuces
            "arugula", "rocket", "lettuce", "romaine", "iceberg", "spinach",
            "kale", "chard", "collard", "endive", "watercress", "frisee",
            "radicchio", "mesclun", "microgreen", "sprouts",
            # Herbs (fresh)
            "basil", "parsley", "cilantro", "mint", "dill", "thyme",
            "rosemary", "sage", "oregano", "tarragon", "chive", "scallion",
            "green onion", "leek", "shallot",
            # Cruciferous & alliums
            "broccoli", "cauliflower", "cabbage", "brussels", "bok choy",
            "onion", "garlic", "fennel",
            # Roots & tubers
            "potato", "sweet potato", "yam", "carrot", "beet", "turnip",
            "parsnip", "radish", "ginger", "turmeric", "rutabaga",
            "jicama", "kohlrabi",
            # Squash family
            "zucchini", "squash", "pumpkin", "cucumber", "eggplant",
            # Peppers (fresh)
            "bell pepper", "jalapeno", "jalapeño", "serrano", "habanero",
            "poblano", "chili pepper", "chile pepper", "anaheim",
            # Tomatoes (fresh)
            "tomato", "cherry tomato", "grape tomato",
            # Other vegetables
            "celery", "asparagus", "artichoke", "okra", "snap pea",
            "snow pea", "green bean", "pea pod", "corn on the cob",
            "mushroom", "shiitake", "portobello", "cremini",
            # Fruit
            "apple", "banana", "orange", "lemon", "lime", "grapefruit",
            "berry", "strawberry", "blueberry", "raspberry", "blackberry",
            "grape", "melon", "watermelon", "cantaloupe", "honeydew",
            "peach", "plum", "pear", "pineapple", "mango", "kiwi",
            "papaya", "avocado", "cherry", "apricot", "fig", "date",
            "pomegranate", "coconut", "fresh herb",
        ]),
        ("Meat & Seafood", [
            "beef", "steak", "ribeye", "sirloin", "brisket", "filet",
            "ground beef", "ground turkey", "ground pork", "ground chicken",
            "chicken", "drumstick", "thigh", "wing", "breast",
            "pork", "bacon", "ham", "prosciutto", "pancetta", "sausage",
            "chorizo", "kielbasa", "hot dog", "pepperoni", "salami",
            "turkey", "lamb", "veal", "duck",
            "shrimp", "prawn", "fish", "salmon", "tuna", "cod", "tilapia",
            "trout", "halibut", "scallop", "crab", "lobster", "clam",
            "mussel", "oyster", "anchovy", "sardine",
        ]),
        ("Dairy & Eggs", [
            "milk", "buttermilk", "half and half", "half-and-half",
            "cream", "heavy cream", "sour cream", "whipping cream",
            "butter", "ghee", "margarine",
            "cheese", "parmesan", "mozzarella", "cheddar", "gouda",
            "swiss cheese", "feta", "ricotta", "cottage cheese",
            "cream cheese", "blue cheese", "brie", "provolone",
            "asiago", "pepper jack", "monterey jack",
            "yogurt", "greek yogurt", "kefir",
            "egg", "eggs",
        ]),
        ("Bakery", [
            "bread", "loaf", "baguette", "ciabatta", "sourdough", "bun",
            "buns", "tortilla", "pita", "naan", "bagel", "roll", "rolls",
            "english muffin", "croissant", "biscuits",
        ]),
        ("Frozen", [
            "frozen", "ice cream", "popsicle", "frozen pizza",
            "frozen vegetables", "frozen fruit",
        ]),
        ("Beverages", [
            "coke", "diet coke", "pepsi", "soda", "sparkling water",
            "seltzer", "juice", "lemonade", "milk alternative",
            "almond milk", "oat milk", "soy milk", "coconut milk drink",
            "wine", "beer", "cider", "champagne", "prosecco",
            "coffee", "espresso", "tea", "matcha", "kombucha",
            "energy drink", "gatorade", "powerade",
        ]),
        ("Snacks", [
            "chip", "chips", "cracker", "crackers", "cookie", "cookies",
            "pretzel", "popcorn", "granola bar", "trail mix",
            "candy", "chocolate bar", "gum",
        ]),
        ("Pantry", [
            # Baking
            "flour", "all-purpose", "bread flour", "cake flour",
            "sugar", "brown sugar", "powdered sugar", "confectioners",
            "baking powder", "baking soda", "yeast", "cocoa",
            "chocolate chip", "vanilla", "vanilla extract", "almond extract",
            # Seasonings (dry)
            "salt", "kosher salt", "sea salt", "black pepper",
            "white pepper", "peppercorn", "cinnamon", "nutmeg",
            "paprika", "cumin", "coriander", "cardamom", "clove",
            "bay leaf", "red pepper flake", "chili powder",
            "garlic powder", "onion powder", "italian seasoning",
            "taco seasoning", "fajita seasoning", "old bay",
            "everything bagel seasoning", "spice", "seasoning",
            # Oils & vinegars
            "olive oil", "vegetable oil", "canola oil", "sesame oil",
            "avocado oil", "coconut oil", "oil",
            "vinegar", "balsamic", "rice vinegar", "apple cider vinegar",
            # Sauces & condiments
            "ketchup", "mustard", "mayo", "mayonnaise", "soy sauce",
            "worcestershire", "hot sauce", "sriracha", "bbq sauce",
            "barbecue sauce", "salsa", "tomato sauce", "tomato paste",
            "marinara", "pasta sauce", "pesto", "alfredo sauce",
            "fish sauce", "oyster sauce", "hoisin", "teriyaki",
            "salad dressing", "ranch dressing", "italian dressing",
            "honey", "maple syrup", "syrup", "jam", "jelly",
            "peanut butter", "almond butter", "nutella",
            # Grains & legumes
            "pasta", "spaghetti", "penne", "rigatoni", "fettuccine",
            "linguine", "lasagna noodle", "noodle", "ramen", "udon",
            "rice", "basmati", "jasmine", "wild rice", "quinoa",
            "couscous", "barley", "oats", "oatmeal",
            "bean", "lentil", "chickpea", "garbanzo", "kidney bean",
            "black bean", "pinto bean",
            # Canned & jarred
            "canned", "can of", "broth", "stock", "bouillon",
            "crushed tomatoes", "diced tomatoes", "tomato puree",
            "coconut milk", "evaporated milk", "condensed milk",
            "olives", "capers", "pickles",
            # Nuts & seeds
            "almond", "walnut", "pecan", "cashew", "pistachio", "peanut",
            "pine nut", "sunflower seed", "pumpkin seed", "chia",
            "flaxseed", "sesame seed",
            # Misc dry
            "cereal", "granola", "breadcrumb", "croutons", "stuffing",
        ]),
        ("Household", [
            "paper towel", "toilet paper", "tissue", "napkin", "foil",
            "plastic wrap", "parchment", "ziploc", "trash bag",
            "dish soap", "laundry detergent", "bleach", "sponge",
            "toothpaste", "toothbrush", "shampoo", "conditioner",
            "soap", "deodorant",
        ]),
    ]
    for category, words in rules:
        for w in words:
            if re.search(r"\b" + re.escape(w) + r"\b", n):
                return category
    return "Other"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

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
    -- key format:  recipe::<name>::<unit>   or   adhoc::<id>
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
"""


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def close_db(_exc=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
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
        # using the latest keyword rules. Safe to run repeatedly: rows that
        # remain unmatched stay as 'Other'.
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
    """Walk the active list_recipe rows, aggregate ingredients by
    (normalized name, normalized unit), then append ad-hoc items."""
    list_rows = db.execute(
        "SELECT lr.id AS lr_id, lr.multiplier, lr.added_by, "
        "       r.id AS recipe_id, r.name AS recipe_name "
        "FROM list_recipe lr JOIN recipe r ON r.id = lr.recipe_id "
        "ORDER BY r.name"
    ).fetchall()

    # key -> AggregatedItem
    bucket: dict[str, AggregatedItem] = {}

    for lr in list_rows:
        ings = db.execute(
            "SELECT name, quantity, unit, category, note "
            "FROM ingredient WHERE recipe_id = ?",
            (lr["recipe_id"],),
        ).fetchall()
        for ing in ings:
            n_name = normalize_name(ing["name"])
            n_unit = normalize_unit(ing["unit"])
            key = f"recipe::{n_name}::{n_unit}"
            qty = float(ing["quantity"]) * float(lr["multiplier"])
            source_label = lr["recipe_name"]
            if lr["multiplier"] != 1:
                source_label += f" (×{format_quantity(lr['multiplier'])})"
            if key in bucket:
                item = bucket[key]
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


_OCR_INGREDIENT_HEAD_RE = re.compile(
    r"^\s*(?:[-*•·]\s*)?"
    r"(?:\d+(?:[\s./]\d+)?|[½⅓⅔¼¾⅛⅜⅝⅞])"
    r"|^\s*(?:a|an|one|two|three|four|five|six|seven|eight|nine|ten)\b",
    re.IGNORECASE,
)
_OCR_UNIT_HINT_RE = re.compile(
    r"\b(?:cup|cups|c|tsp|teaspoon|tbsp|tablespoon|oz|ounce|lb|pound|"
    r"g|gram|kg|ml|l|liter|litre|pinch|dash|clove|cloves|can|cans|pkg|"
    r"package|stick|sticks|slice|slices|bunch|head|piece)s?\b",
    re.IGNORECASE,
)
_OCR_HEADER_RE = re.compile(
    # Tolerates OCR typos: "Ingrdients", "lngredienta" (l-for-I, vowels
    # dropped, trailing 'a' for 's'). Same for "Directions" → "Dirediono".
    r"^\s*['\"]?\s*"  # optional leading quote/apostrophe noise
    r"([il]ngr[a-z]*ent[a-z]*|"
    r"dire[a-z]*ion[a-z]*|instructions?|method|preparation|steps?|"
    r"notes?|you[' ]?ll need)\s*[:.]?\s*$",
    re.IGNORECASE,
)
# Numbered step like "1.", "1)", "Step 1", "Step 1:".
_OCR_STEP_RE = re.compile(
    r"^\s*(?:step\s+)?\d{1,2}\s*[).:]\s+",
    re.IGNORECASE,
)
# Recipe-card metadata lines we can promote to structured fields.
_OCR_SERVES_RE = re.compile(
    r"\b(?:serves|servings?|yield(?:s)?|makes)\s*[:\-]?\s*(\d+)",
    re.IGNORECASE,
)
_OCR_PREP_RE = re.compile(
    r"\bprep\b[^\d\n]{0,30}(\d+\s*(?:hours?|hrs?|minutes?|mins?|h|m)\b)",
    re.IGNORECASE,
)
_OCR_COOK_RE = re.compile(
    r"\bcook\b[^\d\n]{0,30}(\d+\s*(?:hours?|hrs?|minutes?|mins?|h|m)\b)",
    re.IGNORECASE,
)
_OCR_TOTAL_RE = re.compile(
    r"\btotal\b[^\d\n]{0,30}(\d+\s*(?:hours?|hrs?|minutes?|mins?|h|m)\b)",
    re.IGNORECASE,
)
# Lines that are pure garbage from low-contrast / edge artifacts: a couple
# of stray punctuation chars or single letters with no real content.
_OCR_JUNK_RE = re.compile(r"^[\s\W_]{0,4}$")


def _ocr_minutes_from_phrase(phrase: str) -> int:
    """Convert '1 hr 20 min', '15 min', '30m' etc. to integer minutes."""
    if not phrase:
        return 0
    s = str(phrase).lower()
    hours = re.search(r"(\d+)\s*(?:h|hour|hr)", s)
    mins = re.search(r"(\d+)\s*(?:m|min)", s)
    if hours or mins:
        return (int(hours.group(1)) if hours else 0) * 60 + (
            int(mins.group(1)) if mins else 0
        )
    bare = re.search(r"\d+", s)
    return int(bare.group(0)) if bare else 0


def _clean_ocr_text(raw: str) -> str:
    """Normalize obvious OCR artifacts before parsing.

    Operations:
      - Re-join words split by hyphens at line ends ("flav-\nored" → "flavored").
      - Replace common Tesseract fraction misreads ("Y2" / "‘/2" → "1/2").
      - Replace `+` between letters with `t` — printed cards in some fonts
        consistently have Tesseract reading 't' as '+' ("bu++er" → "butter",
        "+he" → "the"). Skipped between digits to avoid math expressions.
      - Drop lines that are pure whitespace/punctuation, very short
        (≤ 2 chars), or alphabetically sparse (< 25% letters).
      - Collapse runs of 3+ blank lines to one.
    """
    if not raw:
        return ""
    # End-of-line hyphenation: "flav-\nored" → "flavored".
    text = re.sub(r"-\n(?=\w)", "", raw)
    # Fraction misreads.
    text = re.sub(r"\bY2\b", "1/2", text)
    text = re.sub(r"\bY4\b", "1/4", text)
    text = re.sub(r"\bY3\b", "1/3", text)
    # Curly/straight quote in front of a fraction ("‘/2 teaspoon") is the
    # leading "1" — Tesseract regularly mangles serifed 1s into quotes.
    text = re.sub(r"[‘'`´]/\s*2\b", "1/2", text)
    text = re.sub(r"[‘'`´]/\s*4\b", "1/4", text)
    text = re.sub(r"[‘'`´]/\s*3\b", "1/3", text)
    # `+`-for-`t` substitution in word context (cards using fonts with
    # cross-stroke t's confuse Tesseract). Don't touch math-like `digit+digit`.
    def _plus_to_t(m: re.Match) -> str:
        idx = m.start()
        prev = text[idx - 1] if idx > 0 else " "
        nxt = text[idx + 1] if idx + 1 < len(text) else " "
        if prev.isdigit() and nxt.isdigit():
            return "+"
        if prev.isalpha() or nxt.isalpha() or prev == "+" or nxt == "+":
            return "t"
        return "+"
    text = re.sub(r"\+", _plus_to_t, text)

    cleaned_lines: list[str] = []
    for ln in text.splitlines():
        stripped = ln.strip()
        if not stripped:
            cleaned_lines.append("")
            continue
        # Junk filters: short noise, very short lines, alpha-sparse lines.
        if _OCR_JUNK_RE.match(stripped):
            continue
        if len(stripped) <= 2:
            continue
        alpha = sum(1 for c in stripped if c.isalpha())
        if len(stripped) >= 4 and alpha / len(stripped) < 0.25:
            continue
        cleaned_lines.append(stripped)
    # Collapse triple-blank runs.
    out_lines: list[str] = []
    blank_run = 0
    for ln in cleaned_lines:
        if not ln:
            blank_run += 1
            if blank_run > 1:
                continue
        else:
            blank_run = 0
        out_lines.append(ln)
    return "\n".join(out_lines).strip()


def _looks_like_ingredient(line: str) -> bool:
    """Heuristic: does this OCR line look like an ingredient row?

    Matches lines that start with a quantity (digit, fraction, or 'a/an')
    or that mention a common cooking unit. Short lines with neither rarely
    parse cleanly so we treat them as instructions/prose."""
    if not line.strip():
        return False
    if _OCR_INGREDIENT_HEAD_RE.search(line):
        return True
    if _OCR_UNIT_HINT_RE.search(line) and len(line.split()) <= 10:
        return True
    return False


def _is_natural_word(s: str) -> bool:
    """Return True if a letter run looks like a real word.

    "Three", "CHEESE", and "oervinga" pass; "ClC" and "U" do not. Used
    by title detection to reject OCR mixed-case soup that happens to
    contain enough letters to fool a simple alpha-ratio check.
    """
    if len(s) < 3:
        return False
    if s.islower() or s.isupper():
        return True
    if s[0].isupper() and s[1:].islower():
        return True
    return False


def _clean_ocr_title(line: str) -> str:
    """Tidy a candidate title line lifted off a recipe card.

    Strips leading punctuation noise OCR captures from card edges,
    title-cases all-caps banners, and fixes the common ampersand misread
    (`MAC 8: CHEESE` → `Mac & Cheese`) which is fairly safe in title
    context but would be too aggressive in the body of the recipe.
    """
    if not line:
        return ""
    cleaned = re.sub(r"^[^A-Za-z0-9]+", "", line).strip()
    # Card edges often produce a stray single letter followed by a slash
    # or backslash (e.g. "A \\ HOMEMADE …"). Strip that pattern too —
    # safe because real titles don't start with "letter \\".
    cleaned = re.sub(r"^[A-Za-z]\s*[\\/|]+\s*", "", cleaned).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    # Common Tesseract misread on stylized title fonts: "&" → "8:" or "8".
    cleaned = re.sub(r"\b8:\s+", "& ", cleaned)
    cleaned = re.sub(r"(\w)\s+8\s+(\w)", r"\1 & \2", cleaned)
    cleaned = cleaned[:120]
    letters = [c for c in cleaned if c.isalpha()]
    if letters and all(c.isupper() for c in letters):
        cleaned = cleaned.title()
    return cleaned


def _parse_ocr_recipe(text: str) -> dict:
    """Split raw OCR text into structured fields.

    Returns a dict with: title, ingredients (list[str]), instructions (str),
    servings (int|None), prep_time (int min), cook_time (int min),
    total_time (int min). Heuristic — good enough to seed the edit screen.
    """
    cleaned = _clean_ocr_text(text)
    # Keep blank lines this time — they're a strong signal of a section
    # break (the gap between the ingredient block and the instructions).
    cleaned_lines = cleaned.splitlines()
    if not any(ln.strip() for ln in cleaned_lines):
        return {
            "title": "",
            "ingredients": [],
            "instructions": "",
            "servings": None,
            "prep_time": 0,
            "cook_time": 0,
            "total_time": 0,
        }

    # Pull metadata out and remove those lines from the body — they
    # otherwise pollute the instructions block. Preserve blank lines.
    servings = None
    prep_time = 0
    cook_time = 0
    total_time = 0
    body_lines: list[str] = []
    for ln in cleaned_lines:
        if not ln.strip():
            body_lines.append("")
            continue
        m = _OCR_SERVES_RE.search(ln)
        if m and servings is None:
            try:
                servings = int(m.group(1))
            except ValueError:
                pass
            residue = _OCR_SERVES_RE.sub("", ln).strip(" \t·-:|")
            # Don't echo back a bare unit word like "servings" / "serves".
            if residue.lower() in {
                "servings", "serving", "serves", "yield", "yields", "makes"
            }:
                residue = ""
            if residue:
                body_lines.append(residue)
            continue
        m = _OCR_PREP_RE.search(ln)
        if m and not prep_time:
            prep_time = _ocr_minutes_from_phrase(m.group(1))
            continue
        m = _OCR_COOK_RE.search(ln)
        if m and not cook_time:
            cook_time = _ocr_minutes_from_phrase(m.group(1))
            continue
        m = _OCR_TOTAL_RE.search(ln)
        if m and not total_time:
            total_time = _ocr_minutes_from_phrase(m.group(1))
            continue
        body_lines.append(ln)

    if not total_time and (prep_time or cook_time):
        total_time = prep_time + cook_time

    # Title: scan the first few non-blank lines for one that holds at
    # least 2 "natural" words (real-looking letter runs). This rejects
    # OCR mixed-case soup like "ClC(U'0n.l 8-10 oervinga" so the user
    # gets the "Imported Recipe" default instead of gibberish.
    title = ""
    title_idx = -1
    non_blank_count = 0
    MAX_TITLE_LOOKAHEAD = 5
    for i, ln in enumerate(body_lines):
        s = ln.strip()
        if not s:
            continue
        non_blank_count += 1
        if non_blank_count > MAX_TITLE_LOOKAHEAD:
            break
        # If we hit a section header or an ingredient line before finding
        # a usable title, give up — no point hunting further.
        if _OCR_HEADER_RE.match(s) or _looks_like_ingredient(s):
            break
        cleaned_title = _clean_ocr_title(s)
        # Real titles are short. Anything > 60 chars is almost certainly
        # an ingredients line that got jammed onto one OCR row.
        if len(cleaned_title) < 3 or len(cleaned_title) > 60:
            continue
        runs = re.findall(r"[A-Za-z]+", cleaned_title)
        natural = [r for r in runs if _is_natural_word(r)]
        if len(natural) >= 2:
            title = cleaned_title
            title_idx = i
            break
    body = body_lines[title_idx + 1 :] if title_idx >= 0 else body_lines

    section = "pre"  # pre | ing | inst
    ing: list[str] = []
    inst: list[str] = []
    saw_blank = False
    for ln in body:
        if not ln.strip():
            # Blank line: remember it so the next non-blank can decide
            # whether the section is changing.
            saw_blank = True
            continue
        # Explicit section headers (case-insensitive, OCR-typo tolerant).
        header = _OCR_HEADER_RE.match(ln)
        if header:
            label = header.group(1).lower()
            if "ngr" in label or label.startswith("you"):
                section = "ing"
            else:  # directions / instructions / method / steps / notes
                section = "inst"
            saw_blank = False
            continue
        # Numbered/Step lines are unambiguously instructions.
        if _OCR_STEP_RE.match(ln):
            section = "inst"
            inst.append(ln)
            saw_blank = False
            continue
        # Blank-line gap inside the ingredient block: switch to instructions
        # only if the new line looks substantive (≥ 4 words). Single-word
        # decorations like "MoreRecipeat" are dropped instead of bleeding
        # into the instructions field.
        if section == "ing" and saw_blank and not _looks_like_ingredient(ln):
            if len(ln.split()) >= 4:
                section = "inst"
            else:
                saw_blank = False
                continue
        if section == "ing":
            if (
                ing
                and not saw_blank
                and not _looks_like_ingredient(ln)
                and len(ln.split()) <= 4
            ):
                ing[-1] = f"{ing[-1]} {ln}".strip()
            else:
                ing.append(ln)
        elif section == "inst":
            inst.append(ln)
        else:  # section == "pre"
            # Discard pre-section noise. Only promote to "ing" when we
            # actually see an ingredient-shaped line — never let stylized
            # title artifacts or OCR garbage become the instructions.
            if _looks_like_ingredient(ln):
                section = "ing"
                ing.append(ln)
            # else: drop the line entirely
        saw_blank = False

    return {
        "title": title,
        "ingredients": ing,
        "instructions": "\n".join(inst).strip(),
        "servings": servings,
        "prep_time": prep_time,
        "cook_time": cook_time,
        "total_time": total_time,
    }


def _resolve_tesseract_cmd() -> str | None:
    """Find the tesseract binary across dev/prod environments.

    Order of resolution:
      1. TESSERACT_CMD env var — explicit override always wins.
      2. On Windows, probe known winget/installer locations (the binary
         doesn't get added to PATH by the user-scope winget install).
      3. Fall through to None — pytesseract will then call `tesseract`
         from PATH, which is how the Linux container works.
    """
    env = os.environ.get("TESSERACT_CMD")
    if env and os.path.isfile(env):
        return env
    if os.name == "nt":
        candidates = [
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Tesseract-OCR", "tesseract.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Tesseract-OCR", "tesseract.exe"),
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ]
        for path in candidates:
            if path and os.path.isfile(path):
                return path
    return None


def _ocr_image_to_text(disk_path: str) -> str:
    """Run Tesseract on a saved image with preprocessing tuned for phone
    photos of recipe cards. Tries PSM 6 (single block of uniform text)
    first; falls back to PSM 3 (auto) if that fails or returns nothing."""
    import pytesseract
    from PIL import Image, ImageOps

    resolved = _resolve_tesseract_cmd()
    if resolved:
        pytesseract.pytesseract.tesseract_cmd = resolved

    img = Image.open(disk_path)
    # iPhone photos carry orientation in EXIF — apply it before OCR.
    img = ImageOps.exif_transpose(img)
    # Tesseract LSTM does best around 1500–2000px tall. Upscale small
    # images so phone shots and scaled-down uploads both read well.
    target_h = 1800
    if img.height < target_h:
        ratio = target_h / img.height
        img = img.resize(
            (int(img.width * ratio), target_h), Image.LANCZOS
        )
    img = ImageOps.grayscale(img)
    img = ImageOps.autocontrast(img, cutoff=2)

    def _try(psm: int) -> str:
        try:
            return pytesseract.image_to_string(
                img,
                config=f"--oem 1 --psm {psm}",
                timeout=30,
            ) or ""
        except Exception:
            return ""

    primary = _try(6)
    if len(primary.strip()) >= 40:
        return primary
    fallback = _try(3)
    return fallback if len(fallback.strip()) > len(primary.strip()) else primary


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
    return f"/static/uploads/{safe}"


def create_app() -> Flask:
    app = Flask(__name__, instance_path=APP_DIR)
    app.secret_key = os.environ.get("SHOPPINGLIST_SECRET", "family-shopping-dev-key")
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES + 512 * 1024
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
        return render_template(
            "index.html",
            recipes=recipes,
            active_recipes=active_recipes,
            grouped=grouped,
            categories=CATEGORIES,
            total_items=total_items,
            checked_count=checked_count,
        )

    @app.route("/recipes")
    def recipes_page():
        db = get_db()
        q = request.args.get("q", "").strip()
        cat = request.args.get("cat", "").strip()
        favs = request.args.get("favs") == "1"
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
        rows = db.execute(sql, params).fetchall()

        all_categories = [
            row["category"]
            for row in db.execute(
                "SELECT DISTINCT category FROM recipe "
                "WHERE category != '' ORDER BY category"
            ).fetchall()
        ]

        recipes = []
        for r in rows:
            ings = db.execute(
                "SELECT id, name, quantity, unit, category, note "
                "FROM ingredient WHERE recipe_id = ? ORDER BY id",
                (r["id"],),
            ).fetchall()
            recipes.append({"recipe": r, "ingredients": ings})
        return render_template(
            "recipes.html",
            recipes=recipes,
            categories=CATEGORIES,
            recipe_categories=RECIPE_CATEGORIES,
            existing_categories=all_categories,
            q=q, cat=cat, favs=favs,
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

        try:
            from recipe_scrapers import scrape_html, scraper_exists_for
            if scraper_exists_for(url):
                scraper = scrape_me(url)
            else:
                # Unsupported site — fetch HTML and let recipe-scrapers
                # parse schema.org JSON-LD via wild_mode.
                from urllib.request import Request, urlopen
                req = Request(
                    url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (compatible; FamilyShoppingList/1.0)"
                        )
                    },
                )
                with urlopen(req, timeout=15) as resp:
                    html = resp.read().decode(
                        resp.headers.get_content_charset() or "utf-8",
                        errors="replace",
                    )
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

            image_url = _safe(scraper.image, "")
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

        db = get_db()
        # Make the name unique if it collides with an existing recipe.
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
        flash(
            f"Imported \"{title}\" — review categories and units, then save.",
            "success",
        )
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
        db = get_db()
        recipe = db.execute(
            "SELECT id, name, description, servings, instructions, source_url, "
            "image_url, prep_time, cook_time, total_time, category, notes, "
            "is_favorite, rating, nutrition, yields_text, cuisine, author, "
            "source_rating, keywords FROM recipe WHERE id = ?",
            (recipe_id,),
        ).fetchone()
        if recipe is None:
            flash("Recipe not found.", "error")
            return redirect(url_for("recipes_page"))
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
        db = get_db()
        recipe = db.execute(
            "SELECT id, name, description, servings, instructions, source_url, image_url, prep_time, cook_time, total_time, category, notes, is_favorite, rating, nutrition, yields_text, cuisine, author, source_rating, keywords FROM recipe WHERE id = ?",
            (recipe_id,),
        ).fetchone()
        if recipe is None:
            flash("Recipe not found.", "error")
            return redirect(url_for("recipes_page"))
        if request.method == "POST":
            return _save_recipe(recipe_id)
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
        db.execute("DELETE FROM recipe WHERE id = ?", (recipe_id,))
        db.commit()
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

        try:
            multiplier = max(0.1, float(multiplier_raw))
        except ValueError:
            multiplier = 1.0

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

    @app.route("/list/add-recipe", methods=["POST"])
    def list_add_recipe():
        recipe_id = request.form.get("recipe_id", type=int)
        multiplier_raw = request.form.get("multiplier", "1").strip() or "1"
        added_by = request.form.get("added_by", "").strip()
        try:
            multiplier = max(1, int(float(multiplier_raw)))
        except ValueError:
            flash("Multiplier must be a whole number ≥ 1.", "error")
            return redirect(url_for("index"))
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
        flash(f"Added {recipe['name']} to the shopping list.", "success")
        return redirect(url_for("index", _anchor="list"))

    @app.route("/list/remove-recipe/<int:list_recipe_id>", methods=["POST"])
    def list_remove_recipe(list_recipe_id: int):
        db = get_db()
        db.execute("DELETE FROM list_recipe WHERE id = ?", (list_recipe_id,))
        db.commit()
        flash("Recipe removed from list.", "success")
        return redirect(url_for("index", _anchor="list"))

    @app.route("/list/add-adhoc", methods=["POST"])
    def list_add_adhoc():
        name = request.form.get("name", "").strip()
        if not name:
            flash("Item name is required.", "error")
            return redirect(url_for("index", _anchor="list"))
        try:
            qty = max(1, int(float(request.form.get("quantity", "1") or 1)))
        except ValueError:
            qty = 1
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
        db.commit()
        flash(f"Added {name} to the shopping list.", "success")
        return redirect(url_for("index", _anchor="list"))

    @app.route("/list/remove-adhoc/<int:adhoc_id>", methods=["POST"])
    def list_remove_adhoc(adhoc_id: int):
        db = get_db()
        db.execute("DELETE FROM adhoc_item WHERE id = ?", (adhoc_id,))
        db.execute("DELETE FROM checked_item WHERE key = ?", (f"adhoc::{adhoc_id}",))
        db.commit()
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
        flash("Shopping list cleared.", "success")
        return redirect(url_for("index"))

    # ---- Helpers --------------------------------------------------------

    def _save_recipe(recipe_id: int | None):
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        instructions = request.form.get("instructions", "").strip()
        source_url = request.form.get("source_url", "").strip()
        image_url = request.form.get("image_url", "").strip()
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

        def _int_field(name: str) -> int:
            raw = request.form.get(name, "0").strip() or "0"
            try:
                return max(0, int(float(raw)))
            except ValueError:
                return 0

        prep_time = _int_field("prep_time")
        cook_time = _int_field("cook_time")
        total_time = _int_field("total_time") or (prep_time + cook_time)
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
    return app


app = create_app()


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    app.run(host=host, port=port, debug=bool(os.environ.get("DEBUG")))
