"""Source-bound static previews for editable detail views (no speech timing)."""
from pathlib import Path
import hashlib
import json
import math

from studio_display import dimensions, validate_display
from studio_review_assets import _render
from studio_review_assets import _inside
from studio_visual_beats import render_frame, validate_beats


def visual_preview(job, source, crop, beats, index, display):
    source={**source,'file':str(_inside(Path(job).resolve(),source['file']))}
    if source.get('type') not in ('page','image'):
        raise ValueError('逐步放大只支持图片与 PDF。')
    if (not isinstance(crop,list) or len(crop)!=4
            or any(type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=1 for v in crop)
            or crop[2]-crop[0]<.08 or crop[3]-crop[1]<.05):
        raise ValueError('原图裁切范围无效。')
    beats=validate_beats(beats)
    if type(index) is not int or not 0<=index<len(beats):
        raise ValueError('放大区域序号无效。')
    for beat in beats:
        x0,y0,x1,y1=beat['bbox']
        if x0<crop[0] or y0<crop[1] or x1>crop[2] or y1>crop[3]:
            raise ValueError('放大区域须位于原图裁切范围内。')
    validate_display(display);width,height=dimensions(display)
    folder=Path(job)/'analysis/visual_previews';folder.mkdir(parents=True,exist_ok=True)
    image,actual=_render(Path(job).resolve(),folder,source,crop)
    if source['type']=='page':
        import pymupdf
        with pymupdf.open(source['file']) as document:
            bounds=document[source['page']-1].rect;sw,sh=bounds.width,bounds.height
    else:
        from PIL import Image
        with Image.open(source['file']) as original:sw,sh=original.size
    x0,y0,x1,y1=[v/(sw if i%2==0 else sh) for i,v in enumerate(actual)]
    prepared=[]
    for number,beat in enumerate(beats):
        a,b,c,d=beat['bbox']
        box=[(a-x0)/(x1-x0),(b-y0)/(y1-y0),(c-x0)/(x1-x0),(d-y0)/(y1-y0)]
        if any(v<-.001 or v>1.001 for v in box):raise ValueError('自动裁图移除了放大区域，请扩大原图裁切。')
        prepared.append({**beat,'bbox':[max(0,min(1,v)) for v in box],'start':float(number)})
    key=hashlib.sha256(json.dumps([str(image),image.stat().st_mtime_ns,prepared,index,width,height,1],sort_keys=True).encode()).hexdigest()
    target=folder/(key+'.png')
    if not target.is_file():render_frame(image,prepared,index+.5,len(prepared)+1,width,height).save(target)
    return target
