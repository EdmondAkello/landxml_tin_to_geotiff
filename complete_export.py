import os, math
import numpy as np
from osgeo import ogr, osr, gdal
from qgis.core import *
from qgis.PyQt.QtCore import QVariant
from .core import read_tin, transform_vertices, rasterize_tin, write_geotiff, read_vertical_profile
from .alignment_algorithm import read_alignments
from .params import number_param
from . import safe_xml
NS={'l':'http://www.landxml.org/schema/LandXML-1.2'}


def _ogr_srs(crs):
    s=osr.SpatialReference(); s.ImportFromWkt(crs.toWkt()); return s

def _delete_layer(ds,name):
    # Idempotent "ensure clean slate" delete: check first rather than
    # attempting the delete and silently swallowing any failure, so a
    # genuine GDAL/OGR error here isn't masked -- only "layer doesn't
    # exist yet" is treated as a normal, expected outcome.
    if ds.GetLayerByName(name) is not None:
        ds.DeleteLayer(name)

def _fld(layer,name,typ=ogr.OFTString): layer.CreateField(ogr.FieldDefn(name,typ))

def _write_points(ds,name,records,srs):
    _delete_layer(ds,name); lyr=ds.CreateLayer(name,srs,ogr.wkbPoint)
    _fld(lyr,'alignment'); _fld(lyr,'station',ogr.OFTReal); _fld(lyr,'bearing',ogr.OFTReal)
    for x,y,a,st,b in records:
        f=ogr.Feature(lyr.GetLayerDefn()); g=ogr.Geometry(ogr.wkbPoint); g.AddPoint(x,y); f.SetGeometry(g)
        f.SetField('alignment',a); f.SetField('station',st); f.SetField('bearing',b); lyr.CreateFeature(f)

def _param_common_local(a):
    a.addParameter(QgsProcessingParameterEnum('METHOD','Coordinate interpretation',options=['Use stored coordinates','Swap X / Y','Apply X / Y offset','Swap X / Y + offset','2-D Helmert (similarity)','Reproject from source CRS'],defaultValue=0))
    a.addParameter(number_param('DX','Delta X / Easting', 0, -1e9, 1e9, decimals=4))
    a.addParameter(number_param('DY','Delta Y / Northing', 0, -1e9, 1e9, decimals=4))
    a.addParameter(number_param('ROTATION','Rotation (degrees)', 0, -360, 360, decimals=8))
    a.addParameter(number_param('SCALE','Scale factor', 1, 1e-8, 1000, decimals=10))
    a.addParameter(QgsProcessingParameterCrs('SOURCE_CRS','Source CRS (for reprojection)',defaultValue='EPSG:4326',optional=True))
    a.addParameter(QgsProcessingParameterCrs('OUTPUT_CRS','Output CRS (result layer / raster)',defaultValue=QgsCoordinateReferenceSystem('EPSG:32636')))

def _transform_params(parameters, context, a):
    methods=['stored','swap','offset','swap_offset','helmert','reproject']
    m=methods[a.parameterAsInt(parameters,'METHOD',context)]
    out=a.parameterAsCrs(parameters,'OUTPUT_CRS',context)
    if not out.isValid(): raise QgsProcessingException('Valid output CRS required')
    src=a.parameterAsCrs(parameters,'SOURCE_CRS',context)
    if m=='reproject' and not src.isValid(): raise QgsProcessingException('Reprojection requires a valid source CRS')
    return m,out,src,dict(dx=a.parameterAsDouble(parameters,'DX',context),dy=a.parameterAsDouble(parameters,'DY',context),rotation_deg=a.parameterAsDouble(parameters,'ROTATION',context),scale=a.parameterAsDouble(parameters,'SCALE',context),src_epsg=src.postgisSrid(),dst_epsg=out.postgisSrid())

class CompleteRoadDesignAlgorithm(QgsProcessingAlgorithm):
    def createInstance(self): return CompleteRoadDesignAlgorithm()
    def name(self): return 'landxml_complete_road_design'
    def displayName(self): return 'Export Complete Road Design to GeoPackage'
    def group(self): return 'Road design extraction'
    def groupId(self): return 'road_design_extraction'
    def shortHelpString(self): return 'Export a configurable LandXML road design package with alignment, profile, station, cross-section, feature-line, breakline, surface-boundary, DEM and contour outputs.'
    def initAlgorithm(self,config=None):
        self.addParameter(QgsProcessingParameterFile('INPUT','LandXML file',behavior=QgsProcessingParameterFile.Behavior.File,fileFilter='LandXML (*.xml *.landxml)'))
        _param_common_local(self)
        self.addParameter(QgsProcessingParameterString('ALIGNMENT','Alignment name (blank = all)',defaultValue='',optional=True))
        self.addParameter(QgsProcessingParameterString('SURFACE','TIN surface name (blank = first)',defaultValue='',optional=True))
        self.addParameter(number_param('RESOLUTION','TIN DEM resolution (m)', 5, .01, 10000, decimals=2))
        self.addParameter(number_param('NODATA','DEM NoData value', -9999, -3.4e38, 3.4e38, decimals=3))
        self.addParameter(number_param('CONTOUR_INTERVAL','Contour interval (m; 0 = skip)', 5, 0, 10000, decimals=2))
        self.addParameter(number_param('ALIGNMENT_SEGMENT','Alignment curve/spiral segment length (m)', 5, .01, 10000, decimals=2))
        self.addParameter(number_param('STATION_INTERVAL','Station point interval (m)', 20, .01, 100000, decimals=2))
        self.addParameter(number_param('PROFILE_INTERVAL','Profile sampling interval (m)', 5, .01, 10000, decimals=2))
        for key,label in [('INCLUDE_ALIGNMENTS','Include horizontal alignments'),('INCLUDE_CENTERLINES','Include 3D centerlines'),('INCLUDE_PROFILES','Include vertical profiles'),('INCLUDE_STATIONS','Include station points'),('INCLUDE_CROSSSECTIONS','Include cross-sections'),('INCLUDE_FEATURELINES','Include feature lines'),('INCLUDE_BREAKLINES','Include breaklines'),('INCLUDE_SURFACE_BOUNDARY','Include TIN surface boundary'),('INCLUDE_DEM','Create TIN-derived DEM'),('INCLUDE_CONTOURS','Create contours')]:
            self.addParameter(QgsProcessingParameterBoolean(key,label,defaultValue=True))
        self.addParameter(QgsProcessingParameterFolderDestination('OUTPUT_DIR','Output folder'))

    def processAlgorithm(self,p,c,fb):
        path=self.parameterAsFile(p,'INPUT',c); out=self.parameterAsString(p,'OUTPUT_DIR',c)
        if not path or not os.path.isfile(path): raise QgsProcessingException('Input LandXML file does not exist.')
        os.makedirs(out,exist_ok=True)
        m,crs,src,tp=_transform_params(p,c,self)
        alignment_filter=self.parameterAsString(p,'ALIGNMENT',c).strip(); surface_filter=self.parameterAsString(p,'SURFACE',c).strip() or None
        align_seg=self.parameterAsDouble(p,'ALIGNMENT_SEGMENT',c); station_interval=self.parameterAsDouble(p,'STATION_INTERVAL',c); profile_interval=self.parameterAsDouble(p,'PROFILE_INTERVAL',c); nodata=self.parameterAsDouble(p,'NODATA',c)
        flags={k:self.parameterAsBoolean(p,k,c) for k in ['INCLUDE_ALIGNMENTS','INCLUDE_CENTERLINES','INCLUDE_PROFILES','INCLUDE_STATIONS','INCLUDE_CROSSSECTIONS','INCLUDE_FEATURELINES','INCLUDE_BREAKLINES','INCLUDE_SURFACE_BOUNDARY','INCLUDE_DEM','INCLUDE_CONTOURS']}
        base=os.path.splitext(os.path.basename(path))[0]; gpkg=os.path.join(out,base+'_GIS.gpkg'); srs=_ogr_srs(crs)
        ds=ogr.GetDriverByName('GPKG').CreateDataSource(gpkg)
        root=safe_xml.parse_root(path)
        al,uns=read_alignments(path,align_seg,feedback=fb,cancel=fb.isCanceled)
        if alignment_filter: al=[a for a in al if a['name']==alignment_filter]

        if flags['INCLUDE_ALIGNMENTS'] or flags['INCLUDE_STATIONS'] or flags['INCLUDE_CENTERLINES']:
            if flags['INCLUDE_ALIGNMENTS']:
                _delete_layer(ds,'alignments'); lyr=ds.CreateLayer('alignments',srs,ogr.wkbLineString)
                for n,t in [('name',ogr.OFTString),('description',ogr.OFTString),('length',ogr.OFTReal),('sta_start',ogr.OFTString),('sta_end',ogr.OFTString)]: _fld(lyr,n,t)
                for a in al:
                    arr=transform_vertices(np.asarray([[x,y,0] for x,y in a['points']],float),method=m,**tp); g=ogr.Geometry(ogr.wkbLineString)
                    for x,y,z in arr: g.AddPoint(float(x),float(y))
                    f=ogr.Feature(lyr.GetLayerDefn()); f.SetGeometry(g); f.SetField('name',a['name']); f.SetField('description',a['desc']); f.SetField('length',float(a['length'] or 0)); f.SetField('sta_start',a['sta_start']); f.SetField('sta_end',a['sta_end']); lyr.CreateFeature(f)
            if flags['INCLUDE_STATIONS']:
                ptsrec=[]
                for a in al:
                    arr=transform_vertices(np.asarray([[x,y,0] for x,y in a['points']],float),method=m,**tp); xy=[(float(x),float(y)) for x,y,z in arr]; cum=[0.0]
                    for i in range(1,len(xy)): cum.append(cum[-1]+math.hypot(xy[i][0]-xy[i-1][0],xy[i][1]-xy[i-1][1]))
                    s=0.0
                    while s<=cum[-1]+1e-8:
                        i=next((j for j in range(1,len(cum)) if cum[j]>=s),len(cum)-1); j=max(0,i-1); L=max(cum[i]-cum[j],1e-12); q=(s-cum[j])/L if i else 0; x=xy[j][0]+q*(xy[i][0]-xy[j][0]); y=xy[j][1]+q*(xy[i][1]-xy[j][1]); b=(math.degrees(math.atan2(xy[i][0]-xy[j][0],xy[i][1]-xy[j][1]))+360)%360; ptsrec.append((x,y,a['name'],s+float(a['sta_start'] or 0),b)); s+=station_interval
                _write_points(ds,'station_points',ptsrec,srs)

        if flags['INCLUDE_CENTERLINES'] or flags['INCLUDE_PROFILES']:
            # Profile output and centerline Z assignment. ProfAlign is a flat
            # PVI/ParaCurve/CircCurve sequence, not a ProfileGeom/Line/Curve
            # wrapper -- see core.read_vertical_profile.
            # A ProfAlign's own `name` is a profile label (e.g. "frl" = finish
            # road level), not the owning alignment's name: real LandXML
            # nests <Profile><ProfAlign> inside the <Alignment> it belongs
            # to, it does not cross-reference it by an `alignment=`
            # attribute equal to a ProfAlign name. Associate by walking each
            # Alignment's own subtree first; fall back to an explicit
            # alignment= reference if an exporter uses that pattern instead.
            profiles={}; profile_elem={}
            for a_el in root.findall('.//l:Alignment',NS):
                key=a_el.attrib.get('name','')
                if alignment_filter and key!=alignment_filter: continue
                if key in profiles: continue
                pa=a_el.find('.//l:ProfAlign',NS)
                if pa is not None:
                    samples=read_vertical_profile(pa, profile_interval)
                    if samples: profiles[key]=samples; profile_elem[key]=pa
            for pa in root.findall('.//l:ProfAlign',NS):
                ref=pa.attrib.get('alignment')
                if ref and (not alignment_filter or ref==alignment_filter) and ref not in profiles:
                    samples=read_vertical_profile(pa, profile_interval)
                    if samples: profiles[ref]=samples; profile_elem[ref]=pa
            if flags['INCLUDE_PROFILES']:
                _delete_layer(ds,'profiles'); pl=ds.CreateLayer('profiles',srs,ogr.wkbLineString); _fld(pl,'alignment'); _fld(pl,'profile'); _fld(pl,'sta_start',ogr.OFTReal); _fld(pl,'sta_end',ogr.OFTReal)
                for key,pts in profiles.items():
                    if len(pts)>=2:
                        pa=profile_elem[key]
                        f=ogr.Feature(pl.GetLayerDefn()); g=ogr.Geometry(ogr.wkbLineString); [g.AddPoint(x,y) for x,y in pts]; f.SetGeometry(g); f.SetField('alignment',key); f.SetField('profile',pa.attrib.get('name','')); f.SetField('sta_start',float(pts[0][0])); f.SetField('sta_end',float(pts[-1][0])); pl.CreateFeature(f)
            if flags['INCLUDE_CENTERLINES']:
                _delete_layer(ds,'centerlines_3d'); cl=ds.CreateLayer('centerlines_3d',srs,ogr.wkbLineString25D); _fld(cl,'alignment'); _fld(cl,'z_source')
                def z_at(samples,sta):
                    if not samples: return 0.0
                    if sta<=samples[0][0]: return samples[0][1]
                    if sta>=samples[-1][0]: return samples[-1][1]
                    for i in range(1,len(samples)):
                        st0,z0=samples[i-1]; st1,z1=samples[i]
                        if sta<=st1:
                            q=(sta-st0)/max(st1-st0,1e-12); return z0+q*(z1-z0)
                    return samples[-1][1]
                for a in al:
                    arr=transform_vertices(np.asarray([[x,y,0] for x,y in a['points']],float),method=m,**tp); xy=arr[:,:2]; cum=[0.0]
                    for i in range(1,len(xy)): cum.append(cum[-1]+math.hypot(xy[i,0]-xy[i-1,0],xy[i,1]-xy[i-1,1]))
                    prof=profiles.get(a['name']) or profiles.get(''); g=ogr.Geometry(ogr.wkbLineString25D)
                    for (x,y),d in zip(xy,cum): g.AddPoint(float(x),float(y),float(z_at(prof,float(a['sta_start'] or 0)+d)))
                    f=ogr.Feature(cl.GetLayerDefn()); f.SetGeometry(g); f.SetField('alignment',a['name']); f.SetField('z_source','Vertical profile' if prof else 'No profile; Z=0'); cl.CreateFeature(f)

        if flags['INCLUDE_CROSSSECTIONS']:
            _delete_layer(ds,'cross_sections'); xl=ds.CreateLayer('cross_sections',srs,ogr.wkbLineString25D); _fld(xl,'alignment'); _fld(xl,'station',ogr.OFTReal); _fld(xl,'point_count',ogr.OFTInteger)
            design_only=0
            for cs in root.findall('.//l:CrossSect',NS):
                if alignment_filter and cs.attrib.get('alignment','')!=alignment_filter: continue
                pl=cs.find('.//l:PntList3D',NS); stride=3
                if pl is None: pl=cs.find('.//l:PntList2D',NS); stride=2
                if pl is None:
                    # Station-relative DesignCrossSectSurf/CrossSectPnt design-template
                    # geometry only, no absolute-coordinate point list -- see the
                    # equivalent comment in road_features.CrossSectionsAlgorithm.
                    if cs.find('.//l:DesignCrossSectSurf',NS) is not None: design_only+=1
                    continue
                vals=[float(v) for v in (pl.text or '').split()]; raw=[(vals[i],vals[i+1],vals[i+2] if stride==3 else 0.0) for i in range(0,len(vals)-stride+1,stride)]
                if len(raw)<2: continue
                arr=transform_vertices(np.asarray(raw,float),method=m,**tp); g=ogr.Geometry(ogr.wkbLineString25D); [g.AddPoint(float(x),float(y),float(z)) for x,y,z in arr]
                f=ogr.Feature(xl.GetLayerDefn()); f.SetGeometry(g); f.SetField('alignment',cs.attrib.get('alignment','')); f.SetField('station',float(cs.attrib.get('sta','0') or 0)); f.SetField('point_count',len(raw)); xl.CreateFeature(f)
            if design_only:
                fb.pushWarning(f"Skipped {design_only} CrossSect record(s) with only station-relative "
                                "design-template geometry (DesignCrossSectSurf/CrossSectPnt), not an "
                                "absolute-coordinate point list.")

        for lname,tags,flag in [('feature_lines',['FeatureLine'],'INCLUDE_FEATURELINES'),('breaklines',['Breakline'],'INCLUDE_BREAKLINES')]:
            if not flags[flag]: continue
            _delete_layer(ds,lname); ll=ds.CreateLayer(lname,srs,ogr.wkbLineString25D); _fld(ll,'name'); _fld(ll,'type')
            for tag in tags:
                for node in root.findall('.//l:'+tag,NS):
                    pl=node.find('.//l:PntList3D',NS); stride=3
                    if pl is None: pl=node.find('.//l:PntList2D',NS); stride=2
                    if pl is None: continue
                    vals=[float(v) for v in (pl.text or '').split()]; raw=[(vals[i],vals[i+1],vals[i+2] if stride==3 else 0.0) for i in range(0,len(vals)-stride+1,stride)]
                    if len(raw)<2: continue
                    arr=transform_vertices(np.asarray(raw,float),method=m,**tp); g=ogr.Geometry(ogr.wkbLineString25D); [g.AddPoint(float(x),float(y),float(z)) for x,y,z in arr]; f=ogr.Feature(ll.GetLayerDefn()); f.SetGeometry(g); f.SetField('name',node.attrib.get('name',node.attrib.get('desc',''))); f.SetField('type',tag); ll.CreateFeature(f)

        if flags['INCLUDE_SURFACE_BOUNDARY'] or flags['INCLUDE_DEM'] or flags['INCLUDE_CONTOURS']:
            xyz,faces,meta=read_tin(path,surface_filter); xyz=transform_vertices(xyz,method=m,**tp)
            if flags['INCLUDE_SURFACE_BOUNDARY']:
                _delete_layer(ds,'surface_boundary'); bl=ds.CreateLayer('surface_boundary',srs,ogr.wkbLineString); _fld(bl,'surface'); edges={}
                for a,b,c3 in faces:
                    for u,v in ((a,b),(b,c3),(c3,a)): edges[tuple(sorted((int(u),int(v))))]=edges.get(tuple(sorted((int(u),int(v)))),0)+1
                for u,v in [e for e,n in edges.items() if n==1]:
                    f=ogr.Feature(bl.GetLayerDefn()); g=ogr.Geometry(ogr.wkbLineString); g.AddPoint(float(xyz[u,0]),float(xyz[u,1])); g.AddPoint(float(xyz[v,0]),float(xyz[v,1])); f.SetGeometry(g); f.SetField('surface',meta['surface_name']); bl.CreateFeature(f)
            dem=os.path.join(out,base+'_DEM.tif')
            if flags['INCLUDE_DEM'] or flags['INCLUDE_CONTOURS']:
                arr,xmin,ymax,res=rasterize_tin(xyz,faces,self.parameterAsDouble(p,'RESOLUTION',c),progress=lambda q,t: fb.setProgress(q),cancel=fb.isCanceled)
                if flags['INCLUDE_DEM']:
                    epsg=crs.postgisSrid(); write_geotiff(dem,arr,xmin,ymax,res,nodata,epsg)
                if flags['INCLUDE_CONTOURS'] and self.parameterAsDouble(p,'CONTOUR_INTERVAL',c)>0:
                    if not flags['INCLUDE_DEM']:
                        epsg=crs.postgisSrid(); write_geotiff(dem,arr,xmin,ymax,res,nodata,epsg)
                    contour=os.path.join(out,base+'_Contours.gpkg'); cds=ogr.GetDriverByName('GPKG').CreateDataSource(contour); clyr=cds.CreateLayer('contours',srs,ogr.wkbLineString); _fld(clyr,'elev',ogr.OFTReal); dem_ds=gdal.Open(dem); band=dem_ds.GetRasterBand(1); gdal.ContourGenerate(band,float(self.parameterAsDouble(p,'CONTOUR_INTERVAL',c)),0,[],0,nodata,clyr,-1,0); dem_ds=None; cds=None
        ds=None
        return {'OUTPUT_DIR':out}
