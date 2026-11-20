"""Prepare bounded review images for the current plan, including unsampled pages.

These files are review inputs, not a claim that a model has inspected them.
Only registered local source files inside the job are read. Intake sampling
records are preserved; the separate manifest records this review's coverage.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path


def _inside(job, value):
    path = Path(value)
    path = (path if path.is_absolute() else job / path).resolve()
    if not path.is_relative_to(job):
        raise ValueError('审核素材超出任务目录。')
    if not path.is_file():
        raise ValueError('审核所选素材文件不存在：' + path.name)
    return path


def _bbox(value):
    if not value:
        return None
    if len(value) != 4 or any(type(n) not in (int, float) or not math.isfinite(n) for n in value):
        raise ValueError('审核裁切区域必须是四个有限数值。')
    x0, y0, x1, y1 = value
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        raise ValueError('审核裁切区域须位于素材内且具有正面积。')
    return list(value)


def _target(folder, path, page, bbox):
    stat = path.stat()
    identity = [str(path), stat.st_size, stat.st_mtime_ns, page, bbox, 'review-assets-v1']
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:24]
    return folder / (digest + '.png')


def _render(job, folder, source, bbox=None, column_crop=None, request=None):
    """Use the same PDF panel clipping and pixel crop coordinates as export."""
    path = _inside(job, source['file'])
    page_number = source.get('page') if source['type'] == 'page' else None
    if column_crop:
        from studio_chart_composition import render_column_crop, RENDER_VERSION
        from studio_display import dimensions
        width, height = dimensions(request or {})
        target = _target(folder, path, page_number, {'column_crop':column_crop,'canvas':[width,height],'version':RENDER_VERSION})
        picture, actual = render_column_crop(path,column_crop,page=page_number,width=width,height=height)
        picture.save(target)
        return target, actual
    target = _target(folder, path, page_number, bbox)
    actual = None
    if source['type'] == 'page':
        import pymupdf
        from studio_crop import panel_clip
        with pymupdf.open(path) as document:
            if type(page_number) is not int or not 1 <= page_number <= len(document):
                raise ValueError('审核所选PDF页码超出范围。')
            if document.needs_pass:
                raise ValueError('审核所选PDF需要密码。')
            page = document[page_number - 1]
            if bbox:
                width, height = page.rect.width, page.rect.height
                clip = panel_clip(page, pymupdf.Rect(bbox[0]*width, bbox[1]*height,
                                                    bbox[2]*width, bbox[3]*height))
                zoom = max(2, min(6, 1600/max(clip.width, clip.height)))
            else:
                clip = page.rect
                zoom = min(1.5, 1600/max(clip.width, clip.height))
            actual = list(clip)
            if not target.is_file():
                page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=False).save(target)
    else:
        from PIL import Image
        with Image.open(path) as image:
            if bbox:
                width, height = image.size
                actual = [int(bbox[0]*width), int(bbox[1]*height), int(bbox[2]*width), int(bbox[3]*height)]
                image = image.crop(tuple(actual))
            if not target.is_file():
                image = image.convert('RGB')
                image.thumbnail((1600, 1600))
                image.save(target)
    return target, actual


def _composition(job,folder,source,bbox,focus,request,column_crop=None):
    """Project a registered source into the renderer's canvas, never auto-crop it.

    Native-pixel metrics describe rectangles, not semantic subject size. This
    planned image omits captions and animation; final encoded review is required.
    """
    from PIL import Image,ImageDraw,ImageOps
    from studio_display import effective_display_settings
    from studio_render import media_geometry
    settings=effective_display_settings(request)
    width,height=settings['width'],settings['height']
    source_path=_inside(job,source['file'])
    protected=source['type']=='page' or bool(column_crop)
    mapped=list(focus or [])
    actual=None
    if column_crop:
        from studio_chart_composition import render_column_crop
        picture,actual=render_column_crop(source_path,column_crop,
            page=source.get('page') if source['type']=='page' else None,width=width,height=height)
        mapped=[]
    elif protected:
        import pymupdf
        from studio_crop import panel_clip
        with pymupdf.open(source_path) as document:
            page=document[source['page']-1];bounds=page.rect
            box=bbox or [0,0,1,1]
            clip=panel_clip(page,pymupdf.Rect(box[0]*bounds.width,box[1]*bounds.height,
                                             box[2]*bounds.width,box[3]*bounds.height))
            actual=list(clip)
            zoom=max(2,min(6,max(width/clip.width,height/clip.height)))
            pix=page.get_pixmap(matrix=pymupdf.Matrix(zoom,zoom),clip=clip,alpha=False)
            picture=Image.frombytes('RGB',(pix.width,pix.height),pix.samples)
            if mapped:
                x0,y0,x1,y1=mapped
                mapped=[(x0*bounds.width-clip.x0)/clip.width,(y0*bounds.height-clip.y0)/clip.height,
                        (x1*bounds.width-clip.x0)/clip.width,(y1*bounds.height-clip.y0)/clip.height]
    else:
        with Image.open(source_path) as original:
            if bbox:
                sw,sh=original.size;x0,y0,x1,y1=bbox
                actual=[int(x0*sw),int(y0*sh),int(x1*sw),int(y1*sh)]
                picture=original.crop(tuple(actual)).convert('RGB')
                if mapped:
                    fx0,fy0,fx1,fy1=mapped
                    mapped=[(fx0-x0)/(x1-x0),(fy0-y0)/(y1-y0),(fx1-x0)/(x1-x0),(fy1-y0)/(y1-y0)]
            else:picture=original.convert('RGB')
    mapped=[max(0,min(1,value)) for value in mapped]
    x,y,mw,mh=media_geometry(width,height,settings,protected)
    mw-=mw%2;mh-=mh%2
    cover=settings.get('media_fit')=='cover' and not protected
    sw,sh=picture.size
    scale=(max if cover else min)(mw/sw,mh/sh)
    dw,dh=sw*scale,sh*scale;dx=x+(mw-dw)/2;dy=y+(mh-dh)/2
    visible=[max(x,dx),max(y,dy),min(x+mw,dx+dw),min(y+mh,dy+dh)]
    visible=[visible[0],visible[1],visible[2]-visible[0],visible[3]-visible[1]]
    focus_rect=[]
    if mapped and mapped[2]>mapped[0] and mapped[3]>mapped[1]:
        left=max(x,dx+mapped[0]*dw);top=max(y,dy+mapped[1]*dh)
        right=min(x+mw,dx+mapped[2]*dw);bottom=min(y+mh,dy+mapped[3]*dh)
        if right>left and bottom>top:focus_rect=[left,top,right-left,bottom-top]
    target=_target(folder,source_path,source.get('page'),
        {'composition-v2':settings,'bbox':bbox,'focus':focus,'column_crop':column_crop,'protected':protected})
    canvas=Image.new('RGB',(width,height),(8,14,21))
    if cover:
        # Resize only the visible center crop, avoiding a huge intermediate for
        # extreme source aspect ratios. This matches the renderer's center fit.
        fitted=ImageOps.fit(picture,(mw,mh),method=Image.Resampling.LANCZOS)
        canvas.paste(fitted,(x,y))
    else:
        # FFmpeg requests even image dimensions inside the media rectangle.
        iw=max(2,int(dw)//2*2);ih=max(2,int(dh)//2*2)
        fitted=picture.resize((iw,ih),Image.Resampling.LANCZOS)
        canvas.paste(fitted,(int(x+(mw-iw)/2),int(y+(mh-ih)/2)))
    if focus_rect:
        left,top,fw,fh=focus_rect
        ImageDraw.Draw(canvas).rectangle((left,top,left+fw,top+fh),outline=(84,214,189),width=max(3,width//250))
    canvas.thumbnail((1600,1600),Image.Resampling.LANCZOS)
    canvas.save(target)
    rounded=lambda values:[round(v,3) for v in values]
    return target,{'canvas':[width,height],'preview_pixels':list(canvas.size),'media_box':[x,y,mw,mh],
        'source_crop_pixels':[sw,sh],'actual_bounds':actual,'fit':'cover' if cover else 'contain',
        'protect_content':protected,'displayed_image_rect':rounded([dx,dy,dw,dh]),
        'visible_image_rect':rounded(visible),'image_canvas_area_ratio':round(visible[2]*visible[3]/(width*height),6),
        'focus_crop_normalized':mapped,'focus_rect':rounded(focus_rect),
        'focus_canvas_area_ratio':round(focus_rect[2]*focus_rect[3]/(width*height),6) if focus_rect else 0,
        'scope':'Planned native-pixel geometry; preview may be downscaled. Image/highlight rectangles are not semantic subject size. No encoded frames, captions, duration or motion inspected.'}


def prepare_review_assets(job, plan, sources, prior_images, max_images=65,request=None):
    """Return ``(label/Path pairs, coverage)`` without changing intake records.

    Selected page/image originals and selected-video contact sheets take
    priority, then unique actual crops, then optional canvas compositions (each
    detail-view stage is a separate image) and unused intake samples. Omissions
    are explicit within the same image budget; stage indices are not timings.
    """
    if type(max_images) is not int or not 1 <= max_images <= 65:
        raise ValueError('审核附图预算须为1至65。')
    job = Path(job).resolve()
    lookup = {source['id']: source for source in sources if source.get('id')}
    selected, crop_requests, missing_ids, used_video_ids = {}, {}, [], set()
    compositions={}
    for block in plan.get('blocks', []):
        for index, shot in enumerate(block.get('shots', [])):
            source_id = shot.get('source_id')
            if shot.get('kind') == 'video':
                used_video_ids.add(source_id)
            if shot.get('kind') not in ('page', 'image'):
                continue
            source = lookup.get(source_id)
            if not source:
                # Normal engine.validate rejects this. Preserve explicit scope
                # when this helper is used independently with a partial catalog.
                if source_id not in missing_ids:
                    missing_ids.append(source_id)
                continue
            if source.get('type') != shot['kind']:
                raise ValueError('审核所选素材类型不匹配：' + source_id)
            _inside(job, source['file'])
            selected.setdefault(source_id, source)
            from studio_chart_composition import validate_column_crop
            column_crop=validate_column_crop(shot.get('column_crop'))
            bbox = _bbox(shot.get('bbox'))
            if column_crop and (bbox or shot.get('focus')):
                raise ValueError('列区间拼接不能同时使用 bbox 或 focus。')
            if column_crop or (bbox and bbox != [0, 0, 1, 1]):
                key = (source_id, tuple(bbox or []),json.dumps(column_crop,sort_keys=True))
                item = crop_requests.setdefault(key, {'source_id': source_id, 'bbox': bbox, 'column_crop':column_crop,'shots': []})
                item['shots'].append({'block_id': block.get('id'), 'shot_index': index})
            if request is not None:
                focus=_bbox(shot.get('focus'))
                key=(source_id,tuple(bbox or []),tuple(focus or []),json.dumps(column_crop,sort_keys=True))
                beats=shot.get('visual_beats') or []
                if beats:
                    from studio_visual_beats import validate_beats
                    beats=validate_beats(beats,block.get('narration'))
                    if focus or column_crop:
                        raise ValueError('逐步放大不能同时使用 focus 或 column_crop。')
                    key=key+(json.dumps(beats,sort_keys=True,ensure_ascii=False),)
                item=compositions.setdefault(key,{'source_id':source_id,'bbox':bbox or [],'focus':focus or [],'column_crop':column_crop,'shots':[]})
                if beats:item['visual_beats']=beats
                item['shots'].append({'block_id':block.get('id'),'shot_index':index})

    def relevant_video(label):
        return any(source_id and (label == source_id or label.startswith(source_id + ' '))
                   for source_id in used_video_ids)

    video_images = []
    seen_paths = set()
    for label, file in prior_images:
        if relevant_video(label):
            path = _inside(job, file)
            if path not in seen_paths:
                seen_paths.add(path)
                video_images.append((label, path))
    required = len(selected) + len(video_images) + sum(bool(item['column_crop']) for item in crop_requests.values())
    if required > max_images:
        raise ValueError(f'当前分镜所选素材需要{required}张审核附图，超过{max_images}张预算；请拆分任务，不能静默省略。')

    folder = job / 'analysis/review_assets'
    folder.mkdir(parents=True, exist_ok=True)
    coverage = {'max_images': max_images, 'selected_source_ids': list(selected),
                'originals': [], 'crops': [], 'omitted_crops': [], 'omitted_prior': [],
                'compositions':[],'omitted_compositions':[],
                'missing_source_ids': missing_ids,
                'scope': 'Current selected PDF pages/images and available selected-video contact sheets; no full-document visual reading or audio inspection.'}
    result = []
    selected_paths = set()
    for source_id, source in selected.items():
        path, bounds = _render(job, folder, source)
        label = source_id + (' · 当前所选PDF第' + str(source['page']) + '页全图' if source['type'] == 'page' else ' · 当前所选图片全图')
        result.append((label, path))
        selected_paths.add(_inside(job, source['file']))
        if source.get('preview'):
            selected_paths.add(_inside(job, source['preview']))
        coverage['originals'].append({'source_id': source_id, 'label': label, 'path': str(path),
                                      'page': source.get('page'), 'actual_bounds': bounds})
    result.extend(video_images)
    from studio_video_crop import preview_video_crop
    for block in plan.get('blocks',[]):
        for shot_index,shot in enumerate(block.get('shots',[])):
            if shot.get('kind')!='video' or not (shot.get('bbox') or shot.get('media_segment_id') or shot.get('subtitle_safe')):continue
            source=lookup.get(shot.get('source_id'))
            if source is None:continue
            sampled=preview_video_crop(job,source,shot['in'],shot['duration'],shot.get('bbox',[]),request or {},shot.get('subtitle_safe',False))
            for frame in sampled['frames']:
                meta={'kind':'video_crop','block_id':block['id'],'shot_index':shot_index,
                      'source_id':source['id'],'source_time':frame['time'],'bbox':shot.get('bbox',[])}
                if len(result)>=max_images:
                    coverage['omitted_compositions'].append(meta|{'reason':'Image budget; selected video composition not inspected.'})
                    continue
                path=job/frame['preview'];label=f"{source['id']} · 取片范围内 {frame['time']:.2f} 秒的裁后构图（单帧）"
                result.append((label,path));coverage['compositions'].append(meta|{'path':str(path),'label':label,
                    'provenance':sampled['metadata'],'scope':'Actual source frame and planned crop/fit; no encoded caption or continuous-action review.'})
    # Motion plans need real renderer frames, not only a list of object names.
    from studio_motion import render_frame as motion_frame
    from studio_display import dimensions
    import hashlib
    width,height=dimensions(request or {})
    for block in plan.get('blocks',[]):
        for shot_index,shot in enumerate(block.get('shots',[])):
            if shot.get('kind')!='motion':continue
            spec=shot['motion']
            staged={**spec,'stages':[{**stage,'start':float(i)} for i,stage in enumerate(spec['stages'])]}
            for stage_index,stage in enumerate(spec['stages']):
                meta={'kind':'motion','block_id':block['id'],'shot_index':shot_index,'stage_index':stage_index,'cue':stage['cue']}
                if len(result)>=max_images:
                    coverage['omitted_compositions'].append(meta|{'reason':'Image budget; motion stage not inspected.'})
                    continue
                key=hashlib.sha256(json.dumps([spec,width,height,stage_index],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
                target=folder/f'motion_{key}.png'
                picture=motion_frame(staged,stage_index+.6,len(spec['stages'])+.5,width,height)
                picture.thumbnail((1600,1600));picture.save(target)
                label=f"{block['id']} · 动作示意阶段 {stage_index+1}（静态布局，非实测时间）"
                result.append((label,target));coverage['compositions'].append(meta|{'path':str(target),'label':label,
                    'scope':'Renderer stage composition only; synthetic preview time, no measured speech or encoded motion.'})
    # A multi-column derivative must always be accompanied by its original.
    # Reserve these before optional single-region crops and canvas previews.
    for item in sorted(crop_requests.values(),key=lambda item:not bool(item['column_crop'])):
        if len(result) >= max_images:
            coverage['omitted_crops'].append(item | {'reason': 'Image budget; selected source full image is still attached.'})
            continue
        path, actual = _render(job, folder, selected[item['source_id']], item['bbox'],item['column_crop'],request)
        label = item['source_id'] + (' · 同尺度列区间拼接（虚线为省略区域） column_crop=' + json.dumps(item['column_crop'],separators=(',', ':')) if item['column_crop'] else ' · 实际裁图 bbox=' + json.dumps(item['bbox'], separators=(',', ':')))
        result.append((label, path))
        coverage['crops'].append(item | {'label': label, 'path': str(path), 'actual_bounds': actual})
    for item in compositions.values():
        if item.get('visual_beats'):
            # Import at call time: visual_preview uses this module's registered
            # source renderer, so importing it at module scope would cycle.
            from studio_visual_preview import visual_preview
            from studio_display import dimensions
            from PIL import Image
            source=dict(selected[item['source_id']])
            source['file']=str(_inside(job,source['file']))
            width,height=dimensions(request)
            beats=item['visual_beats']
            for index,beat in enumerate(beats):
                stage={**item,'kind':'visual_beat','stage_index':index,'stage_count':len(beats),
                       'cue':beat['cue'],'stage_bbox':list(beat['bbox'])}
                if len(result)>=max_images:
                    coverage['omitted_compositions'].append(stage|{
                        'reason':'Image budget; this planned detail-view stage is not attached or inspected.'})
                    continue
                preview=visual_preview(job,source,item['bbox'] or [0,0,1,1],beats,index,request)
                # Keep the same bounded review raster size as other planned
                # compositions, without changing the editor's preview cache.
                path=_target(folder,preview,None,{'visual-stage-review-v1':index})
                with Image.open(preview) as original:
                    picture=original.convert('RGB')
                    picture.thumbnail((1600,1600),Image.Resampling.LANCZOS)
                    picture.save(path)
                label=(item['source_id']+f' · 逐步放大阶段 {index+1}/{len(beats)}（非成片，未计时）'
                       +' cue='+json.dumps(beat['cue'],ensure_ascii=False))
                result.append((label,path))
                coverage['compositions'].append(stage|{'canvas':[width,height],
                    'preview_pixels':list(picture.size),'label':label,'path':str(path),
                    'scope':'Planned source/detail composition for this cue stage; stage index is not time. '
                            'No recorded speech timing, encoded frames, captions or continuous motion inspected.'})
            continue
        if len(result)>=max_images:
            coverage['omitted_compositions'].append(item|{'reason':'Image budget; composition not inspected.'})
            continue
        path,metrics=_composition(job,folder,selected[item['source_id']],item['bbox'],item['focus'],request,item['column_crop'])
        label=item['source_id']+' · 构图预览（非成片） '+str(metrics['canvas'][0])+'×'+str(metrics['canvas'][1])
        result.append((label,path))
        coverage['compositions'].append(item|metrics|{'label':label,'path':str(path)})
    attached_paths = {path for _, path in result} | selected_paths
    for label, file in prior_images:
        path = _inside(job, file)
        if path in attached_paths or label in selected:
            continue
        if len(result) >= max_images:
            coverage['omitted_prior'].append(label)
            continue
        result.append((label, path))
        attached_paths.add(path)
    coverage['attached_count'] = len(result)
    coverage['labels'] = [label for label, _ in result]
    (job / 'analysis/review_assets.json').write_text(json.dumps(coverage, ensure_ascii=False, indent=2), encoding='utf-8')
    return result, coverage
