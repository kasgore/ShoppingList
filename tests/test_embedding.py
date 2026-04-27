"""Smoke tests for embedding.py — focuses on the pure-Python parts that
don't require fastembed or sqlite-vec to be installed. The model load,
serialization, and search are exercised manually during development;
adding heavy ML dependencies to CI would slow tests by 100x for little
gain (they wrap well-tested third-party libs).
"""
import sqlite3
import pytest

import embedding


def test_is_available_returns_bool():
    # Must not raise even if fastembed / sqlite-vec aren't installed.
    assert isinstance(embedding.is_available(), bool)


def test_setup_extension_safe_on_fresh_conn():
    conn = sqlite3.connect(":memory:")
    # Should never raise — returns False if extension can't be loaded.
    result = embedding.setup_extension(conn)
    assert isinstance(result, bool)
    conn.close()


def test_init_schema_safe_on_fresh_conn():
    conn = sqlite3.connect(":memory:")
    result = embedding.init_schema(conn)
    assert isinstance(result, bool)
    conn.close()


def test_encode_returns_none_when_text_empty():
    # Empty text should not call the model; should return None cleanly.
    assert embedding.encode("") is None
    assert embedding.encode(None) is None


def test_search_returns_list_when_unavailable():
    # When the model can't run, search should return [] instead of raising.
    # (We can't easily force the model unavailable without monkeypatching;
    # this is a minimum-friction sanity check.)
    conn = sqlite3.connect(":memory:")
    result = embedding.search(conn, "")
    assert result == []
    conn.close()


class TestBuildRecipeText:
    def _row(self, **kwargs):
        # Minimal Row-like that supports both `row["col"]` lookup and
        # the `.keys()` membership check that build_recipe_text uses.
        defaults = dict(name="", description="", category="", cuisine="", keywords="")
        defaults.update(kwargs)
        return _RowLike(defaults)

    def test_includes_all_fields(self):
        row = self._row(
            name="Chicken Alfredo",
            description="Creamy weeknight pasta",
            category="Dinner",
            cuisine="Italian",
            keywords="weeknight, pasta, creamy",
        )
        ings = [{"name": "chicken breast"}, {"name": "fettuccine"}]
        text = embedding.build_recipe_text(row, ings)
        assert "Chicken Alfredo" in text
        assert "Creamy weeknight pasta" in text
        assert "Dinner" in text
        assert "Italian" in text
        assert "chicken breast" in text
        assert "fettuccine" in text

    def test_handles_missing_optional_fields(self):
        row = self._row(name="Chicken Alfredo")
        text = embedding.build_recipe_text(row, [])
        assert text == "Chicken Alfredo"

    def test_empty_recipe(self):
        row = self._row()
        assert embedding.build_recipe_text(row, []) == ""


class _RowLike(dict):
    """Stand-in for sqlite3.Row that supports both [] and .keys()."""

    def keys(self):
        return list(super().keys())

    def __getitem__(self, key):
        return dict.__getitem__(self, key)
