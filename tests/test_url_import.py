"""Tests for the URL-import hardening helpers in app.py.

Pure-function tests that don't require network. Locks down the
canonicalizer + image magic-byte sniffer so future changes can't
silently regress the dedup / image-baking paths.
"""
import pytest

import app


# ---------------------------------------------------------------------------
# _canonical_source_url
# ---------------------------------------------------------------------------

class TestCanonicalSourceUrl:
    def test_strips_utm_params(self):
        out = app._canonical_source_url(
            "https://example.com/recipe?utm_source=email&utm_campaign=holiday"
        )
        assert out == "https://example.com/recipe"

    def test_strips_fbclid_gclid(self):
        out = app._canonical_source_url(
            "https://example.com/r?fbclid=ABC&gclid=DEF&id=42"
        )
        # Tracking gone, real param kept
        assert "fbclid" not in out
        assert "gclid" not in out
        assert "id=42" in out

    def test_strips_fragment(self):
        out = app._canonical_source_url(
            "https://example.com/recipe#step-3"
        )
        assert "#" not in out
        assert "step-3" not in out

    def test_two_urls_differing_only_in_tracking_canonicalize_equal(self):
        a = app._canonical_source_url(
            "https://x.com/r?utm_source=fb&id=5"
        )
        b = app._canonical_source_url(
            "https://x.com/r?id=5&fbclid=xxx"
        )
        assert a == b

    def test_keeps_real_query_params(self):
        out = app._canonical_source_url(
            "https://example.com/recipe?id=42&category=dessert"
        )
        assert "id=42" in out
        assert "category=dessert" in out

    def test_empty_string(self):
        assert app._canonical_source_url("") == ""

    def test_invalid_url(self):
        # No scheme — return empty (don't crash)
        assert app._canonical_source_url("not a url") == ""

    def test_preserves_scheme_and_host(self):
        out = app._canonical_source_url(
            "https://example.com/recipe?utm_source=foo"
        )
        assert out.startswith("https://example.com/")


# ---------------------------------------------------------------------------
# _sniff_image_format
# ---------------------------------------------------------------------------

class TestSniffImageFormat:
    def test_jpeg(self):
        # SOI marker + JFIF header
        assert app._sniff_image_format(
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01"
        ) == ".jpg"

    def test_png(self):
        # PNG signature
        assert app._sniff_image_format(
            b"\x89PNG\r\n\x1a\nIHDR..."
        ) == ".png"

    def test_webp(self):
        # RIFF...WEBP container
        assert app._sniff_image_format(
            b"RIFF\x00\x00\x00\x00WEBPVP8 "
        ) == ".webp"

    def test_gif87a(self):
        assert app._sniff_image_format(b"GIF87a more bytes") == ".gif"

    def test_gif89a(self):
        assert app._sniff_image_format(b"GIF89a more bytes") == ".gif"

    def test_html_response_rejected(self):
        # Soft-404 HTML page returned for an image URL
        assert app._sniff_image_format(
            b"<!DOCTYPE html><html><body>not an image</body></html>"
        ) is None

    def test_too_short_returns_none(self):
        assert app._sniff_image_format(b"abc") is None

    def test_empty_returns_none(self):
        assert app._sniff_image_format(b"") is None

    def test_garbage_bytes(self):
        assert app._sniff_image_format(b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b") is None


# ---------------------------------------------------------------------------
# _BROWSER_UA constant — sanity check
# ---------------------------------------------------------------------------

class TestBrowserUa:
    def test_is_real_chrome_string(self):
        ua = app._BROWSER_UA
        assert "Mozilla/5.0" in ua
        assert "Chrome/" in ua
        # Don't accidentally tag ourselves as a bot
        for token in ("bot", "crawler", "scraper", "spider"):
            assert token not in ua.lower()
