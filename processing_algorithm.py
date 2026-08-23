import json
import os

from qgis.PyQt.QtCore import QSettings
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterFile,
    QgsProcessingParameterString,
    QgsProcessingParameterNumber,
    QgsProcessingParameterEnum,
    QgsProcessingParameterCrs,
    QgsProcessingParameterRasterDestination,
    QgsProcessingException,
    QgsCoordinateReferenceSystem,
    QgsMessageLog,
    Qgis,
)

from .core import (
    list_surfaces,
    declared_epsg,
    read_tin,
    transform_vertices,
    bounds,
    rasterize_tin,
    write_geotiff,
)
from .params import number_param

PRESETS_KEY = "LandXMLTinToGeoTIFF/presets"
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


class LandXMLTinToGeoTIFFAlgorithm(QgsProcessingAlgorithm):
    INPUT = "INPUT"
    SURFACE = "SURFACE"
    RESOLUTION = "RESOLUTION"
    NODATA = "NODATA"
    METHOD = "METHOD"
    DX = "DX"
    DY = "DY"
    ROTATION = "ROTATION"
    SCALE = "SCALE"
    SOURCE_CRS = "SOURCE_CRS"
    OUTPUT_CRS = "OUTPUT_CRS"
    OUTPUT = "OUTPUT"

    def tr(self, text):
        return text

    def createInstance(self):
        return LandXMLTinToGeoTIFFAlgorithm()

    def name(self):
        return "landxml_tin_to_geotiff"

    def displayName(self):
        return self.tr("LandXML TIN to GeoTIFF")

    def group(self):
        return self.tr("Terrain conversion")

    def groupId(self):
        return "terrain_conversion"

    def shortHelpString(self):
        return self.tr(
            "Converts a LandXML TIN surface directly to a Float32 GeoTIFF using the original LandXML triangle faces and planar interpolation. "
            "Supports coordinate interpretation, offsets, 2-D Helmert transformation, reprojection, selectable surface, output CRS, and configurable raster resolution. "
            "Do not infer a coordinate transformation from unusual coordinates without validating it against survey control."
        )

    def icon(self):
        from qgis.PyQt.QtGui import QIcon
        return QIcon(os.path.join(os.path.dirname(__file__), "icon.png"))

    def initAlgorithm(self, config=None):
        p = QgsProcessingParameterFile(self.INPUT, self.tr("LandXML file"), behavior=QgsProcessingParameterFile.File, fileFilter="LandXML (*.xml *.landxml)")
        self.addParameter(p)
        self.addParameter(QgsProcessingParameterString(self.SURFACE, self.tr("TIN surface name"), defaultValue="", optional=True))
        self.addParameter(number_param(self.RESOLUTION, self.tr("Pixel size"), 5.0, 0.01, 10000.0, decimals=3))
        self.addParameter(number_param(self.NODATA, self.tr("NoData value"), -9999.0, -3.4e38, 3.4e38, decimals=3))
        self.addParameter(QgsProcessingParameterEnum(self.METHOD, self.tr("Coordinate interpretation"), options=METHODS, defaultValue=0))
        self.addParameter(number_param(self.DX, self.tr("Delta X / Easting"), 0.0, -1e9, 1e9, decimals=4))
        self.addParameter(number_param(self.DY, self.tr("Delta Y / Northing"), 0.0, -1e9, 1e9, decimals=4))
        self.addParameter(number_param(self.ROTATION, self.tr("Rotation (degrees)"), 0.0, -360.0, 360.0, decimals=8))
        self.addParameter(number_param(self.SCALE, self.tr("Scale factor"), 1.0, 1e-8, 1000.0, decimals=10))
        self.addParameter(QgsProcessingParameterCrs(self.SOURCE_CRS, self.tr("Source CRS (for reprojection)"), defaultValue="EPSG:4326", optional=True))
        self.addParameter(QgsProcessingParameterCrs(self.OUTPUT_CRS, self.tr("Output CRS (result layer / raster)"), defaultValue=QgsCoordinateReferenceSystem("EPSG:32636")))
        out = QgsProcessingParameterRasterDestination(self.OUTPUT, self.tr("Output GeoTIFF"))
        self.addParameter(out)

    def processAlgorithm(self, parameters, context, feedback):
        path = self.parameterAsFile(parameters, self.INPUT, context)
        if not path or not os.path.isfile(path):
            raise QgsProcessingException("Input LandXML file does not exist.")

        surface = self.parameterAsString(parameters, self.SURFACE, context).strip() or None
        try:
            available = list_surfaces(path)
        except Exception as exc:
            raise QgsProcessingException(f"Could not read LandXML surfaces: {exc}")
        if not available:
            raise QgsProcessingException("No TIN surfaces were found in the LandXML file.")
        if surface is None:
            surface = available[0][0]
        elif surface not in [x[0] for x in available]:
            raise QgsProcessingException(f"Surface '{surface}' was not found. Available surfaces: {', '.join(x[0] for x in available)}")

        method_index = self.parameterAsInt(parameters, self.METHOD, context)
        method_label = METHODS[method_index]
        method = METHOD_MAP[method_label]
        resolution = self.parameterAsDouble(parameters, self.RESOLUTION, context)
        nodata = self.parameterAsDouble(parameters, self.NODATA, context)

        output_crs = self.parameterAsCrs(parameters, self.OUTPUT_CRS, context)
        if not output_crs.isValid():
            raise QgsProcessingException("A valid output CRS is required.")
        epsg = output_crs.postgisSrid()
        if epsg <= 0:
            # write_geotiff accepts EPSG only; use its auth id if it is an EPSG code.
            authid = output_crs.authid()
            if not authid.upper().startswith("EPSG:"):
                raise QgsProcessingException("Output CRS must resolve to an EPSG authority code for GeoTIFF creation.")
            try:
                epsg = int(authid.split(":", 1)[1])
            except Exception:
                raise QgsProcessingException("Could not determine output EPSG code.")

        src_crs = self.parameterAsCrs(parameters, self.SOURCE_CRS, context)
        src_epsg = src_crs.postgisSrid() if src_crs.isValid() else None
        if method == "reproject" and (not src_epsg or src_epsg <= 0):
            raise QgsProcessingException("Reprojection requires a valid source CRS with an EPSG code.")

        params = {
            "dx": self.parameterAsDouble(parameters, self.DX, context),
            "dy": self.parameterAsDouble(parameters, self.DY, context),
            "rotation_deg": self.parameterAsDouble(parameters, self.ROTATION, context),
            "scale": self.parameterAsDouble(parameters, self.SCALE, context),
            "src_epsg": src_epsg,
            "dst_epsg": epsg,
        }

        output = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)
        if not output:
            raise QgsProcessingException("Output GeoTIFF path is required.")

        def progress(value, message):
            feedback.setProgress(value)
            feedback.setProgressText(message)
            if feedback.isCanceled():
                raise QgsProcessingException("Conversion cancelled.")

        feedback.pushInfo(f"LandXML-declared CRS: EPSG:{declared_epsg(path) or 'not specified'} (reference only)")
        feedback.pushInfo(f"Surface: {surface}")
        feedback.pushInfo(f"Coordinate interpretation: {method_label}")
        feedback.pushInfo(f"Output CRS: EPSG:{epsg}")

        try:
            vertices, faces, meta = read_tin(path, surface, progress, feedback.isCanceled)
            raw_bounds = bounds(vertices)
            vertices = transform_vertices(vertices, method, **params)
            transformed_bounds = bounds(vertices)
            feedback.pushInfo("Raw bounds: X %.3f–%.3f; Y %.3f–%.3f" % raw_bounds)
            feedback.pushInfo("Transformed bounds: X %.3f–%.3f; Y %.3f–%.3f" % transformed_bounds)
            arr, xmin, ymax, res = rasterize_tin(vertices, faces, resolution, nodata, progress, feedback.isCanceled)
            write_geotiff(output, arr, xmin, ymax, res, nodata, epsg)
        except QgsProcessingException:
            raise
        except Exception as exc:
            raise QgsProcessingException(str(exc))

        feedback.setProgress(100)
        return {
            self.OUTPUT: output,
            "POINT_COUNT": meta["point_count"],
            "FACE_COUNT": meta["face_count"],
            "RAW_BOUNDS": str(raw_bounds),
            "TRANSFORMED_BOUNDS": str(transformed_bounds),
            "ELEVATION_MIN": meta["zmin"],
            "ELEVATION_MAX": meta["zmax"],
        }
