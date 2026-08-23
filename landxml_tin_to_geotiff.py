from qgis.core import QgsApplication

from .provider import LandXMLTinToGeoTIFFProvider


class LandXMLTinToGeoTIFFPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.provider = None

    def initGui(self):
        self.provider = LandXMLTinToGeoTIFFProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def unload(self):
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None
