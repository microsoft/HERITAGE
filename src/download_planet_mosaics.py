"""Download Planet Labs monthly basemap mosaics for HERITAGE sites.

Two input modes:

    point     One TIF per month per site, clipped to a fixed-size square
              centred on a (lat, lon). Used for the 1,943 Afghanistan sites
              (1 km x 1 km windows).

    polygon   One TIF per month per site, clipped to a user-supplied polygon.
              Used for the 39 global sites.

The script reads the Planet API key from the PLANET_API_KEY environment
variable; it is not embedded in the source. Create a Planet account and
request a key at https://www.planet.com/account/#/.

Examples:

    # Point mode (Afghanistan): one row per site in a CSV
    #   site_name,latitude,longitude
    export PLANET_API_KEY=...
    python download_planet_mosaics.py \\
        --mode point \\
        --sites sites_afghanistan.csv \\
        --start 2016-01 --end 2024-12 \\
        --bbox-km 1.0 \\
        --output ./mosaics_afghanistan

    # Polygon mode (global sites): GeoJSON or GeoPackage with a name field
    python download_planet_mosaics.py \\
        --mode polygon \\
        --sites sites_global.geojson \\
        --start 2017-01 --end 2025-05 \\
        --output ./mosaics_world

Each output directory is laid out as:

    <output>/<site_name>/<YYYY>_<MM>.tif

Files that already exist on disk are skipped, so re-running the script
resumes interrupted downloads.
"""

from __future__ import annotations

import argparse
import calendar
import logging
import os
import re
import sys
from datetime import date
from pathlib import Path

import geopandas as gpd
import pandas as pd
import rasterio
import requests
from pyproj import Geod
from rasterio.mask import mask
from rasterio.merge import merge
from shapely.geometry import box, mapping
from tqdm import tqdm

logger = logging.getLogger(__name__)

PLANET_BASE = "https://api.planet.com/basemaps/v1"
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


# -------------------------------------------------------------------- helpers

def parse_month(s: str) -> tuple[int, int]:
    if not MONTH_RE.match(s):
        raise argparse.ArgumentTypeError(f"Expected YYYY-MM, got {s!r}")
    y, m = s.split("-")
    return int(y), int(m)


def month_range(start: tuple[int, int], end: tuple[int, int]):
    y, m = start
    ey, em = end
    while (y, m) <= (ey, em):
        yield y, m
        m += 1
        if m == 13:
            m = 1
            y += 1


def safe_name(s: str) -> str:
    """Sanitize a site name for use as a directory."""
    s = re.sub(r"<[^>]+>", "", s)        # strip HTML tags
    s = s.replace("&nbsp;", " ")
    s = re.sub(r"[^\w\-]+", "_", s)      # non-word -> underscore
    return s.strip("_")


def bbox_from_point(lon: float, lat: float, side_m: float) -> tuple[float, float, float, float]:
    """Return a (minx, miny, maxx, maxy) bbox centred on (lon, lat).

    side_m is the desired side length in metres. The bbox is computed on the
    WGS84 ellipsoid via geodesic offsets, so it is approximately square on
    the ground at the site's latitude.
    """
    geod = Geod(ellps="WGS84")
    half_diag = (side_m * (2 ** 0.5)) / 2
    minx, miny, _ = geod.fwd(lon, lat, 225, half_diag)
    maxx, maxy, _ = geod.fwd(lon, lat, 45, half_diag)
    return minx, miny, maxx, maxy


# ----------------------------------------------------------------- Planet API

def planet_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"api-key {api_key}"}


def find_monthly_series_id(session: requests.Session, api_key: str) -> str:
    r = session.get(f"{PLANET_BASE}/series", headers=planet_headers(api_key), timeout=30)
    r.raise_for_status()
    for s in r.json().get("series", []):
        if "monthly" in s["name"].lower():
            return s["id"]
    raise RuntimeError("No 'monthly' basemap series found in Planet account.")


def list_monthly_mosaics(
    session: requests.Session, api_key: str, series_id: str
) -> dict[tuple[int, int], tuple[str, str]]:
    """Return a {(year, month): (mosaic_id, mosaic_name)} dict for the series."""
    r = session.get(
        f"{PLANET_BASE}/series/{series_id}/mosaics",
        headers=planet_headers(api_key),
        timeout=30,
    )
    r.raise_for_status()
    out: dict[tuple[int, int], tuple[str, str]] = {}
    for m in r.json().get("mosaics", []):
        name = m.get("name", "")
        m_match = re.search(r"(\d{4})[_-](\d{2})", name)
        if not m_match:
            continue
        y, mo = int(m_match.group(1)), int(m_match.group(2))
        out[(y, mo)] = (m["id"], name)
    return out


def list_quads_for_bbox(
    session: requests.Session,
    api_key: str,
    mosaic_id: str,
    bounds: tuple[float, float, float, float],
) -> list[dict]:
    r = session.get(
        f"{PLANET_BASE}/mosaics/{mosaic_id}/quads",
        headers=planet_headers(api_key),
        params={"bbox": ",".join(map(str, bounds))},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("items", [])


def download_quad(
    session: requests.Session,
    api_key: str,
    quad: dict,
    out_path: Path,
    min_bytes: int = 10_000,
) -> bool:
    """Stream a quad to disk; return True on success."""
    link = quad.get("_links", {}).get("download")
    if not link:
        return False
    with session.get(link, headers=planet_headers(api_key), stream=True, timeout=120) as r:
        if r.status_code != 200:
            logger.warning("  download %s -> HTTP %s", quad.get("id"), r.status_code)
            return False
        ctype = r.headers.get("Content-Type", "")
        if "image/tiff" not in ctype.lower():
            logger.warning("  unexpected content type %s for %s", ctype, quad.get("id"))
            return False
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
    if out_path.stat().st_size < min_bytes:
        out_path.unlink(missing_ok=True)
        return False
    return True


# ------------------------------------------------------------ site processing

def clip_to_geometry(
    quad_paths: list[Path],
    aoi_geom,
    aoi_crs: str,
    out_path: Path,
) -> bool:
    """Merge quads and clip to the AOI geometry."""
    if not quad_paths:
        return False
    srcs = [rasterio.open(p) for p in quad_paths]
    try:
        merged, transform = merge(srcs)
        meta = srcs[0].meta.copy()
        target_crs = srcs[0].crs
    finally:
        for s in srcs:
            s.close()

    # Reproject AOI to raster CRS if needed
    aoi_gdf = gpd.GeoDataFrame(geometry=[aoi_geom], crs=aoi_crs).to_crs(target_crs)

    # Write merged temp, then mask
    tmp = out_path.with_suffix(".merged.tif")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    meta.update(
        driver="GTiff",
        height=merged.shape[1],
        width=merged.shape[2],
        transform=transform,
    )
    with rasterio.open(tmp, "w", **meta) as dst:
        dst.write(merged)
    try:
        with rasterio.open(tmp) as src:
            out_image, out_transform = mask(
                src, [mapping(aoi_gdf.geometry.iloc[0])], crop=True
            )
            out_meta = src.meta.copy()
            out_meta.update(
                height=out_image.shape[1],
                width=out_image.shape[2],
                transform=out_transform,
            )
        with rasterio.open(out_path, "w", **out_meta) as dst:
            dst.write(out_image)
    finally:
        tmp.unlink(missing_ok=True)
    return True


def process_site(
    session: requests.Session,
    api_key: str,
    site_name: str,
    aoi_geom,
    aoi_crs: str,
    months: list[tuple[int, int]],
    mosaics: dict[tuple[int, int], tuple[str, str]],
    output_dir: Path,
) -> dict[str, int]:
    stats = {"downloaded": 0, "skipped": 0, "missing": 0}
    site_dir = output_dir / safe_name(site_name)
    bounds = aoi_geom.bounds

    for y, mo in months:
        out_file = site_dir / f"{y}_{mo:02d}.tif"
        if out_file.exists() and out_file.stat().st_size > 10_000:
            stats["skipped"] += 1
            continue
        if (y, mo) not in mosaics:
            stats["missing"] += 1
            continue
        mosaic_id, _ = mosaics[(y, mo)]

        try:
            quads = list_quads_for_bbox(session, api_key, mosaic_id, bounds)
        except requests.HTTPError as e:
            logger.warning("  %s %04d-%02d: quad list failed (%s)", site_name, y, mo, e)
            stats["missing"] += 1
            continue

        if not quads:
            stats["missing"] += 1
            continue

        quad_files: list[Path] = []
        for q in quads:
            q_path = site_dir / f".raw_{y}_{mo:02d}_{q['id']}.tif"
            if q_path.exists() and q_path.stat().st_size > 10_000:
                quad_files.append(q_path)
                continue
            if download_quad(session, api_key, q, q_path):
                quad_files.append(q_path)

        if not quad_files:
            stats["missing"] += 1
            continue

        try:
            ok = clip_to_geometry(quad_files, aoi_geom, aoi_crs, out_file)
        finally:
            for p in quad_files:
                p.unlink(missing_ok=True)
        stats["downloaded"] += int(ok)

    return stats


# ----------------------------------------------------------------- entrypoint

def load_point_sites(path: Path) -> list[tuple[str, tuple[float, float]]]:
    df = pd.read_csv(path)
    required = {"site_name", "latitude", "longitude"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{path} missing columns: {sorted(missing)}")
    out = []
    for _, row in df.iterrows():
        out.append((str(row["site_name"]), (float(row["longitude"]), float(row["latitude"]))))
    return out


def load_polygon_sites(path: Path, name_field: str) -> list[tuple[str, object]]:
    gdf = gpd.read_file(path).to_crs("EPSG:4326")
    if name_field not in gdf.columns:
        raise ValueError(
            f"{path} has no field {name_field!r}; available: {list(gdf.columns)}"
        )
    return [(str(row[name_field]), row.geometry) for _, row in gdf.iterrows()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download Planet monthly basemap mosaics for HERITAGE sites.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--mode", choices=["point", "polygon"], required=True)
    parser.add_argument(
        "--sites",
        type=Path,
        required=True,
        help="CSV (point mode) or GeoJSON/GeoPackage (polygon mode).",
    )
    parser.add_argument(
        "--name-field",
        default="site_name",
        help="GeoDataFrame field used for the per-site directory name "
        "(polygon mode only).",
    )
    parser.add_argument(
        "--start",
        type=parse_month,
        required=True,
        help="First month, inclusive, as YYYY-MM.",
    )
    parser.add_argument(
        "--end",
        type=parse_month,
        required=True,
        help="Last month, inclusive, as YYYY-MM.",
    )
    parser.add_argument(
        "--bbox-km",
        type=float,
        default=1.0,
        help="Side length of the square AOI in km (point mode only).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output directory; one subdirectory per site is created.",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    api_key = os.environ.get("PLANET_API_KEY")
    if not api_key:
        logger.error("PLANET_API_KEY environment variable is not set.")
        return 2

    if args.end < args.start:
        logger.error("--end must be on or after --start.")
        return 2

    months = list(month_range(args.start, args.end))
    logger.info(
        "Date range %04d-%02d through %04d-%02d (%d months).",
        args.start[0],
        args.start[1],
        args.end[0],
        args.end[1],
        len(months),
    )

    session = requests.Session()
    series_id = find_monthly_series_id(session, api_key)
    mosaics = list_monthly_mosaics(session, api_key, series_id)
    logger.info("Planet series resolved: %d monthly mosaics found.", len(mosaics))

    args.output.mkdir(parents=True, exist_ok=True)

    if args.mode == "point":
        sites = load_point_sites(args.sites)
        side_m = args.bbox_km * 1000.0
        aoi_iter = (
            (name, box(*bbox_from_point(lon, lat, side_m)))
            for name, (lon, lat) in sites
        )
    else:
        sites_p = load_polygon_sites(args.sites, args.name_field)
        aoi_iter = ((name, geom) for name, geom in sites_p)

    totals = {"downloaded": 0, "skipped": 0, "missing": 0}
    for name, geom in tqdm(list(aoi_iter), desc="Sites"):
        try:
            s = process_site(
                session,
                api_key,
                name,
                geom,
                "EPSG:4326",
                months,
                mosaics,
                args.output,
            )
        except Exception as exc:
            # Do NOT use logger.exception here: tracebacks would include
            # Planet's signed download URLs whose query string contains
            # the access token. Log just the message; emit the traceback
            # only at DEBUG level for local diagnosis.
            logger.error("Site %s failed: %s", name, exc)
            logger.debug("Traceback for site %s:", name, exc_info=True)
            continue
        for k, v in s.items():
            totals[k] += v
        logger.info("  %s: %s", name, s)

    logger.info(
        "Finished: %d downloaded, %d already present, %d missing months.",
        totals["downloaded"],
        totals["skipped"],
        totals["missing"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
