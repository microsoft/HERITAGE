"""Convert 4-band Planet GeoTIFFs to RGBA PNGs for the HERITAGE release.

Geographic metadata (CRS, affine transform) is stripped during conversion;
the output PNGs are pure rasters. Channels 0-2 carry the R, G, B reflectance
bands; channel 3 is a binary data-validity mask (255 valid, 0 padding).

Afghanistan sites are centre-cropped or zero-padded to a fixed 186 x 186
target. Global sites use the most common (H, W) seen across that site's
monthly TIFs as the per-site target.

Expected input layout (one directory per site):

    <afg-src>/<looted_N|preserved_N>/<YYYY>_<MM>.tif
    <global-src>/<Country_SiteName>/<YYYY>_<MM>.tif

Output layout (created by this script):

    <dst>/Afghanistan/<looted_N|preserved_N>/<YYYY>_<MM>.png
    <dst>/<Country_SiteName>/<YYYY>_<MM>.png

Usage:

    python tif_to_png_4band.py --test                           # one site each
    python tif_to_png_4band.py --mode all \\
        --src-afghanistan /path/to/raw_afg \\
        --src-global      /path/to/raw_global \\
        --dst             /path/to/dataset
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AFG_SRC = REPO_ROOT / "planet_mosaics_afghanistan"
DEFAULT_GLOBAL_SRC = REPO_ROOT / "planet_mosaics_world"
DEFAULT_DST = REPO_ROOT / "dataset"


def pad_crop_to_target(arr: np.ndarray, target_h: int, target_w: int) -> np.ndarray:
    """Center-crop the input if larger than the target, zero-pad if smaller."""
    h, w = arr.shape[:2]
    if h > target_h:
        start = (h - target_h) // 2
        arr = arr[start : start + target_h, :, :]
    if w > target_w:
        start = (w - target_w) // 2
        arr = arr[:, start : start + target_w, :]
    h, w = arr.shape[:2]
    if h < target_h or w < target_w:
        pad_top = (target_h - h) // 2
        pad_bottom = target_h - h - pad_top
        pad_left = (target_w - w) // 2
        pad_right = target_w - w - pad_left
        arr = np.pad(
            arr,
            ((pad_top, pad_bottom), (pad_left, pad_right), (0, 0)),
            mode="constant",
            constant_values=0,
        )
    return arr


def determine_target_dimensions(site_dir: Path) -> tuple[int, int]:
    """Return the most common (height, width) across TIFs in a site directory."""
    dims: Counter[tuple[int, int]] = Counter()
    for f in sorted(os.listdir(site_dir)):
        if not f.lower().endswith(".tif"):
            continue
        with Image.open(site_dir / f) as im:
            w, h = im.size
            dims[(h, w)] += 1
    if not dims:
        raise ValueError(f"No TIF files in {site_dir}")
    (target_h, target_w), count = dims.most_common(1)[0]
    total = sum(dims.values())
    logger.info("  target %dx%d (%d/%d files match)", target_h, target_w, count, total)
    return target_h, target_w


def read_tif_4band(path: Path) -> np.ndarray:
    with Image.open(path) as im:
        arr = np.array(im)
    if arr.ndim == 2 or arr.shape[2] != 4:
        raise ValueError(f"expected 4 bands, got shape {arr.shape}: {path}")
    return arr.astype(np.uint8)


def save_png_4band(arr: np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr, mode="RGBA").save(str(path), "PNG")


def extract_date(filename: str) -> str | None:
    m = re.search(r"(\d{4}_\d{2})", filename)
    return m.group(1) if m else None


def _hxw(s: str) -> tuple[int, int]:
    """argparse type for HxW dimension strings, e.g. '186x186'."""
    parts = s.lower().split("x")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(
            f"--afg-target must be HxW, got {s!r}"
        )
    try:
        h, w = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--afg-target HxW must be integers, got {s!r}"
        ) from exc
    if h <= 0 or w <= 0:
        raise argparse.ArgumentTypeError(
            f"--afg-target HxW must be positive, got {s!r}"
        )
    return h, w


def convert_afghanistan_sites(
    src: Path,
    dst: Path,
    target_h: int,
    target_w: int,
    test_mode: bool,
) -> dict[str, int]:
    stats = {"converted": 0, "skipped": 0, "errors": 0}
    site_dirs = sorted(d for d in src.iterdir() if d.is_dir() and not d.name.startswith("."))
    if test_mode:
        site_dirs = site_dirs[:1]
    logger.info("Afghanistan: %d sites, target %dx%d", len(site_dirs), target_h, target_w)

    for site_dir in site_dirs:
        out_dir = dst / "Afghanistan" / site_dir.name
        tifs = sorted(f for f in os.listdir(site_dir) if f.lower().endswith(".tif"))
        logger.info("[%s] %d files", site_dir.name, len(tifs))

        for i, fname in enumerate(tifs):
            stem = extract_date(fname) or Path(fname).stem
            out_path = out_dir / f"{stem}.png"
            try:
                arr = read_tif_4band(site_dir / fname)
                arr = pad_crop_to_target(arr, target_h, target_w)
                save_png_4band(arr, out_path)
                stats["converted"] += 1
            except Exception as exc:
                logger.warning("  ERROR %s: %s", fname, exc)
                stats["errors"] += 1
            if (i + 1) % 50 == 0:
                logger.info("  %d/%d done", i + 1, len(tifs))
    return stats


def convert_global_sites(src: Path, dst: Path, test_mode: bool) -> dict[str, int]:
    stats = {"converted": 0, "skipped": 0, "errors": 0}
    site_dirs = sorted(
        d
        for d in src.iterdir()
        if d.is_dir() and not d.name.endswith("_grid") and not d.name.startswith(".")
    )
    if test_mode:
        site_dirs = site_dirs[:1]
    logger.info("Global: %d sites", len(site_dirs))

    for site_dir in site_dirs:
        out_dir = dst / site_dir.name
        logger.info("[%s]", site_dir.name)
        target_h, target_w = determine_target_dimensions(site_dir)
        tifs = sorted(f for f in os.listdir(site_dir) if f.lower().endswith(".tif"))

        for i, fname in enumerate(tifs):
            date_str = extract_date(fname)
            if date_str is None:
                stats["skipped"] += 1
                continue
            out_path = out_dir / f"{date_str}.png"
            try:
                arr = read_tif_4band(site_dir / fname)
                arr = pad_crop_to_target(arr, target_h, target_w)
                save_png_4band(arr, out_path)
                stats["converted"] += 1
            except Exception as exc:
                logger.warning("  ERROR %s: %s", fname, exc)
                stats["errors"] += 1
            if (i + 1) % 50 == 0:
                logger.info("  %d/%d done", i + 1, len(tifs))
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert 4-band Planet TIFs to RGBA PNGs.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--mode", choices=["afghanistan", "global", "all"], default="all")
    parser.add_argument("--test", action="store_true", help="Process one site per subset.")
    parser.add_argument(
        "--afg-target",
        type=_hxw,
        default=(186, 186),
        help="HxW for Afghanistan tiles, e.g. 186x186.",
    )
    parser.add_argument("--src-afghanistan", type=Path, default=DEFAULT_AFG_SRC)
    parser.add_argument("--src-global", type=Path, default=DEFAULT_GLOBAL_SRC)
    parser.add_argument("--dst", type=Path, default=DEFAULT_DST)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    th, tw = args.afg_target

    if args.mode in ("afghanistan", "all"):
        if not args.src_afghanistan.exists():
            logger.error("Afghanistan source not found: %s", args.src_afghanistan)
            return 2
        logger.info("Afghanistan done: %s",
                    convert_afghanistan_sites(args.src_afghanistan, args.dst, th, tw, args.test))

    if args.mode in ("global", "all"):
        if not args.src_global.exists():
            logger.error("Global source not found: %s", args.src_global)
            return 2
        logger.info("Global done: %s",
                    convert_global_sites(args.src_global, args.dst, args.test))

    return 0


if __name__ == "__main__":
    sys.exit(main())
