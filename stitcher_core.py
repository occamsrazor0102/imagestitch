"""Pure stitching logic for the MRI Ribbon Stitcher. No GUI imports.

All grid parameters are keyword-only (cols=, rows=) so call sites are
unambiguous about which dimension is which.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Callable, Iterable, Sequence

from PIL import Image, ImageColor, ImageDraw, ImageFont

# JPEG stores dimensions in 16 bits; stay a little under the 65535 hard cap.
JPEG_MAX_DIM = 65500


class StitchError(Exception):
    """Base class for errors raised by this module."""


class StitchCancelled(StitchError):
    """Raised when the cancel event is set mid-stitch."""


class ImageReadError(StitchError):
    """Raised when an input image cannot be opened or decoded."""

    def __init__(self, path: Path, original: Exception):
        self.path = Path(path)
        self.original = original
        super().__init__(f"Cannot read image: {self.path.name} ({original})")


class JpegSizeError(StitchError):
    """Raised when the output is too large for the JPEG format."""


_NUM_RE = re.compile(r"(\d+)")


def natural_sort_key(name: str) -> tuple:
    """Sort key so that 'slice2' orders before 'slice10' (case-insensitive).

    Uses isdecimal(), not isdigit(): characters like '²' or '①' are isdigit()
    but int() rejects them, which would crash the sort.
    """
    return tuple(
        (0, int(part), "") if part.isdecimal() else (1, 0, part.casefold())
        for part in _NUM_RE.split(str(name))
    )


def layout_presets(n: int) -> list[tuple[int, int]]:
    """Grid presets for n images as (cols, rows) pairs, sorted by cols.

    Includes every exact factor pair (1xN ... Nx1) plus the near-square
    ceil(sqrt(n)) grid (which may contain blank cells).
    """
    if n <= 0:
        return []
    pairs = set()
    for cols in range(1, n + 1):
        if n % cols == 0:
            pairs.add((cols, n // cols))
    square_cols = math.isqrt(n)
    if square_cols * square_cols < n:
        square_cols += 1
    pairs.add((square_cols, math.ceil(n / square_cols)))
    return sorted(pairs)


def compute_grid(n: int, *, cols: int | None = None, rows: int | None = None) -> tuple[int, int]:
    """Complete a grid for n images given exactly one of cols/rows.

    Returns (cols, rows) with the missing dimension = ceil(n / given).
    """
    if n <= 0:
        raise ValueError("Need at least one image")
    if (cols is None) == (rows is None):
        raise ValueError("Give exactly one of cols or rows")
    if cols is not None:
        if cols < 1:
            raise ValueError("Columns must be at least 1")
        return cols, math.ceil(n / cols)
    if rows < 1:
        raise ValueError("Rows must be at least 1")
    return math.ceil(n / rows), rows


def read_sizes(paths: Iterable[str | Path]) -> list[tuple[int, int]]:
    """Return (width, height) per path without decoding full images.

    Raises ImageReadError naming the first unreadable file.
    """
    sizes = []
    for p in paths:
        try:
            with Image.open(p) as im:
                sizes.append(im.size)
        except Exception as exc:  # Pillow raises many types here
            raise ImageReadError(Path(p), exc) from exc
    return sizes


def output_size(
    sizes: Sequence[tuple[int, int]], *, cols: int, rows: int, gap: int = 0, scale: float = 1.0
) -> tuple[int, int]:
    """Final canvas size in pixels for the given images and grid."""
    cell_w, cell_h = _cell_size(sizes, scale)
    return (
        cols * cell_w + (cols - 1) * gap,
        rows * cell_h + (rows - 1) * gap,
    )


def _cell_size(sizes: Sequence[tuple[int, int]], scale: float) -> tuple[int, int]:
    if not sizes:
        raise ValueError("Need at least one image")
    if scale <= 0:
        raise ValueError("Scale must be positive")
    max_w = max(w for w, _ in sizes)
    max_h = max(h for _, h in sizes)
    return max(1, math.ceil(max_w * scale)), max(1, math.ceil(max_h * scale))


def stitch(
    paths: Sequence[str | Path],
    *,
    cols: int,
    rows: int,
    fill: str = "row",
    gap: int = 0,
    bg: str = "#000000",
    scale: float = 1.0,
    labels: bool = False,
    progress: Callable[[int, int], None] | None = None,
    cancel=None,
) -> Image.Image:
    """Stitch images into a cols x rows grid and return the composite.

    fill="row" places images left-to-right then top-to-bottom;
    fill="column" places them top-to-bottom then left-to-right.
    Each cell is sized to the largest image (times scale); smaller images are
    centered. Trailing empty cells keep the background color.
    """
    n = len(paths)
    if n == 0:
        raise ValueError("Need at least one image")
    if fill not in ("row", "column"):
        raise ValueError(f"fill must be 'row' or 'column', not {fill!r}")
    if cols < 1 or rows < 1:
        raise ValueError("Grid dimensions must be at least 1")
    if cols * rows < n:
        raise ValueError(f"A {cols}x{rows} grid holds {cols * rows} images; you have {n}")
    if gap < 0:
        raise ValueError("Gap cannot be negative")

    sizes = read_sizes(paths)
    cell_w, cell_h = _cell_size(sizes, scale)
    canvas_w, canvas_h = output_size(sizes, cols=cols, rows=rows, gap=gap, scale=scale)

    bg_rgb = ImageColor.getrgb(bg)
    canvas = Image.new("RGB", (canvas_w, canvas_h), bg_rgb)
    font = _label_font(cell_h) if labels else None

    for i, path in enumerate(paths):
        if cancel is not None and cancel.is_set():
            raise StitchCancelled("Stitch cancelled")
        if fill == "row":
            r, c = divmod(i, cols)
        else:
            c, r = divmod(i, rows)
        x0 = c * (cell_w + gap)
        y0 = r * (cell_h + gap)

        try:
            with Image.open(path) as im:
                im = im.convert("RGB")
                if scale != 1.0:
                    im = im.resize(
                        (max(1, round(im.width * scale)), max(1, round(im.height * scale))),
                        Image.LANCZOS,
                    )
        except StitchError:
            raise
        except Exception as exc:
            raise ImageReadError(Path(path), exc) from exc

        # Center within the cell; every image fits because the cell is the max size.
        canvas.paste(im, (x0 + (cell_w - im.width) // 2, y0 + (cell_h - im.height) // 2))

        if font is not None:
            _draw_label(canvas, str(i + 1), x0, y0, font)
        if progress is not None:
            progress(i + 1, n)

    return canvas


def _label_font(cell_h: int):
    size = max(12, cell_h // 12)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow without sized default font
        return ImageFont.load_default()


def _draw_label(canvas: Image.Image, text: str, x0: int, y0: int, font) -> None:
    draw = ImageDraw.Draw(canvas)
    pad = 3
    draw.text(
        (x0 + pad, y0 + pad),
        text,
        fill="white",
        font=font,
        stroke_width=2,
        stroke_fill="black",
    )


def save_image(img: Image.Image, path: str | Path, quality: int = 95) -> Path:
    """Save by extension (.png/.jpg/.jpeg/.tif/.tiff). Returns the path.

    Writes to a temporary '.part' file first and renames into place, so an
    interrupted save never leaves a truncated file at the destination.
    Raises JpegSizeError when a JPEG would exceed the format's size limit.
    """
    path = Path(path)
    ext = path.suffix.lower()
    if ext in (".jpg", ".jpeg"):
        fmt, kwargs = "JPEG", {"quality": quality}
        if img.width > JPEG_MAX_DIM or img.height > JPEG_MAX_DIM:
            raise JpegSizeError(
                f"Output is {img.width} x {img.height} px, but JPEG allows at most "
                f"{JPEG_MAX_DIM} px per side. Save as PNG or TIFF instead."
            )
    elif ext == ".png":
        fmt, kwargs = "PNG", {}
    elif ext in (".tif", ".tiff"):
        fmt, kwargs = "TIFF", {}
    else:
        raise ValueError(f"Unsupported format: {ext or '(no extension)'} — use .png, .jpg, or .tiff")

    tmp = path.with_name(path.name + ".part")
    try:
        img.save(tmp, format=fmt, **kwargs)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    return path
