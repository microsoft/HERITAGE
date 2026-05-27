# Third-Party Notices

This repository depends on third-party packages. Their licenses are governed by the respective projects.

The list below captures the **direct Python dependencies** used by this repo (as pinned in `requirements.txt`).

## Python dependencies

- `geopandas==1.0.1`
- `matplotlib==3.9.2`
- `numpy==1.26.4`
- `pandas==2.2.3`
- `pillow==10.4.0`
- `planetary-computer==1.0.0`
- `pyproj==3.6.1`
- `pystac-client==0.8.5`
- `rasterio==1.4.4`
- `requests==2.32.3`
- `shapely==2.0.6`
- `tqdm==4.66.5`

## External data sources

The scripts in `src/` read from the following third-party data services. Their terms of use and licenses apply to any imagery or data products retrieved through them:

- **Planet Labs PBC** monthly basemap mosaics, retrieved via the Planet Basemaps API. Access requires a Planet account and an API key. See https://www.planet.com/account/ and the Planet Labs licensing terms for permitted uses.
- **ESA WorldCover 2021** global land-cover product, queried via the Microsoft Planetary Computer STAC catalog. ESA WorldCover is released under CC BY 4.0 by the European Space Agency.

## Models and datasets

- This repository does **not** include proprietary or restricted datasets.
- The HERITAGE imagery itself is hosted separately from this code repository; see the README for download instructions.
- Any imagery or land-cover data downloaded through the helper scripts must be used in compliance with the upstream provider's license.
