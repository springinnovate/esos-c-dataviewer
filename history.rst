History
=======

Unreleased
----------
* Added algorithm to pick no more than 1M pixels when determining histograms
  for purposes of speedy calculations.

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