from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import QgsProcessingProvider

from .processing_algorithm import LandXMLTinToGeoTIFFAlgorithm
from .alignment_algorithm import LandXMLAlignmentsAlgorithm
from .road_features import ProfileAlgorithm, Centerline3DAlgorithm, CrossSectionsAlgorithm, SurfaceBoundaryAlgorithm, StationPointsAlgorithm, FeatureLinesAlgorithm, BreaklinesAlgorithm
from .complete_export import CompleteRoadDesignAlgorithm


class LandXMLTinToGeoTIFFProvider(QgsProcessingProvider):
    PROVIDER_ID = "landxml_tin"

    def id(self):
        return self.PROVIDER_ID

    def name(self):
        return self.tr("LandXML TIN")

    def displayName(self):
        return self.tr("LandXML TIN")

    def longName(self):
        return self.tr("LandXML TIN to GeoTIFF")

    def icon(self):
        from qgis.PyQt.QtGui import QIcon
        import os
        return QIcon(os.path.join(os.path.dirname(__file__), "icon.png"))

    def tr(self, string):
        return QCoreApplication.translate("LandXMLTinToGeoTIFFProvider", string)

    def loadAlgorithms(self):
        self.addAlgorithm(LandXMLTinToGeoTIFFAlgorithm())
        self.addAlgorithm(LandXMLAlignmentsAlgorithm())
        self.addAlgorithm(ProfileAlgorithm())
        self.addAlgorithm(Centerline3DAlgorithm())
        self.addAlgorithm(CrossSectionsAlgorithm())
        self.addAlgorithm(SurfaceBoundaryAlgorithm())
        self.addAlgorithm(StationPointsAlgorithm())
        self.addAlgorithm(FeatureLinesAlgorithm())
        self.addAlgorithm(BreaklinesAlgorithm())
        self.addAlgorithm(CompleteRoadDesignAlgorithm())
