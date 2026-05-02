#!/usr/bin/env python3
"""Split collage images using dark bars and save unique crops to results."""

from __future__ import annotations

import argparse
import hashlib
from collections import Counter
from pathlib import Path
from typing import Iterable

import numpy as np
from PIL import Image

DEFAULT_BAR_COLOR = (25, 25, 25)
DEFAULT_TOLERANCE = 3
DEFAULT_LINE_STD_THRESHOLD = 10
DEFAULT_LINE_UNIFORM_FRACTION = 0.98
DEFAULT_MIN_HEIGHT = 300
DEFAULT_MIN_WIDTH = 300
DEFAULT_THIN_ACTION = "skip"


def load_image(path: Path) -> Image.Image:
    return Image.open(path).convert("RGB")


def find_separator_positions(mask: np.ndarray, axis: int) -> list[int]:
    if axis == 0:
        line_mask = np.all(mask, axis=1)
    else:
        line_mask = np.all(mask, axis=0)

    return [idx for idx, is_bar in enumerate(line_mask) if is_bar]


def detect_separator_lines(image: Image.Image, tolerance: int, std_threshold: int, uniform_fraction: float) -> tuple[list[int], list[int]]:
    arr = np.asarray(image)
    if arr.ndim != 3 or arr.shape[2] != 3:
        arr = np.asarray(image.convert("RGB"))

    rows: list[int] = []
    for i in range(arr.shape[0]):
        line = arr[i].astype(np.int16)
        mean_color = np.round(line.mean(axis=0)).astype(np.int16)
        close = np.abs(line - mean_color).max(axis=1) <= tolerance
        if close.mean() >= uniform_fraction and np.std(line, axis=0).max() <= std_threshold:
            rows.append(i)

    cols: list[int] = []
    for j in range(arr.shape[1]):
        line = arr[:, j, :].astype(np.int16)
        mean_color = np.round(line.mean(axis=0)).astype(np.int16)
        close = np.abs(line - mean_color).max(axis=1) <= tolerance
        if close.mean() >= uniform_fraction and np.std(line, axis=0).max() <= std_threshold:
            cols.append(j)

    return rows, cols


def detect_bar_color(image: Image.Image, tolerance: int) -> tuple[tuple[int, int, int], int]:
    arr = np.asarray(image)
    if arr.ndim != 3 or arr.shape[2] != 3:
        arr = np.asarray(image.convert("RGB"))

    row_ref = arr[:, :1, :].astype(np.int16)
    row_consistent = np.all(np.abs(arr.astype(np.int16) - row_ref) <= tolerance, axis=(1, 2))
    row_colors = [tuple(arr[i, 0].tolist()) for i in np.nonzero(row_consistent)[0]]

    col_ref = arr[:1, :, :].astype(np.int16)
    col_consistent = np.all(np.abs(arr.astype(np.int16) - col_ref) <= tolerance, axis=(0, 2))
    col_colors = [tuple(arr[0, j].tolist()) for j in np.nonzero(col_consistent)[0]]

    candidates = Counter(row_colors + col_colors)
    if not candidates:
        return DEFAULT_BAR_COLOR, tolerance

    top_candidates = [color for color, _ in candidates.most_common(10)]
    best_color: tuple[int, int, int] = DEFAULT_BAR_COLOR
    best_tolerance = tolerance
    best_score = -1

    for color in top_candidates:
        for tol in range(tolerance, tolerance + 6):
            crops = crop_regions(image, color, tol)
            score = len(make_unique(crops))
            if score > best_score or (score == best_score and tol < best_tolerance):
                best_color = color
                best_tolerance = tol
                best_score = score

    return best_color, best_tolerance


def split_edges(positions: list[int], length: int) -> list[tuple[int, int]]:
    if not positions:
        return [(0, length)]

    blocks: list[tuple[int, int]] = []
    start = positions[0]
    last = positions[0]

    for pos in positions[1:]:
        if pos != last + 1:
            blocks.append((start, last + 1))
            start = pos
        last = pos
    blocks.append((start, last + 1))

    ranges: list[tuple[int, int]] = []
    cursor = 0
    for block_start, block_end in blocks:
        if cursor < block_start:
            ranges.append((cursor, block_start))
        cursor = block_end

    if cursor < length:
        ranges.append((cursor, length))

    return ranges


def trim_side_bars(arr: np.ndarray, color: np.ndarray, tolerance: int) -> tuple[np.ndarray, int]:
    height, width, _ = arr.shape
    mask = np.all(np.abs(arr.astype(np.int16) - color) <= tolerance, axis=2)
    col_fraction = mask.mean(axis=0)
    min_fraction = 0.99

    left = 0
    while left < width and col_fraction[left] >= min_fraction:
        left += 1

    right = width
    while right > left and col_fraction[right - 1] >= min_fraction:
        right -= 1

    if left == 0 and right == width:
        return arr, 0

    return arr[:, left:right, :], left


def trim_crop_sides(crop: Image.Image, tolerance: int = 4, min_fraction: float = 0.99, max_border: int = 40) -> Image.Image:
    arr = np.asarray(crop).astype(np.int16)
    height, width, _ = arr.shape

    left = 0
    while left < min(width, max_border):
        col = arr[:, left, :]
        mean_color = np.round(col.mean(axis=0)).astype(np.int16)
        if np.mean(np.all(np.abs(col - mean_color) <= tolerance, axis=1)) >= min_fraction:
            left += 1
        else:
            break

    right = width
    while right > left and width - right < max_border:
        col = arr[:, right - 1, :]
        mean_color = np.round(col.mean(axis=0)).astype(np.int16)
        if np.mean(np.all(np.abs(col - mean_color) <= tolerance, axis=1)) >= min_fraction:
            right -= 1
        else:
            break

    if left == 0 and right == width:
        return crop

    return crop.crop((left, 0, right, height))


def crop_regions(image: Image.Image, bar_color: tuple[int, int, int] | None, tolerance: int) -> list[Image.Image]:
    arr = np.asarray(image)
    if bar_color is None:
        bar_color, tolerance = detect_bar_color(image, tolerance)

    color = np.array(bar_color, dtype=np.int16)
    trimmed_arr, x_offset = trim_side_bars(arr, color, tolerance)
    mask = np.all(np.abs(trimmed_arr.astype(np.int16) - color) <= tolerance, axis=2)

    horiz_positions = find_separator_positions(mask, axis=0)
    vert_positions = find_separator_positions(mask, axis=1)

    horiz_ranges = split_edges(horiz_positions, trimmed_arr.shape[0])
    vert_ranges = split_edges(vert_positions, trimmed_arr.shape[1])

    crops: list[Image.Image] = []
    for y0, y1 in horiz_ranges:
        for x0, x1 in vert_ranges:
            if y1 > y0 and x1 > x0:
                crop = image.crop((x0 + x_offset, y0, x1 + x_offset, y1))
                crop = trim_crop_sides(crop, tolerance=tolerance)
                if crop.width > 0 and crop.height > 0:
                    crops.append(crop)
    return crops


def make_unique(images: Iterable[Image.Image]) -> list[Image.Image]:
    seen: set[str] = set()
    unique_images: list[Image.Image] = []
    for img in images:
        digest = hashlib.sha256(img.tobytes()).hexdigest()
        if digest not in seen:
            seen.add(digest)
            unique_images.append(img)
    return unique_images


def save_crops(
    crops: Iterable[Image.Image],
    base_name: str,
    output_dir: Path,
    min_height: int,
    min_width: int,
    thin_action: str,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    skipped = 0
    saved_index = 1
    for crop in crops:
        if thin_action == "skip" and (crop.height < min_height or crop.width < min_width):
            skipped += 1
            continue
        output_path = output_dir / f"{base_name}_crop_{saved_index:02d}.png"
        crop.save(output_path)
        saved.append(output_path)
        saved_index += 1

    if skipped:
        print(
            f"Skipped {skipped} thin crop(s) smaller than {min_width}px x {min_height}px"
        )
    return saved


def process_image(
    path: Path,
    output_dir: Path,
    bar_color: tuple[int, int, int] | None,
    tolerance: int,
    min_height: int,
    min_width: int,
    thin_action: str,
) -> list[Path]:
    image = load_image(path)
    if bar_color is None:
        print(f"Auto-detecting bar color and tolerance for {path.name}")

    crops = crop_regions(image, bar_color, tolerance)
    unique_crops = make_unique(crops)
    base_name = path.stem
    saved = save_crops(
        unique_crops,
        base_name,
        output_dir,
        min_height,
        min_width,
        thin_action,
    )
    return saved


def collect_images(path: Path) -> list[Path]:
    if path.is_dir():
        return sorted([p for p in path.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}])
    if path.is_file():
        return [path]
    raise FileNotFoundError(f"No images found at {path}")


def parse_color(value: str) -> tuple[int, int, int]:
    value = value.strip().lstrip("#")
    if len(value) == 6:
        return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))
    raise argparse.ArgumentTypeError("Color must be a 6-digit hex string like 191919 or #191919")


def main() -> None:
    parser = argparse.ArgumentParser(description="Crop collage images split by dark bars into unique image pieces.")
    parser.add_argument("input", type=Path, help="Input image file or directory containing images.")
    parser.add_argument("--output", type=Path, default=Path("results"), help="Output directory for cropped images.")
    parser.add_argument("--bar-color", type=parse_color, default=None,
                        help="Hex bar color, e.g. 191919 or #191919. If omitted, the tool detects the separator color automatically.")
    parser.add_argument("--tolerance", type=int, default=DEFAULT_TOLERANCE,
                        help="Tolerance for bar color matching.")
    parser.add_argument("--min-height", type=int, default=DEFAULT_MIN_HEIGHT,
                        help="Minimum height in pixels for saved crops.")
    parser.add_argument("--min-width", type=int, default=DEFAULT_MIN_WIDTH,
                        help="Minimum width in pixels for saved crops.")
    parser.add_argument("--thin-action", choices=("save", "skip"), default=DEFAULT_THIN_ACTION,
                        help="Action for crops smaller than --min-width or --min-height: save or skip them.")
    args = parser.parse_args()

    images = collect_images(args.input)
    output_dir = args.output
    all_saved: list[Path] = []
    for image_path in images:
        saved = process_image(
            image_path,
            output_dir,
            args.bar_color,
            args.tolerance,
            args.min_height,
            args.min_width,
            args.thin_action,
        )
        all_saved.extend(saved)
        print(f"Processed {image_path.name}: saved {len(saved)} crops")

    if all_saved:
        print(f"Saved {len(all_saved)} images to {output_dir.resolve()}")
    else:
        print("No crops found. Verify the bar color and input image.")


if __name__ == "__main__":
    main()
