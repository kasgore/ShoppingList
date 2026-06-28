"""Tests for the Proposed-recipes ranking (app.propose_from_recipes) and the
key-gated web helper. Uses an in-memory DB built straight from db.SCHEMA so
no seeding or DB_PATH juggling is needed."""
import sqlite3

import app as appmod
import db as dbmod
from ingredient import normalize_name


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(dbmod.SCHEMA)
    return c


def _add_recipe(c, name, ingredients):
    rid = c.execute(
        "INSERT INTO recipe (name, servings) VALUES (?, 2)", (name,)
    ).lastrowid
    for nm, cat in ingredients:
        c.execute(
            "INSERT INTO ingredient (recipe_id, name, quantity, unit, category) "
            "VALUES (?, ?, 1, '', ?)",
            (rid, nm, cat),
        )
    return rid


def _add_pantry(c, name, category="Other"):
    c.execute(
        "INSERT INTO pantry_item (name, normalized, category) VALUES (?, ?, ?)",
        (name, normalize_name(name), category),
    )


class TestProposeFromRecipes:
    def test_staples_and_pantry_matches_excluded_from_missing(self):
        c = _conn()
        _add_recipe(c, "Stir Fry", [
            ("Rice", "Dried Goods & Grains"),
            ("Soy Sauce", "Oils & Condiments"),     # staple aisle → ignored
            ("Tofu", "Dried Goods & Grains"),
            ("Salt", "Spices & Seasonings"),         # staple aisle → ignored
        ])
        _add_pantry(c, "Rice- White", "Dried Goods & Grains")  # fuzzy-covers "Rice"
        c.commit()

        (p,) = appmod.propose_from_recipes(c)
        assert p["missing"] == ["Tofu"]   # rice covered; soy sauce & salt are staples
        assert p["total"] == 4
        assert p["have"] == 3

    def test_ranked_fewest_missing_first(self):
        c = _conn()
        _add_recipe(c, "Easy", [("Tofu", "Dried Goods & Grains")])
        _add_recipe(c, "Harder", [
            ("Tofu", "Produce"), ("Carrots", "Produce"), ("Mango", "Produce"),
        ])
        c.commit()
        names = [p["name"] for p in appmod.propose_from_recipes(c)]
        assert names == ["Easy", "Harder"]

    def test_full_coverage_when_everything_matches_or_is_staple(self):
        c = _conn()
        _add_recipe(c, "Toast", [
            ("Bread", "Bread/Wraps"),
            ("Butter", "Dairy & Eggs"),
            ("Cinnamon", "Spices & Seasonings"),  # staple → assumed on hand
        ])
        _add_pantry(c, "Sourdough Bread", "Bread/Wraps")
        _add_pantry(c, "Butter", "Dairy & Eggs")
        c.commit()
        (p,) = appmod.propose_from_recipes(c)
        assert p["missing_count"] == 0
        assert p["coverage"] == 1.0

    def test_recipes_without_ingredients_are_skipped(self):
        c = _conn()
        c.execute("INSERT INTO recipe (name, servings) VALUES ('Empty', 2)")
        c.commit()
        assert appmod.propose_from_recipes(c) == []


class TestProposeFromWeb:
    def test_disabled_without_api_key(self, monkeypatch):
        monkeypatch.delenv("SPOONACULAR_API_KEY", raising=False)
        c = _conn()
        assert appmod.propose_from_web(c) is None
