"""Tesseract-based OCR pipeline for recipe-card photo imports.

Pure parsing/preprocessing logic split out of app.py so it can be tested
in isolation without spinning up Flask. The route in app.py still owns
file handling and DB writes; this module owns:

  - Image preprocessing tuned for phone photos
  - Tesseract invocation (PSM 6 primary, PSM 3 fallback)
  - Heuristic parsing of OCR text into title / ingredients / instructions
    with metadata extraction (servings, prep/cook/total times)
  - Cross-platform Tesseract binary discovery

HEIC support for iPhone photos is enabled at module load via pillow_heif
when available; the module still works for JPEG/PNG/WebP if it's not.
"""
from __future__ import annotations

import os
import re

# Register HEIC/HEIF support so iPhone photos (default camera format on
# iOS 11+) open through Pillow without conversion. Optional dep.
try:
    import pillow_heif  # type: ignore[import-untyped]

    pillow_heif.register_heif_opener()
except Exception:
    pass


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
    text = re.sub(r"-\n(?=\w)", "", raw)
    text = re.sub(r"\bY2\b", "1/2", text)
    text = re.sub(r"\bY4\b", "1/4", text)
    text = re.sub(r"\bY3\b", "1/3", text)
    text = re.sub(r"[‘'`´]/\s*2\b", "1/2", text)
    text = re.sub(r"[‘'`´]/\s*4\b", "1/4", text)
    text = re.sub(r"[‘'`´]/\s*3\b", "1/3", text)

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
        if _OCR_JUNK_RE.match(stripped):
            continue
        if len(stripped) <= 2:
            continue
        alpha = sum(1 for c in stripped if c.isalpha())
        if len(stripped) >= 4 and alpha / len(stripped) < 0.25:
            continue
        cleaned_lines.append(stripped)
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
    """Heuristic: does this OCR line look like an ingredient row?"""
    if not line.strip():
        return False
    if _OCR_INGREDIENT_HEAD_RE.search(line):
        return True
    if _OCR_UNIT_HINT_RE.search(line) and len(line.split()) <= 10:
        return True
    return False


def _is_natural_word(s: str) -> bool:
    """Return True if a letter run looks like a real word.

    "Three", "CHEESE", and "oervinga" pass; "ClC" and "U" do not. Used by
    title detection to reject OCR mixed-case soup that happens to contain
    enough letters to fool a simple alpha-ratio check.
    """
    if len(s) < 3:
        return False
    if s.islower() or s.isupper():
        return True
    if s[0].isupper() and s[1:].islower():
        return True
    return False


def _clean_ocr_title(line: str) -> str:
    """Tidy a candidate title line lifted off a recipe card."""
    if not line:
        return ""
    cleaned = re.sub(r"^[^A-Za-z0-9]+", "", line).strip()
    cleaned = re.sub(r"^[A-Za-z]\s*[\\/|]+\s*", "", cleaned).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
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
        if _OCR_HEADER_RE.match(s) or _looks_like_ingredient(s):
            break
        cleaned_title = _clean_ocr_title(s)
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
            saw_blank = True
            continue
        header = _OCR_HEADER_RE.match(ln)
        if header:
            label = header.group(1).lower()
            if "ngr" in label or label.startswith("you"):
                section = "ing"
            else:
                section = "inst"
            saw_blank = False
            continue
        if _OCR_STEP_RE.match(ln):
            section = "inst"
            inst.append(ln)
            saw_blank = False
            continue
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
    img = ImageOps.exif_transpose(img)
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
