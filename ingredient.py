"""Ingredient string parsing, unit normalization, and aisle classification.

Pure-Python utilities with no Flask or database dependencies — exercised
by the URL importer, the photo OCR pipeline, the recipe save handler, and
the shopping-list aggregator. Extracted from app.py for testability.
"""
from __future__ import annotations

import re
from fractions import Fraction


# Aisle categories used to group items in the shopping list view.
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


# Units that share a physical dimension can be aggregated even if recipes
# stored them differently — "1 cup butter" + "8 tbsp butter" should land
# on the shopping list as one row, not two. Each entry maps a normalized
# unit (post-UNIT_ALIASES) to (dimension, factor_to_base_unit).
#
# Bases:
#   volume → milliliters (mL)
#   mass   → grams (g)
#
# "oz" is treated as mass here. Recipe convention is that "fl oz" is
# volume; bare "oz" usually means weight (cheese, meat). We only
# canonicalize when the unit appears in this map; everything else stays
# in its native form so recipes using "stick", "clove", "can", or no
# unit at all continue to aggregate by exact-match.
UNIT_DIMENSIONS: dict[str, tuple[str, float]] = {
    # Volume → mL
    "tsp":   ("volume",   4.92892),
    "tbsp":  ("volume",  14.78676),
    "cup":   ("volume", 236.588),
    "ml":    ("volume",   1.0),
    "l":     ("volume", 1000.0),
    "pint":  ("volume", 473.176),
    "quart": ("volume", 946.353),
    "gallon": ("volume", 3785.41),
    # Mass → g
    "g":     ("mass",       1.0),
    "kg":    ("mass",    1000.0),
    "oz":    ("mass",      28.3495),
    "lb":    ("mass",     453.592),
}

# Display breakpoints: (max_value_in_base_unit, display_unit).
# When the aggregated total in base units is < max_value, render in
# display_unit. Last entry uses inf to catch the tail.
_VOLUME_DISPLAY_BREAKPOINTS: list[tuple[float, str]] = [
    (15.0, "tsp"),       # under one tablespoon (~3 tsp) → tsp
    (180.0, "tbsp"),     # under three-quarters of a cup → tbsp
    (1000.0, "cup"),     # under a liter → cup
    (float("inf"), "l"), # otherwise liters
]
_MASS_DISPLAY_BREAKPOINTS: list[tuple[float, str]] = [
    (28.0, "g"),         # under an ounce → grams
    (1000.0, "oz"),      # under a kilogram → ounces
    (float("inf"), "kg"),
]


def to_canonical_qty(qty: float, unit: str) -> tuple[str, float] | None:
    """If `unit` belongs to a known physical dimension (volume / mass),
    convert `qty` to base units and return (dimension, base_qty).
    Returns None for unknown units — caller should keep the original."""
    u = (unit or "").strip().lower()
    info = UNIT_DIMENSIONS.get(u)
    if info is None:
        return None
    dimension, factor = info
    return dimension, qty * factor


def from_canonical(qty_in_base: float, dimension: str) -> tuple[float, str]:
    """Pick a sensible display unit + quantity for an aggregated total
    expressed in canonical base units. Returns (display_qty, display_unit)."""
    if dimension == "volume":
        breakpoints = _VOLUME_DISPLAY_BREAKPOINTS
    elif dimension == "mass":
        breakpoints = _MASS_DISPLAY_BREAKPOINTS
    else:
        return qty_in_base, ""
    for max_val, display_unit in breakpoints:
        if qty_in_base < max_val:
            _, target_factor = UNIT_DIMENSIONS[display_unit]
            return qty_in_base / target_factor, display_unit
    # Safety net — shouldn't reach here because the last breakpoint is inf.
    last_unit = breakpoints[-1][1]
    return qty_in_base / UNIT_DIMENSIONS[last_unit][1], last_unit


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
    text = f"{qty:.2f}".rstrip("0").rstrip(".")
    return text


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


# Unicode spaces that arrive from Word / email pastes and break parsing.
# Vulgar fractions (½ ⅓ ¼ etc.) are preserved deliberately — the regex
# matches them as single characters.
#      NO-BREAK SPACE
#      FIGURE SPACE
#      THIN SPACE
#      NARROW NO-BREAK SPACE
_UNICODE_SPACES_RE = re.compile("[    ]")


_QTY_MODIFIER_RE = re.compile(
    r"^\s*(?:scant|about|approximately|approx\.?|roughly|heaping|"
    r"generous|generously|rounded|good|big|small|large|tiny)\s+",
    re.IGNORECASE,
)
# Trailing parenthetical at the end of an ingredient — "(1/2 medium)",
# "(or 1 cup goat cheese)" — meant as a note to the cook, not part of
# the ingredient name. Captured even when the closing paren is missing
# (recipe-scrapers occasionally truncates).
_TRAILING_PAREN_RE = re.compile(r"\s*\(([^)]{2,})\)?\s*$")

# Trailing prepositional / qualifier phrases meaning "this isn't really
# a measured ingredient" — keep the ingredient, capture the qualifier.
# Anchored at end of string so we don't accidentally clip the middle of
# names like "for the cake".
_TRAILING_QUALIFIER_RE = re.compile(
    r",?\s+("
    r"to\s+taste|"
    r"as\s+needed|"
    r"for\s+serving|"
    r"for\s+garnish|"
    r"for\s+drizzling|"
    r"for\s+dusting|"
    r"for\s+sprinkling|"
    r"if\s+desired|"
    r"optional|"
    r"plus\s+more\s+(?:for\s+\w+|to\s+taste)|"
    r"divided"
    r")\s*$",
    re.IGNORECASE,
)

# Mid-string parenthetical that immediately follows the quantity — the
# "(15 oz)" in "1 (15 oz) can crushed tomatoes" describes the package
# size, not the quantity itself. Becomes a note.
_POST_QTY_PAREN_RE = re.compile(r"^\s*\(([^)]+)\)\s+")


def parse_ingredient(line: str) -> dict:
    """Parse a free-form ingredient string like '1 1/2 cups flour, sifted'
    into {name, quantity, unit, note}. Best-effort; the user can fix up
    anything that looks off in the edit form."""
    original = line.strip()
    if not original:
        return {"name": "", "quantity": 1.0, "unit": "", "note": ""}
    # Replace odd Unicode space characters with a regular ASCII space so
    # paste-from-Word strings like "1[NBSP]½ cups" parse cleanly.
    original = _UNICODE_SPACES_RE.sub(" ", original)
    # Strip a leading bullet/dash decoration from photo OCR or pasted lists.
    # Must happen before quantity parsing — otherwise "- 1 pound" yields no qty.
    original = re.sub(r"^[-*•·–—]+\s*", "", original)

    # Capture modifier prefix as a note, then strip it so the quantity
    # parser sees "½ teaspoon kosher salt" instead of "Scant ½ teaspoon
    # kosher salt". Common in real-world recipes.
    leading_modifier = ""
    mod_match = _QTY_MODIFIER_RE.match(original)
    if mod_match:
        leading_modifier = mod_match.group(0).strip().rstrip(",").lower()
        original = original[mod_match.end():]

    # Capture trailing parenthetical as a note. Done before quantity
    # parsing so "1 small garlic clove (1/2 medium)" doesn't drag the
    # "(1/2 medium)" into the name field.
    trailing_note = ""
    trail_match = _TRAILING_PAREN_RE.search(original)
    if trail_match:
        trailing_note = trail_match.group(1).strip()
        original = original[:trail_match.start()].rstrip()

    # Capture trailing qualifier phrases ("to taste", "for serving",
    # etc.) as a note. Same reason — they aren't part of the ingredient
    # name and clutter the shopping list.
    trailing_qualifier = ""
    qual_match = _TRAILING_QUALIFIER_RE.search(original)
    if qual_match:
        trailing_qualifier = qual_match.group(1).strip().lower()
        original = original[:qual_match.start()].rstrip()

    qty, rest = _parse_quantity_token(original)
    if qty is not None:
        m = re.match(r"^\s*[-–to]+\s*", rest)
        if m:
            rest2 = rest[m.end():]
            qty2, rest3 = _parse_quantity_token(rest2)
            if qty2 is not None:
                qty = qty2
                rest = rest3

    # Strip a parenthetical that sits between the quantity and the rest
    # of the ingredient — "(15 oz)" in "1 (15 oz) can crushed tomatoes"
    # is the package size, not part of the name.
    post_paren_note = ""
    if qty is not None:
        post_match = _POST_QTY_PAREN_RE.match(rest)
        if post_match:
            post_paren_note = post_match.group(1).strip()
            rest = rest[post_match.end():]

    rest = rest.strip()

    note = ""
    if "," in rest:
        head, _, tail = rest.partition(",")
        rest, note = head.strip(), tail.strip()
    # Merge in every note-piece we lifted off elsewhere. Order:
    # leading modifier, post-quantity paren, comma-tail, trailing
    # qualifier, trailing paren.
    note_parts = [p for p in (
        leading_modifier,
        post_paren_note,
        note,
        trailing_qualifier,
        trailing_note,
    ) if p]
    note = "; ".join(note_parts)

    rest = re.sub(r"^of\s+", "", rest, flags=re.I)

    unit = ""
    parts = rest.split(None, 1)
    if parts:
        raw_first = parts[0].rstrip(".")
        # Recipe cards use "T" for tablespoon and "t" for teaspoon — case
        # is the only distinguishing signal, so dispatch before lowercasing.
        case_aliases = {"T": "tbsp", "t": "tsp", "Tb": "tbsp", "Tbs": "tbsp"}
        if raw_first in case_aliases:
            unit = case_aliases[raw_first]
            rest = parts[1] if len(parts) > 1 else ""
        else:
            candidate = raw_first.lower()
            if candidate in UNIT_ALIASES:
                unit = UNIT_ALIASES[candidate]
                rest = parts[1] if len(parts) > 1 else ""
        # Drop a leading "of " that follows the unit ("1 cup of flour").
        rest = re.sub(r"^of\s+", "", rest, flags=re.I)

    name = rest.strip()
    if not name:
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
