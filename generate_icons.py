"""One-time script to generate the PWA app icons for Family Shopping List.
Run: `python generate_icons.py`. Writes PNGs into static/icons/.
"""
from __future__ import annotations

import math
import os

from PIL import Image, ImageDraw, ImageFont

OUT_DIR = os.path.join(os.path.dirname(__file__), "static", "icons")
os.makedirs(OUT_DIR, exist_ok=True)

# Sizes:
#   - 180: iOS apple-touch-icon
#   - 192/512: Android home-screen + splash
#   - 1024: future-proof, never hurts
SIZES = [180, 192, 512, 1024]

# Brand color (matches the app's botanical kitchen herb-green accent).
BG_TOP = (139, 169, 115)    # #8ba973 — lighter herb green
BG_BOTTOM = (74, 105, 50)   # #4a6932 — deeper herb green


def gradient(w: int, h: int) -> Image.Image:
    img = Image.new("RGB", (w, h), BG_TOP)
    for y in range(h):
        t = y / max(h - 1, 1)
        r = int(BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * t)
        g = int(BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * t)
        b = int(BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * t)
        for x in range(w):
            img.putpixel((x, y), (r, g, b))
    return img


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle((0, 0, size, size), radius, fill=255)
    return mask


def draw_icon(size: int) -> Image.Image:
    """Stockpot with three steam wisps on an herb-green rounded square.
    Mirrors the topbar brand SVG."""
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bg = gradient(size, size).convert("RGBA")
    canvas.paste(bg, (0, 0), rounded_mask(size, int(size * 0.22)))

    d = ImageDraw.Draw(canvas)
    white = (255, 255, 255, 255)
    cream = (254, 243, 199, 255)  # #fef3c7 — used for the steam wisps

    # --- Pot body (rounded rectangle, lower half of canvas) ---
    body_left   = size * 0.21
    body_right  = size * 0.79
    body_top    = size * 0.52
    body_bottom = size * 0.83
    body_radius = int(size * 0.055)
    d.rounded_rectangle(
        (body_left, body_top, body_right, body_bottom),
        radius=body_radius, fill=white,
    )

    # --- Rim (slightly wider strip just above the body) ---
    rim_left   = size * 0.17
    rim_right  = size * 0.83
    rim_top    = size * 0.46
    rim_bottom = size * 0.53
    rim_radius = int(size * 0.018)
    d.rounded_rectangle(
        (rim_left, rim_top, rim_right, rim_bottom),
        radius=rim_radius, fill=white,
    )

    # --- Handle stubs flanking the rim ---
    handle_h = size * 0.055
    handle_cy = (rim_top + rim_bottom) / 2
    handle_top = handle_cy - handle_h / 2
    handle_bot = handle_cy + handle_h / 2
    handle_radius = int(handle_h / 2)
    # Left
    d.rounded_rectangle(
        (size * 0.085, handle_top, size * 0.175, handle_bot),
        radius=handle_radius, fill=white,
    )
    # Right
    d.rounded_rectangle(
        (size * 0.825, handle_top, size * 0.915, handle_bot),
        radius=handle_radius, fill=white,
    )

    # --- Three steam wisps rising above the rim ---
    # Each wisp is a sampled "S" curve (one full sine cycle vertically)
    # rendered as a smoothed wide line, with filled circles at the ends
    # to round the caps (Pillow's line doesn't guarantee round caps).
    steam_width = max(3, int(size * 0.022))
    cap_r = steam_width / 2
    centers = [0.32, 0.50, 0.68]   # x-positions of the three wisps
    y_top = size * 0.08
    y_bot = size * 0.42
    amp = size * 0.038
    n_steps = 60
    for cx_pct in centers:
        cx = size * cx_pct
        pts = []
        for i in range(n_steps + 1):
            t = i / n_steps
            y = y_top + t * (y_bot - y_top)
            x = cx + amp * math.sin(t * math.pi * 2)
            pts.append((x, y))
        d.line(pts, fill=cream, width=steam_width, joint="curve")
        # Rounded caps at both ends.
        for (cx_e, cy_e) in (pts[0], pts[-1]):
            d.ellipse(
                (cx_e - cap_r, cy_e - cap_r, cx_e + cap_r, cy_e + cap_r),
                fill=cream,
            )
    return canvas


def main() -> None:
    for s in SIZES:
        img = draw_icon(s)
        out = os.path.join(OUT_DIR, f"icon-{s}.png")
        img.save(out, "PNG", optimize=True)
        print(f"wrote {out}  ({img.size[0]}x{img.size[1]})")

    # Maskable icon: a 512px version with safe-zone padding so Android can
    # crop it into circles/squircles without cutting off the pot.
    base = draw_icon(512)
    canvas = Image.new("RGBA", (512, 512), (107, 140, 74, 255))  # #6b8c4a — accent herb green
    inset = int(512 * 0.10)
    shrunk = base.resize((512 - inset * 2, 512 - inset * 2), Image.LANCZOS)
    canvas.paste(shrunk, (inset, inset), shrunk)
    out = os.path.join(OUT_DIR, "icon-maskable-512.png")
    canvas.save(out, "PNG", optimize=True)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
