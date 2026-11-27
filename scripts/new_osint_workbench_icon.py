from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

CANVAS_SIZE = 256
OUTER_SHIELD = (
    (128, 14),
    (225, 48),
    (225, 118),
    (216, 164),
    (186, 207),
    (128, 242),
    (70, 207),
    (40, 164),
    (31, 118),
    (31, 48),
)
INNER_SHIELD = (
    (128, 31),
    (207, 59),
    (207, 118),
    (199, 156),
    (174, 190),
    (128, 222),
    (82, 190),
    (57, 156),
    (49, 118),
    (49, 59),
)


def render_icon() -> Image.Image:
    image = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    draw.polygon(OUTER_SHIELD, fill="#4f6b50", outline="#a5c19d", width=8)
    draw.polygon(INNER_SHIELD, fill="#17251b", outline="#dbe8d6", width=4)

    for radius in (23, 43, 63):
        draw.ellipse(
            (128 - radius, 125 - radius, 128 + radius, 125 + radius),
            outline="#92b58a",
            width=4,
        )
    draw.line((62, 125, 194, 125), fill="#92b58a", width=4)
    draw.line((128, 59, 128, 191), fill="#92b58a", width=4)
    draw.polygon(((128, 125), (155, 58), (194, 83)), fill=(220, 235, 215, 205))
    draw.ellipse((118, 115, 138, 135), fill="#eef5eb")

    for x, y, radius in ((91, 89, 6), (83, 150, 8), (158, 151, 6), (172, 106, 5)):
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill="#d7e9d2")
    return image


def write_icon(destination: Path) -> None:
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    image = render_icon()
    image.save(destination, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate the branded Corvus Windows icon"
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    write_icon(args.output)
    print(f"Generated Corvus icon: {args.output.resolve()}")


if __name__ == "__main__":
    main()
