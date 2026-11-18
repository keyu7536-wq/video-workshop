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


def _render(job, folder, source, bbox=None):
    """Use the same PDF panel clipping and pixel crop coordinates as export."""
    path = _inside(job, source['file'])
    page_number = source.get('page') if source['type'] == 'page' else None
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


def prepare_review_assets(job, plan, sources, prior_images, max_images=65):
    """Return ``(label/Path pairs, coverage)`` without changing intake records.

    Selected page/image originals and selected-video contact sheets take
    priority, then unique actual crops, then unused intake samples. Too many
    mandatory images fail explicitly; crop/optional omissions are listed.
    """
    if type(max_images) is not int or not 1 <= max_images <= 65:
        raise ValueError('审核附图预算须为1至65。')
    job = Path(job).resolve()
    lookup = {source['id']: source for source in sources if source.get('id')}
    selected, crop_requests, missing_ids, used_video_ids = {}, {}, [], set()
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
            bbox = _bbox(shot.get('bbox'))
            if bbox and bbox != [0, 0, 1, 1]:
                key = (source_id, tuple(bbox))
                item = crop_requests.setdefault(key, {'source_id': source_id, 'bbox': bbox, 'shots': []})
                item['shots'].append({'block_id': block.get('id'), 'shot_index': index})

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
    required = len(selected) + len(video_images)
    if required > max_images:
        raise ValueError(f'当前分镜所选素材需要{required}张审核附图，超过{max_images}张预算；请拆分任务，不能静默省略。')

    folder = job / 'analysis/review_assets'
    folder.mkdir(parents=True, exist_ok=True)
    coverage = {'max_images': max_images, 'selected_source_ids': list(selected),
                'originals': [], 'crops': [], 'omitted_crops': [], 'omitted_prior': [],
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
    for item in crop_requests.values():
        if len(result) >= max_images:
            coverage['omitted_crops'].append(item | {'reason': 'Image budget; selected source full image is still attached.'})
            continue
        path, actual = _render(job, folder, selected[item['source_id']], item['bbox'])
        label = item['source_id'] + ' · 实际裁图 bbox=' + json.dumps(item['bbox'], separators=(',', ':'))
        result.append((label, path))
        coverage['crops'].append(item | {'label': label, 'path': str(path), 'actual_bounds': actual})
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
