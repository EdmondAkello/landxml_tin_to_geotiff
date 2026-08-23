import os, math, xml.etree.ElementTree as ET
from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm, QgsProcessingParameterFile,
    QgsProcessingParameterCrs, QgsProcessingParameterFeatureSink,
    QgsProcessingParameterDistance, QgsProcessingParameterNumber,
    QgsProcessingParameterEnum, QgsProcessingParameterString, QgsProcessingParameterFolderDestination, QgsProcessingParameterBoolean,
    QgsProcessingException, QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry, QgsPoint, QgsPointXY,
    QgsFields, QgsField, QgsWkbTypes
)
from qgis.PyQt.QtCore import QVariant
from osgeo import ogr, osr, gdal
from .core import transform_vertices
from .alignment_algorithm import _spiral_points
from .core import read_tin, write_geotiff, rasterize_tin, bounds, read_vertical_profile
from .params import number_param

NS={"l":"http://www.landxml.org/schema/LandXML-1.2"}
METHODS=["Use stored coordinates","Swap X / Y","Apply X / Y offset","Swap X / Y + offset","2-D Helmert (similarity)","Reproject from source CRS"]

def _xy(text):
    v=[float(x) for x in (text or '').split()[:2]]
    if len(v)<2: raise ValueError('Expected X Y coordinates')
    return v[0],v[1]

def _param_common(a):
    a.addParameter(QgsProcessingParameterString('ALIGNMENT','Alignment name (blank = all)',defaultValue='',optional=True))
    a.addParameter(QgsProcessingParameterEnum('METHOD','Coordinate interpretation',options=METHODS,defaultValue=0))
    a.addParameter(number_param('DX', 'Delta X / Easting', 0, -1e9, 1e9, decimals=4))
    a.addParameter(number_param('DY', 'Delta Y / Northing', 0, -1e9, 1e9, decimals=4))
    a.addParameter(number_param('ROTATION', 'Rotation (degrees)', 0, -360, 360, decimals=8))
    a.addParameter(number_param('SCALE', 'Scale factor', 1, 1e-8, 1000, decimals=10))
    a.addParameter(QgsProcessingParameterCrs('SOURCE_CRS','Source CRS (for reprojection)',defaultValue='EPSG:4326',optional=True))
    a.addParameter(QgsProcessingParameterCrs('OUTPUT_CRS','Output CRS (result layer / raster)',defaultValue=QgsCoordinateReferenceSystem('EPSG:32636')))

def _method_map(i): return ['stored','swap','offset','swap_offset','helmert','reproject'][i]

def _transform_params(parameters, context, a):
    m=_method_map(a.parameterAsInt(parameters,'METHOD',context)); out=a.parameterAsCrs(parameters,'OUTPUT_CRS',context)
    if not out.isValid(): raise QgsProcessingException('Valid output CRS required')
    src=a.parameterAsCrs(parameters,'SOURCE_CRS',context)
    return m,out,src,dict(dx=a.parameterAsDouble(parameters,'DX',context),dy=a.parameterAsDouble(parameters,'DY',context),rotation_deg=a.parameterAsDouble(parameters,'ROTATION',context),scale=a.parameterAsDouble(parameters,'SCALE',context),src_epsg=src.postgisSrid(),dst_epsg=out.postgisSrid())

def _sink(a, parameters, context, fields, wkb):
    return a.parameterAsSink(parameters,'OUTPUT',context,fields,wkb,a.parameterAsCrs(parameters,'OUTPUT_CRS',context))

def _line_chain(points):
    return QgsGeometry.fromPolylineXY([QgsPointXY(x,y) for x,y in points])

def _alignment_parts(path, segment_length=5.0, alignment_filter=""):

    root=ET.parse(path).getroot(); ans=[]
    for a in root.findall('.//l:Alignment',NS):
        name=a.attrib.get('name','Alignment'); desc=a.attrib.get('desc','');
        if alignment_filter and name != alignment_filter: continue
        sta0=a.attrib.get('staStart',a.attrib.get('startStation','')); sta1=a.attrib.get('staEnd',a.attrib.get('endStation',''))
        cg=a.find('l:CoordGeom',NS); parts=[]; unsupported=[]
        if cg is not None:
            for g in list(cg):
                tag=g.tag.split('}')[-1]
                if tag=='Line':
                    s=g.find('l:Start',NS); e=g.find('l:End',NS)
                    if s is not None and e is not None: parts.append([_xy(s.text),_xy(e.text)])
                elif tag=='Curve':
                    s=g.find('l:Start',NS); e=g.find('l:End',NS); c=g.find('l:Center',NS)
                    if s is None or e is None or c is None: continue
                    sx,sy=_xy(s.text); ex,ey=_xy(e.text); cx,cy=_xy(c.text); r=float(g.attrib.get('radius') or math.hypot(sx-cx,sy-cy)); a0=math.atan2(sy-cy,sx-cx); a1=math.atan2(ey-cy,ex-cx); rot=g.attrib.get('rot','cw').lower()
                    da_raw=(a1-a0+math.pi)%(2*math.pi)-math.pi
                    da=abs(da_raw) if rot=='cw' else -abs(da_raw)
                    a1=a0+da
                    n=max(3,int(math.ceil(abs(da)*r/max(segment_length,0.001)))+1); pts=[(cx+r*math.cos(a0+da*i/(n-1)),cy+r*math.sin(a0+da*i/(n-1))) for i in range(n)]; pts[0]=(sx,sy); pts[-1]=(ex,ey); parts.append(pts)
                elif tag=='Spiral':
                    try:
                        parts.append(_spiral_points(g, segment_length))
                    except Exception:
                        unsupported.append('Spiral')
        ans.append((name,desc,sta0,sta1,parts,unsupported))
    return ans

class ProfileAlgorithm(QgsProcessingAlgorithm):
    def createInstance(self): return ProfileAlgorithm()
    def name(self): return 'landxml_profiles_to_vector'
    def displayName(self): return 'Extract LandXML Profiles'
    def group(self): return 'Road design extraction'
    def groupId(self): return 'road_design_extraction'
    def shortHelpString(self): return 'Extract vertical profile geometry and PVI/grade data into GIS lines and profile points.'
    def initAlgorithm(self,config=None):
        self.addParameter(QgsProcessingParameterFile('INPUT','LandXML file',behavior=QgsProcessingParameterFile.File,fileFilter='LandXML (*.xml *.landxml)')); _param_common(self)
        self.addParameter(QgsProcessingParameterString('PROFILE','Profile name (blank = all)',defaultValue='',optional=True))
        self.addParameter(number_param('POINT_INTERVAL', 'Sampling interval for profile line (m)', 5, .01, 10000, decimals=2))
        self.addParameter(QgsProcessingParameterFeatureSink('OUTPUT','Profile lines',type=QgsProcessing.TypeVectorLine))
    def processAlgorithm(self,p,c,fb):
        path=self.parameterAsFile(p,'INPUT',c); m,out,src,tp=_transform_params(p,c,self); alignment_filter=self.parameterAsString(p,'ALIGNMENT',c).strip(); profile_filter=self.parameterAsString(p,'PROFILE',c).strip()
        fields=QgsFields()
        for n in ('alignment','profile','sta_start','sta_end'): fields.append(QgsField(n,QVariant.String,len=254))
        sink, dest=_sink(self,p,c,fields,QgsWkbTypes.LineString); root=ET.parse(path).getroot(); n=0
        interval=max(self.parameterAsDouble(p,'POINT_INTERVAL',c),.01)
        for prof in root.findall('.//l:ProfAlign',NS):
            if fb.isCanceled(): break
            if alignment_filter and prof.attrib.get('alignment',prof.attrib.get('name','')) != alignment_filter: continue
            if profile_filter and prof.attrib.get('name','') != profile_filter: continue
            # ProfAlign is a flat PVI/ParaCurve/CircCurve sequence, not a
            # ProfileGeom/Line/Curve wrapper -- see core.read_vertical_profile.
            pts=read_vertical_profile(prof, interval)
            if len(pts)<2: continue
            sta,end=pts[0][0],pts[-1][0]
            # Represent profile as station,elevation pairs in GIS coordinate space; horizontal X=station, Y=elevation.
            geom=QgsGeometry.fromPolylineXY([QgsPointXY(x,y) for x,y in pts]); feat=QgsFeature(fields); feat.setGeometry(geom); feat.setAttributes([prof.attrib.get('alignment',''),prof.attrib.get('name',''),str(sta),str(end)]); sink.addFeature(feat)
            n+=1
        return {'OUTPUT':dest}

class Centerline3DAlgorithm(QgsProcessingAlgorithm):
    def createInstance(self): return Centerline3DAlgorithm()
    def name(self): return 'landxml_3d_centerlines'
    def displayName(self): return 'Create 3D Road Centerlines'
    def group(self): return 'Road design extraction'
    def groupId(self): return 'road_design_extraction'
    def shortHelpString(self): return 'Creates 3D centerlines by combining horizontal alignment geometry with the matching LandXML vertical profile when available.'
    def initAlgorithm(self,config=None):
        self.addParameter(QgsProcessingParameterFile('INPUT','LandXML file',behavior=QgsProcessingParameterFile.File,fileFilter='LandXML (*.xml *.landxml)')); _param_common(self)
        self.addParameter(number_param('SEGMENT', 'Maximum geometry segment length (m)', 5, .01, 10000, decimals=2))
        self.addParameter(QgsProcessingParameterFeatureSink('OUTPUT','3D centerlines',type=QgsProcessing.TypeVectorLine))
    def processAlgorithm(self,p,c,fb):
        path=self.parameterAsFile(p,'INPUT',c); m,out,src,tp=_transform_params(p,c,self); alignment_filter=self.parameterAsString(p,'ALIGNMENT',c).strip(); seg=self.parameterAsDouble(p,'SEGMENT',c)
        fields=QgsFields()
        for n,ln in (('alignment',254),('sta_start',80),('sta_end',80),('z_source',80)): fields.append(QgsField(n,QVariant.String,len=ln))
        sink,dest=_sink(self,p,c,fields,QgsWkbTypes.LineStringZ); root=ET.parse(path).getroot()
        # Build sampled vertical profiles keyed by alignment name. ProfAlign
        # is a flat PVI/ParaCurve/CircCurve sequence -- see core.read_vertical_profile.
        # A ProfAlign's own `name` is a profile label (e.g. "frl" = finish
        # road level), not the owning alignment's name: real LandXML nests
        # <Profile><ProfAlign> inside the <Alignment> it belongs to, it does
        # not cross-reference it by an `alignment=` attribute equal to a
        # ProfAlign name. Associate by walking each Alignment's own subtree
        # first; fall back to an explicit alignment= reference if an exporter
        # uses that pattern instead.
        profiles={}
        for a_el in root.findall('.//l:Alignment',NS):
            key=a_el.attrib.get('name','')
            if key in profiles: continue
            pa=a_el.find('.//l:ProfAlign',NS)
            if pa is not None:
                samples=read_vertical_profile(pa, seg)
                if samples: profiles[key]=samples
        for prof in root.findall('.//l:ProfAlign',NS):
            ref=prof.attrib.get('alignment')
            if ref and ref not in profiles:
                samples=read_vertical_profile(prof, seg)
                if samples: profiles[ref]=samples
        def z_at(samples,sta):
            if not samples: return 0.0
            if sta<=samples[0][0]: return samples[0][1]
            if sta>=samples[-1][0]: return samples[-1][1]
            for i in range(1,len(samples)):
                st0,z0=samples[i-1]; st1,z1=samples[i]
                if sta<=st1:
                    q=(sta-st0)/max(st1-st0,1e-12); return z0+q*(z1-z0)
            return samples[-1][1]
        parts_all=_alignment_parts(path,seg,alignment_filter);
        for ai,(name,desc,sta0,sta1,parts,unsupported) in enumerate(parts_all):
            if fb.isCanceled(): break
            pts=[]
            for part in parts:
                if pts and part and pts[-1]==part[0]: pts.extend(part[1:])
                else: pts.extend(part)
            if len(pts)<2: continue
            import numpy as np
            arr=np.asarray([[x,y,0] for x,y in pts],dtype=float); arr=transform_vertices(arr,method=m,**tp); xy=arr[:,:2]
            cum=[0.0]
            for i in range(1,len(xy)): cum.append(cum[-1]+math.hypot(xy[i,0]-xy[i-1,0],xy[i,1]-xy[i-1,1]))
            s0=float(sta0 or 0); prof=profiles.get(name) or profiles.get('')
            z=[z_at(prof,s0+d) for d in cum]
            points3=[QgsPoint(float(x),float(y),float(zz)) for (x,y),zz in zip(xy,z)]
            feat=QgsFeature(fields); feat.setGeometry(QgsGeometry.fromPolyline(points3)); feat.setAttributes([name,sta0,sta1,'Vertical profile' if prof else 'No profile; Z=0']); sink.addFeature(feat); fb.setProgress(int(100*(ai+1)/max(1,len(parts_all))))
        return {'OUTPUT':dest}

class SurfaceBoundaryAlgorithm(QgsProcessingAlgorithm):
    def createInstance(self): return SurfaceBoundaryAlgorithm()
    def name(self): return 'landxml_surface_boundary'
    def displayName(self): return 'Extract TIN Surface Boundary'
    def group(self): return 'Surface extraction'
    def groupId(self): return 'surface_extraction'
    def shortHelpString(self): return 'Extracts the outer boundary of the LandXML TIN as a polygon/line.'
    def initAlgorithm(self,config=None):
        self.addParameter(QgsProcessingParameterFile('INPUT','LandXML file',behavior=QgsProcessingParameterFile.File,fileFilter='LandXML (*.xml *.landxml)')); _param_common(self); self.addParameter(QgsProcessingParameterString('SURFACE','Surface name (blank = first)',defaultValue='',optional=True))
        self.addParameter(QgsProcessingParameterFeatureSink('OUTPUT','Surface boundary',type=QgsProcessing.TypeVectorLine))
    def processAlgorithm(self,p,c,fb):
        path=self.parameterAsFile(p,'INPUT',c); surf=self.parameterAsString(p,'SURFACE',c) or None; xyz,faces,meta=read_tin(path,surf); m,out,src,tp=_transform_params(p,c,self); xyz=transform_vertices(xyz,method=m,**tp)
        edges={}
        for a,b,cc in faces:
            for u,v in [(a,b),(b,cc),(cc,a)]:
                k=tuple(sorted((int(u),int(v)))); edges[k]=edges.get(k,0)+1
        bedges=[k for k,v in edges.items() if v==1]
        adj={}
        for a,b in bedges: adj.setdefault(a,[]).append(b); adj.setdefault(b,[]).append(a)
        lines=[]; unused=set(bedges)
        while unused:
            a,b=next(iter(unused)); line=[a,b]; unused.remove((a,b)); unused.discard((b,a)); cur=b; prev=a
            while True:
                nxts=[n for n in adj.get(cur,[]) if n!=prev and (min(cur,n),max(cur,n)) in unused]
                if not nxts: break
                nxt=nxts[0]; unused.discard((min(cur,nxt),max(cur,nxt))); line.append(nxt); prev,cur=cur,nxt
            lines.append(line)
        fields=QgsFields(); fields.append(QgsField('surface',QVariant.String,len=254)); sink,dest=_sink(self,p,c,fields,QgsWkbTypes.LineString)
        for line in lines:
            f=QgsFeature(fields); f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(xyz[i,0],xyz[i,1]) for i in line])); f.setAttribute('surface',meta['surface_name']); sink.addFeature(f)
        return {'OUTPUT':dest}

class StationPointsAlgorithm(QgsProcessingAlgorithm):
    def createInstance(self): return StationPointsAlgorithm()
    def name(self): return 'landxml_station_points'
    def displayName(self): return 'Create Alignment Station Points'
    def group(self): return 'Road design extraction'
    def groupId(self): return 'road_design_extraction'
    def shortHelpString(self): return 'Creates points along LandXML alignments at a user-defined station interval.'
    def initAlgorithm(self,config=None):
        self.addParameter(QgsProcessingParameterFile('INPUT','LandXML file',behavior=QgsProcessingParameterFile.File,fileFilter='LandXML (*.xml *.landxml)')); _param_common(self); self.addParameter(number_param('INTERVAL', 'Station interval', 20, .01, 100000, decimals=2)); self.addParameter(QgsProcessingParameterBoolean('INCLUDE_ENDPOINT','Include alignment end station',defaultValue=True))
        self.addParameter(QgsProcessingParameterFeatureSink('OUTPUT','Station points',type=QgsProcessing.TypeVectorPoint))
    def processAlgorithm(self,p,c,fb):
        path=self.parameterAsFile(p,'INPUT',c); m,out,src,tp=_transform_params(p,c,self); alignment_filter=self.parameterAsString(p,'ALIGNMENT',c).strip(); include_end=self.parameterAsBoolean(p,'INCLUDE_ENDPOINT',c)
        fields=QgsFields()
        for n in ('alignment','station','bearing_deg'): fields.append(QgsField(n,QVariant.Double if n!='alignment' else QVariant.String,len=254))
        sink,dest=_sink(self,p,c,fields,QgsWkbTypes.Point); interval=self.parameterAsDouble(p,'INTERVAL',c)
        for name,desc,sta0,sta1,parts,_ in _alignment_parts(path,5.0,alignment_filter):
            pts=[pt for part in parts for pt in (part if not [] else part)]
            if len(pts)<2: continue
            # approximate stationing by geometry length
            import numpy as np
            arr=np.column_stack([np.array([x for x,y in pts]),np.array([y for x,y in pts]),np.zeros(len(pts))]); arr=transform_vertices(arr,method=m,**tp); coords=[(float(x),float(y)) for x,y,_ in arr]
            cum=[0.0]
            for i in range(1,len(coords)): cum.append(cum[-1]+math.hypot(coords[i][0]-coords[i-1][0],coords[i][1]-coords[i-1][1]))
            s=0.0
            while s<=cum[-1]+1e-8:
                i=next((j for j in range(1,len(cum)) if cum[j]>=s),len(cum)-1); prev=max(0,i-1); L=max(cum[i]-cum[prev],1e-12); q=(s-cum[prev])/L if i else 0; x=coords[prev][0]+q*(coords[i][0]-coords[prev][0]); y=coords[prev][1]+q*(coords[i][1]-coords[prev][1]); bearing=(math.degrees(math.atan2(coords[i][0]-coords[prev][0],coords[i][1]-coords[prev][1]))+360)%360
                f=QgsFeature(fields); f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x,y))); f.setAttributes([name,s+float(sta0 or 0),bearing]); sink.addFeature(f); s+=interval
            if include_end and (not cum or cum[-1] > 0):
                s=cum[-1]; i=len(cum)-1; prev=max(0,i-1); L=max(cum[i]-cum[prev],1e-12); x=coords[i][0]; y=coords[i][1]; bearing=(math.degrees(math.atan2(coords[i][0]-coords[prev][0],coords[i][1]-coords[prev][1]))+360)%360
                if not pts or s > 0:
                    f=QgsFeature(fields); f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x,y))); f.setAttributes([name,s+float(sta0 or 0),bearing]); sink.addFeature(f)
        return {'OUTPUT':dest}


class CrossSectionsAlgorithm(QgsProcessingAlgorithm):
    def createInstance(self): return CrossSectionsAlgorithm()
    def name(self): return 'landxml_cross_sections'
    def displayName(self): return 'Extract LandXML Cross-Sections'
    def group(self): return 'Road design extraction'
    def groupId(self): return 'road_design_extraction'
    def shortHelpString(self): return 'Extracts LandXML CrossSect geometry as line features. Supports PntList3D/PntList2D-style section point lists and station attributes where present. CrossSect records that only carry station-relative DesignCrossSectSurf/CrossSectPnt design-template geometry (pavement/subbase layers etc.), without an absolute-coordinate point list, are skipped and reported in Processing messages rather than being placed at a fabricated position.'
    def initAlgorithm(self,config=None):
        self.addParameter(QgsProcessingParameterFile('INPUT','LandXML file',behavior=QgsProcessingParameterFile.File,fileFilter='LandXML (*.xml *.landxml)')); _param_common(self)
        self.addParameter(QgsProcessingParameterFeatureSink('OUTPUT','Cross-section lines',type=QgsProcessing.TypeVectorLine))
    def processAlgorithm(self,p,c,fb):
        path=self.parameterAsFile(p,'INPUT',c); m,out,src,tp=_transform_params(p,c,self); alignment_filter=self.parameterAsString(p,'ALIGNMENT',c).strip()
        fields=QgsFields(); fields.append(QgsField('alignment',QVariant.String,len=254)); fields.append(QgsField('station',QVariant.Double)); fields.append(QgsField('point_count',QVariant.Int))
        sink,dest=_sink(self,p,c,fields,QgsWkbTypes.LineString); root=ET.parse(path).getroot(); count=0
        design_only=0
        for cs in root.findall('.//l:CrossSect',NS):
            if fb.isCanceled(): break
            if alignment_filter and cs.attrib.get('alignment','') != alignment_filter: continue
            raw=[]
            pl=cs.find('.//l:PntList3D',NS)
            if pl is None: pl=cs.find('.//l:PntList2D',NS)
            if pl is None:
                # No CrossSectSurf/PntList absolute-coordinate point list --
                # this CrossSect only carries DesignCrossSectSurf/CrossSectPnt
                # records, which are STATION-RELATIVE (offset, elev-delta)
                # design-template geometry, not absolute X/Y/Z. Reconstructing
                # real coordinates from those needs the alignment's coordinate
                # geometry and perpendicular-offset transform at that station,
                # which this algorithm does not currently do -- skip rather
                # than silently fabricate a wrong absolute position.
                if cs.find('.//l:DesignCrossSectSurf',NS) is not None:
                    design_only+=1
                continue
            vals=[float(v) for v in (pl.text or '').split()]
            stride=3 if pl.tag.endswith('PntList3D') else 2
            for i in range(0,len(vals)-stride+1,stride): raw.append((vals[i],vals[i+1],vals[i+2] if stride==3 else 0.0))
            if len(raw)<2: continue
            import numpy as np
            arr=np.asarray(raw,dtype=float); arr=transform_vertices(arr,method=m,**tp)
            f=QgsFeature(fields); f.setGeometry(QgsGeometry.fromPolyline([QgsPoint(*row) for row in arr])); f.setAttributes([cs.attrib.get('alignment',''),float(cs.attrib.get('sta','0') or 0),len(raw)]); sink.addFeature(f); count+=1
        if design_only:
            fb.pushWarning(f"Skipped {design_only} CrossSect record(s) that only contain station-relative "
                            "DesignCrossSectSurf/CrossSectPnt design-template geometry (pavement/subbase "
                            "layers etc.), not an absolute-coordinate CrossSectSurf point list. These are "
                            "not currently reconstructed into GIS coordinates.")
        return {'OUTPUT':dest}

class GenericLinesAlgorithm(QgsProcessingAlgorithm):
    MODE='FEATURE'
    def createInstance(self): return GenericLinesAlgorithm(self.mode)
    def __init__(self,mode='FEATURE'): super().__init__(); self.mode=mode
    def name(self): return 'landxml_' + ('feature_lines' if self.mode=='FEATURE' else 'breaklines')
    def displayName(self): return 'Extract LandXML ' + ('Feature Lines' if self.mode=='FEATURE' else 'Breaklines')
    def group(self): return 'Surface extraction'
    def groupId(self): return 'surface_extraction'
    def shortHelpString(self): return 'Extracts 3D polyline-style FeatureLine or Breakline records from LandXML when present. Unsupported structures are skipped with a Processing message.'
    def initAlgorithm(self,config=None):
        self.addParameter(QgsProcessingParameterFile('INPUT','LandXML file',behavior=QgsProcessingParameterFile.File,fileFilter='LandXML (*.xml *.landxml)')); _param_common(self); self.addParameter(QgsProcessingParameterString('NAME_FILTER','Feature/breakline name (blank = all)',defaultValue='',optional=True))
        self.addParameter(QgsProcessingParameterFeatureSink('OUTPUT','Output lines',type=QgsProcessing.TypeVectorLine))
    def processAlgorithm(self,p,c,fb):
        path=self.parameterAsFile(p,'INPUT',c); m,out,src,tp=_transform_params(p,c,self); name_filter=self.parameterAsString(p,'NAME_FILTER',c).strip()
        fields=QgsFields(); fields.append(QgsField('name',QVariant.String,len=254)); fields.append(QgsField('type',QVariant.String,len=40))
        sink,dest=_sink(self,p,c,fields,QgsWkbTypes.LineStringZ); root=ET.parse(path).getroot(); tags=['FeatureLine'] if self.mode=='FEATURE' else ['Breakline']
        seen=0
        for tag in tags:
            for node in root.findall('.//l:'+tag,NS):
                if name_filter and node.attrib.get('name',node.attrib.get('desc','')) != name_filter: continue
                pl=node.find('.//l:PntList3D',NS)
                if pl is None: pl=node.find('.//l:PntList2D',NS)
                if pl is None: continue
                vals=[float(v) for v in (pl.text or '').split()]; stride=3 if pl.tag.endswith('PntList3D') else 2; arr=[]
                for i in range(0,len(vals)-stride+1,stride): arr.append((vals[i],vals[i+1],vals[i+2] if stride==3 else 0.0))
                if len(arr)<2: continue
                import numpy as np
                xyz=transform_vertices(np.asarray(arr,dtype=float),method=m,**tp); f=QgsFeature(fields); f.setGeometry(QgsGeometry.fromPolyline([QgsPoint(*r) for r in xyz])); f.setAttributes([node.attrib.get('name',node.attrib.get('desc','')),tag]); sink.addFeature(f); seen+=1
        return {'OUTPUT':dest}

class FeatureLinesAlgorithm(GenericLinesAlgorithm):
    def __init__(self): super().__init__('FEATURE')
    def createInstance(self): return FeatureLinesAlgorithm()

class BreaklinesAlgorithm(GenericLinesAlgorithm):
    def __init__(self): super().__init__('BREAK')
    def createInstance(self): return BreaklinesAlgorithm()
