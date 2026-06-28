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
    significant_tokens,
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

    # Real-world cases from the acouplecooks.com homemade-pizza scrape.
    def test_leading_modifier_scant(self):
        result = parse_ingredient("Scant ½ teaspoon kosher salt")
        assert result["quantity"] == 0.5
        assert result["unit"] == "tsp"
        assert "kosher salt" in result["name"].lower()
        assert "scant" in result["note"].lower()

    def test_leading_modifier_about(self):
        result = parse_ingredient("About 2 cups flour")
        assert result["quantity"] == 2.0
        assert result["unit"] == "cup"
        assert result["name"].lower() == "flour"
        assert "about" in result["note"].lower()

    def test_leading_modifier_heaping(self):
        result = parse_ingredient("Heaping 1 tablespoon sugar")
        assert result["quantity"] == 1.0
        assert result["unit"] == "tbsp"
        assert "heaping" in result["note"].lower()

    def test_trailing_parenthetical_captured_as_note(self):
        result = parse_ingredient("1 small garlic clove (1/2 medium)")
        assert result["quantity"] == 1.0
        # "small" is a leading modifier — captured as note
        assert "garlic clove" in result["name"].lower()
        # The "(1/2 medium)" parenthetical → note
        assert "1/2 medium" in result["note"]

    def test_unclosed_trailing_parenthetical_still_captured(self):
        # recipe-scrapers occasionally truncates; we still treat
        # everything from "(" onward as a note.
        result = parse_ingredient(
            "3/4 cup mozzarella (or 1/2 cup mozzarella and 2 oz goat cheese"
        )
        assert result["quantity"] == 0.75
        assert result["unit"] == "cup"
        assert "mozzarella" in result["name"].lower()
        assert "goat cheese" in result["note"].lower()

    def test_normal_ingredient_no_false_modifier_strip(self):
        # "Goat cheese" starts with "Goat" which is NOT in the modifier
        # list — name shouldn't get clipped.
        result = parse_ingredient("4 oz goat cheese")
        assert result["unit"] == "oz"
        assert result["name"].lower() == "goat cheese"

    def test_post_qty_paren_is_note(self):
        # "1 (15 oz) can crushed tomatoes" — the "(15 oz)" is a package
        # size, not part of the name.
        result = parse_ingredient("1 (15 oz) can crushed tomatoes")
        assert result["quantity"] == 1.0
        assert result["unit"] == "can"
        assert "crushed tomatoes" in result["name"].lower()
        assert "15 oz" in result["note"]

    def test_post_qty_paren_decimal(self):
        result = parse_ingredient("2 (14.5 oz) cans diced tomatoes")
        assert result["quantity"] == 2.0
        assert result["unit"] == "can"
        assert "14.5 oz" in result["note"]

    def test_trailing_to_taste(self):
        result = parse_ingredient("Salt and pepper to taste")
        assert "salt and pepper" in result["name"].lower()
        assert "to taste" in result["note"].lower()

    def test_trailing_for_garnish(self):
        result = parse_ingredient("Fresh parsley for garnish")
        assert result["name"].lower() == "fresh parsley"
        assert "for garnish" in result["note"].lower()

    def test_trailing_for_serving(self):
        result = parse_ingredient("1 lemon, sliced, for serving")
        assert result["quantity"] == 1.0
        assert "lemon" in result["name"].lower()
        # comma-tail picks up "sliced", trailing-qualifier picks up "for serving"
        assert "sliced" in result["note"].lower()
        assert "for serving" in result["note"].lower()

    def test_trailing_optional(self):
        result = parse_ingredient("1 tsp vanilla extract optional")
        assert result["quantity"] == 1.0
        assert result["unit"] == "tsp"
        assert "vanilla extract" in result["name"].lower()
        assert "optional" in result["note"].lower()

    def test_plus_more_for_dusting(self):
        result = parse_ingredient("2 cups flour, plus more for dusting")
        assert result["quantity"] == 2.0
        assert result["unit"] == "cup"
        assert "flour" in result["name"].lower()
        # Either the comma-tail or the trailing-qualifier should catch it
        assert "more" in result["note"].lower() or "dusting" in result["note"].lower()

    def test_ball_unit_left_in_name(self):
        # "ball" isn't in our UNIT_ALIASES so the parser leaves it in the
        # name — acceptable behavior. Locked in by this test so a future
        # "treat ball as a unit" change is intentional.
        result = parse_ingredient("1 ball Best Pizza Dough")
        assert result["quantity"] == 1.0
        assert result["unit"] == ""
        assert "ball" in result["name"].lower()
        assert "pizza dough" in result["name"].lower()

    def test_no_false_qualifier_strip(self):
        # "for the cake" appears in section headers — make sure we don't
        # accidentally strip "for X" anywhere mid-string.
        result = parse_ingredient("4 oz cream cheese")
        assert result["unit"] == "oz"
        assert result["name"].lower() == "cream cheese"
        # "cream cheese" doesn't contain "for serving" etc., so note stays empty
        assert result["note"] == ""


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

    # normalize_name strips a short list of redundant qualifiers so
    # near-duplicates aggregate on the shopping list.
    @pytest.mark.parametrize("raw,expected", [
        ("Organic Brown Sugar", "brown sugar"),
        ("organic brown sugar", "brown sugar"),
        ("Granulated Sugar", "sugar"),
        ("Organic Granulated Sugar", "sugar"),
        ("Raw Honey", "honey"),
        ("Pure Vanilla Extract", "vanilla extract"),
        ("Fresh Basil", "basil"),
        ("Extra-Virgin Olive Oil", "olive oil"),
        ("Extra Virgin Olive Oil", "olive oil"),
        ("Virgin Olive Oil", "olive oil"),
        ("All-Purpose Flour", "flour"),
        ("All Purpose Flour", "flour"),
    ])
    def test_normalize_name_strips_qualifiers(self, raw, expected):
        assert normalize_name(raw) == expected

    # ...but qualifiers that change what you'd actually buy are kept.
    @pytest.mark.parametrize("raw,expected", [
        ("Brown Sugar", "brown sugar"),       # color qualifier stays
        ("White Sugar", "white sugar"),
        ("Low-Sodium Soy Sauce", "low-sodium soy sauce"),
        ("Bread Flour", "bread flour"),       # specific flour type stays
        ("Almond Extract", "almond extract"),  # "extract" stays
        ("Ground Ginger", "ground ginger"),   # form qualifier stays
        ("Shredded Cheddar", "shredded cheddar"),  # product form stays
        ("Frozen Corn", "frozen corn"),       # frozen ≠ fresh
        ("Ground Beef", "ground beef"),       # form qualifier stays
    ])
    def test_normalize_name_keeps_meaningful_qualifiers(self, raw, expected):
        assert normalize_name(raw) == expected

    # Size adjectives are shopping-irrelevant — "large eggs" and "small
    # onion" buy the same product as "eggs" and "onion".
    @pytest.mark.parametrize("raw,expected", [
        ("Large Eggs", "egg"),
        ("Small Onion", "onion"),
        ("Medium Tomato", "tomato"),
        ("Jumbo Shrimp", "shrimp"),
        ("Extra-Large Eggs", "egg"),
        ("Extra Large Eggs", "egg"),
    ])
    def test_normalize_name_strips_size_adjectives(self, raw, expected):
        assert normalize_name(raw) == expected

    # Prep adjectives describe what the cook will do, not what to buy.
    @pytest.mark.parametrize("raw,expected", [
        ("Minced Garlic", "garlic"),
        ("Chopped Fresh Cilantro", "cilantro"),
        ("Diced Red Onion", "red onion"),
        ("Sliced Scallions", "scallion"),
        ("Grated Parmesan Cheese", "parmesan cheese"),
        ("Freshly Grated Parmesan", "parmesan"),
        ("Finely Chopped Onion", "onion"),
        ("Thinly Sliced Almonds", "almond"),
        ("Crushed Red Pepper", "red pepper"),
    ])
    def test_normalize_name_strips_prep_adjectives(self, raw, expected):
        assert normalize_name(raw) == expected

    # Singular/plural normalization so recipes that disagree on form
    # ("egg" vs "eggs") still aggregate on the shopping list.
    @pytest.mark.parametrize("raw,expected", [
        ("Egg", "egg"),
        ("Eggs", "egg"),
        ("Avocados", "avocado"),
        ("Roma Tomatoes", "roma tomato"),
        ("Potatoes", "potato"),
        ("Blueberries", "blueberry"),
        ("Cherries", "cherry"),
        ("Olives", "olive"),
        ("Chives", "chive"),
        ("Anchovies", "anchovy"),
        ("Carrots", "carrot"),
        ("Noodles", "noodle"),
    ])
    def test_normalize_name_singularizes(self, raw, expected):
        assert normalize_name(raw) == expected

    # Words that look plural but aren't — must not be stripped.
    @pytest.mark.parametrize("raw,expected", [
        ("Asparagus", "asparagus"),
        ("Hummus", "hummus"),
        ("Couscous", "couscous"),
        ("Molasses", "molasses"),
        ("Watercress", "watercress"),
        ("Swiss Cheese", "swiss cheese"),
        ("Brussels Sprouts", "brussels sprout"),  # only 'sprouts' is plural
    ])
    def test_normalize_name_singularize_exceptions(self, raw, expected):
        assert normalize_name(raw) == expected

    # Paste forms of garlic/ginger are functionally the same shopping
    # item as the minced or whole form.
    @pytest.mark.parametrize("raw,expected", [
        ("Garlic Paste", "garlic"),
        ("Ginger Paste", "ginger"),
        ("Minced Garlic", "garlic"),
        ("Minced Ginger", "ginger"),
        # "Tomato paste" must NOT collapse — distinct product.
        ("Tomato Paste", "tomato paste"),
    ])
    def test_normalize_name_paste_synonyms(self, raw, expected):
        assert normalize_name(raw) == expected


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
        # Bread/Wraps
        ("sourdough bread", "Bread/Wraps"),
        ("bagel", "Bread/Wraps"),
        ("flour tortillas", "Bread/Wraps"),
        # Baking
        ("flour", "Baking"),
        ("baking powder", "Baking"),
        ("almond flour", "Baking"),
        ("cornstarch", "Baking"),
        # Oils & Condiments
        ("olive oil", "Oils & Condiments"),
        ("ketchup", "Oils & Condiments"),
        ("peanut oil", "Oils & Condiments"),
        # Canned & Jarred
        ("crushed tomatoes", "Canned & Jarred"),
        ("refried beans", "Canned & Jarred"),
        # Spices & Seasonings
        ("kosher salt", "Spices & Seasonings"),
        ("dried oregano", "Spices & Seasonings"),
        # Dried Goods & Grains
        ("spaghetti", "Dried Goods & Grains"),
        ("quinoa", "Dried Goods & Grains"),
        ("dried black lentils", "Dried Goods & Grains"),
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

    # Longest-keyword-first dispatch fixes the rule-ordering surprises
    # that an earlier keyword-rules-in-order classifier suffered from.
    def test_longest_match_almond_milk_wins_beverages(self):
        # "almond milk" (Beverages) beats "milk" (Dairy & Eggs).
        assert guess_category("almond milk") == "Beverages"

    def test_longest_match_ice_cream_wins_frozen(self):
        # "ice cream" (Frozen) beats "cream" (Dairy & Eggs).
        assert guess_category("ice cream") == "Frozen"

    def test_longest_match_baking_soda_wins_baking(self):
        # The bug that prompted this fix: "soda" → Beverages used to
        # eat "baking soda" before the baking keywords got a turn.
        assert guess_category("baking soda") == "Baking"

    def test_plurals_are_matched(self):
        # Keywords are plural-aware, so singular rules catch plural names.
        assert guess_category("black beans") == "Dried Goods & Grains"
        assert guess_category("potatoes") == "Produce"
        assert guess_category("sweet potatoes") == "Produce"

    def test_canned_overrides_keyword_aisle(self):
        # An explicit "canned" wins over the aisle a keyword would pick:
        # "tuna" (Meat) and "black beans" (Dried Goods) both go to cans.
        assert guess_category("Tuna- Canned") == "Canned & Jarred"
        assert guess_category("Black Beans- Canned") == "Canned & Jarred"
        # ...but the un-canned forms keep their natural aisle.
        assert guess_category("tuna steak") == "Meat & Seafood"


# ---------------------------------------------------------------------------
# significant_tokens — fuzzy pantry matching
# ---------------------------------------------------------------------------

class TestSignificantTokens:
    def test_strips_form_size_and_punctuation(self):
        # "Medium" is a size word; the trailing punctuation shouldn't block
        # singularization of "noodles".
        assert significant_tokens("Rice Noodles- Medium") == {"rice", "noodle"}

    def test_matches_reordered_variants(self):
        # The whole point: a pantry staple named differently than the recipe
        # still shares its key food words.
        recipe = significant_tokens("apple cider vinegar")
        pantry = significant_tokens("Vinegar- Apple Cider")
        assert recipe & pantry == {"apple", "cider", "vinegar"}

    def test_singular_plural_overlap(self):
        assert significant_tokens("black beans") & significant_tokens("Black Beans- Canned")

    def test_color_only_overlap_is_not_a_match(self):
        # "black pepper" vs "black beans" share only the color word, which
        # is a stopword — so they must NOT be considered similar.
        assert not (
            significant_tokens("black pepper") & significant_tokens("black beans")
        )

    def test_connectives_and_units_dropped(self):
        assert significant_tokens("1 cup of flour") == {"flour"}


# ---------------------------------------------------------------------------
# parse_ingredient — additional patches
# ---------------------------------------------------------------------------

class TestParseIngredientPatches:
    # "3-inch piece fresh ginger" used to read 3 as the quantity and
    # "-Inch Piece Fresh Ginger" as the name. Now we lift the size token.
    def test_leading_size_inch_piece_ginger(self):
        result = parse_ingredient(
            "3-inch piece fresh ginger, peeled and finely minced"
        )
        # The leading "3" is no longer the quantity — defaults to 1.
        assert result["quantity"] == 1.0
        # "piece" is now a unit alias, so it gets pulled out cleanly.
        assert result["unit"] == "piece"
        # "fresh" is stripped by normalize_name when names are matched,
        # but stays in the raw display name.
        assert "ginger" in result["name"].lower()
        assert "3-inch" in result["note"].lower()
        # The comma-tail descriptor still lands in the note too.
        assert "peeled" in result["note"].lower()

    def test_leading_size_em_dash(self):
        # En-dashes (–) from word-processor pastes also count.
        result = parse_ingredient("2–pound roast")
        assert result["quantity"] == 1.0
        assert "2" in result["note"]
        assert "pound" in result["note"].lower()
        assert "roast" in result["name"].lower()

    def test_dont_confuse_numeric_range_with_size(self):
        # "1-2 cups flour" still parses as a range (qty=2), since "2"
        # isn't a size-unit keyword.
        result = parse_ingredient("1-2 cups flour")
        assert result["quantity"] == 2.0
        assert result["unit"] == "cup"

    # "1 and ½ cups flour" — the connector word "and" used to keep the
    # parser from seeing the mixed number.
    @pytest.mark.parametrize("text,qty,unit", [
        ("1 and ½ cups flour", 1.5, "cup"),
        ("1 and 1/2 cups flour", 1.5, "cup"),
        ("2 and ¾ tablespoons sugar", 2.75, "tbsp"),
    ])
    def test_and_between_number_and_fraction(self, text, qty, unit):
        result = parse_ingredient(text)
        assert result["quantity"] == pytest.approx(qty)
        assert result["unit"] == unit

    # Recipe-page footnote pointers ("Notes 1 and 2", "See 3") used to
    # leak into the note field via the trailing-parenthetical lift.
    @pytest.mark.parametrize("text", [
        "3 medium overripe bananas (Notes 1 and 2)",
        "1 cup flour (Note 1)",
        "2 eggs (see 3)",
        "1 onion (footnote 2)",
    ])
    def test_footnote_only_paren_is_dropped(self, text):
        result = parse_ingredient(text)
        # Whatever else lands in the note, the footnote ref shouldn't.
        n = result["note"].lower()
        assert "note" not in n
        assert "see " not in n
        assert "footnote" not in n

    def test_real_trailing_paren_still_kept(self):
        # The footnote filter shouldn't swallow legitimate parentheticals.
        result = parse_ingredient("1 small garlic clove (1/2 medium)")
        assert "1/2 medium" in result["note"]


# ---------------------------------------------------------------------------
# from_canonical — display unit selection
# ---------------------------------------------------------------------------

from ingredient import from_canonical, to_canonical_qty


class TestFromCanonical:
    def test_third_cup_displays_as_cup_not_tbsp(self):
        # 1/3 cup ≈ 78.86 mL. Old breakpoints picked tbsp → "5 1/3 tbsp".
        # The new selector picks cup → "1/3" of a cup.
        _, base = to_canonical_qty(1 / 3, "cup")  # type: ignore
        qty, unit = from_canonical(base, "volume")
        assert unit == "cup"
        assert qty == pytest.approx(1 / 3, rel=1e-3)

    def test_half_cup_displays_as_cup(self):
        _, base = to_canonical_qty(0.5, "cup")  # type: ignore
        qty, unit = from_canonical(base, "volume")
        assert unit == "cup"
        assert qty == pytest.approx(0.5, rel=1e-3)

    def test_three_tbsp_stays_as_tbsp(self):
        # 3 tbsp = 3/16 cup — the cup display would be awkward, so tbsp
        # is preferred.
        _, base = to_canonical_qty(3, "tbsp")  # type: ignore
        qty, unit = from_canonical(base, "volume")
        assert unit == "tbsp"
        assert qty == pytest.approx(3, rel=1e-3)

    def test_one_cup_displays_as_cup(self):
        _, base = to_canonical_qty(1, "cup")  # type: ignore
        qty, unit = from_canonical(base, "volume")
        assert unit == "cup"

    def test_half_pound_displays_as_lb(self):
        _, base = to_canonical_qty(0.5, "lb")  # type: ignore
        qty, unit = from_canonical(base, "mass")
        assert unit == "lb"
        assert qty == pytest.approx(0.5, rel=1e-3)

    def test_two_oz_displays_as_oz(self):
        _, base = to_canonical_qty(2, "oz")  # type: ignore
        qty, unit = from_canonical(base, "mass")
        assert unit == "oz"

    def test_two_tsp_stays_as_tsp(self):
        # tsp→tbsp only promotes at v_tbsp >= 1, so 2 tsp doesn't become
        # "2/3 tbsp" — a real regression we hit during the redesign.
        _, base = to_canonical_qty(2, "tsp")  # type: ignore
        qty, unit = from_canonical(base, "volume")
        assert unit == "tsp"
        assert qty == pytest.approx(2.0, rel=1e-3)

    def test_one_tsp_stays_as_tsp(self):
        _, base = to_canonical_qty(1, "tsp")  # type: ignore
        qty, unit = from_canonical(base, "volume")
        assert unit == "tsp"

    def test_three_tsp_promotes_to_one_tbsp(self):
        # 3 tsp = 1 tbsp exactly — at the boundary, the tbsp display wins.
        _, base = to_canonical_qty(3, "tsp")  # type: ignore
        qty, unit = from_canonical(base, "volume")
        assert unit == "tbsp"
        assert qty == pytest.approx(1.0, rel=1e-3)

    def test_one_and_a_half_cup_displays_as_cup(self):
        # Mixed numbers in cup still beat the tbsp equivalent — "1 1/2 cup"
        # not "24 tbsp".
        _, base = to_canonical_qty(1.5, "cup")  # type: ignore
        qty, unit = from_canonical(base, "volume")
        assert unit == "cup"
        assert qty == pytest.approx(1.5, rel=1e-3)
