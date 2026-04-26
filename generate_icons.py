"""One-time script to generate the PWA app icons for Family Shopping List.
Run: `python generate_icons.py`. Writes PNGs into static/icons/.
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

OUT_DIR = os.path.join(os.path.dirname(__file__), "static", "icons")
os.makedirs(OUT_DIR, exist_ok=True)

# Sizes:
#   - 180: iOS apple-touch-icon
#   - 192/512: Android home-screen + splash
#   - 1024: future-proof, never hurts
SIZES = [180, 192, 512, 1024]

# Brand color (matches the app's accent green).
BG_TOP = (47, 125, 50)      # #2f7d32
BG_BOTTOM = (22, 101, 52)   # #166534


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
    """Stylized fork + spoon on a green rounded square, with a small
    shopping basket grid below them as a nod to the shopping list."""
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bg = gradient(size, size).convert("RGBA")
    canvas.paste(bg, (0, 0), rounded_mask(size, int(size * 0.22)))

    d = ImageDraw.Draw(canvas)
    white = (255, 255, 255, 255)
    cream = (254, 243, 199, 255)  # #fef3c7

    # --- Fork ---
    fx = size * 0.32
    handle_w = size * 0.06
    fork_top = size * 0.18
    fork_bot = size * 0.78
    # tines
    for i, off in enumerate((-0.07, 0.0, 0.07)):
        d.rounded_rectangle(
            (
                fx + off * size - handle_w * 0.35,
                fork_top,
                fx + off * size + handle_w * 0.35,
                fork_top + size * 0.18,
            ),
            radius=int(size * 0.025),
            fill=white,
        )
    # tine connector
    d.rounded_rectangle(
        (fx - size * 0.105, fork_top + size * 0.16,
         fx + size * 0.105, fork_top + size * 0.20),
        radius=int(size * 0.02), fill=white,
    )
    # handle
    d.rounded_rectangle(
        (fx - handle_w / 2, fork_top + size * 0.18,
         fx + handle_w / 2, fork_bot),
        radius=int(size * 0.03), fill=white,
    )

    # --- Spoon ---
    sx = size * 0.62
    bowl_top = size * 0.18
    bowl_h = size * 0.26
    d.ellipse(
        (sx - size * 0.10, bowl_top, sx + size * 0.10, bowl_top + bowl_h),
        fill=cream,
    )
    d.rounded_rectangle(
        (sx - handle_w / 2, bowl_top + bowl_h * 0.85,
         sx + handle_w / 2, fork_bot),
        radius=int(size * 0.03), fill=cream,
    )

    # --- Subtle basket grid at the bottom for "shopping" hint ---
    basket_top = size * 0.84
    basket_bot = size * 0.92
    d.rounded_rectangle(
        (size * 0.18, basket_top, size * 0.82, basket_bot),
        radius=int(size * 0.015),
        outline=white, width=max(2, size // 80),
    )
    return canvas


def main() -> None:
    for s in SIZES:
        img = draw_icon(s)
        out = os.path.join(OUT_DIR, f"icon-{s}.png")
        img.save(out, "PNG", optimize=True)
        print(f"wrote {out}  ({img.size[0]}x{img.size[1]})")

    # Maskable icon: a 512px version with safe-zone padding so Android can
    # crop it into circles/squircles without cutting off the fork & spoon.
    base = draw_icon(512)
    canvas = Image.new("RGBA", (512, 512), (47, 125, 50, 255))
    inset = int(512 * 0.10)
    shrunk = base.resize((512 - inset * 2, 512 - inset * 2), Image.LANCZOS)
    canvas.paste(shrunk, (inset, inset), shrunk)
    out = os.path.join(OUT_DIR, "icon-maskable-512.png")
    canvas.save(out, "PNG", optimize=True)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
