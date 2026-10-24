from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "LEDERG-CRMP-Avatar.ico"
PNG_OUTPUT = ROOT / "LEDERG-CRMP-Cover.png"
SIZE = 512


def make_icon() -> Image.Image:
    image = Image.new("RGBA", (SIZE, SIZE), (5, 5, 8, 255))

    # A restrained red aura beneath the mark.
    glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse((82, 74, 444, 440), fill=(255, 24, 65, 80))
    glow = glow.filter(ImageFilter.GaussianBlur(52))
    image = Image.alpha_composite(image, glow)

    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (22, 22, 490, 490),
        radius=112,
        fill=(13, 13, 18, 255),
        outline=(64, 37, 48, 255),
        width=5,
    )
    draw.rounded_rectangle(
        (34, 34, 478, 478),
        radius=98,
        outline=(255, 255, 255, 15),
        width=2,
    )

    # Angular L + slash signature shared with the launcher cover.
    draw.polygon(
        [(122, 112), (205, 112), (205, 302), (354, 302), (354, 390), (122, 390)],
        fill=(245, 245, 248, 255),
    )
    slash_glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    slash_draw = ImageDraw.Draw(slash_glow)
    slash_draw.polygon(
        [(321, 78), (409, 78), (238, 432), (150, 432)],
        fill=(255, 25, 67, 170),
    )
    slash_glow = slash_glow.filter(ImageFilter.GaussianBlur(16))
    image = Image.alpha_composite(image, slash_glow)

    draw = ImageDraw.Draw(image)
    draw.polygon(
        [(321, 78), (409, 78), (238, 432), (150, 432)],
        fill=(255, 43, 77, 255),
    )
    draw.line((57, 84, 57, 56, 87, 56), fill=(255, 43, 77, 235), width=5)
    draw.line((425, 456, 455, 456, 455, 426), fill=(255, 43, 77, 235), width=5)
    return image


def main() -> None:
    icon = make_icon()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    icon.save(PNG_OUTPUT, format="PNG", optimize=True)
    icon.save(
        OUTPUT,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f"Generated {OUTPUT}")


if __name__ == "__main__":
    main()
