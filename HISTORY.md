# Plugin History

## Project purpose

**LandXML Road & Terrain GIS Tools** was developed to provide a repeatable QGIS Processing workflow for bringing Civil 3D/LandXML engineering information into GIS while retaining road-design geometry and terrain relationships.

## Development history

### Phase 1 — LandXML TIN to GeoTIFF
The project started with a requirement to convert a Civil 3D LandXML TIN surface into a high-resolution engineering raster. The implementation was designed to preserve the original TIN topology by reading LandXML point and face records and evaluating the planar elevation surface within each source triangle.

The initial use case was a client-supplied corridor surface, where the LandXML contained a Civil 3D surface and declared a coordinate system that required independent verification before GIS use.

### Phase 2 — Generalized coordinate handling
Testing against real engineering data showed that LandXML CRS metadata cannot always be assumed to represent the effective project coordinate system. The conversion workflow was therefore generalized to support explicit coordinate operations rather than embedding one project's correction into the code.

The resulting coordinate-operation options include stored coordinates, XY swapping, offsets, combined swap/offset operations, 2-D Helmert transformation, and reprojection to a target CRS.

A key design principle was adopted: the plugin should not infer a transformation merely because coordinate magnitudes appear unusual. Any transformation used for engineering work should be validated against known survey control.

### Phase 3 — QGIS Processing integration
The plugin was converted from a standalone conversion dialog into a native QGIS Processing provider. This made the algorithms accessible from the Processing Toolbox and enabled batch processing, Processing history, and Model Designer workflows.

### Phase 4 — Horizontal alignment extraction
The plugin was expanded to extract LandXML horizontal alignments as GIS line features. Straight and circular alignment geometry was supported, with configurable curve densification and engineering attributes such as alignment name, stationing, and reported length.

### Phase 5 — Road-design GIS conversion
The scope was expanded from isolated geometry extraction to broader road-design interoperability. Added capabilities include vertical profiles, 3D centerlines, station points, cross-sections, feature lines, breaklines, surface boundaries, and one-click export of a complete road-design package to GeoPackage.

### Phase 6 — Sample alignment validation and geometry corrections
A bridge-realignment LandXML export was used as a structured validation case because it contains a long road alignment with tangents, circular curves, clothoid transition spirals, a vertical profile, and a large TIN surface.

Testing identified two geometry issues that were corrected before v1.4.1:

1. LandXML `<Spiral>` elements were previously skipped. They are now represented using clothoid approximation so the transition geometry is not lost.
2. Civil 3D/LandXML `cw` and `ccw` curve semantics were being interpreted with the wrong mathematical sign convention. The correction ensures circular curves follow the intended design direction.

The sample validation case reported an alignment length of approximately 2,687.782 m. The corrected 5 m-densified extracted line differed from that reported length by about 0.005 m, demonstrating close agreement while using a finite GIS vertex spacing.

## Current design principles

1. Preserve source engineering geometry wherever practical rather than reconstructing it from lower-fidelity derivatives.
2. Keep coordinate transformation explicit and user-controlled.
3. Treat LandXML as a structured engineering interchange format, not merely a point-file source.
4. Make outputs native to QGIS Processing so they can participate in repeatable GIS workflows.
5. Preserve engineering provenance and expose validation information where possible.
6. Prefer conservative handling of unsupported geometry over silently creating misleading geometry.

## Planned direction

Potential future work includes higher-fidelity spiral evaluation, richer Civil 3D corridor/code extraction, superelevation extraction, drainage-object extraction, cut/fill comparison between surfaces, contour generation directly from the source TIN, formal transformation preset management, and expanded automated validation against survey control.

### v1.4.4 — Explicit output CRS controls

Following user testing in QGIS 3.44, output CRS selection was made a first-class Processing parameter across the provider. The selected CRS is now visibly exposed in the algorithm dialog and is used for vector sink creation and GeoTIFF spatial reference metadata.


### 1.4.5 — Processing parameter audit
Reviewed each algorithm for missing or non-functional controls. Added selection filters, sampling controls, station endpoint behavior, DEM NoData, and complete-export inclusion toggles. Also corrected two XML/geometry robustness issues discovered during the audit.
