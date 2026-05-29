History
=======

1.4.0 (2026-05-29)
------------------
* Added valid area, sum, and average summaries beside histogram and scatter
  plots for sampled continuous layers.

1.3.2 (2026-05-29)
------------------
* Fixed categorical legends so labels are rendered from YAML metadata, preserving
  commas in labels and collapsing repeated category codes with the same label
  and color.

1.3.1 (2026-05-29)
------------------
* Fixed remote container startup by removing the unused ``ecoshard`` dependency
  from the GeoServer registration path, avoiding a GDAL version mismatch during
  imports.

1.3.0 (2026-05-29)
------------------
* Added support for categorical rasters as selectable A/B layers, including
  categorical legends and disabled histogram states when categorical data cannot
  be plotted as continuous values.
* Fixed viewer startup compatibility with newer FastAPI/Starlette template
  response handling and pinned the viewer Python dependency versions used by
  the image build.
* Fixed GeoServer initialization style configuration so the dynamic SLD path is
  configurable via ``STYLE_PATH`` and mounted into ``geoserver-init``.
* Improved local data directory compatibility in Docker Compose by supporting
  both ``LOCAL_DATA`` and ``LOCAL_DATA_DIR`` environment variable names.

1.2.0 (2026-03-20)
------------------
* Added algorithm to pick no more than 1M pixels when determining histograms
  for purposes of speedy calculations.
* Added a feature to define min/max values in the layer.yml file for faster
  initial loading.
* Added feature to allow for a base landcover layer.
* Added feature to view the bivariate plot color range.
* Fixed issues where there were unneeded .env variables and/or confusing ones
  which bled into the compose file.

1.1.1 (2026-03-02)
------------------
* Fixed issue with viewer not resolving with traefik on external host.

1.1.0 (2026-03-02)
------------------

Changed
~~~~~~~
* Frontend build container now uses ``npm ci`` (with an npm cache mount) and builds via ``vite`` with sourcemaps enabled. :contentReference[oaicite:1]{index=1}
* Raster stats service now loads layer metadata from ``/app/layers.yml`` (container-mounted) instead of relying on an env-provided path. :contentReference[oaicite:2]{index=2}

Added
~~~~~
* Implemented bivariate raster visualization with color blending for dual-variable analysis.
* Added configurable LULC basemap layer for improved spatial context.
* Exposed ``title`` and ``description`` fields from ``layers.yaml`` for UI display.
* Layer registry loading now supports multiple sections (e.g. ``layers`` and ``baseLayers``) and derives optional categorical label mappings (``rendering.category_labels``). :contentReference[oaicite:3]{index=3}

Fixed
~~~~~
* Type/output handling for pixel-value responses broadened to allow non-numeric values when needed. :contentReference[oaicite:4]{index=4}

UI
~~
* Assorted UI/template polish around layer selection (including base layers), palette controls, and sampling-window controls. :contentReference[oaicite:5]{index=5}


1.0.4 (2025-11-08)
------------------

* Maintenance release (see tag / PR history). :contentReference[oaicite:6]{index=6}


1.0.3 (2025-11-07)
------------------

* Maintenance release (see tag / PR history). :contentReference[oaicite:7]{index=7}


1.0.2 (YYYY-MM-DD)
------------------

* Maintenance release (details TBD).


1.0.1 (2025-11-07)
------------------

* Maintenance release. :contentReference[oaicite:8]{index=8}


1.0.0 (2025-11-06)
------------------

* Initial release. :contentReference[oaicite:9]{index=9}
