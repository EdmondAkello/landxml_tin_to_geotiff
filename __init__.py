def classFactory(iface):
    from .landxml_tin_to_geotiff import LandXMLTinToGeoTIFFPlugin
    return LandXMLTinToGeoTIFFPlugin(iface)
