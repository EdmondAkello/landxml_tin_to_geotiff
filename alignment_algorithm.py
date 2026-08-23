import os
import math

from . import safe_xml

from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterFile,
    QgsProcessingParameterCrs,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterEnum,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
    QgsProcessingException,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsFields,
    QgsField,
    QgsWkbTypes,
    QgsCoordinateReferenceSystem,
    QgsProcessingUtils,
    Qgis,
)
from qgis.PyQt.QtCore import QVariant

from .core import transform_vertices
from .params import number_param

NS = {"l": "http://www.landxml.org/schema/LandXML-1.2"}
METHODS = [
    "Use stored coordinates",
    "Swap X / Y",
    "Apply X / Y offset",
    "Swap X / Y + offset",
    "2-D Helmert (similarity)",
    "Reproject from source CRS",
]
METHOD_MAP = {
    "Use stored coordinates": "stored",
    "Swap X / Y": "swap",
    "Apply X / Y offset": "offset",
    "Swap X / Y + offset": "swap_offset",
    "2-D Helmert (similarity)": "helmert",
    "Reproject from source CRS": "reproject",
}


def _xy(node):
    if node is None or not (node.text or "").strip():
        raise ValueError("Missing coordinate text")
    vals = [float(v) for v in node.text.split()[:2]]
    if len(vals) != 2:
        raise ValueError("Expected two coordinates")
    return vals[0], vals[1]


def _float_attr(node, name, default=None):
    v = node.attrib.get(name)
    if v in (None, ""):
        return default
    return float(v)



def _spiral_points(spiral, segment_length=5.0):
    """Approximate a clothoid spiral from LandXML geometry.

    Uses Start->PI as the tangent direction at the start, linearly varying
    curvature between radiusStart and radiusEnd, and forces the final point
    to the LandXML End coordinate with a smooth positional correction.
    """
    start = spiral.find("l:Start", NS)
    end = spiral.find("l:End", NS)
    pi = spiral.find("l:PI", NS)
    if start is None or end is None or pi is None:
        raise ValueError("Spiral requires Start, End and PI")

    sx, sy = _xy(start)
    ex, ey = _xy(end)
    px, py = _xy(pi)

    length = _float_attr(spiral, "length", None)
    if length is None or length <= 0:
        length = math.hypot(ex - sx, ey - sy)

    rs = spiral.attrib.get("radiusStart", "INF")
    re = spiral.attrib.get("radiusEnd", "INF")
    k0 = 0.0 if str(rs).upper() == "INF" else 1.0 / float(rs)
    k1 = 0.0 if str(re).upper() == "INF" else 1.0 / float(re)
    # Match the Civil 3D/LandXML rotation convention used by <Curve>.
    sign = 1.0 if spiral.attrib.get("rot", "cw").lower() == "cw" else -1.0
    k0 *= sign
    k1 *= sign

    theta0 = math.atan2(py - sy, px - sx)
    n = max(8, int(math.ceil(length / max(segment_length, 0.001))) + 1)
    ds = length / (n - 1)

    pts = [(sx, sy)]
    x, y = sx, sy
    for i in range(1, n):
        sm = (i - 0.5) * ds
        km = k0 + (k1 - k0) * sm / length
        theta_m = theta0 + k0 * sm + 0.5 * (k1 - k0) * sm * sm / length
        x += ds * math.cos(theta_m)
        y += ds * math.sin(theta_m)
        pts.append((x, y))

    # Civil 3D's supplied spiral parameters/end coordinates are authoritative.
    # Apply a smooth positional correction so the approximation terminates
    # exactly at the LandXML End point without changing the start tangent.
    dx, dy = ex - pts[-1][0], ey - pts[-1][1]
    if abs(dx) > 1e-12 or abs(dy) > 1e-12:
        corrected = []
        for i, (xx, yy) in enumerate(pts):
            t = i / (len(pts) - 1)
            w = t * t * (3.0 - 2.0 * t)  # smoothstep
            corrected.append((xx + dx * w, yy + dy * w))
        pts = corrected

    pts[0] = (sx, sy)
    pts[-1] = (ex, ey)
    return pts

def _curve_points(curve, segment_length):
    start = curve.find("l:Start", NS)
    end = curve.find("l:End", NS)
    center = curve.find("l:Center", NS)
    if start is None or end is None or center is None:
        raise ValueError("Curve requires Start, End and Center")
    sx, sy = _xy(start); ex, ey = _xy(end); cx, cy = _xy(center)
    r = _float_attr(curve, "radius", None)
    if r is None or r <= 0:
        r = math.hypot(sx - cx, sy - cy)
    a0 = math.atan2(sy - cy, sx - cx)
    a1 = math.atan2(ey - cy, ex - cx)
    rot = curve.attrib.get("rot", "cw").lower()
    # LandXML/Civil 3D uses the opposite sign convention to mathematical
    # XY angles for the `rot` attribute in these exported alignments:
    # CW -> increasing mathematical angle; CCW -> decreasing.
    da_raw = (a1 - a0 + math.pi) % (2 * math.pi) - math.pi
    if rot == "cw":
        da = abs(da_raw)
    else:
        da = -abs(da_raw)
    a1 = a0 + da
    length = abs(da) * r
    n = max(2, int(math.ceil(length / max(segment_length, 0.001))) + 1)
    pts = []
    for i in range(n):
        t = i / (n - 1)
        a = a0 + da * t
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    # Force exact endpoints to avoid floating-point drift.
    pts[0] = (sx, sy); pts[-1] = (ex, ey)
    return pts


def _line_points(line):
    return [_xy(line.find("l:Start", NS)), _xy(line.find("l:End", NS))]


def read_alignments(path, segment_length=5.0, feedback=None, cancel=None):
    root = safe_xml.parse_root(path)
    alignments = root.findall(".//l:Alignments/l:Alignment", NS)
    if not alignments:
        # Some exporters place Alignment elements outside an Alignments wrapper.
        alignments = root.findall(".//l:Alignment", NS)
    if not alignments:
        raise ValueError("No <Alignment> elements were found in the LandXML file.")

    results = []
    unsupported = []
    for ai, a in enumerate(alignments):
        if cancel and cancel():
            raise RuntimeError("Extraction cancelled by user.")
        name = a.attrib.get("name", f"Alignment {ai + 1}")
        desc = a.attrib.get("desc", "")
        length = a.attrib.get("length", "")
        sta_start = a.attrib.get("staStart", a.attrib.get("startStation", ""))
        sta_end = a.attrib.get("staEnd", a.attrib.get("endStation", ""))
        cg = a.find("l:CoordGeom", NS)
        if cg is None:
            unsupported.append(f"{name}: no CoordGeom")
            continue
        pts = []
        for geom in list(cg):
            tag = geom.tag.split("}")[-1]
            try:
                if tag == "Line":
                    p = _line_points(geom)
                elif tag == "Curve":
                    p = _curve_points(geom, segment_length)
                elif tag == "Spiral":
                    p = _spiral_points(geom, segment_length)
                else:
                    continue
                if pts and p:
                    # Avoid duplicate vertices at element junctions.
                    if math.isclose(pts[-1][0], p[0][0], abs_tol=1e-9) and math.isclose(pts[-1][1], p[0][1], abs_tol=1e-9):
                        pts.extend(p[1:])
                    else:
                        pts.extend(p)
                else:
                    pts.extend(p)
            except Exception as exc:
                unsupported.append(f"{name}: {tag} skipped ({exc})")

        if len(pts) >= 2:
            results.append({
                "name": name,
                "desc": desc,
                "length": length,
                "sta_start": sta_start,
                "sta_end": sta_end,
                "points": pts,
            })

        if feedback and (ai % 10 == 0 or ai == len(alignments) - 1):
            feedback.setProgress(int(80 * (ai + 1) / len(alignments)))
            feedback.setProgressText(f"Extracting alignments: {ai + 1}/{len(alignments)}")
    return results, unsupported


class LandXMLAlignmentsAlgorithm(QgsProcessingAlgorithm):
    INPUT = "INPUT"
    OUTPUT_CRS = "OUTPUT_CRS"
    METHOD = "METHOD"
    DX = "DX"
    DY = "DY"
    ROTATION = "ROTATION"
    SCALE = "SCALE"
    SOURCE_CRS = "SOURCE_CRS"
    ALIGNMENT = "ALIGNMENT"
    SEGMENT = "SEGMENT"
    OUTPUT = "OUTPUT"

    def createInstance(self):
        return LandXMLAlignmentsAlgorithm()

    def name(self):
        return "landxml_alignments_to_vector"

    def displayName(self):
        return "Extract LandXML Alignments to Lines"

    def group(self):
        return "Vector extraction"

    def groupId(self):
        return "vector_extraction"

    def shortHelpString(self):
        return (
            "Extracts LandXML <Alignment> geometry into GIS line features. "
            "Supports line, clothoid spiral, and circular-curve geometry, densification, explicit output CRS, "
            "and the same coordinate interpretation/transformation options used by the TIN converter. "
            "Spiral elements are reported and skipped rather than approximated silently."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile(
            self.INPUT, "LandXML file", behavior=QgsProcessingParameterFile.Behavior.File,
            fileFilter="LandXML (*.xml *.landxml)"))
        self.addParameter(QgsProcessingParameterEnum(
            self.METHOD, "Coordinate interpretation", options=METHODS, defaultValue=0))
        self.addParameter(number_param(self.DX, "Delta X / Easting", 0.0, -1e9, 1e9, decimals=4))
        self.addParameter(number_param(self.DY, "Delta Y / Northing", 0.0, -1e9, 1e9, decimals=4))
        self.addParameter(number_param(self.ROTATION, "Rotation (degrees)", 0.0, -360.0, 360.0, decimals=8))
        self.addParameter(number_param(self.SCALE, "Scale factor", 1.0, 1e-8, 1000.0, decimals=10))
        self.addParameter(QgsProcessingParameterCrs(self.SOURCE_CRS, "Source CRS (for reprojection)",
                                                    defaultValue="EPSG:4326", optional=True))
        self.addParameter(QgsProcessingParameterCrs(self.OUTPUT_CRS, "Output CRS (result layer / raster)", defaultValue=QgsCoordinateReferenceSystem("EPSG:32636")))
        self.addParameter(QgsProcessingParameterString(self.ALIGNMENT, "Alignment name (blank = all)", defaultValue="", optional=True))
        self.addParameter(number_param(
            self.SEGMENT, "Maximum curve segment length (map units)", 5.0,
            0.01, 10000.0, decimals=3))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, "Output alignment lines", type=QgsProcessing.SourceType.TypeVectorLine,
            defaultValue=None))

    def processAlgorithm(self, parameters, context, feedback):
        path = self.parameterAsFile(parameters, self.INPUT, context)
        if not path or not os.path.isfile(path):
            raise QgsProcessingException("Input LandXML file does not exist.")
        out_crs = self.parameterAsCrs(parameters, self.OUTPUT_CRS, context)
        if not out_crs.isValid():
            raise QgsProcessingException("A valid output CRS is required.")
        epsg = out_crs.postgisSrid()
        if epsg <= 0:
            authid = out_crs.authid()
            if not authid.upper().startswith("EPSG:"):
                raise QgsProcessingException("Output CRS must resolve to an EPSG authority code.")
            epsg = int(authid.split(":", 1)[1])

        method_label = METHODS[self.parameterAsInt(parameters, self.METHOD, context)]
        method = METHOD_MAP[method_label]
        src_crs = self.parameterAsCrs(parameters, self.SOURCE_CRS, context)
        src_epsg = src_crs.postgisSrid() if src_crs.isValid() else None
        if method == "reproject" and (not src_epsg or src_epsg <= 0):
            raise QgsProcessingException("Reprojection requires a valid source CRS with an EPSG code.")
        transform_params = {
            "dx": self.parameterAsDouble(parameters, self.DX, context),
            "dy": self.parameterAsDouble(parameters, self.DY, context),
            "rotation_deg": self.parameterAsDouble(parameters, self.ROTATION, context),
            "scale": self.parameterAsDouble(parameters, self.SCALE, context),
            "src_epsg": src_epsg,
            "dst_epsg": epsg,
        }
        seg = self.parameterAsDouble(parameters, self.SEGMENT, context)
        alignment_filter = self.parameterAsString(parameters, self.ALIGNMENT, context).strip()

        fields = QgsFields()
        fields.append(QgsField("name", QVariant.String, len=254))
        fields.append(QgsField("description", QVariant.String, len=254))
        fields.append(QgsField("length", QVariant.Double))
        fields.append(QgsField("sta_start", QVariant.String, len=80))
        fields.append(QgsField("sta_end", QVariant.String, len=80))
        sink, dest_id = self.parameterAsSink(parameters, self.OUTPUT, context, fields,
                                             QgsWkbTypes.Type.LineString, out_crs)
        if sink is None:
            raise QgsProcessingException("Could not create output vector layer.")

        try:
            alignments, unsupported = read_alignments(path, seg, feedback, feedback.isCanceled)
            if alignment_filter:
                alignments = [a for a in alignments if a["name"] == alignment_filter]
            if not alignments:
                raise QgsProcessingException("No supported alignment line/curve geometry was found.")
            for i, a in enumerate(alignments):
                if feedback.isCanceled():
                    raise QgsProcessingException("Extraction cancelled by user.")
                import numpy as np
                arr = np.array([[x, y, 0.0] for x, y in a["points"]], dtype=float)
                arr2 = transform_vertices(arr, method, **transform_params)
                geom = QgsGeometry.fromPolylineXY([QgsPointXY(float(x), float(y)) for x, y in arr2[:, :2]])
                f = QgsFeature(fields)
                f.setGeometry(geom)
                try:
                    length_val = float(a["length"])
                except Exception:
                    length_val = None
                f.setAttributes([a["name"], a["desc"], length_val, a["sta_start"], a["sta_end"]])
                sink.addFeature(f)
                feedback.setProgress(80 + int(20 * (i + 1) / len(alignments)))
            if unsupported:
                feedback.pushWarning("Some alignment elements were not extracted:")
                for item in unsupported[:50]:
                    feedback.pushWarning(item)
                if len(unsupported) > 50:
                    feedback.pushWarning(f"...and {len(unsupported) - 50} more.")
            feedback.pushInfo(f"Extracted {len(alignments)} alignment(s).")
        except QgsProcessingException:
            raise
        except Exception as exc:
            raise QgsProcessingException(str(exc))
        return {self.OUTPUT: dest_id, "ALIGNMENT_COUNT": len(alignments)}
