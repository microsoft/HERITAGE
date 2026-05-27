"""Rasterize per-site bounding polygons into binary PNG masks.

Each Afghanistan site in the HERITAGE release ships with a single
``mask.png`` that delineates the archaeological area inside its
186 x 186 image chip. This script produces those masks from a CSV of
(site_name, latitude, longitude, polygon_wkt) inputs.

For each site the script:

1. Picks the WGS84 / UTM zone covering the site (north zone if
   latitude >= 0, south otherwise).
2. Projects the (lat, lon) centre to that UTM zone.
3. Builds an affine transform for a (size x size) grid at the given
   resolution in metres per pixel, centred on the projected centre.
   The default 186 x 186 grid at 4.77 m/pixel matches the Afghanistan
   subset of HERITAGE and covers approximately 1 km x 1 km on the ground.
4. Projects the polygon (provided as WKT in EPSG:4326) into the same
   UTM zone.
5. Rasterizes the polygon onto the grid with value 255 inside and 0
   outside, matching the mask convention described in the paper.
6. Writes the result as an 8-bit single-band PNG to
   ``<output>/<site_name>/mask.png``.

The script consumes only public, non-confidential inputs. Coordinates
and polygons are not part of the HERITAGE public release; users must
supply their own.

Example:

    python generate_site_masks.py \\
        --sites sites_afghanistan.csv \\
        --output ./dataset/Afghanistan

Input CSV format:

    site_name,latitude,longitude,polygon_wkt
    looted_0,34.5123,69.1820,"POLYGON ((69.180 34.510, 69.184 34.510, ...))"
    preserved_0,34.6011,69.2350,"POLYGON ((69.232 34.599, ...))"

Polygons are interpreted as WGS84 (EPSG:4326) longitude/latitude rings.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from PIL import Image
from pyproj import Transformer
from rasterio.features import rasterize
from rasterio.transform import from_origin
from shapely.geometry import mapping
from shapely.ops import transform as shapely_transform
from shapely.wkt import loads as wkt_loads

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = {"site_name", "latitude", "longitude", "polygon_wkt"}


def utm_epsg(lon: float, lat: float) -> int:
    """Return the EPSG code for the WGS84 / UTM zone containing (lon, lat)."""
    zone = int((lon + 180.0) // 6.0) + 1
    return (32600 if lat >= 0 else 32700) + zone


def make_grid_transform(
    centre_x: float, centre_y: float, size: int, resolution: float
):
    """Affine transform for a square grid of ``size`` cells at ``resolution`` m/pixel,
    centred on (``centre_x``, ``centre_y``) in the same CRS."""
    half = (size * resolution) / 2.0
    upper_left_x = centre_x - half
    upper_left_y = centre_y + half
    return from_origin(upper_left_x, upper_left_y, resolution, resolution)


def rasterize_polygon(
    polygon_wgs84,
    centre_lon: float,
    centre_lat: float,
    size: int,
    resolution: float,
) -> np.ndarray:
    """Project a WGS84 polygon and centre into a local UTM zone, then rasterize
    the polygon onto a (size x size) grid centred on (centre_lon, centre_lat).
    Returns a uint8 array with 255 inside the polygon and 0 outside.
    """
    target_epsg = utm_epsg(centre_lon, centre_lat)
    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{target_epsg}", always_xy=True).transform

    centre_x, centre_y = to_utm(centre_lon, centre_lat)
    transform = make_grid_transform(centre_x, centre_y, size, resolution)

    polygon_utm = shapely_transform(to_utm, polygon_wgs84)

    mask = rasterize(
        [(mapping(polygon_utm), 255)],
        out_shape=(size, size),
        transform=transform,
        fill=0,
        dtype=np.uint8,
        all_touched=False,
    )
    return mask


def save_png(arr: np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr, mode="L").save(str(path), "PNG")


def process_sites(
    csv_path: Path, output_root: Path, size: int, resolution: float
) -> dict[str, int]:
    df = pd.read_csv(csv_path)
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(
            f"{csv_path} missing required columns: {sorted(missing)}. "
            f"Got: {sorted(df.columns)}"
        )

    stats = {"written": 0, "skipped": 0, "errors": 0}
    for _, row in df.iterrows():
        name = str(row["site_name"])
        try:
            polygon = wkt_loads(str(row["polygon_wkt"]))
            mask = rasterize_polygon(
                polygon,
                float(row["longitude"]),
                float(row["latitude"]),
                size,
                resolution,
            )
        except Exception as exc:
            logger.warning("  %s: skipped (%s)", name, exc)
            stats["errors"] += 1
            continue

        if mask.max() == 0:
            logger.warning(
                "  %s: empty mask -- polygon falls outside the %dx%d window at "
                "%.2f m/pixel; check coordinates.",
                name,
                size,
                size,
                resolution,
            )
            stats["skipped"] += 1
            continue

        out_path = output_root / name / "mask.png"
        save_png(mask, out_path)
        stats["written"] += 1
        logger.info("  %s: wrote %s (coverage %.1f%%)",
                    name, out_path, 100.0 * (mask > 0).mean())
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rasterize per-site polygons to binary PNG masks "
                    "centred on each site's (lat, lon).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--sites",
        type=Path,
        required=True,
        help="CSV with columns: site_name, latitude, longitude, polygon_wkt.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output root; one mask.png is written per site sub-directory.",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=186,
        help="Side length of the mask in pixels (default matches HERITAGE Afghanistan).",
    )
    parser.add_argument(
        "--resolution",
        type=float,
        default=4.77,
        help="Pixel resolution in metres (default matches Planet zoom-15 mosaics).",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.sites.exists():
        logger.error("Site CSV not found: %s", args.sites)
        return 2

    args.output.mkdir(parents=True, exist_ok=True)
    stats = process_sites(args.sites, args.output, args.size, args.resolution)
    logger.info(
        "Finished: %d masks written, %d skipped (empty), %d errors.",
        stats["written"],
        stats["skipped"],
        stats["errors"],
    )
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
