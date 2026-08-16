#!/usr/bin/env python3
"""生成 XiaoPaw 应用图标（占位符，可用 `npx tauri icon` 替换为正式图标）。

依赖：Pillow
用法：python3 desktop/scripts/gen_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ICONS_DIR = Path(__file__).resolve().parents[1] / "src-tauri" / "icons"


def make_base(size: int = 1024) -> Image.Image:
    """生成一张简单的爪子主题方形图标。"""
    img = Image.new("RGBA", (size, size), (99, 102, 241, 255))  # 紫色底
    d = ImageDraw.Draw(img)

    # 中心大圆（肉垫）
    pad = size // 5
    d.ellipse([pad, pad, size - pad, size - pad], fill=(255, 255, 255, 255))

    # 四个脚趾（小圆）
    r = size // 7
    positions = [
        (size * 0.30, size * 0.28),
        (size * 0.70, size * 0.28),
        (size * 0.22, size * 0.52),
        (size * 0.78, size * 0.52),
    ]
    for cx, cy in positions:
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 255))

    return img


def main() -> None:
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    base = make_base(1024)

    # PNG 各尺寸
    base.resize((32, 32), Image.LANCZOS).save(ICONS_DIR / "32x32.png")
    base.resize((128, 128), Image.LANCZOS).save(ICONS_DIR / "128x128.png")
    base.resize((256, 256), Image.LANCZOS).save(ICONS_DIR / "128x128@2x.png")

    # Windows ICO
    base.save(ICONS_DIR / "icon.ico", sizes=[(16, 16), (32, 32), (48, 48), (128, 128), (256, 256)])

    # macOS ICNS
    try:
        base.save(ICONS_DIR / "icon.icns")
    except Exception as exc:  # noqa: BLE001
        print(f"icns 生成跳过（需 macOS 环境）：{exc}")

    print(f"✅ 图标已生成：{ICONS_DIR}")


if __name__ == "__main__":
    main()
