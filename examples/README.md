# Example data

Two real Civil 3D LandXML exports were used to validate this plugin and are
not bundled in this repository directly because of their size (see below) —
keep them alongside the plugin (e.g. in a sibling `Sample Landxml/` folder)
when testing:

- **Sample bridge realignment.xml** (~45 MB) — 1 alignment
  (2,687.782 m, 8 clothoid spirals, 4 circular curves, 5 tangents), 1 vertical
  profile (1,995 station/elevation pairs), and a TIN surface with 275,175
  points and 550,206 faces. This is the file referenced in `README.md` under
  "Sample alignment validation".
- **Sample corridor design.xml** (~59 MB) — a multi-surface TIN
  (`BOTTOM`, `TOP`, and an intermediate design surface) plus a ~16.7 km
  design alignment.

If you want to version these files alongside the plugin source, consider
Git LFS rather than committing them directly — both are within GitHub's
100 MB hard limit but large enough to bloat a normal repository's history.

## Suggested workflow

1. QGIS > Processing Toolbox > **LandXML TIN > LandXML TIN to GeoTIFF**.
   Point it at one of the sample files, leave "Coordinate interpretation" at
   "Use stored coordinates" (both samples use real, already-correct
   EPSG:21037 coordinates — see the LandXML-declared CRS reported in the
   algorithm's log), and set the output CRS to EPSG:21037.
2. **Vector extraction > Extract LandXML Alignments to Lines** on the same
   file to pull out the horizontal alignment as a GIS line layer.
3. For the full picture, run **Export Complete Road Design to GeoPackage**,
   which produces alignments, profiles, 3D centerlines, station points,
   cross-sections, a TIN-derived DEM, and contours in one pass.
