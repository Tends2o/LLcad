"""Rigid reference frames. Construction remains local; public geometry is world-space."""
import itertools
import numpy as np
from geometry import gp_Trsf, BRepBuilderAPI_Transform
import topology

IDENTITY={'rotation':[[1.,0.,0.],[0.,1.,0.],[0.,0.,1.]],'translation':[0.,0.,0.],'hash':'world'}

def arrays(placement):
    p=placement or IDENTITY
    return np.array(p['rotation'],float),np.array(p['translation'],float)

def between(source,target=None):
    if source and target and source['hash']==target['hash']:return None
    a,x=arrays(source);b,y=arrays(target)
    rotation=b.T@a;translation=b.T@(x-y)
    if np.array_equal(rotation,np.eye(3)) and np.array_equal(translation,np.zeros(3)):return None
    transform=gp_Trsf();transform.SetValues(*(value for row,t in zip(rotation,translation) for value in [*row,t]))
    return transform

def place(shape,trace,source,target=None):
    transform=between(source,target)
    if transform is None:return shape,trace
    maker=BRepBuilderAPI_Transform(shape,transform,False);result=maker.Shape()
    return result,topology.propagate(result,[trace],None,maker,preserve_origins=True)

def world_bounds(bounds,placement):
    rotation,translation=arrays(placement)
    points=np.array([rotation@np.array(p)+translation for p in itertools.product(*zip(bounds[:3],bounds[3:]))])
    return [*points.min(axis=0).tolist(),*points.max(axis=0).tolist()]

def world_mesh(mesh,placement):
    rotation,translation=arrays(placement)
    mesh['vertices']=[(rotation@np.array(p)+translation).tolist() for p in mesh['vertices']]
    if 'normals' in mesh:mesh['normals']=[(rotation@np.array(n)).tolist() for n in mesh['normals']]
    mesh['coordinate_frame']='world'
    return mesh
