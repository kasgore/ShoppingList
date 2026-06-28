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
    "Bread/Wraps",
    "Spices & Seasonings",
    "Canned & Jarred",
    "Dried Goods & Grains",
    "Baking",
    "Oils & Condiments",
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
    "piece": "piece",
    "pieces": "piece",
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

# Per-unit "promote to this unit when" rules: (unit, min_v, require_clean).
# `min_v` is the smallest v in this unit that justifies promotion from
# the previous (smaller) one; `require_clean` means the format_quantity()
# rendering must not fall back to a decimal (so "1/4 cup" beats "0.27 cup").
# Empirically tuned to cooking conventions:
#   - tsp→tbsp only at v_tbsp >= 1 (so "2 tsp" stays tsp, not "2/3 tbsp").
#   - tbsp→cup at v_cup >= 1/4 AND clean (so "1/3 cup" beats "5 1/3 tbsp"
#     but "3 tbsp" beats "3/16 cup").
# Liter is left out of volume — US family recipes are cup-centric.
_VOLUME_PROMOTIONS: list[tuple[str, float, bool]] = [
    ("tsp",  0.0,  False),
    ("tbsp", 1.0,  False),
    ("cup",  0.25, True),
]
_MASS_PROMOTIONS: list[tuple[str, float, bool]] = [
    ("g",  0.0,  False),
    ("oz", 1.0,  True),
    ("lb", 0.5,  True),   # 1/2 lb shows as lb; 1/4 lb stays as 4 oz
    ("kg", 1.0,  True),
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


def _is_clean_display(v: float) -> bool:
    """True if format_quantity(v) doesn't fall back to a decimal — i.e.
    it found an integer or a clean fraction form. Used by `from_canonical`
    to avoid promoting to a unit that would render as "0.27 cup"."""
    s = format_quantity(v)
    return bool(s) and "." not in s


def from_canonical(qty_in_base: float, dimension: str) -> tuple[float, str]:
    """Pick a sensible display unit + quantity for an aggregated total
    expressed in canonical base units. Returns (display_qty, display_unit).

    Walks unit candidates smallest → largest, promoting whenever the
    candidate's threshold is met (see _VOLUME_PROMOTIONS / _MASS_PROMOTIONS)."""
    if dimension == "volume":
        promotions = _VOLUME_PROMOTIONS
    elif dimension == "mass":
        promotions = _MASS_PROMOTIONS
    else:
        return qty_in_base, ""

    smallest = promotions[0][0]
    best: tuple[str, float] = (
        smallest, qty_in_base / UNIT_DIMENSIONS[smallest][1]
    )
    for unit, min_v, require_clean in promotions:
        v = qty_in_base / UNIT_DIMENSIONS[unit][1]
        if v < min_v:
            continue
        if require_clean and not _is_clean_display(v):
            continue
        best = (unit, v)
    return best[1], best[0]


# Common adjectives that don't change the shopping identity of an
# ingredient — stripped so "Organic Brown Sugar" merges with "Brown
# Sugar". Curated conservatively: color qualifiers (white/brown sugar)
# and dietary forms (low-sodium/reduced-fat) stay, because they change
# what you'd actually buy. Note the deliberate omissions: 'ground'
# (ground beef ≠ beef), 'shredded' (pre-shredded cheese is a product
# form), and 'frozen' (frozen ≠ fresh at the store) are NOT stripped.
_NAME_QUALIFIER_STRIP_RE = re.compile(
    r"\b("
    # Generic adjectives that don't change shopping identity.
    r"organic|raw|pure|fresh(?:ly)?|"
    # Oils.
    r"extra[- ]virgin|virgin|"
    # Flour / sugar.
    r"all[- ]purpose|granulated|"
    # Size — irrelevant when picking an ingredient off a shopping list.
    # extra-large/small spelled out so the hyphenated form is stripped
    # cleanly instead of leaving "extra-" behind.
    r"extra[- ]large|extra[- ]small|"
    r"large|medium|small|big|little|jumbo|"
    # Prep adjectives — these describe what the cook will do, not what
    # to buy. "Minced garlic" buys the same jar/bulb as "garlic".
    r"minced|chopped|diced|sliced|grated|crushed|"
    r"finely|thinly|coarsely"
    r")\b",
    re.IGNORECASE,
)


# Words ending in 's' that are not plurals — leave alone or we get
# nonsense like "asparagu" / "molass". Extend conservatively when a new
# false positive shows up.
_PLURAL_EXCEPTIONS = frozenset({
    "asparagus", "hummus", "couscous", "molasses", "watercress",
    "brussels", "swiss", "anise",
})


def _singularize_word(word: str) -> str:
    """Best-effort singularization of one word. Errs toward leaving the
    word alone — missed merges are fine, but turning "cheese" into
    "chees" is not. Driven by suffix rules + a small exception set
    rather than a dictionary because the latter would balloon the file
    and still miss brand/variety names users invent."""
    if len(word) <= 3:
        return word
    lw = word.lower()
    if lw in _PLURAL_EXCEPTIONS:
        return word
    # Latin-style endings that look plural but aren't: "us" (asparagus,
    # citrus, hummus), "is" (basis, oasis), "ss" (cress, dress-singular).
    if lw.endswith(("us", "is", "ss")):
        return word
    # "berries" → "berry", "anchovies" → "anchovy".
    if lw.endswith("ies") and len(lw) > 4:
        return word[:-3] + "y"
    # "tomatoes" → "tomato", "dishes" → "dish", "boxes" → "box",
    # "kisses" → "kiss". For other -es words ("olives" → "olive",
    # "chives" → "chive") strip just the trailing 's'.
    if lw.endswith("es") and len(lw) > 4:
        stem = lw[:-2]
        if (
            stem.endswith(("o", "x", "z"))
            or stem.endswith(("ch", "sh", "ss"))
        ):
            return word[:-2]
        return word[:-1]
    if lw.endswith("s"):
        return word[:-1]
    return word


# Functionally interchangeable names that recipes spell differently.
# Applied AFTER qualifier-strip + singularize so the keys here are in
# already-normalized form. Curated narrowly: "garlic paste" really is
# the same shopping item as garlic (you can swap them 1:1 in any
# recipe), but "tomato paste" / "miso paste" / "curry paste" are
# distinct products and stay out of this map.
_NAME_SYNONYMS = {
    "garlic paste": "garlic",
    "ginger paste": "ginger",
}


def normalize_name(name: str) -> str:
    """Lowercase and strip a short list of redundant qualifiers so
    near-duplicates aggregate together on the shopping list. Also
    singularizes per-word ("eggs" → "egg") and applies a tiny synonym
    map so paste/minced forms merge."""
    n = name.strip().lower()
    n = _NAME_QUALIFIER_STRIP_RE.sub("", n)
    n = re.sub(r"\s+", " ", n).strip()
    n = " ".join(_singularize_word(w) for w in n.split())
    return _NAME_SYNONYMS.get(n, n)


# Words that don't identify *what* a food is — packaging/form/prep, sizes,
# colors, units, and connectives — so two names sharing only these don't
# count as a pantry match. Everything left after this filter is a "real"
# food word ("rice", "noodle", "vinegar", "tofu", ...).
_MATCH_STOPWORDS = frozenset({
    # connectives
    "of", "and", "or", "the", "a", "an", "to", "for", "with", "in", "into",
    "plus", "your", "my", "no", "non",
    # packaging / form / prep / state
    "canned", "jarred", "dried", "frozen", "fresh", "whole", "ground",
    "raw", "light", "dark", "mix", "style", "powdered", "instant", "quick",
    "roasted", "smoked", "toasted", "cooked", "shredded", "sweetened",
    "unsweetened", "reduced", "low", "fat", "free",
    # sizes
    "medium", "large", "small", "extra", "mini", "thin", "thick", "regular",
    # colors
    "red", "green", "white", "black", "brown", "yellow", "golden", "purple",
    "orange",
    # units / measures
    "oz", "ounce", "lb", "pound", "cup", "tbsp", "tsp", "tablespoon",
    "teaspoon", "gram", "g", "kg", "ml", "l", "liter", "litre", "can", "jar",
    "bottle", "bag", "pkg", "package", "box", "bunch", "head", "clove",
    "slice", "piece", "pinch", "dash", "count", "ct",
})


def significant_tokens(name: str) -> set[str]:
    """The set of meaningful food words in a name, used for fuzzy pantry
    matching. Normalizes (lowercase, de-qualify, singularize), splits into
    word tokens, then drops stopwords and 1-letter tokens. So "Rice
    Noodles- Medium" and "rice noodles" both reduce to {rice, noodle}, and
    "Vinegar- Apple Cider" matches "apple cider vinegar"."""
    # Re-singularize per token: normalize_name singularizes on whitespace
    # splits, so a token glued to punctuation ("noodles-") slips through.
    toks = (_singularize_word(t) for t in re.findall(r"[a-z0-9]+", normalize_name(name)))
    # Drop 1-letter tokens, pure numbers (embedded quantities like "60" in
    # "60 ml water"), and stopwords — none identify a food.
    return {t for t in toks if len(t) > 1 and not t.isdigit() and t not in _MATCH_STOPWORDS}


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

# Leading size descriptor like "3-inch piece fresh ginger" — without this
# the quantity parser reads "3" as qty and "-Inch Piece Fresh Ginger" as
# the name. Lifts the whole "N-unit" token into a note, then the parser
# sees "piece fresh ginger" and aggregates as 1 piece.
_LEADING_SIZE_RE = re.compile(
    r"^(\d+(?:\.\d+)?\s*[-–]\s*(?:"
    r"inch|in|cm|mm|foot|feet|ft|"
    r"pound|pounds|lb|lbs|"
    r"oz|ounce|ounces|"
    r"gram|grams|g|kg|kilo|kilos|"
    r"liter|liters|l|milliliter|milliliters|ml"
    r")\b)\s*",
    re.IGNORECASE,
)

# Recipe-page footnote pointer that ends up inside a trailing parenthetical
# — "3 medium overripe bananas (Notes 1 and 2)". Dropped from the note
# field; the reference is meaningless once you're at the store.
_FOOTNOTE_NOTE_RE = re.compile(
    r"^\s*(?:notes?|see|footnotes?|step|steps?)\s*\d+"
    r"(?:\s*(?:,|and|&)\s*\d+)*\s*\.?\s*$",
    re.IGNORECASE,
)

# "1 and ½ cups" / "1 and 1/2 cups" — recipe text sometimes spells out
# the connector between the whole part and the fraction. Collapse to a
# single space so _parse_quantity_token reads it as a mixed number.
_AND_FRACTION_RE = re.compile(
    r"(\d)\s+and\s+([" + "".join(VULGAR_FRACTIONS) + r"]|\d+/\d+)",
    re.IGNORECASE,
)


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

    # Lift a leading size descriptor ("3-inch piece fresh ginger") into a
    # note so the quantity parser doesn't read "3" as the count.
    leading_size = ""
    size_match = _LEADING_SIZE_RE.match(original)
    if size_match:
        leading_size = size_match.group(1).strip()
        original = original[size_match.end():]

    # Capture trailing parenthetical as a note. Done before quantity
    # parsing so "1 small garlic clove (1/2 medium)" doesn't drag the
    # "(1/2 medium)" into the name field.
    trailing_note = ""
    trail_match = _TRAILING_PAREN_RE.search(original)
    if trail_match:
        trailing_note = trail_match.group(1).strip()
        original = original[:trail_match.start()].rstrip()
    # Footnote pointers ("Notes 1 and 2", "See 3") that recipe pages
    # sometimes leave inside trailing parens carry no useful info once
    # you're at the store — drop them entirely.
    if trailing_note and _FOOTNOTE_NOTE_RE.match(trailing_note):
        trailing_note = ""

    # Capture trailing qualifier phrases ("to taste", "for serving",
    # etc.) as a note. Same reason — they aren't part of the ingredient
    # name and clutter the shopping list.
    trailing_qualifier = ""
    qual_match = _TRAILING_QUALIFIER_RE.search(original)
    if qual_match:
        trailing_qualifier = qual_match.group(1).strip().lower()
        original = original[:qual_match.start()].rstrip()

    # "1 and ½ cups" → "1 ½ cups" so the mixed-number parser handles it.
    original = _AND_FRACTION_RE.sub(r"\1 \2", original)

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
    # Merge in every note-piece we lifted off elsewhere. Order: leading
    # modifier, leading size descriptor, post-quantity paren, comma-tail,
    # trailing qualifier, trailing paren.
    note_parts = [p for p in (
        leading_modifier,
        leading_size,
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


_CATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("Produce", [
        # Greens & lettuces
        "arugula", "rocket", "lettuce", "romaine", "iceberg", "spinach",
        "kale", "chard", "collard", "endive", "watercress", "frisee",
        "radicchio", "mesclun", "microgreen", "sprouts",
        # Fresh herbs (the dried-by-default ones — oregano, thyme,
        # rosemary, sage, parsley — live under Spices & Seasonings).
        "basil", "cilantro", "mint", "dill", "chive", "scallion",
        "green onion", "leek", "shallot",
        # Cruciferous & alliums
        "broccoli", "cauliflower", "cabbage", "brussels", "bok choy",
        "onion", "garlic", "fennel",
        # Roots & tubers
        "potato", "sweet potato", "yam", "carrot", "beet", "turnip",
        "parsnip", "radish", "ginger", "rutabaga",
        "jicama", "kohlrabi",
        # Squash family
        "zucchini", "squash", "cucumber", "eggplant",
        # Peppers (fresh)
        "bell pepper", "jalapeno", "jalapeño", "serrano", "habanero",
        "poblano", "chili pepper", "chile pepper", "anaheim",
        # Tomatoes (fresh)
        "tomato", "cherry tomato", "grape tomato",
        # Other vegetables
        "celery", "asparagus", "artichoke", "okra", "snap pea",
        "snow pea", "pea pod", "corn on the cob",
        "mushroom", "shiitake", "portobello", "cremini",
        # Fruit
        "apple", "banana", "orange", "lemon", "lime", "grapefruit",
        "berry", "strawberry", "blueberry", "raspberry", "blackberry",
        "grape", "melon", "watermelon", "cantaloupe", "honeydew",
        "peach", "plum", "pear", "pineapple", "mango", "kiwi",
        "papaya", "avocado", "cherry", "apricot", "fig",
        "pomegranate", "fresh herb",
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
    ("Bread/Wraps", [
        "bread", "loaf", "baguette", "ciabatta", "sourdough", "bun",
        "tortilla", "wrap", "flatbread", "pita", "naan", "bagel",
        "roll", "english muffin", "croissant", "biscuit",
    ]),
    ("Frozen", [
        "frozen", "ice cream", "popsicle", "frozen pizza",
        "frozen vegetable", "frozen fruit",
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
        "chip", "tortilla chip", "cracker", "cookie",
        "pretzel", "popcorn", "granola", "granola bar", "trail mix",
        "sunflower seed", "candy", "chocolate bar", "gum",
    ]),
    ("Spices & Seasonings", [
        # Salts & peppers
        "salt", "kosher salt", "sea salt", "black salt", "garlic salt",
        "seasoning salt", "black pepper", "white pepper", "peppercorn",
        "cayenne", "red pepper flake", "red pepper powder", "pepper powder",
        "red chili", "chili powder", "chili flake", "paprika", "smoked paprika",
        "ancho", "kashmiri", "sichuan", "korean red pepper",
        # Whole & ground spices
        "cinnamon", "nutmeg", "cumin", "coriander", "cardamom", "clove",
        "allspice", "all spice", "mace", "star anise", "fenugreek",
        "fennel seed", "caraway", "celery seed", "coriander seed",
        "cumin seed", "mustard seed", "poppy seed", "sesame seed",
        "methi", "kasuri methi", "turmeric", "ground ginger", "ginger- ground",
        # Dried herbs
        "oregano", "thyme", "rosemary", "sage", "parsley", "tarragon",
        "marjoram", "bay leaf", "bay leaves",
        # Blends & rubs
        "italian seasoning", "taco seasoning", "fajita seasoning",
        "old bay", "everything bagel seasoning", "garam masala",
        "chat masala", "curry powder", "five spice", "pumpkin spice",
        "cajun", "creole", "jerk seasoning", "za'atar", "zaatar",
        "tajin", "shawarma", "marinade", "rub", "chicken rub", "pork rub",
        "poultry seasoning", "browning", "chinese five spice",
        # Aromatics & misc
        "garlic powder", "onion powder", "minced onion", "onion soup mix",
        "mushroom powder", "nutritional yeast", "alum",
        "cream of tartar", "cream of tarter", "no-salt seasoning",
        "spice", "seasoning",
    ]),
    ("Canned & Jarred", [
        "canned", "can of", "broth", "stock", "bouillon",
        # Canned tomatoes
        "crushed tomato", "diced tomato", "tomato puree", "tomato paste",
        "tomato sauce", "fire roasted", "sun dried tomato",
        "sundried tomato",
        # Jarred cooking sauces & pastes
        "pizza sauce", "pasta sauce", "marinara", "alfredo",
        "curry paste", "green curry", "butter chicken", "channa masala",
        "tikka masala", "pesto", "basil pesto", "chutney",
        # Canned veg, beans & fruit (the explicitly-canned ones; "canned"
        # above is also force-matched, see guess_category)
        "baked bean", "refried bean", "green bean", "butter bean",
        "lima bean", "roasted red pepper", "hearts of palm",
        "heart of palm", "artichoke heart", "jackfruit", "candied jalapeno",
        "coconut milk", "evaporated milk", "condensed milk",
        "olive", "caper", "pickle", "spam",
    ]),
    ("Dried Goods & Grains", [
        # Pasta & noodles
        "pasta", "spaghetti", "penne", "rigatoni", "fettuccine",
        "linguine", "lasagna noodle", "noodle", "ramen", "udon",
        "angel hair", "rotini", "macaroni", "mac & cheese", "rice noodle",
        "rice paper",
        # Rice & grains
        "rice", "basmati", "jasmine", "arborio", "wild rice", "quinoa",
        "couscous", "barley", "oats", "oatmeal", "farro", "buckwheat",
        "groats", "millet", "bulgur", "potato flake",
        # Legumes & meat substitutes
        "bean", "lentil", "chickpea", "garbanzo", "kidney bean",
        "black bean", "pinto bean", "split pea", "dal", "moong",
        "tofu", "tvp", "soy curl", "seitan",
        # Nuts & seeds
        "almond", "walnut", "pecan", "cashew", "pistachio", "peanut",
        "pine nut", "pumpkin seed", "chia", "flaxseed", "nori",
        # Cereal & other dry goods
        "cereal", "crouton", "stuffing",
    ]),
    ("Baking", [
        # Flours & starches
        "flour", "all-purpose", "bread flour", "cake flour",
        "almond flour", "cassava", "masa", "tapioca", "arrowroot",
        "cornstarch", "corn starch", "cornmeal", "corn meal",
        "potato starch", "vital wheat gluten", "wheat gluten",
        # Sugars & sweeteners
        "sugar", "brown sugar", "powdered sugar", "confectioners",
        "molasses", "corn syrup",
        # Leaveners & flavorings
        "baking powder", "baking soda", "yeast", "cocoa", "cacao",
        "chocolate chip", "vanilla", "vanilla extract", "almond extract",
        # Bits & add-ins
        "shortening", "pancake mix", "raisin", "date", "coconut",
        "shredded coconut", "breadcrumb", "bread crumb", "pea protein",
    ]),
    ("Oils & Condiments", [
        # Oils & vinegars
        "olive oil", "vegetable oil", "canola oil", "sesame oil",
        "avocado oil", "coconut oil", "peanut oil", "oil",
        "vinegar", "balsamic", "rice vinegar", "apple cider vinegar",
        "red wine vinegar", "white vinegar", "vinaigrette", "vinegarette",
        # Sauces & condiments
        "ketchup", "mustard", "mayo", "mayonnaise", "aioli", "soy sauce",
        "worcestershire", "hot sauce", "sriracha", "sambal",
        "chili crisp", "chili paste", "bbq sauce", "barbecue sauce",
        "salsa", "fish sauce", "oyster sauce", "hoisin", "teriyaki",
        "salad dressing", "ranch dressing", "italian dressing",
        "dressing", "tahini", "liquid smoke",
        "honey", "maple syrup", "syrup", "jam", "jelly",
        "peanut butter", "almond butter", "nutella",
    ]),
    ("Household", [
        "paper towel", "toilet paper", "tissue", "kleenex", "napkin",
        "paper plate", "foil", "plastic wrap", "parchment", "ziploc",
        "trash bag", "garbage bag", "dish soap", "laundry detergent",
        "bleach", "sponge", "toothpaste", "toothbrush", "shampoo",
        "conditioner", "soap", "deodorant",
    ]),
]

# Precompiled longest-keyword-first patterns so multi-word phrases win
# over single-word substrings: "baking soda" (Baking) beats "soda"
# (Beverages); "ice cream" (Frozen) beats "cream" (Dairy & Eggs);
# "almond milk" (Beverages) beats "milk" (Dairy & Eggs). Tie-breaking on
# equal-length keywords falls to source order via the stable sort.
#
# A trailing "(?:es|s)?" makes every keyword plural-aware so "bean" matches
# "beans", "lentil" matches "lentils", and "potato"/"tomato" match
# "potatoes"/"tomatoes" — the bulk of how staples are actually written. The
# ranking length is the *base* keyword, so "kidney bean" still beats "bean".
_CLASSIFIER_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b" + re.escape(kw) + r"(?:es|s)?\b", re.IGNORECASE), cat)
    for kw, cat in sorted(
        ((kw, cat) for cat, kws in _CATEGORY_RULES for kw in kws),
        key=lambda x: -len(x[0]),
    )
]

# "Canned" is a deliberate, unambiguous signal — people write "Black Beans-
# Canned", "Tuna- Canned", etc. — so it wins outright over any aisle a
# keyword like "tuna" (Meat) or "black bean" (Dried Goods) would otherwise
# claim. Checked before the keyword scan in guess_category.
_CANNED_RE = re.compile(r"\bcanned\b|\bcan of\b", re.IGNORECASE)


def guess_category(name: str) -> str:
    """Lightweight keyword classifier so imported items don't all default
    to 'Other'. Users can correct anything wrong on the edit page.

    Longest keyword wins, so "baking soda" → Baking (not Beverages via
    "soda"), "ice cream" → Frozen (not Dairy & Eggs via "cream"), and
    "almond milk" → Beverages (not Dairy & Eggs via "milk"). An explicit
    "canned" anywhere in the name short-circuits to Canned & Jarred."""
    if not name:
        return "Other"
    if _CANNED_RE.search(name):
        return "Canned & Jarred"
    for pattern, category in _CLASSIFIER_PATTERNS:
        if pattern.search(name):
            return category
    return "Other"
