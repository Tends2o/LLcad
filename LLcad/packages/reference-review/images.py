"""Bounded image decoding and registered-view metrics; never execute source text."""
import json, sys, warnings
from pathlib import Path
import numpy as np
from PIL import Image, ImageOps
from scipy import ndimage

Image.MAX_IMAGE_PIXELS=20_000_000
warnings.simplefilter('error',Image.DecompressionBombWarning)

def load(path):
    with Image.open(path) as image:
        if image.format not in ('PNG','JPEG','WEBP'):raise ValueError('unsupported_image_format')
        image=ImageOps.exif_transpose(image).convert('RGBA')
        background=Image.new('RGBA',image.size,'white');background.alpha_composite(image)
        return background.convert('RGB')

def normalize(source,destination,limit):
    image=load(source);original=list(image.size)
    image.thumbnail((limit,limit),Image.Resampling.LANCZOS)
    image.save(destination,'PNG')
    return {'width':image.width,'height':image.height,'original_size':original}

def mask(image):
    a=np.asarray(image).astype(float)
    corners=np.concatenate([a[:8,:8].reshape(-1,3),a[-8:,:8].reshape(-1,3),a[:8,-8:].reshape(-1,3),a[-8:,-8:].reshape(-1,3)])
    bg=np.median(corners,axis=0)
    m=np.max(np.abs(a-bg),axis=2)>28
    m=ndimage.binary_fill_holes(m)
    labels,n=ndimage.label(m)
    if not n:raise ValueError('no_object_foreground')
    sizes=np.bincount(labels.ravel());sizes[0]=0
    # Keep every foreground component: floating components must affect the score.
    return m

def compare(reference,actual,destination):
    r=load(reference);a=load(actual)
    if r.size!=a.size:raise ValueError('registered_images_require_same_resolution')
    rm,am=mask(r),mask(a)
    re=rm^ndimage.binary_erosion(rm);ae=am^ndimage.binary_erosion(am)
    rd=ndimage.distance_transform_edt(~re);ad=ndimage.distance_transform_edt(~ae)
    precision=float(np.mean(rd[ae]<=2));recall=float(np.mean(ad[re]<=2))
    f1=2*precision*recall/max(precision+recall,1e-12)
    iou=float(np.count_nonzero(rm&am)/max(np.count_nonzero(rm|am),1))
    rgb=np.asarray(a).copy();rgb[rm^am]=[235,40,70]
    Image.fromarray(rgb).save(destination,'PNG')
    return {'silhouette_iou':iou,'edge_f1':f1,'edge_tolerance_px':2,'registration':'declared_same_camera_and_resolution','scope':'visible silhouette only; no surface or texture certificate'}

if __name__=='__main__':
    if sys.argv[1]=='normalize':result=normalize(sys.argv[2],sys.argv[3],int(sys.argv[4]))
    elif sys.argv[1]=='compare':result=compare(sys.argv[2],sys.argv[3],sys.argv[4])
    else:raise ValueError('unknown_image_action')
    print(json.dumps(result,allow_nan=False))
