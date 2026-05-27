"""Generate land use / land cover analysis for the HERITAGE paper.

Uses ESA WorldCover 2021 (10m, 11 classes) via Microsoft Planetary Computer.

Two phases:

* ``fetch``   queries WorldCover per site, writes the per-site class
              percentages to ``lulc_data.json``. Requires the coordinate
              inputs (``site_coordinates.csv`` for Afghanistan, a global-
              sites GeoJSON). Neither is part of the public HERITAGE
              release; authors hold them under the same coordinate-
              withholding policy described in the paper.

* ``analyze`` reads the cached ``lulc_data.json`` shipped alongside this
              script, prints the per-country table, and writes
              ``figs/fig_lulc_breakdown.png``. Runs without the coordinate
              inputs, so readers of the public repository can reproduce
              the figure and the table values directly.

Usage:
    python generate_lulc_analysis.py                  # fetch + analyze
    python generate_lulc_analysis.py --phase fetch    # WorldCover query only
    python generate_lulc_analysis.py --phase analyze  # cached -> figure/table
"""

import argparse
import csv
import json
import logging
import os
import time
from collections import defaultdict
from math import cos, radians
from pathlib import Path

import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import requests

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_DIR = REPO_ROOT / "dataset"
FIGURES_DIR = REPO_ROOT / "figs"
# Coordinate inputs are not part of the public release; provide them locally
# or override via CLI to run the ``fetch`` phase.
COORDS_CSV = REPO_ROOT / "site_coordinates.csv"
GEOJSON = REPO_ROOT / "sites_global.geojson"
CACHE_FILE = REPO_ROOT / "lulc_data.json"

# ESA WorldCover 2021 class definitions
WORLDCOVER_CLASSES = {
    10: "Tree cover",
    20: "Shrubland",
    30: "Grassland",
    40: "Cropland",
    50: "Built-up",
    60: "Bare / sparse veg.",
    70: "Snow and ice",
    80: "Permanent water",
    90: "Herbaceous wetland",
    95: "Mangrove",
    100: "Moss and lichen",
}

# ESA WorldCover official colors (approximate RGB)
WORLDCOVER_COLORS = {
    10: "#006400",
    20: "#ffbb22",
    30: "#ffff4c",
    40: "#f096ff",
    50: "#fa0000",
    60: "#b4b4b4",
    70: "#f0f0f0",
    80: "#0064c8",
    90: "#0096a0",
    95: "#00cf75",
    100: "#fae6a0",
}

# Afghanistan image params
AFG_PX = 186
AFG_RES = 4.77  # metres per pixel
AFG_HALF_M = (AFG_PX * AFG_RES) / 2  # ~443.5m

plt.rcParams.update({
    "font.family": "serif",
    "font.size": 15,
    "axes.titlesize": 16,
    "axes.labelsize": 15,
    "xtick.labelsize": 14,
    "ytick.labelsize": 14,
    "legend.fontsize": 12,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "figure.dpi": 300,
    "savefig.dpi": 300,
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.15,
})


# ----------------------------------------------------------------
# Coordinate loading
# ----------------------------------------------------------------

def load_afghanistan_sites() -> list[dict]:
    """Load Afghanistan site centroids and compute bboxes."""
    sites = []
    with open(COORDS_CSV, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            lat, lon = row["coordinates"].split(",")
            lat, lon = float(lat), float(lon)
            d_lat = AFG_HALF_M / 111000
            d_lon = AFG_HALF_M / (111000 * cos(radians(lat)))
            sites.append({
                "key": f"Afghanistan/{row['site_name']}",
                "country": "Afghanistan",
                "bbox": [lon - d_lon, lat - d_lat, lon + d_lon, lat + d_lat],
            })
    return sites


def normalize_dirname(name: str) -> str:
    """Normalize a GeoJSON site name to match dataset directory naming."""
    # "Egypt - El-Hibeh" -> "Egypt_El_Hibeh"
    name = name.strip()
    name = name.replace(" - ", "_")
    name = name.replace("-", "_")
    name = name.replace(" ", "_")
    # Remove parentheses: "Upano (Kunguints)" -> "Upano_Kunguints"
    name = name.replace("(", "").replace(")", "")
    # Remove special chars
    for ch in ",.;:'\"!?":
        name = name.replace(ch, "")
    # Handle accented characters common in the dataset
    replacements = {
        "\u0131": "",  # Turkish dotless i in Kaymakci
        "\u00e7": "",  # c-cedilla
        "\u00fa": "",  # u-acute in Pakatnam
        "\u00e9": "e", # e-acute
        "\u00e8": "e", # e-grave
    }
    for old, new in replacements.items():
        name = name.replace(old, new)
    # Collapse multiple underscores
    while "__" in name:
        name = name.replace("__", "_")
    return name


def load_global_sites() -> list[dict]:
    """Load global site bounding boxes from GeoJSON."""
    if not GEOJSON.exists():
        logger.warning("GeoJSON not found at %s", GEOJSON)
        return []

    gdf = gpd.read_file(GEOJSON)
    # Get list of actual dataset directories for matching
    actual_dirs = set()
    for d in DATASET_DIR.iterdir():
        if d.is_dir() and d.name != "Afghanistan":
            actual_dirs.add(d.name)

    sites = []
    matched = set()
    for _, row in gdf.iterrows():
        name = row.get("name") or row.get("Name") or str(row.get("id", ""))
        if not name:
            continue
        normalized = normalize_dirname(name)
        country = normalized.split("_")[0]

        # Try exact match first
        dir_name = None
        if normalized in actual_dirs:
            dir_name = normalized
        else:
            # Fuzzy: find directory starting with the same country prefix
            for d in actual_dirs:
                if d.startswith(country + "_"):
                    # Check if significant overlap in tokens
                    norm_tokens = set(normalized.lower().split("_"))
                    dir_tokens = set(d.lower().split("_"))
                    if len(norm_tokens & dir_tokens) >= 2:
                        dir_name = d
                        break

        if dir_name is None or dir_name in matched:
            continue

        matched.add(dir_name)
        bounds = row.geometry.bounds  # (minx, miny, maxx, maxy)
        sites.append({
            "key": dir_name,
            "country": country,
            "bbox": list(bounds),
        })

    # Report unmatched directories
    unmatched = actual_dirs - matched
    if unmatched:
        logger.warning("%d dataset dirs unmatched: %s", len(unmatched), unmatched)

    return sites


# ----------------------------------------------------------------
# LULC data fetching
# ----------------------------------------------------------------

def fetch_worldcover(sites: list[dict], cache: dict) -> dict:
    """Query ESA WorldCover 2021 for each site via Planetary Computer."""
    import planetary_computer
    import pystac_client
    import rasterio

    catalog = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=planetary_computer.sign_inplace,
    )

    total = len(sites)
    fetched = 0
    errors = 0

    for i, site in enumerate(sites):
        key = site["key"]
        if key in cache.get("sites", {}):
            continue  # already cached

        bbox = site["bbox"]
        for attempt in range(3):
            try:
                # WorldCover ships two non-equivalent products (2020 v100,
                # 2021 v200). Pin year and product version so the choice is
                # not order-dependent on STAC result ordering.
                search = catalog.search(
                    collections=["esa-worldcover"],
                    bbox=bbox,
                    datetime="2021-01-01/2021-12-31",
                    query={"esa_worldcover:product_version": {"eq": "v200"}},
                )
                items = list(search.items())
                if not items:
                    logger.warning("No WorldCover 2021 v200 tile for %s", key)
                    break

                item = items[0]
                asset = item.assets["map"]
                href = asset.href

                with rasterio.open(href) as src:
                    window = rasterio.windows.from_bounds(
                        *bbox, transform=src.transform
                    )
                    data = src.read(1, window=window)

                # Count pixels per class (exclude 0 = nodata)
                unique, counts = np.unique(data, return_counts=True)
                pixel_counts = {}
                total_px = 0
                for val, cnt in zip(unique, counts):
                    if val == 0:
                        continue
                    pixel_counts[str(int(val))] = int(cnt)
                    total_px += int(cnt)

                cache.setdefault("sites", {})[key] = {
                    "country": site["country"],
                    "pixel_counts": pixel_counts,
                    "total_pixels": total_px,
                }
                fetched += 1
                break

            except (
                # rasterio.errors.RasterioIOError extends OSError, so OSError
                # covers I/O failures opening signed STAC asset HREFs.
                OSError,
                requests.RequestException,
                RuntimeError,  # raised above on empty STAC result
                TimeoutError,
            ) as e:
                if attempt < 2:
                    wait = 2 ** (attempt + 1)
                    logger.warning(
                        "Retry %d for %s: %s (waiting %ds)",
                        attempt + 1, key, e, wait,
                    )
                    time.sleep(wait)
                else:
                    logger.error("FAILED %s: %s", key, e)
                    errors += 1

        if (i + 1) % 100 == 0 or i == total - 1:
            logger.info(
                "Progress: %d/%d (fetched=%d, errors=%d)",
                i + 1, total, fetched, errors,
            )
            # Incremental save
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=2)

    return cache


# ----------------------------------------------------------------
# Analysis and output generation
# ----------------------------------------------------------------

def compute_country_stats(cache: dict) -> dict:
    """Aggregate LULC percentages by country."""
    country_sites = defaultdict(list)
    for key, data in cache["sites"].items():
        country = data["country"]
        if data["total_pixels"] == 0:
            continue
        pcts = {}
        for code_str, cnt in data["pixel_counts"].items():
            pcts[int(code_str)] = cnt / data["total_pixels"] * 100
        country_sites[country].append(pcts)

    stats = {}
    for country, site_pcts in sorted(country_sites.items()):
        n = len(site_pcts)
        mean_pcts = {}
        for code in WORLDCOVER_CLASSES:
            vals = [s.get(code, 0.0) for s in site_pcts]
            mean_pcts[code] = np.mean(vals)
        stats[country] = {"n_sites": n, "mean_pcts": mean_pcts}

    return stats


def generate_latex_table(stats: dict):
    """Write LaTeX table fragment to figures/tab_lulc.tex."""
    # Determine which classes to show (>1% in at least one country)
    active_classes = []
    for code in WORLDCOVER_CLASSES:
        max_val = max(s["mean_pcts"].get(code, 0) for s in stats.values())
        if max_val >= 1.0:
            active_classes.append(code)

    # Short column labels
    short_names = {
        10: "Tree", 20: "Shrub", 30: "Grass", 40: "Crop",
        50: "Built", 60: "Bare", 70: "Snow", 80: "Water",
        90: "Wetland", 95: "Mangrove", 100: "Moss",
    }

    n_cols = len(active_classes)
    col_spec = "lr" + "r" * n_cols + "r"
    header_cols = " & ".join(short_names[c] for c in active_classes)

    lines = []
    lines.append(r"\begin{table}[ht]")
    lines.append(r"\centering")
    lines.append(r"\caption{Land cover composition around HERITAGE sites, derived from ESA WorldCover 2021 (\SI{10}{\metre} resolution)\cite{zanaga2022worldcover}. Values are mean percentage of pixels within each site's spatial extent, aggregated by country.}")
    lines.append(r"\label{tab:lulc}")
    lines.append(r"\small")
    lines.append(r"\begin{tabular}{" + col_spec + "}")
    lines.append(r"\toprule")
    lines.append(f"Country & $N$ & {header_cols} & Other \\\\")
    lines.append(r"\midrule")

    for country in sorted(stats.keys()):
        s = stats[country]
        n = s["n_sites"]
        vals = []
        shown_total = 0.0
        for code in active_classes:
            v = s["mean_pcts"].get(code, 0.0)
            shown_total += v
            vals.append(f"{v:.1f}")
        other = max(0.0, 100.0 - shown_total)
        vals.append(f"{other:.1f}")
        val_str = " & ".join(vals)
        lines.append(f"{country} & {n} & {val_str} \\\\")

    lines.append(r"\bottomrule")
    lines.append(r"\end{tabular}")
    lines.append(r"\end{table}")

    tex_path = FIGURES_DIR / "tab_lulc.tex"
    with open(tex_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    logger.info("Table written to %s", tex_path)


def generate_figure(stats: dict):
    """Generate stacked horizontal bar chart of LULC composition."""
    # Determine active classes (>1% in at least one country)
    active_classes = []
    for code in WORLDCOVER_CLASSES:
        max_val = max(s["mean_pcts"].get(code, 0) for s in stats.values())
        if max_val >= 1.0:
            active_classes.append(code)

    countries = sorted(stats.keys())
    n = len(countries)

    fig, ax = plt.subplots(figsize=(14, max(5, n * 0.45 + 1)))

    y_pos = np.arange(n)
    left = np.zeros(n)

    for code in active_classes:
        vals = [stats[c]["mean_pcts"].get(code, 0) for c in countries]
        color = WORLDCOVER_COLORS.get(code, "#999999")
        label = WORLDCOVER_CLASSES[code]
        ax.barh(y_pos, vals, left=left, color=color, edgecolor="white",
                linewidth=0.3, label=label, height=0.7)
        left += np.array(vals)

    # "Other" bar for remaining classes
    other_vals = [100.0 - left[i] for i in range(n)]
    if any(v > 0.5 for v in other_vals):
        ax.barh(y_pos, other_vals, left=left, color="#e0e0e0",
                edgecolor="white", linewidth=0.3, label="Other", height=0.7)

    ax.set_yticks(y_pos)
    ax.set_yticklabels(
        [f"{c} (n={stats[c]['n_sites']})" for c in countries],
        fontsize=13,
    )
    ax.set_xlabel("Land cover (%)")
    ax.set_xlim(0, 100)
    ax.invert_yaxis()
    ax.legend(
        loc="upper center",
        bbox_to_anchor=(0.5, -0.08),
        ncol=min(6, len(active_classes) + 1),
        frameon=False,
        fontsize=11,
    )

    fig.tight_layout()
    fig.savefig(FIGURES_DIR / "fig_lulc_breakdown.pdf")
    fig.savefig(FIGURES_DIR / "fig_lulc_breakdown.png")
    plt.close(fig)
    logger.info("Figure saved: fig_lulc_breakdown.{pdf,png}")


def log_summary(stats: dict):
    """Log per-country LULC top-3 class summary at INFO level."""
    logger.info("=== LULC Summary ===")
    for country in sorted(stats.keys()):
        s = stats[country]
        top = sorted(s["mean_pcts"].items(), key=lambda x: -x[1])[:3]
        top_str = ", ".join(
            f"{WORLDCOVER_CLASSES[c]}={v:.1f}%" for c, v in top
        )
        logger.info("%s (n=%d): %s", country, s["n_sites"], top_str)


# ----------------------------------------------------------------
# Main
# ----------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="LULC analysis for HERITAGE paper"
    )
    parser.add_argument(
        "--phase", choices=["fetch", "analyze", "all"], default="all",
        help="Phase to run: fetch, analyze, or all (default: all)",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    FIGURES_DIR.mkdir(parents=True, exist_ok=True)

    if args.phase in ("fetch", "all"):
        logger.info("Phase 1: Fetching LULC data from Planetary Computer...")

        # Load existing cache
        cache = {"product": "ESA WorldCover 2021", "resolution_m": 10,
                 "class_map": {str(k): v for k, v in WORLDCOVER_CLASSES.items()},
                 "sites": {}}
        if CACHE_FILE.exists():
            with open(CACHE_FILE, encoding="utf-8") as f:
                cache = json.load(f)
            logger.info("Loaded cache with %d sites", len(cache.get("sites", {})))

        # Load sites
        afg_sites = load_afghanistan_sites()
        global_sites = load_global_sites()
        all_sites = afg_sites + global_sites
        logger.info(
            "Total sites: %d (%d Afghanistan + %d global)",
            len(all_sites), len(afg_sites), len(global_sites),
        )

        already = len(cache.get("sites", {}))
        remaining = len([s for s in all_sites
                         if s["key"] not in cache.get("sites", {})])
        logger.info("Already cached: %d, remaining: %d", already, remaining)

        if remaining > 0:
            cache = fetch_worldcover(all_sites, cache)
            # Final save
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=2)
            logger.info("Cache saved: %d sites", len(cache["sites"]))
        else:
            logger.info("All sites cached, skipping fetch")

    if args.phase in ("analyze", "all"):
        logger.info("Phase 2: Generating analysis outputs...")

        if not CACHE_FILE.exists():
            logger.error("No cache file found. Run --phase fetch first.")
            return

        with open(CACHE_FILE, encoding="utf-8") as f:
            cache = json.load(f)

        logger.info("Loaded %d sites from cache", len(cache["sites"]))
        stats = compute_country_stats(cache)
        generate_latex_table(stats)
        generate_figure(stats)
        log_summary(stats)

    logger.info("Done.")


if __name__ == "__main__":
    main()
