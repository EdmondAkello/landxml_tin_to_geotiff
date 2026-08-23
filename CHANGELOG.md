# Changelog

## 1.4.11 — 2026-08-23

### Security
- **Fixed: QGIS Plugin Repository security scan (Bandit) still BLOCKED v1.4.10 for 1 finding: "Using ElementTree to parse untrusted XML data" at `safe_xml.py:45`.** v1.4.10's `safe_xml.py` already parsed untrusted LandXML exclusively through hardened `xml.parsers.expat` callbacks (not through `ElementTree.parse()`/`.fromstring()`), but it still imported the inert plumbing classes `ElementTree`, `ParseError` and `TreeBuilder` *from* `xml.etree.ElementTree` to assemble the parsed tree. Bandit's rule for this (B405, `import_xml_etree`) is a blanket "was anything imported from `xml.etree.ElementTree`/`cElementTree`" match — confirmed by reading Bandit's own `bandit.blacklists.imports` source — with no inspection of which names were imported or how they were used, so importing even non-parsing helper classes from that module trips it. Fixed by removing every import from `xml.etree.ElementTree`/`cElementTree`: `safe_xml.py` now implements its own minimal `Element`/`TreeBuilder`/`ElementTree`/`ParseError` equivalents, and uses `xml.etree.ElementPath` (confirmed *not* on Bandit's blacklist, and structurally can't be — it only evaluates a path expression against an already-built tree, it never parses raw XML text) to implement `.find()`/`.findall()` with CPython's own tested path-query semantics rather than a hand-rolled subset-XPath parser. No behavior change for any of the plugin's algorithms: verified zero Bandit findings across the entire plugin source (`bandit -r .`), byte-identical parse output against plain `ElementTree` across all 825,498 nodes of a real multi-megabyte sample LandXML file, and that DOCTYPE/entity-bomb/XXE attack payloads are still correctly rejected while ordinary malformed XML still raises a parse error.
- **Fixed: 15 Pyflakes findings (F403/F405) in `complete_export.py`** from `from qgis.core import *` making every name it used ambiguous ("may be undefined, or defined from star imports"). Replaced with an explicit import list of the 9 names actually used (`QgsCoordinateReferenceSystem`, `QgsProcessingAlgorithm`, `QgsProcessingException`, `QgsProcessingParameterBoolean`, `QgsProcessingParameterCrs`, `QgsProcessingParameterEnum`, `QgsProcessingParameterFile`, `QgsProcessingParameterFolderDestination`, `QgsProcessingParameterString`), matching the explicit-import style already used in every other file in this plugin.

## 1.4.10 — 2026-08-23

### Security
- **Fixed: QGIS Plugin Repository security scan (Bandit) blocked this release for 14 findings of "Using `xml.etree.ElementTree` [`.parse`] to parse untrusted XML data is known to be vulnerable to XML attacks."** Every LandXML file this plugin opens comes from Processing's file-picker parameter — i.e. it is user-supplied, untrusted input, not something the plugin author controls. Plain `xml.etree.ElementTree` inherits CPython's `expat` parser with its default entity-expansion behaviour, so a crafted file (billion-laughs entity bomb, or an external entity reference for XXE) could hang/crash QGIS or read local files before any of the plugin's own code ran. Replaced every `ET.parse(path).getroot()` call site (`core.py`, `alignment_algorithm.py`, `complete_export.py`, `road_features.py`) with a new internal `safe_xml` module that drives `xml.parsers.expat` directly and rejects any DOCTYPE declaration, entity declaration, or external entity reference outright — real LandXML exports never declare any of these, so a file that does is refused with a clear error instead of being parsed. Deliberately does not add `defusedxml` as a dependency (not part of any stock QGIS Python environment, would break the plugin on install); verified byte-identical output against plain `ElementTree` on synthetic data and on real multi-megabyte sample LandXML files, confirmed ~40% slower but correct, and confirmed entity-bomb/XXE/bare-DOCTYPE payloads are rejected while ordinary malformed XML still raises the normal parse error.
- **Fixed: Bandit "Try, Except, Pass detected" finding in `complete_export.py`.** `_delete_layer()` previously attempted `ds.DeleteLayer(name)` inside a bare `try/except Exception: pass`, silently swallowing any GDAL/OGR error alongside the expected "layer doesn't exist yet" case. Replaced with an explicit `GetLayerByName()` existence check before deleting, so a genuine OGR error is no longer masked.

### Changed
- **QGIS 4 / PyQt6 compatibility: fixed all "QT6 Check" enum-scoping findings** reported by the plugin repository's `pyqgis4-checker` across `alignment_algorithm.py`, `complete_export.py`, `processing_algorithm.py`, `params.py`, and `road_features.py`. PyQt6 requires fully-scoped enum access (e.g. `QgsWkbTypes.Type.LineString`, `QgsProcessing.SourceType.TypeVectorLine`, `QgsProcessingParameterFile.Behavior.File`, `QgsProcessingParameterNumber.Type.Double`) where PyQt5 accepted the legacy unscoped form; this check is informational rather than blocking for repository approval, but fixed anyway so the plugin loads cleanly under QGIS 4 without relying on PyQt6's temporary backward-compatibility shims.

## 1.4.9 — 2026-08-23

### Fixed
- **`ContourGenerate` crash: `TypeError: in method 'ContourGenerate', argument 1 of type 'GDALRasterBandShadow *'`**, reported against `Export Complete Road Design to GeoPackage` on real sample data. Root cause: `complete_export.py` called `gdal.Open(dem).GetRasterBand(1)` — the intermediate `Dataset` returned by `gdal.Open()` had no persisted reference, so it was garbage-collected immediately, invalidating the `Band` object before `gdal.ContourGenerate()` used it. Fixed by holding the `Dataset` in a named variable for the lifetime of the call. Reproduced and confirmed fixed with a minimal, real-GDAL repro script before being re-verified against both real sample LandXML files end-to-end.
- **Vertical profile extraction silently produced zero features on real data.** The code assumed a `<ProfileGeom>` wrapper with `<Line>`/`<Curve>` children carrying `staStart`/`staEnd`/`elevStart`/`elevEnd` attributes — a structure that does not exist in the real LandXML 1.2 schema or in either real sample file. The real structure is a flat, ordered `<PVI>`/`<ParaCurve>`/`<CircCurve>` sequence directly under `<ProfAlign>`. Added a new `core.read_vertical_profile()` that parses this correctly, including AASHTO symmetric parabolic curve sampling for `<ParaCurve>`/`<CircCurve>` elements, and wired it into `ProfileAlgorithm`, `Centerline3DAlgorithm`, and the Complete Road Design export. Verified against real sample data: profile elevations now match the raw PVI values in the source XML exactly.
- **3D centerlines always reported `z_source: "No profile; Z=0"` even when a matching profile existed.** `Centerline3DAlgorithm` and the Complete Road Design export both looked for an `alignment=` attribute on `<ProfAlign>` to associate a profile with its alignment, but real `<ProfAlign>` elements carry no such attribute — the owning alignment's name lives on the parent `<Alignment>` element, with `<Profile><ProfAlign>` nested inside it. Fixed by walking `<Alignment>` elements and searching within each for its nested profile, with a fallback pass for any `ProfAlign` that does carry an explicit `alignment=` reference. Verified: centerlines now correctly report `z_source: "Vertical profile"` with Z-values matching the source profile's elevation range.

### Known limitation (documented, not fixed)
- **Design-template cross-sections (`DesignCrossSectSurf`/`CrossSectPnt`) are not extracted.** Only absolute-coordinate `CrossSectSurf`/`PntList2D`|`PntList3D` cross-sections were ever supported. In the Olkaria sample, 3,235 of 3,569 `CrossSect` records are design-template-only and are skipped. Full support would require reconstructing XY geometry from alignment station + offset, which is a larger feature than a bug fix and was not attempted here to avoid an unverified, rushed change. `CrossSectionsAlgorithm` and the Complete Road Design export now emit an explicit `pushWarning()` reporting the skipped count instead of silently dropping the data, and the limitation is documented in the tool's help text.

### Changed
- Trademark/branding review for this release: no ArcGIS/ArcHydro/ESRI references were found in this plugin. "Civil 3D" file-format-origin references were kept, since they describe the source file format rather than a comparison claim.

### Verification
All 10 Processing algorithms were run end-to-end against both real sample LandXML files (`Proposed Nithi Bridge Realignment.xml`, `Olkaria LOT 3-design alignment 09.07.2026.xml`), including `Complete Road Design` run twice per sample mirroring the exact parameter combinations from the reported crash. Verification used a QGIS test stub whose parameter-reading and sink/geometry glue is backed by real `osgeo.gdal`/`ogr`/`osr` bindings rather than mocks, so algorithms ran to completion and produced real, inspectable GeoTIFF/GeoPackage output — not just "no exception raised." Final run: 22/22 cases passed, with output feature counts, geometries, and elevation ranges checked directly against the source XML.

## 1.4.8 — 2026-08-23

### Changed
- Documentation pass: replaced project-identifying client/site names with
  generic labels throughout `README.md`, `HISTORY.md`, `CHANGELOG.md`, and
  `examples/README.md` ahead of public distribution. No functional changes.

## 1.4.7 — 2026-08-23

### Fixed
This release fixes a systemic class of bug: several QGIS Processing parameter
constructors were called with keyword arguments that read naturally but do
not exist in the real PyQGIS API. These are silent no-ops under plain Python
(and under a permissive test double), but the real SIP-generated QGIS
bindings reject unknown keywords outright, so every one of these raised
`TypeError` the moment QGIS tried to build the algorithm's parameter dialog
— which is what produced the "'decimals' is an unknown keyword argument"
error, and (because the crash aborts `initAlgorithm()` partway through)
made it look like later input parameters were simply missing from the
dialog.

- **`decimals=` is not a constructor argument of `QgsProcessingParameterNumber`**, in any QGIS version. It never was — spin-box precision is set after construction via `setMetadata({'widget_wrapper': {'decimals': N}})`, which is what QGIS's own Processing dialog reads. Fixed at all 26 call sites across `processing_algorithm.py`, `alignment_algorithm.py`, `road_features.py`, and `complete_export.py` via a new shared `params.number_param()` helper that does this correctly (and preserves the intended precision, e.g. 8 decimals for rotation, 10 for the Helmert scale factor, rather than just dropping it).
- **`fields=` is not a constructor argument of `QgsProcessingParameterFeatureSink`.** The output feature schema is supplied at run time to `parameterAsSink()`, not at parameter-definition time. Fixed at all 7 call sites (one in `alignment_algorithm.py`, six in `road_features.py`).
- **`fileFilter=` is not a constructor argument of `QgsProcessingParameterRasterDestination`** (unlike `QgsProcessingParameterFile`, where it is valid). Fixed in `processing_algorithm.py`.
- **`self.parameterDefinition('OUTPUT').fields()` does not exist** — `QgsProcessingParameterFeatureSink` has no `.fields()` method. Five algorithms (`ProfileAlgorithm`, `Centerline3DAlgorithm`, `StationPointsAlgorithm`, `CrossSectionsAlgorithm`, and the shared `FeatureLinesAlgorithm`/`BreaklinesAlgorithm` base) relied on this to recover the output field schema inside `processAlgorithm()`; it would have raised `AttributeError` the moment any of these tools actually ran (i.e. even after the dialog opened successfully). Fixed by building the `QgsFields` schema directly inside `processAlgorithm()`, matching the pattern the two unaffected algorithms (`LandXMLAlignmentsAlgorithm`, `SurfaceBoundaryAlgorithm`) already used correctly.
- Also caught in the same audit: `LandXMLAlignmentsAlgorithm`'s `SEGMENT` parameter was missing `type=QgsProcessingParameterNumber.Double`, so it defaulted to the constructor's `Integer` type despite a fractional default value.

### Verification
Every one of the plugin's 10 Processing algorithms now has its
`initAlgorithm()` re-run against a QGIS stub whose constructor signatures
are transcribed from the official PyQGIS API docs and strictly reject
unknown keyword arguments (rather than silently accepting them, as a
permissive test double would) — this is the same class of check that would
have caught all four bugs above before release. The stub was cross-checked
by running it against the pre-fix code, where it reproduces the exact
`TypeError: 'decimals' is an unknown keyword argument` seen in real QGIS
3.44.6. TIN and alignment parsing were re-verified against both real sample
LandXML files with identical results to prior releases.

Live install/run testing inside actual QGIS was still not performed here —
this sandbox has no QGIS/GDAL runtime available — so a real install-and-run
smoke test in QGIS remains the recommended final check before store
submission.

## 1.4.6 — 2026-08-23

### Fixed
- **Packaging: plugin directory renamed from `landxml_tin_to_geotiff_v1.4.5` to the stable `landxml_tin_to_geotiff`.** A folder name containing dots is not a valid Python package name; QGIS imports the plugin using its folder name, so a version-suffixed, dotted directory name caused the plugin to fail to load (the regression this reintroduced was previously fixed once already, in 1.4.2 — the versioned name crept back into later release zips). The plugin folder name must now stay constant across all future versions so QGIS treats upgrades as updates rather than separate installs.
- Added the missing `QgsProcessingParameterString` import in `alignment_algorithm.py`. The "Extract LandXML Alignments to Lines" algorithm referenced this class in `initAlgorithm()` without importing it, raising a `NameError` as soon as the algorithm was registered.
- Corrected the `gdal.ContourGenerate()` argument list in `complete_export.py`. The previous call passed the wrong number/order of arguments (`dstLayer` was passed as `None` and the contour layer was passed in the `callback` slot, with an extra trailing argument), which raised `TypeError: ContourGenerate() takes from 9 to 11 positional arguments but 12 were given` any time "Export Complete Road Design to GeoPackage" ran with contour generation enabled (the default).
- Added a `LICENSE` file (GPL-2.0-or-later) — required for listing on the official QGIS plugin repository.
- Standardized `metadata.txt`: correct author/email, repository/tracker/homepage URLs, and a changelog reference.

## 1.4.5
- Completed Processing-parameter audit across the provider.
- Added alignment/profile/surface/name filters where the source structure supports selection.
- Added alignment curve/spiral segment-length control to downstream road exports.
- Added station endpoint control.
- Added DEM NoData control.
- Added complete-export layer inclusion controls.
- Added configurable station and vertical-profile sampling intervals to the complete export.
- Wired exposed parameters into processing logic so they affect results.
- Fixed LandXML point-list handling where empty XML child elements could evaluate as false.
- Corrected Feature Line extraction so it no longer also exports Breakline records.

## 1.4.3 — 2026-08-23

### Fixed
- Corrected `road_features.py` imports so TIN functions are loaded from `core.py`, not `alignment_algorithm.py`.
- Prevents provider-load failure before Processing algorithms are registered.
- Removed Python bytecode caches from the distributable package.

## 1.4.2 — 2026-08-23

### Fixed
- Replaced unavailable `QgsProcessingParameterDouble` imports with `QgsProcessingParameterNumber` using the `Double` type for QGIS 3.44 compatibility.
- Corrected the internal plugin package directory name so QGIS can import `landxml_tin_to_geotiff`.

## 1.4.1 — 2026-08-23

### Fixed
- Added clothoid spiral approximation for LandXML alignment spirals.
- Corrected LandXML/Civil 3D curve rotation handling for circular curves.
- Validated the sample bridge-realignment alignment against its LandXML length and element structure.

## 1.4.0 — 2026-08-23

### Added
- Expanded the plugin into a broader LandXML road and terrain GIS Processing provider.
- Added vertical profiles, 3D centerlines, cross-sections, station points, surface boundaries, feature lines, breaklines, and Complete Road Design → GeoPackage export.

## 1.3.0 — 2026-08-22

### Added
- Added horizontal alignment extraction, curve densification, alignment attributes, coordinate transformations and CRS controls.

## 1.2.0 — 2026-08-22

### Changed
- Converted the plugin into a native QGIS Processing provider.
- Added reusable coordinate interpretation and transformation options.

## 1.1.0 — 2026-08-22

### Added
- Added coordinate-operation controls, transformed-bounds diagnostics, and explicit output CRS handling.

## 1.0.0 — 2026-08-21

### Added
- Initial LandXML TIN → GeoTIFF conversion using original TIN topology and planar interpolation.
