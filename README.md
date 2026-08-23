# LandXML Road & Terrain GIS Tools

Native QGIS Processing provider for engineering interoperability between Civil 3D/LandXML and GIS.

Author: Edmond Akello · License: GPL-2.0-or-later · Current version: 1.4.11

## QGIS compatibility
Tested at the source-code/API level against QGIS 3.44. Uses `QgsProcessingParameterNumber` (with `Double`/`Integer` type) for numeric Processing parameters rather than the QGIS 3.36+-only `QgsProcessingParameterDouble`, for broader compatibility across the QGIS 3.28–3.99 range declared in `metadata.txt`.

## Processing groups
- LandXML TIN: TIN to GeoTIFF
- Vector extraction: horizontal alignments
- Road design extraction: vertical profiles, 3D centerlines, cross-sections, station points, complete road-design export
- Surface extraction: TIN boundary, feature lines, breaklines

## Coordinate handling
The provider intentionally does not trust the LandXML CRS declaration. Each algorithm exposes explicit coordinate interpretation and output CRS controls: stored coordinates, XY swap, offsets, 2-D Helmert, and reprojection.

## Complete Road Design to GeoPackage
The one-click exporter creates a GeoPackage plus optional TIN-derived DEM and contour GeoPackage in the chosen output folder. Existing/missing LandXML structures are handled best-effort and reported in Processing messages.

## Engineering caution
Coordinate transformations should be validated against known survey control before design use. The plugin does not automatically infer offsets or transformations from coordinate magnitudes.

## Installation
QGIS > Plugins > Manage and Install Plugins > Install from ZIP, or install directly from the official QGIS plugin repository once published.
The algorithms appear in the Processing Toolbox, under "LandXML Road & Terrain GIS Tools", after installation.

## Sample alignment validation (v1.4.1)
Validated against a Civil 3D LandXML bridge-realignment export containing:
- 1 alignment, 2,687.782 m long
- 8 clothoid spirals
- 4 circular curves
- 5 tangents
- 1 profile with 1,995 station/elevation pairs
- 1 TIN surface with 275,175 points and 550,206 faces
- all TIN face point references valid

The alignment geometry uses clothoid approximation for `<Spiral>` elements and Civil 3D/LandXML rotation semantics for `<Curve>` elements. At 5 m densification, the extracted alignment polyline length differs from the LandXML reported length by about 0.005 m.

## Parameter coverage (v1.4.5 audit)
All major Processing algorithms expose source selection, coordinate transformation, output CRS, sampling/densification, and output-selection controls appropriate to their operation. The Complete Road Design export additionally supports per-layer inclusion, surface/alignment filters, DEM NoData, and sampling intervals. Parameters are wired to processing logic rather than being display-only.

## Version history
See `CHANGELOG.md` for release-level changes and `HISTORY.md` for the development history and design principles.
