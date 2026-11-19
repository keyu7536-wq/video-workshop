"""Faithful, same-scale column crops for registered PDF pages and images.

Columns retain their original horizontal order and one common vertical range.
Only the spaces between selected columns are removed; a visible gutter marks
each join. Callers must supply the source original alongside this derivative
for review. This module neither chooses meaningful columns nor alters labels.
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw


PAIR = {'type': 'array', 'items': {'type': 'number', 'minimum': 0, 'maximum': 1},
        'minItems': 2, 'maxItems': 2}
COLUMN_CROP_SCHEMA = {'anyOf': [{'type': 'null'}, {
    'type': 'object', 'properties': {'y': PAIR, 'columns': {
        'type': 'array', 'items': PAIR, 'minItems': 2, 'maxItems': 6}},
    'required': ['y', 'columns'], 'additionalProperties': False}]}
RENDER_VERSION = 1
MAX_PIXELS = 32_000_000


def validate_column_crop(spec):
    """Return a detached normalized copy, or None for an unused composition."""
    if spec is None:
        return None
    if not isinstance(spec, dict) or set(spec) != {'y', 'columns'}:
        raise ValueError('column_crop 必须只含 y 和 columns 两个字段。')

    def pair(value, name, minimum):
        if (not isinstance(value, (list, tuple)) or len(value) != 2 or
                any(type(n) not in (int, float) or not math.isfinite(n) for n in value)):
            raise ValueError(f'column_crop.{name} 须为两个有限数值。')
        low, high = map(float, value)
        if not (0 <= low < high <= 1) or high - low < minimum - 1e-10:
            raise ValueError(f'column_crop.{name} 须满足 0≤起点<终点≤1，跨度至少 {minimum}。')
        return [low, high]

    y = pair(spec['y'], 'y', .05)
    columns = spec['columns']
    if not isinstance(columns, (list, tuple)) or not 2 <= len(columns) <= 6:
        raise ValueError('column_crop.columns 须有 2～6 个列区间。')
    result = []
    for index, value in enumerate(columns):
        column = pair(value, f'columns[{index}]', .01)
        if result and column[0] < result[-1][1]:
            raise ValueError(f'column_crop.columns[{index}] 与前一列重叠或顺序颠倒；须按原图从左到右排列。')
        result.append(column)
    return {'y': y, 'columns': result}


def _join(picture, spec, source_size, origin=(0, 0), scale=1.0, page=None):
    sw, sh = source_size
    y0, y1 = spec['y']
    # All boundaries use one raster grid. No column-specific resizing occurs.
    top = max(0, int(round(y0 * sh * scale)) - origin[1])
    bottom = min(picture.height, int(round(y1 * sh * scale)) - origin[1])
    height = bottom - top
    if height < 1:
        raise ValueError('column_crop 高度不足一个源像素。')
    crops, records = [], []
    for x0, x1 in spec['columns']:
        left = max(0, int(round(x0 * sw * scale)) - origin[0])
        right = min(picture.width, int(round(x1 * sw * scale)) - origin[0])
        if right <= left:
            raise ValueError('column_crop 某列不足一个源像素。')
        crops.append(picture.crop((left, top, right, bottom)))
        records.append({'source_bounds': [x0 * sw, y0 * sh, x1 * sw, y1 * sh],
                        'raster_bounds': [left + origin[0], top + origin[1], right + origin[0], bottom + origin[1]]})
    gap = max(4, round(height * .018))
    width = sum(crop.width for crop in crops) + gap * (len(crops) - 1)
    if width * height > MAX_PIXELS:
        raise ValueError('column_crop 合成图超过 3200 万像素，请降低采样尺寸。')
    output = Image.new('RGB', (width, height), (8, 14, 21))
    draw = ImageDraw.Draw(output)
    offset = 0
    for index, (crop, record) in enumerate(zip(crops, records)):
        output.paste(crop, (offset, 0))
        record['output_bounds'] = [offset, 0, offset + crop.width, height]
        offset += crop.width
        if index < len(crops) - 1:
            center = offset + gap // 2
            dash = max(3, round(height * .018))
            for start in range(0, height, dash * 2):
                draw.line((center, start, center, min(height - 1, start + dash)), fill=(134, 151, 167), width=max(1, gap // 8))
            offset += gap
    return output, {'render_version': RENDER_VERSION, 'column_crop': spec, 'source_size': list(source_size),
                    'source_page': page, 'source_bounds': [spec['columns'][0][0]*sw, y0*sh,
                                                       spec['columns'][-1][1]*sw, y1*sh],
                    'output_size': list(output.size), 'gap_pixels': gap, 'strips': records,
                    'scale': scale, 'scope': 'Same-scale ordered source strips; dashed gutters mark omitted horizontal regions. No data or labels redrawn.'}


def render_column_crop(path, spec, *, page=None, width=1920, height=1080):
    """Return (RGB image, provenance). Path access authorization belongs to caller.

    ``page`` selects a PDF page (1-based); None reads a raster image. The canvas
    size controls PDF sampling only. Export, review and previews must pass the
    same size and use this function, then fit the result with content protected.
    """
    spec = validate_column_crop(spec)
    if spec is None:
        raise ValueError('未提供 column_crop。')
    if any(type(n) is not int or not 1 <= n <= 7680 for n in (width, height)):
        raise ValueError('采样参考宽高须为 1～7680 的整数。')
    path = Path(path)
    if page is None:
        with Image.open(path) as original:
            if original.width * original.height > MAX_PIXELS:
                raise ValueError('源图片超过 3200 万像素，请提供缩小后的清晰原图。')
            return _join(original.convert('RGB'), spec, original.size)
    if type(page) is not int:
        raise ValueError('PDF 页码须为从 1 开始的整数。')
    import pymupdf
    with pymupdf.open(path) as document:
        if document.needs_pass:
            raise ValueError('PDF 需要密码。')
        if not 1 <= page <= len(document):
            raise ValueError('PDF 页码超出范围。')
        source = document[page - 1]
        bounds = source.rect
        sw, sh = bounds.width, bounds.height
        y0, y1 = spec['y']
        selected_width = sum(x1 - x0 for x0, x1 in spec['columns']) * sw
        selected_height = (y1 - y0) * sh
        scale = max(2, min(6, max(width / selected_width, height / selected_height)))
        clip = pymupdf.Rect(spec['columns'][0][0]*sw, y0*sh, spec['columns'][-1][1]*sw, y1*sh)
        scale = min(scale, math.sqrt((MAX_PIXELS - 100_000) / (clip.width * clip.height)))
        pix = source.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False)
        picture = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
        return _join(picture, spec, (sw, sh), origin=(pix.x, pix.y), scale=scale, page=page)
