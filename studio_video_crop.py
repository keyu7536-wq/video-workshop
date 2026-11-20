"""Explicit source-video crops and bounded, local decoded-frame previews.

Coordinates refer to the complete decoded video frame, before canvas fitting.
Preview frames are samples only: neither motion nor source conditions are
verified by this module. No caption, highlight or invented content is added.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import uuid


VIDEO_CROP_VERSION = 1


def validate_video_crop(bbox):
    """Return a fresh normalized box, or [] for an uncropped legacy shot."""
    if bbox is None or bbox == []:
        return []
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise ValueError('视频裁切区域须为四个 0–1 坐标。')
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in bbox):
        raise ValueError('视频裁切坐标必须是有限数值。')
    left, top, right, bottom = bbox
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise ValueError('视频裁切区域须位于完整原帧内。')
    if right-left < .08-1e-12 or bottom-top < .05-1e-12:
        raise ValueError('视频裁切宽度至少为原帧的 8%，高度至少为 5%。')
    return [float(value) for value in bbox]


def _number(value):
    # Remove arithmetic noise such as .3-.2 while retaining subpixel precision.
    return format(value, '.15g')


def video_crop_filter(bbox):
    """FFmpeg crop with even sizes/origins, shared by preview and encoding."""
    box = validate_video_crop(bbox)
    if not box:
        return ''
    left, top, right, bottom = box
    width, height = _number(right-left), _number(bottom-top)
    x, y = _number(left), _number(top)
    return (f"crop=w='max(2,trunc(iw*{width}/2)*2)':h='max(2,trunc(ih*{height}/2)*2)':"
            f"x='min(trunc(iw*{x}/2)*2,trunc((iw-ow)/2)*2)':"
            f"y='min(trunc(ih*{y}/2)*2,trunc((ih-oh)/2)*2)'")


def _local_source(job, source):
    if not isinstance(source, dict) or source.get('type') != 'video':
        raise ValueError('只能预览已登记的视频素材。')
    value = source.get('file')
    if not isinstance(value, str) or not value:
        raise ValueError('视频素材缺少本地文件路径。')
    path = Path(value)
    path = (path if path.is_absolute() else job/path).resolve()
    if not path.is_relative_to(job):
        raise ValueError('视频素材路径超出任务目录。')
    if not path.is_file():
        raise ValueError('视频素材文件不存在：' + path.name)
    return path


def _finite_time(value, name, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError(name + '必须是有效的非负秒数。')
    return float(value)


def _probe(path):
    import imageio_ffmpeg
    reader = imageio_ffmpeg.read_frames(str(path), pix_fmt='rgb24')
    try:
        meta = next(reader)
    finally:
        reader.close()
    duration = _finite_time(meta.get('duration'), '视频实际时长', positive=True)
    return duration


def _extract(ffmpeg, source, target, timestamp, filters=''):
    """Write complete PNGs before exposing them; never reuse a partial output."""
    from make_video import run
    pending = target.with_name(target.stem + '.' + uuid.uuid4().hex + '.png')
    try:
        command = [ffmpeg, '-hide_banner', '-v', 'error', '-y', '-ss', str(timestamp),
                   '-i', str(source), '-map', '0:v:0', '-frames:v', '1', '-an']
        if filters:
            command += ['-vf', filters]
        command += ['-threads', '1', str(pending)]
        run(command, timeout=45)
        if not pending.is_file() or not pending.stat().st_size:
            raise RuntimeError('所选时间未解码出视频画面。')
        from PIL import Image
        with Image.open(pending) as image:
            image.verify()
        pending.replace(target)
    finally:
        pending.unlink(missing_ok=True)


def _crop_pixels(size, bbox):
    width, height = size
    if not bbox:
        return [0, 0, width, height]
    left, top, right, bottom = bbox
    # Match the decimal coefficients and even-pixel truncation in the filter.
    cw = max(2, int(width*float(_number(right-left))/2)*2)
    ch = max(2, int(height*float(_number(bottom-top))/2)*2)
    x = min(int(width*float(_number(left))/2)*2, int((width-cw)/2)*2)
    y = min(int(height*float(_number(top))/2)*2, int((height-ch)/2)*2)
    return [x, y, cw, ch]


def preview_video_crop(job, source, in_seconds, duration, bbox, request, subtitle_safe=False):
    """Return JSON-compatible job-relative originals, compositions and scope.

    At most three existing intake sample times inside the requested interval
    are decoded again. Source bytes, crop and display settings identify each
    preview folder. Canvas images match the renderer's crop -> fit -> pad path;
    cover may crop the selected region a second time and is reported explicitly.
    """
    from PIL import Image
    from make_video import ffmpeg_path
    from studio_display import validate_display, effective_display_settings
    from studio_render import media_geometry
    from studio_revision import file_hash

    job = Path(job).resolve()
    if type(subtitle_safe) is not bool:raise ValueError('字幕留白设置须为开关。')
    path = _local_source(job, source)
    box = validate_video_crop(bbox)
    start = _finite_time(in_seconds, '开始时间')
    span = _finite_time(duration, '取段长度', positive=True)
    if span > 900:
        raise ValueError('视频预览取段不能超过 900 秒。')
    end = start + span
    samples = source.get('sample_times')
    if not isinstance(samples, list) or not samples or len(samples) > 1000:
        raise ValueError('视频缺少有效的已有抽帧时间，请先重新准备素材。')
    samples = sorted(set(_finite_time(value, '抽帧时间') for value in samples))
    validate_display(request)
    settings = effective_display_settings(request)
    actual_duration = _probe(path)
    if start >= actual_duration or end > actual_duration + .001:
        raise ValueError('所选视频区间超出文件的实际时长范围。')
    candidates = [value for value in samples if start <= value < min(end, actual_duration)]
    if not candidates:
        raise ValueError('所选时间段内没有已有抽帧，请选择覆盖已采样时间的区间。')
    eligible_count = len(candidates)
    if len(candidates) > 3:
        candidates = [candidates[index] for index in (0, (len(candidates)-1)//2, len(candidates)-1)]

    digest = file_hash(path)
    identity = {'source_sha256': digest, 'crop': box, 'window': [start, end],
                'samples': candidates, 'display': settings, 'subtitle_safe':subtitle_safe,'version': VIDEO_CROP_VERSION}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
    folder = job/'analysis/video_crop_previews'/key
    if not folder.resolve().is_relative_to(job):
        raise ValueError('视频预览目录超出任务目录。')
    folder.mkdir(parents=True, exist_ok=True)
    width, height = settings['width'], settings['height']
    x, y, mw, mh = media_geometry(width, height, settings, protect_content=subtitle_safe)
    mw -= mw % 2; mh -= mh % 2
    cover = settings.get('media_fit') == 'cover' and not subtitle_safe
    fit = (f'scale={mw}:{mh}:force_original_aspect_ratio=increase:force_divisible_by=2,crop={mw}:{mh}'
           if cover else f'scale={mw}:{mh}:force_original_aspect_ratio=decrease:force_divisible_by=2')
    filters = [video_crop_filter(box), fit,
               f'pad={width}:{height}:{x}+({mw}-iw)/2:{y}+({mh}-ih)/2:color=0x080e15', 'setsar=1']
    filters = ','.join(value for value in filters if value)
    ffmpeg = ffmpeg_path()
    frames = []
    original_size = crop_pixels = None
    for index, timestamp in enumerate(candidates):
        original = folder/f'original_{index}.png'
        preview = folder/f'preview_{index}.png'
        # Re-extract rather than infer validity from a cached PNG's existence.
        _extract(ffmpeg, path, original, timestamp)
        with Image.open(original) as image:
            size = image.size
        if min(size) < 2:
            raise ValueError('视频原帧像素不足，无法生成有效裁切。')
        if original_size is not None and size != original_size:
            raise ValueError('视频在所选区间内改变了分辨率，无法使用同一裁切框。')
        original_size = size
        crop_pixels = _crop_pixels(size, box)
        _extract(ffmpeg, path, preview, timestamp, filters)
        frames.append({'time': timestamp, 'time_basis': 'requested_seek_seconds',
                       'original': original.relative_to(job).as_posix(),
                       'preview': preview.relative_to(job).as_posix()})
    result = {'frames': frames, 'metadata': {
        'source_id': source.get('id', ''), 'source_sha256': digest,
        'requested_window': [start, end], 'actual_source_duration': actual_duration,
        'sampled_times': candidates, 'available_sample_count': len(samples),
        'eligible_sample_count': eligible_count,
        'original_pixels': list(original_size), 'video_crop': box, 'crop_pixels': crop_pixels,
        'crop_pixels_order': 'left,top,width,height',
        'canvas': [width, height], 'media_box': [x, y, mw, mh],
        'fit': 'cover' if cover else 'contain', 'protect_content': subtitle_safe,
        'additional_center_crop': cover and abs(crop_pixels[2]/crop_pixels[3]-mw/mh) > 1e-6,
        'inspection': 'sampled_frames_only', 'continuous_motion_reviewed': False,
        'scope': '真实解码抽帧；时间为所请求的 seek 秒数。未检查连续动作、声音、字幕遮挡或被裁掉的条件说明。',
        'version': VIDEO_CROP_VERSION}}
    from paper_video import save_json
    save_json(folder/'metadata.json', result)
    return result
