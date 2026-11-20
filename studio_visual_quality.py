"""Read-only visual inventory and measured-hold suggestions.

This is deliberately not a quality gate: source relevance, explanatory value,
motion and voice quality cannot be established from a JSON timeline alone.
"""
from collections.abc import Mapping
import json
import math


LONG_STATIC_SECONDS = 18.0


def _records(value):
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, (list, tuple)) else []


def _number(value):
    return (float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) else None)


def _identifier(value):
    return value if isinstance(value, str) and value else None


def _kind(shot):
    if shot.get('kind') == 'diagram' or (shot.get('kind') == 'video' and shot.get('diagram')):
        return 'diagram'
    if shot.get('visual_beats') or shot.get('visual_timeline') or shot.get('visual_beat_timeline'):
        return 'staged_image'
    value = shot.get('kind')
    return value if value in ('page', 'image', 'video', 'card') else 'unknown'


def _counts(shots):
    counts = {kind: 0 for kind in ('page', 'image', 'video', 'card', 'diagram', 'staged_image', 'unknown')}
    for shot in shots:
        counts[_kind(shot)] += 1
    return counts


def _interval(shot):
    start, end = _number(shot.get('start')), _number(shot.get('end'))
    if start is not None and end is not None and 0 <= start < end:
        return start, end, end-start
    duration = _number(shot.get('render_duration'))
    if duration is not None and duration > 0:
        return None, None, duration
    # Planned `duration` is a weight/estimate, not a measured rendered length.
    return None, None, None


def _static_key(shot):
    file = shot.get('file')
    if not isinstance(file, str) or not file.strip():
        return None
    # Prepared files identify actual crops. Retain explicit composition metadata
    # too, so two views of one file are never merged merely by their source ID.
    fields = ('bbox', 'crop', 'focus', 'column_crop', 'columns', 'media_fit', 'layout_style')
    return (file.replace('\\', '/'), json.dumps({key: shot.get(key) for key in fields},
                                              sort_keys=True, ensure_ascii=False))


def visual_quality_report(plan, sources, rendered_shots=None):
    """Return JSON-serializable advisory statistics without changing inputs.

    `sources` is the intake source list. A video with a `file` entry is an
    inventory candidate; this function neither opens that file nor establishes
    availability, relevance or reuse permission. `rendered_shots` must describe
    the current render if supplied; without it, no duration inference is made.
    """
    plan = plan if isinstance(plan, Mapping) else {}
    planned, lookup = [], {}
    for block in _records(plan.get('blocks')):
        block_id = _identifier(block.get('id'))
        for index, shot in enumerate(_records(block.get('shots'))):
            item = dict(shot, block_id=block_id, shot_index=index)
            planned.append(item)
            if block_id is not None:
                lookup[(block_id, index)] = item

    available = sorted({_identifier(source.get('id')) for source in _records(sources)
                        if source.get('type') == 'video' and _identifier(source.get('id'))
                        and isinstance(source.get('file'), str) and source['file'].strip()})
    used = sorted({_identifier(shot.get('source_id')) for shot in planned
                   if _kind(shot) == 'video' and _identifier(shot.get('source_id'))})
    unused = sorted(set(available)-set(used))
    issues = []
    if unused:
        issues.append({'code': 'unused_video_candidates', 'block_id': None, 'severity': 'suggestion',
            'source_ids': unused,
            'reason': f'素材清单中有 {len(unused)} 个登记了本地文件的视频未用于当前分镜。'
                      '可结合旁白核对是否有适合的动作或对照片段；尚未判断相关性、文件可用性或使用许可，不要求插入。'})

    rendered = []
    for shot in _records(rendered_shots):
        index = shot.get('shot_index')
        key = (shot.get('block_id'), index) if isinstance(index, int) and not isinstance(index, bool) else None
        original = lookup.get(key, {}) if key is not None else {}
        # Rendered fields win. Planned stages remain available for older render
        # manifests that omitted stage metadata, but never establish motion QA.
        rendered.append({**original, **shot})

    measured = []
    run = None

    def finish():
        if run is not None and run['seconds'] >= LONG_STATIC_SECONDS:
            issue = {'code': 'long_static_hold', 'block_id': run['block_id'],
                     'severity': 'suggestion', 'seconds': round(run['seconds'], 3),
                     'reason': f'当前渲染时间线中，同一静态画面连续停留 {run["seconds"]:.1f} 秒。'
                               '请结合这段旁白检查是否需要逐项标注、局部讲解或相关动作；图表确需停留时可以保留，不要求固定频率切镜。'}
            if run['shot_index'] is not None:
                issue['shot_index'] = run['shot_index']
            if run['source_ids']:
                issue['source_ids'] = sorted(run['source_ids'])
            issues.append(issue)

    for shot in rendered:
        start, end, seconds = _interval(shot)
        if seconds is not None:
            measured.append(seconds)
        if seconds is None or _kind(shot) not in ('page', 'image', 'card'):
            finish()
            run = None
            continue
        key = _static_key(shot)
        source_id = _identifier(shot.get('source_id'))
        contiguous = (run is not None and key is not None and run['key'] == key
                      and start is not None and run['end'] is not None
                      and abs(start-run['end']) <= .001)
        if contiguous:
            run['seconds'] += seconds
            run['end'] = end
            if source_id:
                run['source_ids'].add(source_id)
        else:
            finish()
            index = shot.get('shot_index')
            run = {'key': key, 'end': end, 'seconds': seconds,
                   'block_id': _identifier(shot.get('block_id')),
                   'shot_index': index if isinstance(index, int) and not isinstance(index, bool) else None,
                   'source_ids': {source_id} if source_id else set()}
    finish()

    return {'status': 'advisory',
        'scope': '仅依据分镜结构、素材登记和提供的渲染时间线提出建议；未观看连续画面或听音，'
                 '不判断语义相关性、解释质量或配音自然度，不代表审核通过，也不修改文案、分镜或审核状态。',
        'statistics': {
            'planned_shot_count': len(planned), 'planned_shot_counts': _counts(planned),
            'available_video_count': len(available), 'available_video_source_ids': available,
            'used_video_count': len(used), 'used_video_source_ids': used,
            'unused_video_count': len(unused), 'unused_video_source_ids': unused,
            'video_inventory_scope': '来源清单中登记了本地文件的视频候选；未检查文件或相关性。',
            'rendered_shot_count': len(rendered), 'rendered_shot_counts': _counts(rendered),
            'measured_shot_count': len(measured),
            'timing_status': ('unavailable' if not measured else 'complete' if len(measured) == len(rendered) else 'partial'),
            'measured_shot_seconds': round(sum(measured), 3) if measured else None,
            'long_static_threshold_seconds': LONG_STATIC_SECONDS,
        },
        'issues': issues}
