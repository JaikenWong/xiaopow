"""Generate XiaoPaw (小爪子) app icons.

Design: rounded-square container with indigo→violet gradient,
white stylized chicken foot print (3 forward toes fanning out,
1 small back spur, central foot pad) with soft inner shadow
+ subtle top highlight. Recognizable at 32px.

Outputs:
  - 32x32.png        (Tauri / Linux tray)
  - 128x128.png      (Tauri / macOS)
  - 128x128@2x.png   (= 256x256, Tauri)
  - icon.icns        (macOS bundle)
  - icon.ico         (Windows bundle)

Run from repo root:
  python3 scripts/generate_app_icons.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ICONS_DIR = Path(__file__).resolve().parents[1] / "desktop" / "src-tauri" / "icons"
SIZE = 1024  # master canvas

# Palette — kept in sync with desktop/src/styles.css :root
BG_TOP = (99, 102, 241)      # #6366f1  --accent
BG_BOTTOM = (139, 92, 246)   # #8b5cf6  violet (gradient end)
PAD_WHITE = (255, 255, 255)
HIGHLIGHT = (255, 255, 255, 22)  # very subtle top sheen
SHADOW = (40, 25, 90, 38)       # subtle bottom shadow


def _vertical_gradient(size: int) -> Image.Image:
    """Indigo → violet top-to-bottom gradient."""
    img = Image.new("RGB", (size, size), BG_TOP)
    px = img.load()
    for y in range(size):
        t = y / (size - 1)
        r = int(BG_TOP[0] * (1 - t) + BG_BOTTOM[0] * t)
        g = int(BG_TOP[1] * (1 - t) + BG_BOTTOM[1] * t)
        b = int(BG_TOP[2] * (1 - t) + BG_BOTTOM[2] * t)
        for x in range(size):
            px[x, y] = (r, g, b)
    return img


def _rounded_mask(size: int, radius: int) -> Image.Image:
    """White rounded-rect mask, used to alpha the background."""
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return mask


def _claw_shape(length: int, base_w: int) -> list[tuple[int, int]]:
    """Tapered claw polygon (base-center at origin, pointing up).

    11-point smooth taper: flat wide base, narrowing gradually through
    the shaft to a softly rounded tip (not knife-sharp).
    """
    return [
        (-base_w // 2, 0),
        (-int(base_w * 0.50), -int(length * 0.18)),
        (-int(base_w * 0.44), -int(length * 0.40)),
        (-int(base_w * 0.34), -int(length * 0.62)),
        (-int(base_w * 0.22), -int(length * 0.82)),
        (-int(base_w * 0.10), -int(length * 0.96)),
        (0, -length),                      # tip (not super sharp)
        (int(base_w * 0.10), -int(length * 0.96)),
        (int(base_w * 0.22), -int(length * 0.82)),
        (int(base_w * 0.34), -int(length * 0.62)),
        (int(base_w * 0.44), -int(length * 0.40)),
        (int(base_w * 0.50), -int(length * 0.18)),
        (base_w // 2, 0),
    ]


def _draw_claw(layer: Image.Image, origin_xy: tuple[int, int],
               angle_deg: float, length: int, base_w: int) -> None:
    """Draw one tapered claw on layer, rotated from origin.

    Uses a square local canvas large enough to contain the rotated
    shape for any angle, then paste so the base-center lands at origin_xy.
    """
    # Side length must accommodate the rotated shape's diagonal.
    side = max(2 * length, 2 * base_w) + 2 * base_w
    toe = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    bx = by = side // 2
    # Translate polygon so base-center lands at (bx, by)
    points = [(p[0] + bx, p[1] + by) for p in _claw_shape(length, base_w)]
    ImageDraw.Draw(toe).polygon(points, fill=PAD_WHITE + (255,))
    rotated = toe.rotate(
        angle_deg, resample=Image.BICUBIC, expand=False, center=(bx, by)
    )
    # expand=False keeps canvas size; rotation center (bx, by) maps to the
    # SAME pixel position in the output image (because canvas doesn't grow).
    layer.paste(rotated, (origin_xy[0] - bx, origin_xy[1] - by), rotated)


def _paw_layer(size: int) -> Image.Image:
    """White stylized chicken foot on transparent RGBA.

    Anatomy:
      - 1 horizontal foot pad (the "palm" / ankle joint)
      - 3 forward toes fanning up at -32°/0°/+32°
      - 1 small back spur pointing straight down
    """
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    s = size
    cx = s // 2

    # ── foot pad (horizontal ellipse) ────────────────────────────────────
    pad_w = int(s * 0.17)        # foot pad width
    pad_h = int(s * 0.085)       # foot pad height
    pad_cy = int(s * 0.58)
    d = ImageDraw.Draw(layer)
    d.ellipse(
        (cx - pad_w, pad_cy - pad_h, cx + pad_w, pad_cy + pad_h),
        fill=PAD_WHITE + (255,),
    )

    # ── 3 forward toes ───────────────────────────────────────────────────
    base_origin_y = pad_cy - int(pad_h * 0.20)   # top of pad
    toe_len = int(s * 0.28)        # slightly shorter, less dominant
    toe_bw = int(s * 0.072)        # wider base for friendlier look
    # PIL rotate convention: positive angle moves "up" → "up-left" visually,
    # so left toe uses +angle and right toe uses -angle.
    toes_fwd = [
        # (origin_x_offset, origin_y_offset, angle_deg)
        (-int(s * 0.090), +6,  +34),   # left  (tilts up-left)
        (0,                -6,    0),   # center (straight up)
        (+int(s * 0.090), +6,  -34),   # right (tilts up-right)
    ]
    for dx, dy, ang in toes_fwd:
        _draw_claw(layer, (cx + dx, base_origin_y + dy), ang, toe_len, toe_bw)

    # ── 1 back spur (shorter & thicker for visual balance) ──────────────
    back_origin_y = pad_cy + pad_h + int(s * 0.03)
    _draw_claw(layer, (cx, back_origin_y), 180,
               int(s * 0.14), int(s * 0.058))

    return layer


def _inner_shadow(layer: Image.Image, size: int, offset: int = 10) -> Image.Image:
    """Subtle bottom drop shadow under paw for depth."""
    paw_alpha = layer.split()[3]
    shadow_color_img = Image.new("RGBA", (size, size), SHADOW)
    shadow_only = Image.composite(
        shadow_color_img,
        Image.new("RGBA", (size, size), (0, 0, 0, 0)),
        paw_alpha,
    )
    shadow_only = shadow_only.filter(ImageFilter.GaussianBlur(radius=size * 0.008))
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(shadow_only, (0, offset), shadow_only)
    return canvas


def _top_highlight(size: int) -> Image.Image:
    """Very subtle top sheen — barely visible, just adds dimension."""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse(
        (-int(size * 0.2), -int(size * 0.3),
         int(size * 0.6), int(size * 0.25)),
        fill=HIGHLIGHT,
    )
    return layer.filter(ImageFilter.GaussianBlur(radius=size * 0.06))


def build_master() -> Image.Image:
    size = SIZE
    bg = _vertical_gradient(size)
    mask = _rounded_mask(size, radius=int(size * 0.22))
    bg_rgba = bg.convert("RGBA")
    bg_rgba.putalpha(mask)

    foot = _paw_layer(size)

    # Layer order: bg → shadow → foot → highlight
    composite = Image.alpha_composite(bg_rgba, _inner_shadow(foot, size))
    composite = Image.alpha_composite(composite, foot)
    composite = Image.alpha_composite(composite, _top_highlight(size))
    return composite


def _save_png(img: Image.Image, size: int, out: Path) -> None:
    img.resize((size, size), Image.LANCZOS).save(out, "PNG", optimize=True)
    print(f"  {out.name} ({size}x{size})")


def _save_ico(img: Image.Image, out: Path) -> None:
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save(out, format="ICO", sizes=sizes)
    print(f"  {out.name} (ICO with {len(sizes)} sizes)")


def _save_icns(img: Image.Image, out: Path) -> None:
    # Pillow supports ICNS directly for 1024 master
    img.resize((1024, 1024), Image.LANCZOS).save(out, "ICNS")
    print(f"  {out.name} (ICNS)")


def main() -> int:
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Generating icons → {ICONS_DIR}")
    master = build_master()
    master.save(ICONS_DIR / "_master_preview.png", "PNG")

    _save_png(master, 32, ICONS_DIR / "32x32.png")
    _save_png(master, 128, ICONS_DIR / "128x128.png")
    _save_png(master, 256, ICONS_DIR / "128x128@2x.png")
    _save_ico(master, ICONS_DIR / "icon.ico")
    _save_icns(master, ICONS_DIR / "icon.icns")
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
