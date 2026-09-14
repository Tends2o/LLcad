"""OpenVDB export of bounded, explicitly truncated implicit samples."""
import sys, math
import numpy as np
from geometry import require
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
    grid.transform=vdb.createLinearTransform(voxelSize=step);grid.transform.translate(tuple(lo))
    grid.copyFromArray(values);grid.prune()
    grid['llcad_semantics']='truncated_implicit_samples'
    grid['source_field_semantics']=f['field']['semantics']
    grid['value_unit']=f['field'].get('value_unit','length')
    grid['unit']='mm';grid['coordinate_unit']='mm';grid['domain_min']=tuple(lo);grid['domain_max']=tuple(hi)
    grid['sampled_max']=tuple(lo+step*(n-1))
    grid['band_limit']=float(band);grid['continuous_distance_certificate']='none'
    grid['sample_storage_error_max']=rounding_error;grid['sample_truncation_error_max']=truncation_error
    vdb.write(path,grid)
    restored,metadata=vdb.readAll(path)
    require(len(restored)==1,'Unerwartete Gridanzahl beim OpenVDB-Roundtrip.')
    actual=np.empty_like(values);restored[0].copyToArray(actual)
    require(np.array_equal(actual,values),'OpenVDB-Roundtrip verändert gespeicherte Samples.')
    require(np.allclose(restored[0].transform.indexToWorld((0,0,0)),lo,rtol=0,atol=1e-12),'OpenVDB-Transformationsfehler.')
    require(restored[0]['value_unit']==f['field'].get('value_unit','length') and restored[0]['coordinate_unit']=='mm' and restored[0]['source_field_semantics']==f['field']['semantics'],'OpenVDB-Einheiten oder Feldsemantik gingen beim Roundtrip verloren.')
    for axis in np.eye(3): require(np.allclose(restored[0].transform.indexToWorld(tuple(axis)),lo+step*axis,rtol=0,atol=1e-12),'OpenVDB-Voxelmaßstab ging verloren.')
    return {'status':'checks_passed_within_profile','method':'OpenVDB_full_voxel_roundtrip','library_version':list(vdb.LIBRARY_VERSION),
            'grid_count':1,'sample_count':int(np.prod(n)),'active_voxels':grid.activeVoxelCount(),
            'sample_roundtrip_error':0,'voxel_size_mm':step,'domain':c['domain'],'sampled_max':list(lo+step*(n-1)),
            'sample_storage_error_max':rounding_error,'sample_truncation_error_max':truncation_error,'truncated_samples':truncated,
            'stored_semantics':'truncated_implicit_samples','source_field_semantics':f['field']['semantics'],
            'value_unit':f['field'].get('value_unit','length'),
            'continuous_distance_certificate':None,'float_storage':'float32','band_limit':band,
            'lost_semantics':['analytic_expression_graph_in_standalone_vdb']}
