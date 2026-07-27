from __future__ import annotations

import shutil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = ROOT / "desktop" / "assets"
ICONSET = ASSET_DIR / "app.iconset"
OUTPUT = ASSET_DIR / "app.png"


def make_base_icon() -> Image.Image:
    size = 1024
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pixels = image.load()
    for y in range(size):
        ratio = y / (size - 1)
        color = (
            int(24 - ratio * 10),
            int(79 - ratio * 27),
            int(68 - ratio * 22),
            255,
        )
        for x in range(size):
            pixels[x, y] = color

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((28, 28, 996, 996), radius=220, fill=255)
    image.putalpha(mask)
    draw = ImageDraw.Draw(image)

    grid_color = (255, 255, 255, 42)
    for offset in (220, 390, 560, 730):
        draw.rounded_rectangle((offset, 370, offset + 92, 655), radius=28, fill=grid_color)
    draw.rounded_rectangle((160, 710, 864, 820), radius=55, fill=(235, 157, 105, 255))
    draw.rounded_rectangle((160, 710, 680, 820), radius=55, fill=(246, 201, 164, 255))

    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 230)
    except OSError:
        font = ImageFont.load_default()
    text = "PL"
    box = draw.textbbox((0, 0), text, font=font)
    text_width = box[2] - box[0]
    draw.text(((size - text_width) / 2, 90), text, font=font, fill=(255, 248, 238, 255))
    return image


def main() -> None:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    if ICONSET.exists():
        shutil.rmtree(ICONSET)
    base = make_base_icon()
    base.save(OUTPUT)
    print(OUTPUT)


if __name__ == "__main__":
    main()
