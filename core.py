"""LandXML TIN -> GeoTIFF conversion core for QGIS.

The original LandXML TIN topology is preserved. Coordinates can be interpreted
as stored, axis-swapped, translated, transformed with a 2-D Helmert operation,
or reprojected with a source/target CRS using GDAL/OSR.
"""
from __future__ import annotations

import math
import os
import xml.etree.ElementTree as ET

import numpy as np
from osgeo import gdal, osr

NS = {"l": "http://www.landxml.org/schema/LandXML-1.2"}


def list_surfaces(path):
    root = ET.parse(path).getroot()
    surfaces = root.findall(".//l:Surfaces/l:Surface", NS)
    return [(s.attrib.get("name", f"Surface {i+1}"), s) for i, s in enumerate(surfaces)]


def declared_epsg(path):
    root = ET.parse(path).getroot()
    cs = root.find("l:CoordinateSystem", NS)
    if cs is None:
        return None
    value = cs.attrib.get("epsgCode")
    if value and str(value).isdigit():
        return int(value)
    return None


def _surface_elements(root, surface_name):
    for s in root.findall(".//l:Surfaces/l:Surface", NS):
        if s.attrib.get("name", "") == surface_name:
            return s
    raise ValueError(f"Surface not found: {surface_name}")


def read_tin(path, surface_name=None, progress=None, cancel=None):
    if progress:
        progress(2, "Parsing LandXML…")
    root = ET.parse(path).getroot()
    surfaces = root.findall(".//l:Surfaces/l:Surface", NS)
    if not surfaces:
        raise ValueError("No <Surface> elements were found in the LandXML file.")
    if surface_name is None:
        surface = surfaces[0]
        surface_name = surface.attrib.get("name", "Surface 1")
    else:
        surface = _surface_elements(root, surface_name)

    pnts = surface.find(".//l:Definition/l:Pnts", NS)
    faces = surface.find(".//l:Definition/l:Faces", NS)
    if pnts is None or faces is None:
        pnts_candidates = surface.findall(".//l:Pnts", NS)
        face_candidates = surface.findall(".//l:Faces", NS)
        pnts = pnts_candidates[0] if pnts_candidates else None
        faces = face_candidates[0] if face_candidates else None
    if pnts is None or faces is None:
        raise ValueError(f"Surface '{surface_name}' does not contain a TIN Definition with Pnts and Faces.")

    point_nodes = pnts.findall("l:P", NS)
    if not point_nodes:
        raise ValueError(f"Surface '{surface_name}' contains no TIN points.")

    xyz = np.empty((len(point_nodes), 3), dtype=np.float64)
    id_to_idx = {}
    for i, p in enumerate(point_nodes):
        try:
            pid = int(p.attrib["id"])
            vals = [float(v) for v in (p.text or "").split()[:3]]
            if len(vals) != 3:
                raise ValueError
        except Exception as exc:
            raise ValueError(f"Invalid LandXML point at position {i+1}.") from exc
        xyz[i] = vals
        id_to_idx[pid] = i

    face_nodes = faces.findall("l:F", NS)
    if not face_nodes:
        raise ValueError(f"Surface '{surface_name}' contains no TIN faces.")
    face_list = []
    for i, f in enumerate(face_nodes):
        vals = (f.text or "").split()
        if len(vals) < 3:
            continue
        try:
            refs = [int(v) for v in vals[:3]]
            face_list.append([id_to_idx[r] for r in refs])
        except KeyError as exc:
            raise ValueError(f"Face {i+1} references missing point ID {exc.args[0]}.") from exc
    if not face_list:
        raise ValueError("No valid triangular faces were found.")

    farr = np.asarray(face_list, dtype=np.int32)
    b = xyz[:, :2]
    epsg = declared_epsg(path)
    meta = {
        "surface_name": surface_name,
        "point_count": len(xyz),
        "face_count": len(farr),
        "epsg": epsg,
        "bounds": (float(b[:, 0].min()), float(b[:, 1].min()), float(b[:, 0].max()), float(b[:, 1].max())),
        "zmin": float(xyz[:, 2].min()),
        "zmax": float(xyz[:, 2].max()),
    }
    if progress:
        progress(12, f"Loaded {len(xyz):,} vertices and {len(farr):,} triangles.")
    return xyz, farr, meta


def transform_vertices(vertices, method="stored", **params):
    """Apply a coordinate interpretation/transformation to X/Y; Z is preserved."""
    out = np.array(vertices, dtype=np.float64, copy=True)
    x = out[:, 0].copy()
    y = out[:, 1].copy()

    if method == "stored":
        return out
    if method == "swap":
        out[:, 0], out[:, 1] = y, x
        return out
    if method == "offset":
        out[:, 0] = x + float(params.get("dx", 0.0))
        out[:, 1] = y + float(params.get("dy", 0.0))
        return out
    if method == "swap_offset":
        out[:, 0] = y + float(params.get("dx", 0.0))
        out[:, 1] = x + float(params.get("dy", 0.0))
        return out
    if method == "helmert":
        # 2-D similarity: X' = tx + s(cosθ X - sinθ Y), Y' = ty + s(sinθ X + cosθ Y)
        theta = math.radians(float(params.get("rotation_deg", 0.0)))
        scale = float(params.get("scale", 1.0))
        tx = float(params.get("dx", 0.0)); ty = float(params.get("dy", 0.0))
        c, s = math.cos(theta), math.sin(theta)
        out[:, 0] = tx + scale * (c * x - s * y)
        out[:, 1] = ty + scale * (s * x + c * y)
        return out
    if method == "reproject":
        src_epsg = int(params["src_epsg"]); dst_epsg = int(params["dst_epsg"])
        src = osr.SpatialReference(); dst = osr.SpatialReference()
        if src.ImportFromEPSG(src_epsg) != 0 or dst.ImportFromEPSG(dst_epsg) != 0:
            raise ValueError("Could not load one of the specified EPSG codes.")
        src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        ct = osr.CoordinateTransformation(src, dst)
        transformed = np.empty_like(out)
        for i in range(len(out)):
            xx, yy, zz = ct.TransformPoint(float(x[i]), float(y[i]), 0.0)
            transformed[i, 0] = xx; transformed[i, 1] = yy; transformed[i, 2] = out[i, 2]
        return transformed
    raise ValueError(f"Unknown coordinate transformation method: {method}")


def read_vertical_profile(prof_align, sample_interval=5.0):
    """Sample a LandXML <ProfAlign> vertical profile into (station, elevation) points.

    Real Civil 3D/LandXML exports do NOT wrap the profile in a <ProfileGeom>
    element with <Line>/<Curve> children carrying staStart/staEnd/elevStart/
    elevEnd attributes -- that structure does not exist in the LandXML 1.2
    schema and never matched real exports. The actual profile is a flat,
    ordered sequence of <PVI> (tangent vertex; text is "station elevation")
    and <ParaCurve>/<CircCurve> (vertical curve; text is the curve's own PVI
    station/elevation, i.e. where the incoming and outgoing tangent grades
    would otherwise intersect; the `length` attribute is the curve's
    horizontal length, symmetric about that station -- length/2 before and
    length/2 after) elements, directly under <ProfAlign>.

    Grades for a curve are derived from its immediate neighbours in the
    sequence and the standard AASHTO symmetric parabolic vertical curve
    formula is used to sample through the curve; PVI vertices and the
    straight tangents between control points are otherwise exact.
    """
    ctrl = []
    for el in prof_align:
        tag = el.tag.split("}")[-1]
        if tag not in ("PVI", "ParaCurve", "CircCurve"):
            continue
        parts = (el.text or "").split()
        if len(parts) < 2:
            continue
        try:
            sta = float(parts[0])
            elev = float(parts[1])
        except ValueError:
            continue
        length_attr = el.attrib.get("length")
        length = float(length_attr) if length_attr not in (None, "") else None
        ctrl.append((sta, elev, length))
    if not ctrl:
        return []
    ctrl.sort(key=lambda c: c[0])

    samples = []
    n = len(ctrl)
    for i, (sta, elev, length) in enumerate(ctrl):
        if not length:
            samples.append((sta, elev))
            continue
        prev_sta, prev_elev, _ = ctrl[i - 1] if i > 0 else (sta, elev, None)
        next_sta, next_elev, _ = ctrl[i + 1] if i < n - 1 else (sta, elev, None)
        g_in = (elev - prev_elev) / (sta - prev_sta) if sta != prev_sta else 0.0
        g_out = (next_elev - elev) / (next_sta - sta) if next_sta != sta else 0.0
        half = length / 2.0
        bvc_sta = sta - half
        bvc_elev = elev - g_in * half
        count = max(2, int(math.ceil(length / max(sample_interval, 0.01))) + 1)
        for k in range(count):
            x = length * k / (count - 1)
            e = bvc_elev + g_in * x + (g_out - g_in) / (2.0 * length) * x * x
            samples.append((bvc_sta + x, e))

    samples.sort(key=lambda s: s[0])
    dedup = []
    for s in samples:
        if dedup and abs(s[0] - dedup[-1][0]) < 1e-9:
            continue
        dedup.append(s)
    return dedup


def bounds(vertices):
    return (float(vertices[:, 0].min()), float(vertices[:, 1].min()),
            float(vertices[:, 0].max()), float(vertices[:, 1].max()))


def rasterize_tin(vertices, faces, resolution, nodata=-9999.0, progress=None, cancel=None,
                  max_cells=250_000_000):
    if resolution <= 0:
        raise ValueError("Resolution must be greater than zero.")
    x = vertices[:, 0]; y = vertices[:, 1]
    xmin, xmax = float(x.min()), float(x.max())
    ymin, ymax = float(y.min()), float(y.max())
    xmin_r = math.floor(xmin / resolution) * resolution
    ymax_r = math.ceil(ymax / resolution) * resolution
    xmax_r = math.ceil(xmax / resolution) * resolution
    ymin_r = math.floor(ymin / resolution) * resolution
    cols = int(round((xmax_r - xmin_r) / resolution)); rows = int(round((ymax_r - ymin_r) / resolution))
    if cols <= 0 or rows <= 0:
        raise ValueError("Computed raster dimensions are invalid.")
    cells = rows * cols
    if cells > max_cells:
        raise ValueError(f"Requested resolution produces {cells:,} cells ({cols:,} × {rows:,}), exceeding the safety limit of {max_cells:,}. Increase the pixel size.")

    arr = np.full((rows, cols), np.float32(nodata), dtype=np.float32)
    eps = max(resolution * 1e-8, 1e-9)
    nfaces = len(faces)
    for fi in range(nfaces):
        if cancel and cancel():
            raise RuntimeError("Conversion cancelled by user.")
        ia, ib, ic = faces[fi]
        x1, y1, z1 = vertices[ia]; x2, y2, z2 = vertices[ib]; x3, y3, z3 = vertices[ic]
        det = (x2 - x1) * (y3 - y1) - (x3 - x1) * (y2 - y1)
        if abs(det) < 1e-14:
            continue
        c0 = int(math.floor((min(x1, x2, x3) - xmin_r) / resolution))
        c1 = int(math.ceil((max(x1, x2, x3) - xmin_r) / resolution))
        r0 = int(math.floor((ymax_r - max(y1, y2, y3)) / resolution))
        r1 = int(math.ceil((ymax_r - min(y1, y2, y3)) / resolution))
        c0 = max(0, min(cols - 1, c0)); c1 = max(0, min(cols, c1 + 1))
        r0 = max(0, min(rows - 1, r0)); r1 = max(0, min(rows, r1 + 1))
        if c0 >= c1 or r0 >= r1: continue
        xs = xmin_r + (np.arange(c0, c1, dtype=np.float64) + 0.5) * resolution
        ys = ymax_r - (np.arange(r0, r1, dtype=np.float64) + 0.5) * resolution
        X = xs[None, :]; Y = ys[:, None]
        e1 = (x2 - x1) * (Y - y1) - (y2 - y1) * (X - x1)
        e2 = (x3 - x2) * (Y - y2) - (y3 - y2) * (X - x2)
        e3 = (x1 - x3) * (Y - y3) - (y1 - y3) * (X - x3)
        inside = ((e1 >= -eps) & (e2 >= -eps) & (e3 >= -eps)) | ((e1 <= eps) & (e2 <= eps) & (e3 <= eps))
        if not inside.any(): continue
        a = ((z1 * (y2 - y3) + z2 * (y3 - y1) + z3 * (y1 - y2)) / det)
        b = ((z1 * (x3 - x2) + z2 * (x1 - x3) + z3 * (x2 - x1)) / det)
        c = z1 - a * x1 - b * y1
        Z = (a * X + b * Y + c).astype(np.float32, copy=False)
        arr[r0:r1, c0:c1][inside] = Z[inside]
        if progress and (fi % 5000 == 0 or fi == nfaces - 1):
            progress(12 + int(83 * (fi + 1) / nfaces), f"Rasterizing triangles: {fi+1:,}/{nfaces:,}")
    return arr, xmin_r, ymax_r, resolution


def _crs_from_epsg(epsg):
    if not epsg: return None
    srs = osr.SpatialReference()
    if srs.ImportFromEPSG(int(epsg)) != 0: return None
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs.ExportToWkt()


def write_geotiff(path, array, xmin, ymax, resolution, nodata=-9999.0, epsg=None, creation_options=None):
    rows, cols = array.shape
    options = creation_options or ["TILED=YES", "COMPRESS=DEFLATE", "PREDICTOR=2", "BIGTIFF=IF_SAFER"]
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, cols, rows, 1, gdal.GDT_Float32, options=options)
    if ds is None: raise IOError(f"Could not create output GeoTIFF: {path}")
    ds.SetGeoTransform((xmin, resolution, 0.0, ymax, 0.0, -resolution))
    if epsg:
        wkt = _crs_from_epsg(epsg)
        if wkt: ds.SetProjection(wkt)
    band = ds.GetRasterBand(1); band.SetNoDataValue(float(nodata)); band.WriteArray(array)
    band.SetDescription("LandXML TIN elevation"); band.FlushCache(); ds.FlushCache(); ds = None
    return path
