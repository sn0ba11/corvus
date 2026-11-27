"""Generate Corvus Windows branding assets from the approved raven artwork.

Produces a multi-resolution ICO and, optionally, the 24-bit BMPs used by the
NSIS Modern UI wizard (welcome/finish side panel and page header).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SOURCE_MARK = Path(__file__).resolve().parents[1] / "app" / "static" / "corvus-mark.jpg"
ICON_SIZES = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)]
BACKGROUND = (14, 19, 25)
ACCENT = (146, 181, 138)
TEXT = (232, 238, 230)
MUTED = (150, 160, 158)
WIZARD_SIZE = (164, 314)
HEADER_SIZE = (150, 57)


def load_mark() -> Image.Image:
    data = SOURCE_MARK.read_bytes()
    if len(data) < 4 or data[:2] != b"\xff\xd8" or data[-2:] != b"\xff\xd9":
        raise SystemExit(f"Approved Corvus raster mark is not a complete JPEG payload: {SOURCE_MARK}")
    with Image.open(SOURCE_MARK) as image:
        return image.convert("RGB")


def rounded_mark(mark: Image.Image, size: int) -> Image.Image:
    """Scale the mark to ``size`` with transparent rounded corners."""
    scale = 4
    big = mark.resize((size * scale, size * scale), Image.Resampling.LANCZOS).convert("RGBA")
    mask = Image.new("L", big.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, big.width - 1, big.height - 1), radius=int(big.width * 0.18), fill=255
    )
    big.putalpha(mask)
    return big.resize((size, size), Image.Resampling.LANCZOS)


def font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    for candidate in ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def write_icon(destination: Path, mark: Image.Image | None = None) -> None:
    mark = mark or load_mark()
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    largest = rounded_mark(mark, 256)
    frames = [rounded_mark(mark, width) for width, _ in ICON_SIZES[:-1]]
    largest.save(destination, format="ICO", sizes=ICON_SIZES, append_images=frames)


def write_wizard_bitmap(destination: Path, mark: Image.Image | None = None) -> None:
    mark = mark or load_mark()
    canvas = Image.new("RGB", WIZARD_SIZE, BACKGROUND)
    draw = ImageDraw.Draw(canvas)
    logo = rounded_mark(mark, 132)
    canvas.paste(logo, ((WIZARD_SIZE[0] - logo.width) // 2, 52), logo)
    title_font = font(24)
    width = draw.textlength("Corvus", font=title_font)
    draw.text(((WIZARD_SIZE[0] - width) / 2, 200), "Corvus", font=title_font, fill=TEXT)
    draw.line((52, 236, 112, 236), fill=ACCENT, width=2)
    tagline_font = font(10)
    tagline = "Open-source intelligence"
    width = draw.textlength(tagline, font=tagline_font)
    draw.text(((WIZARD_SIZE[0] - width) / 2, 246), tagline, font=tagline_font, fill=MUTED)
    _save_bmp(canvas, destination)


def write_header_bitmap(destination: Path, mark: Image.Image | None = None) -> None:
    mark = mark or load_mark()
    canvas = Image.new("RGB", HEADER_SIZE, BACKGROUND)
    draw = ImageDraw.Draw(canvas)
    logo = rounded_mark(mark, 45)
    canvas.paste(logo, (8, (HEADER_SIZE[1] - logo.height) // 2), logo)
    draw.text((62, 17), "Corvus", font=font(17), fill=TEXT)
    _save_bmp(canvas, destination)


def _save_bmp(image: Image.Image, destination: Path) -> None:
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(destination, format="BMP")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the branded Corvus Windows icon and wizard artwork")
    parser.add_argument("--output", required=True, type=Path, help="ICO destination")
    parser.add_argument("--wizard-bitmap", type=Path, help="164x314 welcome/finish panel BMP destination")
    parser.add_argument("--header-bitmap", type=Path, help="150x57 page header BMP destination")
    args = parser.parse_args()
    mark = load_mark()
    write_icon(args.output, mark)
    print(f"Generated Corvus icon from approved artwork: {args.output.resolve()}")
    if args.wizard_bitmap:
        write_wizard_bitmap(args.wizard_bitmap, mark)
        print(f"Generated Corvus wizard bitmap: {args.wizard_bitmap.resolve()}")
    if args.header_bitmap:
        write_header_bitmap(args.header_bitmap, mark)
        print(f"Generated Corvus header bitmap: {args.header_bitmap.resolve()}")


if __name__ == "__main__":
    main()
