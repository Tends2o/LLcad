"""OpenVDB export of bounded, explicitly truncated implicit samples, and import of sampled grids
as interpolating cubic B-spline fields with a measured Lipschitz bound (Bauplan 17.2, 21.5)."""
import sys, os, math, json, itertools
import numpy as np
from geometry import require
from frames import arrays,world_bounds
sys.path.append('/usr/lib/python3/dist-packages')
import pyopenvdb as vdb

def export_vdb(f,path,sample):
    c=f['construction'];lo=np.array(c['domain']['min'],float);hi=np.array(c['domain']['max'],float)
    step=f['field']['cell_size'];n=np.floor((hi-lo)/step).astype(int)+1
    require(max(n)<=128 and int(np.prod(n))<=125000,'OpenVDB-Ausgabe überschreitet das deklarierte Voxelbudget.','BUDGET_EXCEEDED')
    band=step*4*f['field']['lipschitz']
    require(math.isfinite(band) and 0<band<=float(np.finfo(np.float32).max),'OpenVDB-Band überschreitet den endlichen Float32-Bereich.','BUDGET_EXCEEDED')
    values=np.empty(tuple(n),dtype=np.float32)
    rounding_error=0.;truncation_error=0.;truncated=0
    for index in np.ndindex(*n):
        value=sample(lo+step*np.array(index));require(math.isfinite(value),'Nichtendliche OpenVDB-Eingangsprobe.','GEOMETRY_INVALID')
        clipped=float(np.clip(value,-band,band));values[index]=clipped
        rounding_error=max(rounding_error,abs(float(values[index])-clipped))
        truncation_error=max(truncation_error,abs(value-clipped));truncated+=int(value!=clipped)
    require(np.isfinite(values).all(),'OpenVDB enthält nichtendliche gespeicherte Werte.','GEOMETRY_INVALID')
    grid=vdb.FloatGrid(background=float(np.float32(band)));grid.name=f['id']
    rotation,translation=arrays(f.get('placement'))
    transform=np.eye(4);transform[:3,:3]=step*rotation.T;transform[3,:3]=rotation@lo+translation
    grid.transform=vdb.createLinearTransform(transform.tolist())
    probe_indices=[(0,0,0),*map(tuple,np.eye(3)),*itertools.product(*[(0,int(v)-1) for v in n])]
    expected_transform=[tuple(grid.transform.indexToWorld(index)) for index in probe_indices]
    grid.copyFromArray(values);grid.prune()
    grid['llcad_semantics']='truncated_implicit_samples'
    grid['source_field_semantics']=f['field']['semantics']
    grid['value_unit']=f['field'].get('value_unit','length')
    grid['unit']='mm';grid['coordinate_unit']='mm';grid['domain_min']=tuple(lo);grid['domain_max']=tuple(hi)
    grid['sampled_max']=tuple(lo+step*(n-1))
    grid['domain_frame']=f.get('local_frame','world')
    grid['frame_to_world']=json.dumps(f.get('placement') or {'rotation':rotation.tolist(),'translation':translation.tolist()},sort_keys=True)
    grid['band_limit']=float(band);grid['continuous_distance_certificate']='none'
    grid['sample_storage_error_max']=rounding_error;grid['sample_truncation_error_max']=truncation_error
    vdb.write(path,grid)
    restored,metadata=vdb.readAll(path)
    require(len(restored)==1,'Unerwartete Gridanzahl beim OpenVDB-Roundtrip.')
    actual=np.empty_like(values);restored[0].copyToArray(actual)
    require(np.array_equal(actual,values),'OpenVDB-Roundtrip verändert gespeicherte Samples.')
    require([tuple(restored[0].transform.indexToWorld(index)) for index in probe_indices]==expected_transform,'OpenVDB-Transformationsfehler.')
    require(restored[0]['value_unit']==f['field'].get('value_unit','length') and restored[0]['coordinate_unit']=='mm' and restored[0]['source_field_semantics']==f['field']['semantics'],'OpenVDB-Einheiten oder Feldsemantik gingen beim Roundtrip verloren.')
    require(restored[0]['domain_frame']==f.get('local_frame','world') and restored[0]['frame_to_world']==grid['frame_to_world'],'OpenVDB-Bezugsrahmen ging verloren.')
    return {'status':'checks_passed_within_profile','method':'OpenVDB_full_voxel_roundtrip','library_version':list(vdb.LIBRARY_VERSION),
            'grid_count':1,'sample_count':int(np.prod(n)),'active_voxels':grid.activeVoxelCount(),
            'sample_roundtrip_error':0,'voxel_size_mm':step,'domain':c['domain'],'sampled_max':list(lo+step*(n-1)),
            'domain_frame':f.get('local_frame','world'),'world_domain_bounds':world_bounds([*lo,*hi],f.get('placement')),
            'transform_roundtrip_error':0,'transform_probes':len(probe_indices),
            'sample_storage_error_max':rounding_error,'sample_truncation_error_max':truncation_error,'truncated_samples':truncated,
            'stored_semantics':'truncated_implicit_samples','source_field_semantics':f['field']['semantics'],
            'value_unit':f['field'].get('value_unit','length'),
            'continuous_distance_certificate':None,'float_storage':'float32','band_limit':band,
            'lost_semantics':['analytic_expression_graph_in_standalone_vdb']}

MAX_IMPORT_VOXELS=2000000
MAX_IMPORT_BYTES=64*1024**2
_GRIDS={}
UNIT_SCALE={'mm':1.0,'m':1000.0,'um':0.001}

def _linear_axes(transform):
    """Origin and per-axis voxel size of an axis-aligned index->world map, or None when rotated or sheared."""
    if not transform.isLinear:return None
    origin=np.array(transform.indexToWorld((0,0,0)),float)
    axes=np.array([np.array(transform.indexToWorld(tuple(int(v) for v in np.eye(3)[i])),float)-origin for i in range(3)])
    diagonal=np.diag(axes).copy();off=axes-np.diag(diagonal)
    if np.abs(off).max()>1e-12*max(1.0,float(np.abs(axes).max())) or np.any(diagonal<=0):return None
    return origin,diagonal

def _metadata(grid):
    out={}
    for key in list(grid.metadata)[:32]:
        value=grid.metadata[key]
        if isinstance(value,(bool,int,float)):
            if isinstance(value,float) and not math.isfinite(value):continue
            out[str(key)[:64]]=value
        elif isinstance(value,str):out[str(key)[:64]]=value[:200]
        elif isinstance(value,(tuple,list)) and len(value)<=4 and all(isinstance(v,(int,float)) and math.isfinite(v) for v in value):out[str(key)[:64]]=[float(v) for v in value]
    return out

def _natural_coefficients(values):
    """Interpolating uniform cubic B-spline coefficients with natural ends, axis by axis.

    Interior knots satisfy (c[i-1]+4c[i]+c[i+1])/6 = v[i]; zero second derivative at both ends gives
    c[0]=v[0], c[n-1]=v[n-1] and linear extrapolation c[-1]=2c[0]-c[1], c[n]=2c[n-1]-c[n-2]. Linear
    data are reproduced exactly. Returns the coefficients padded by that one extrapolated layer.
    """
    from scipy.linalg import solve_banded
    c=np.array(values,float)
    for axis in range(3):
        moved=np.moveaxis(c,axis,0);n=moved.shape[0];flat=moved.reshape(n,-1)
        if n>2:
            rhs=6*flat[1:-1].copy();rhs[0]-=flat[0];rhs[-1]-=flat[-1]
            m=n-2;band=np.zeros((3,m));band[0,1:]=1;band[1,:]=4;band[2,:-1]=1
            interior=solve_banded((1,1),band,rhs) if m>1 else rhs/4
            flat=np.concatenate([flat[:1],interior,flat[-1:]])
        padded=np.concatenate([2*flat[:1]-flat[1:2],flat,2*flat[-1:]-flat[-2:-1]])
        c=np.moveaxis(padded.reshape(n+2,*moved.shape[1:]),0,axis)
    return c

def _weights(t):
    """Uniform cubic B-spline basis values for the four knots around local parameter t in [0, 1]."""
    return np.array([(1-t)**3,3*t**3-6*t**2+4,-3*t**3+3*t**2+3*t+1,t**3])/6

def load_grid(path,name=None,source_unit='mm'):
    """Load one scalar grid as an interpolating cubic B-spline field with rigorous difference bounds.

    The stored samples are interpolated by a C^2 natural cubic B-spline (tensor product). Every value
    on a box is a convex combination of the coefficients in its support window and every partial
    derivative a convex combination of consecutive coefficient differences, so hulls over those
    windows are rigorous enclosures. Outside the active voxel box the field continues with the
    nearest boundary value (C^0 there). World coordinates and length-valued samples are converted
    from source_unit to mm. There is no continuous distance certificate.
    """
    scale=UNIT_SCALE[source_unit];key=(os.path.abspath(path),name,scale)
    if key in _GRIDS:return _GRIDS[key]
    require(os.path.isfile(path),'OpenVDB-Eingabe fehlt.','ACCESS_DENIED')
    require(os.path.getsize(path)<=MAX_IMPORT_BYTES,'OpenVDB-Datei überschreitet das Budget.','BUDGET_EXCEEDED')
    grids,_=vdb.readAll(path)
    require(grids,'OpenVDB-Datei enthält kein Grid.','INVALID_SCHEMA')
    named=[g for g in grids if name is None or g.name==name]
    require(named,'Benanntes OpenVDB-Grid fehlt.','INVALID_SCHEMA')
    require(len(named)==1,'Mehrere Grids in der Datei; grid-Name angeben.','OUT_OF_SCOPE')
    grid=named[0]
    require(isinstance(grid,vdb.FloatGrid),'Nur skalare Float-Grids sind als Feld importierbar.','OUT_OF_SCOPE')
    linear=_linear_axes(grid.transform)
    require(linear is not None,'Nur achsparallele OpenVDB-Transformationen (Skalierung und Verschiebung) werden importiert.','OUT_OF_SCOPE')
    origin,voxel=linear
    (i0,j0,k0),(i1,j1,k1)=grid.evalActiveVoxelBoundingBox()
    dims=np.array([i1-i0+1,j1-j0+1,k1-k0+1],int)
    require(grid.activeVoxelCount()>0 and bool(np.all(dims>=2)),'OpenVDB-Grid ohne ausgedehnte aktive Voxelregion.','GEOMETRY_INVALID')
    require(int(np.prod(dims))<=MAX_IMPORT_VOXELS,'OpenVDB-Voxelbudget überschritten (aktive Box höchstens 2 Millionen Voxel).','BUDGET_EXCEEDED')
    stored=np.empty(tuple(int(d) for d in dims),np.float32);grid.copyToArray(stored,ijk=(int(i0),int(j0),int(k0)))
    values=stored.astype(float)
    require(bool(np.isfinite(values).all()),'OpenVDB enthält nichtendliche Werte.','GEOMETRY_INVALID')
    metadata=_metadata(grid)
    coordinate_unit=metadata.get('coordinate_unit') or metadata.get('unit')
    require(coordinate_unit in (None,source_unit),'Koordinateneinheit der Datei widerspricht source_unit.','UNIT_MISMATCH')
    declared_value_unit=metadata.get('value_unit')
    require(declared_value_unit in (None,'length','dimensionless'),'Unbekannte Werteinheit im OpenVDB-Grid.','UNIT_MISMATCH')
    value_unit=declared_value_unit or 'length'
    value_scale=scale if value_unit=='length' else 1.0
    values=values*value_scale
    padded=_natural_coefficients(values)
    require(bool(np.isfinite(padded).all()),'Spline-Interpolation erzeugte nichtendliche Koeffizienten.','GEOMETRY_INVALID')
    base=np.array([i0,j0,k0],float);spacing=voxel*scale
    axis_bounds=[]
    for axis in range(3):
        d=np.abs(np.diff(padded,axis=axis))/spacing[axis]
        axis_bounds.append(float(np.nextafter(d.max(),np.inf))*(1+1e-12) if d.size else 0.0)
    lipschitz=float(np.nextafter(math.sqrt(sum(b*b for b in axis_bounds)),np.inf))*(1+1e-12)
    lo=(origin+voxel*base)*scale;hi=(origin+voxel*(base+dims-1))*scale
    upper=(dims-1).astype(float)
    def sample(point):
        u=np.minimum(np.maximum((np.asarray(point,float)/scale-origin)/voxel-base,0.0),upper)
        i=np.minimum(np.floor(u).astype(int),dims-2);t=u-i
        block=padded[i[0]:i[0]+4,i[1]:i[1]+4,i[2]:i[2]+4]
        return float(np.einsum('i,j,k,ijk->',_weights(t[0]),_weights(t[1]),_weights(t[2]),block))
    probes=[np.zeros(3,int),dims-1,(dims-1)//2,np.array([dims[0]-1,0,(dims[2]-1)//2])]
    interpolation_error=max(abs(sample((origin+voxel*(base+index))*scale)-values[tuple(index)]) for index in probes)
    result={'sample':sample,'values':values,'coefficients':padded,'origin':origin,'voxel':voxel,'scale':scale,'base':base,'dims':dims,'spacing_mm':spacing,
            'lipschitz':lipschitz,'axis_lipschitz':axis_bounds,'domain':[lo.tolist(),hi.tolist()],'cell_size':float(spacing.min()),
            'value_unit':value_unit,'declared_value_unit':declared_value_unit,'grid_name':grid.name,'metadata':metadata,
            'active_voxels':int(grid.activeVoxelCount()),'background':float(grid.background)*value_scale,
            'value_range':[float(values.min()),float(values.max())],'coefficient_range':[float(padded.min()),float(padded.max())],
            'interpolation_error_at_probes':float(interpolation_error),'semantics':'sampled_implicit_cubic_bspline',
            'source_semantics':metadata.get('source_field_semantics') or metadata.get('llcad_semantics') or 'unknown'}
    _GRIDS[key]=result
    return result

def import_field(f):
    """Imported VDB feature as a field feature over the active voxel box (world mm)."""
    c=f['construction'];path='input-'+c['artifact_id']+'.vdb';unit=c.get('source_unit','mm')
    g=load_grid(path,c.get('grid'),unit)
    decimal=lambda v:format(float(v),'.15f').rstrip('0').rstrip('.') or '0'
    node={'op':'sampled_grid','artifact_id':c['artifact_id'],'lipschitz':decimal(max(g['lipschitz'],1e-12)),'value_unit':g['value_unit'],
          'source_unit':unit,'interpolation':'trilinear',**({'grid':c['grid']} if c.get('grid') else {})}
    lo,hi=g['domain']
    field=dict(f,construction={'operator':'field','expression':node,'domain':{'min':lo,'max':hi},'cell_size':g['cell_size']},
               field={'semantics':g['semantics'],'lipschitz':g['lipschitz'],'cell_size':g['cell_size'],'value_unit':g['value_unit']})
    report={'method':'OpenVDB_active_box_trilinear_import','library_version':list(vdb.LIBRARY_VERSION),'grid_name':g['grid_name'],
            'source_unit':unit,'voxel_size_mm':g['spacing_mm'].tolist(),'dimensions':g['dims'].tolist(),'active_voxels':g['active_voxels'],
            'domain_mm':{'min':lo,'max':hi},'value_unit':g['value_unit'],'value_unit_assumed':g['declared_value_unit'] is None,
            'value_range':g['value_range'],'background':g['background'],'lipschitz_bound':g['lipschitz'],'axis_lipschitz_bounds':g['axis_lipschitz'],
            'lipschitz_method':'max_absolute_spline_coefficient_difference_per_axis_over_voxel_size_directed_rounding',
            'interpolation':'interpolating_natural_cubic_bspline_C2_inside_active_box_clamped_outside','coefficient_range':g['coefficient_range'],
            'interpolation_error_at_probes':g['interpolation_error_at_probes'],'source_field_semantics':g['source_semantics'],
            'continuous_distance_certificate':None,'stored_metadata':g['metadata'],
            'lost_semantics':['analytic_expression_graph','sub_voxel_topology','distance_property_beyond_stored_samples']}
    return field,report
