"""Tests for ingredient.py — parsing, formatting, classification.

These exercise the pure functions that are called from URL imports, OCR
pipeline, recipe save handler, and shopping list aggregation. Locking
them down before any future LLM/embedding refactors touch them.
"""
import pytest

from ingredient import (
    _parse_quantity_token,
    format_quantity,
    guess_category,
    normalize_name,
    normalize_unit,
    parse_ingredient,
)


# ---------------------------------------------------------------------------
# _parse_quantity_token
# ---------------------------------------------------------------------------

class TestParseQuantityToken:
    def test_integer(self):
        assert _parse_quantity_token("1 cup flour") == (1.0, " cup flour")

    def test_decimal(self):
        assert _parse_quantity_token("1.5 cups") == (1.5, " cups")

    def test_simple_fraction(self):
        qty, rest = _parse_quantity_token("1/2 cup")
        assert qty == 0.5
        assert rest == " cup"

    def test_mixed_fraction(self):
        qty, rest = _parse_quantity_token("1 1/2 cups")
        assert qty == 1.5
        assert rest == " cups"

    def test_vulgar_unicode_alone(self):
        qty, rest = _parse_quantity_token("½ cup")
        assert qty == 0.5
        assert rest == " cup"

    def test_vulgar_unicode_with_integer(self):
        qty, rest = _parse_quantity_token("1 ½ cup")
        assert qty == 1.5
        assert rest == " cup"

    def test_no_quantity_returns_none(self):
        assert _parse_quantity_token("abc") == (None, "abc")

    def test_empty(self):
        assert _parse_quantity_token("") == (None, "")

    def test_leading_whitespace_stripped(self):
        qty, rest = _parse_quantity_token("   2 cups")
        assert qty == 2.0

    def test_division_by_zero_safe(self):
        # "1/0" should not raise — should return the original.
        result = _parse_quantity_token("1/0 cup")
        assert result[0] is None


# ---------------------------------------------------------------------------
# format_quantity
# ---------------------------------------------------------------------------

class TestFormatQuantity:
    @pytest.mark.parametrize("qty,expected", [
        (0, ""),
        (-1, ""),
        (1.0, "1"),
        (2.0, "2"),
        (10.0, "10"),
    ])
    def test_whole_numbers(self, qty, expected):
        assert format_quantity(qty) == expected

    @pytest.mark.parametrize("qty,expected", [
        (0.5, "1/2"),
        (0.25, "1/4"),
        (0.75, "3/4"),
        (0.125, "1/8"),
        (0.375, "3/8"),
        (0.625, "5/8"),
        (0.875, "7/8"),
    ])
    def test_eighths_and_quarters(self, qty, expected):
        assert format_quantity(qty) == expected

    @pytest.mark.parametrize("qty,expected", [
        (1 / 3, "1/3"),
        (2 / 3, "2/3"),
        (0.333, "1/3"),
        (0.667, "2/3"),
    ])
    def test_thirds(self, qty, expected):
        assert format_quantity(qty) == expected

    @pytest.mark.parametrize("qty,expected", [
        (1 / 6, "1/6"),
        (5 / 6, "5/6"),
    ])
    def test_sixths(self, qty, expected):
        assert format_quantity(qty) == expected

    @pytest.mark.parametrize("qty,expected", [
        (1.5, "1 1/2"),
        (2.25, "2 1/4"),
        (1 + 1 / 3, "1 1/3"),
        (2 + 2 / 3, "2 2/3"),
    ])
    def test_mixed_numbers(self, qty, expected):
        assert format_quantity(qty) == expected

    @pytest.mark.parametrize("qty", [0.4, 0.6, 0.7])
    def test_non_cooking_fractions_fall_back_to_decimal(self, qty):
        # 0.4 isn't a cooking fraction; should render as decimal not "2/5"
        result = format_quantity(qty)
        assert "/" not in result


# ---------------------------------------------------------------------------
# parse_ingredient
# ---------------------------------------------------------------------------

class TestParseIngredient:
    def test_basic_qty_unit_name(self):
        result = parse_ingredient("1 cup flour")
        assert result == {
            "name": "flour", "quantity": 1.0, "unit": "cup", "note": ""
        }

    def test_fraction(self):
        result = parse_ingredient("1/2 cup flour")
        assert result["quantity"] == 0.5
        assert result["unit"] == "cup"
        assert result["name"] == "flour"

    def test_mixed_fraction(self):
        result = parse_ingredient("1 1/2 cups flour, sifted")
        assert result["quantity"] == 1.5
        assert result["unit"] == "cup"
        assert result["name"] == "flour"
        assert result["note"] == "sifted"

    def test_unicode_vulgar_fraction(self):
        result = parse_ingredient("½ tsp salt")
        assert result["quantity"] == 0.5
        assert result["unit"] == "tsp"
        assert result["name"] == "salt"

    def test_capital_T_means_tablespoon(self):
        result = parse_ingredient("2 T butter")
        assert result["unit"] == "tbsp"
        assert result["name"] == "butter"

    def test_lowercase_t_means_teaspoon(self):
        result = parse_ingredient("1 t pepper")
        assert result["unit"] == "tsp"
        assert result["name"] == "pepper"

    def test_bullet_prefix_stripped(self):
        result = parse_ingredient("- 1 pound chicken breast")
        assert result["quantity"] == 1.0
        assert result["unit"] == "lb"
        assert result["name"] == "chicken breast"

    def test_asterisk_bullet(self):
        result = parse_ingredient("* 2 cups milk")
        assert result["quantity"] == 2.0
        assert result["unit"] == "cup"
        assert result["name"] == "milk"

    def test_em_dash_bullet(self):
        result = parse_ingredient("— 3 onions")
        assert result["quantity"] == 3.0
        assert result["name"] == "onions"

    def test_of_phrase_dropped(self):
        result = parse_ingredient("1 cup of flour")
        assert result["unit"] == "cup"
        assert result["name"] == "flour"

    def test_note_after_comma(self):
        result = parse_ingredient("2 cloves garlic, minced")
        assert result["quantity"] == 2.0
        assert result["unit"] == "clove"
        assert result["name"] == "garlic"
        assert result["note"] == "minced"

    def test_range_takes_larger(self):
        # "1-2 cups" should become 2
        result = parse_ingredient("1-2 cups flour")
        assert result["quantity"] == 2.0

    def test_no_quantity_defaults_to_one(self):
        result = parse_ingredient("salt to taste")
        assert result["quantity"] == 1.0
        assert "salt" in result["name"]

    def test_empty_string(self):
        assert parse_ingredient("") == {
            "name": "", "quantity": 1.0, "unit": "", "note": ""
        }

    def test_unit_with_period_normalized(self):
        # "2 lbs. ground beef" — "lbs." should normalize to "lb"
        result = parse_ingredient("2 lbs. ground beef")
        assert result["unit"] == "lb"
        assert "ground beef" in result["name"]


# ---------------------------------------------------------------------------
# normalize_unit / normalize_name
# ---------------------------------------------------------------------------

class TestNormalize:
    @pytest.mark.parametrize("raw,expected", [
        ("cups", "cup"),
        ("CUP", "cup"),
        ("Tbsp", "tbsp"),
        ("teaspoons", "tsp"),
        ("lbs", "lb"),
        ("pounds", "lb"),
        ("each", ""),
        ("ct", ""),
        ("", ""),
        (None, ""),
        # Unknown units pass through lowercased
        ("smidge", "smidge"),
    ])
    def test_normalize_unit(self, raw, expected):
        assert normalize_unit(raw) == expected

    def test_normalize_name(self):
        assert normalize_name("  Chicken Breast  ") == "chicken breast"


# ---------------------------------------------------------------------------
# guess_category
# ---------------------------------------------------------------------------

class TestGuessCategory:
    @pytest.mark.parametrize("name,category", [
        # Produce
        ("spinach", "Produce"),
        ("yellow onion", "Produce"),
        ("cherry tomato", "Produce"),
        ("avocado", "Produce"),
        # Meat & Seafood
        ("chicken breast", "Meat & Seafood"),
        ("ground beef", "Meat & Seafood"),
        ("salmon fillet", "Meat & Seafood"),
        ("bacon", "Meat & Seafood"),
        # Dairy & Eggs
        ("milk", "Dairy & Eggs"),
        ("cheddar cheese", "Dairy & Eggs"),
        ("eggs", "Dairy & Eggs"),
        ("greek yogurt", "Dairy & Eggs"),
        # Bakery
        ("sourdough bread", "Bakery"),
        ("bagel", "Bakery"),
        # Pantry
        ("flour", "Pantry"),
        ("olive oil", "Pantry"),
        ("crushed tomatoes", "Pantry"),
        ("kosher salt", "Pantry"),
        ("ketchup", "Pantry"),
        # Frozen
        ("frozen pizza", "Frozen"),
        ("popsicle", "Frozen"),
        # Beverages
        ("diet coke", "Beverages"),
        ("coffee", "Beverages"),
        ("kombucha", "Beverages"),
        # Snacks
        ("chips", "Snacks"),
        ("granola bar", "Snacks"),
        # Household
        ("paper towel", "Household"),
        ("dish soap", "Household"),
        # Unknown
        ("xenon widget", "Other"),
        ("", "Other"),
    ])
    def test_categorization(self, name, category):
        assert guess_category(name) == category

    def test_case_insensitive(self):
        assert guess_category("CHICKEN BREAST") == "Meat & Seafood"
        assert guess_category("Spinach") == "Produce"

    # Documented current limitations of the keyword-rule classifier:
    # earlier-listed categories win over later ones when keywords overlap,
    # and \b-anchored matches don't handle plurals. Listed here so future
    # work to upgrade to embedding-based classification can target these.
    def test_known_classifier_limitation_almond_milk_hits_dairy_first(self):
        # Beverages list has "almond milk" but Dairy & Eggs has "milk"
        # earlier in the rules → Dairy wins. To fix: ordered specificity
        # (longer phrases beat shorter) or a real embedding classifier.
        assert guess_category("almond milk") == "Dairy & Eggs"

    def test_known_classifier_limitation_ice_cream_hits_dairy_first(self):
        # "cream" matches in Dairy & Eggs before Frozen sees "ice cream".
        assert guess_category("ice cream") == "Dairy & Eggs"

    def test_known_classifier_limitation_plural_not_handled(self):
        # Bakery has "tortilla" but \btortilla\b doesn't match "tortillas"
        # → falls through to Pantry's "flour".
        assert guess_category("flour tortillas") == "Pantry"
