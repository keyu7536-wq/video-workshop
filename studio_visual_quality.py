"""Read-only visual inventory and measured-hold suggestions.

This is deliberately not a quality gate: source relevance, explanatory value,
motion and voice quality cannot be established from a JSON timeline alone.
"""
from collections.abc import Mapping
import json
import math
import re


LONG_STATIC_SECONDS = 18.0
LONG_EXPLANATION_SECONDS = 30.0
CONSECUTIVE_EXPLANATION_SHOTS = 3
EXPLANATION_KINDS = ('diagram', 'motion')


def _records(value):
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, (list, tuple)) else []


def _number(value):
    return (float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) else None)


def _identifier(value):
    return value if isinstance(value, str) and value else None


def _kind(shot):
    if shot.get('kind')=='motion' or (shot.get('kind')=='video' and shot.get('motion')):
        return 'motion'
    if shot.get('kind') == 'diagram' or (shot.get('kind') == 'video' and shot.get('diagram')):
        return 'diagram'
    if shot.get('visual_beats') or shot.get('visual_timeline') or shot.get('visual_beat_timeline'):
        return 'staged_image'
    value = shot.get('kind')
    return value if value in ('page', 'image', 'video', 'card') else 'unknown'


def _counts(shots):
    counts = {kind: 0 for kind in ('page', 'image', 'video', 'card', 'diagram', 'motion', 'staged_image', 'unknown')}
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


def _shot_ref(shot):
    index = shot.get('shot_index')
    return {'block_id': _identifier(shot.get('block_id')),
            'shot_index': index if type(index) is int and index >= 0 else None}


def _explanation_sequences(shots, measured=False):
    """Plan adjacency is not evidence of timing; measured joins need endpoints."""
    result, run = [], []

    def finish():
        if not run:
            return
        record = {'shots': [_shot_ref(shot) for shot in run], 'shot_count': len(run),
                  'block_ids': list(dict.fromkeys(shot.get('block_id') for shot in run
                                                 if _identifier(shot.get('block_id')))),
                  'kinds': list(dict.fromkeys(_kind(shot) for shot in run))}
        if measured:
            record.update(start=_interval(run[0])[0], end=_interval(run[-1])[1],
                          seconds=round(sum(_interval(shot)[2] for shot in run), 3),
                          timing_basis='rendered_intervals')
        else:
            weights = [_number(shot.get('duration')) for shot in run]
            known = [weight for weight in weights if weight is not None and weight > 0]
            record.update(planned_weight=round(sum(known), 3) if len(known) == len(run) else None,
                          known_weight_count=len(known), timing_basis='plan_weights_not_seconds')
        result.append(record)

    for shot in shots:
        interval = _interval(shot) if measured else None
        if _kind(shot) not in EXPLANATION_KINDS or (measured and interval[2] is None):
            finish(); run = []
            continue
        if measured and run:
            previous_end = _interval(run[-1])[1]
            if previous_end is None or interval[0] is None or abs(interval[0]-previous_end) > .001:
                finish(); run = []
        run.append(shot)
    finish()
    return result


def _hashes(value):
    if not isinstance(value, Mapping):
        return {}
    return {key: digest.lower() for key, digest in value.items()
            if _identifier(key) and isinstance(digest, str) and re.fullmatch(r'[0-9a-fA-F]{64}', digest)}


def _identity_summary(identifiers, hashes):
    identifiers = sorted(set(identifiers))
    known = sorted({hashes[identifier] for identifier in identifiers if identifier in hashes})
    missing = [identifier for identifier in identifiers if identifier not in hashes]
    return {'source_id_count': len(identifiers), 'source_ids': identifiers,
            'known_hash_count': len(known), 'unhashed_source_ids': missing,
            'independent_count': len(known) if not missing else None,
            'dedup_status': 'complete' if not missing else 'partial' if known else 'unavailable'}


def _composition(shot):
    fields = ('focus', 'column_crop', 'columns', 'media_fit', 'layout_style',
              'visual_beats', 'keep_full', 'subtitle_safe')
    # Empty/default fields mean the same composition. Do not inspect prose.
    return json.dumps({key: shot[key] for key in fields if shot.get(key)},
                      sort_keys=True, ensure_ascii=False)


def _crop(shot):
    box = shot.get('bbox') or shot.get('crop') or [0, 0, 1, 1]
    if (not isinstance(box, (list, tuple)) or len(box) != 4
            or any(_number(value) is None for value in box)
            or not 0 <= box[0] < box[2] <= 1 or not 0 <= box[1] < box[3] <= 1):
        return None
    return tuple(float(value) for value in box)


def _same_crop(left, right):
    if left is None or right is None:
        return False
    if left == right:
        return True
    if max(abs(a-b) for a, b in zip(left, right)) > .005:
        return False
    intersection = max(0, min(left[2], right[2])-max(left[0], right[0])) * max(
        0, min(left[3], right[3])-max(left[1], right[1]))
    area = (left[2]-left[0])*(left[3]-left[1]) + (right[2]-right[0])*(right[3]-right[1])
    return intersection > 0 and intersection/(area-intersection) >= .9


def _repeated_views(shots, sources, hashes, video=False):
    """Source/crop metadata reuse, not a semantic or perceptual duplicate test."""
    groups = []
    for shot in shots:
        kind = _kind(shot)
        if (video and kind != 'video') or (not video and kind not in ('page', 'image', 'staged_image')):
            continue
        identifier = _identifier(shot.get('source_id'))
        if identifier is None:
            continue
        source = sources.get(identifier, {})
        digest = hashes.get(identifier)
        page_source = source.get('type') == 'page' or kind == 'page'
        page = source.get('page') if page_source else None
        # A PDF hash alone cannot establish that two unidentified pages match.
        page_key = page if type(page) is int else identifier if page_source else None
        identity = ('sha256', digest, page_key) if digest else ('source_id_unverified', identifier, page_key)
        crop, composition = _crop(shot), _composition(shot)
        selection = None
        if video:
            start, weight = _number(shot.get('in')), _number(shot.get('duration'))
            if start is None or start < 0 or weight is None or weight <= 0:
                continue
            selection = (start, weight)
        found = next((group for group in groups if group['_identity'] == identity
                      and group['_composition'] == composition and group['_selection'] == selection
                      and _same_crop(group['_crop'], crop)), None)
        if found is None:
            found = {'_identity': identity, '_composition': composition, '_crop': crop,
                     '_selection': selection, 'identity_basis': identity[0], 'source_ids': [],
                     'shots': [], 'crop_match': 'exact'}
            groups.append(found)
        if identifier not in found['source_ids']:
            found['source_ids'].append(identifier)
        found['shots'].append(_shot_ref(shot))
        if crop != found['_crop']:
            found['crop_match'] = 'near_identical_metadata'
    public = []
    for group in groups:
        item = {key: value for key, value in group.items() if not key.startswith('_')}
        item['shot_count'] = len(item['shots'])
        if video:
            item.update(source_in=group['_selection'][0], planned_duration_weight=group['_selection'][1],
                        timing_basis='same_planned_selection_not_measured_playback')
        public.append(item)
    return public


def visual_quality_report(plan, sources, rendered_shots=None, *, source_hashes=None, context=None):
    """Return JSON-serializable advisory statistics without changing inputs.

    `sources` is the intake source list. A video with a `file` entry is an
    inventory candidate; this function neither opens that file nor establishes
    availability, relevance or reuse permission. `source_hashes` may map source
    IDs to caller-verified SHA256 values, overriding source `content_sha256`
    recorded during intake. This pure report does not re-read files to verify
    those intake records, trust hash fields in plans, or claim that missing
    hashes were deduplicated. `rendered_shots` must describe
    the current render if supplied; without it, no duration inference is made.
    """
    plan = plan if isinstance(plan, Mapping) else {}
    source_lookup = {source['id']: source for source in _records(sources) if _identifier(source.get('id'))}
    recorded_hashes = _hashes({identifier: source.get('content_sha256') for identifier, source in source_lookup.items()})
    hashes = dict(recorded_hashes)
    # An explicit unknown/invalid override must not reuse an old intake hash.
    if isinstance(source_hashes, Mapping):
        for identifier in source_hashes:
            hashes.pop(identifier, None)
    supplied_hashes = _hashes(source_hashes)
    hashes.update(supplied_hashes)
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
    used_hashes = {hashes[identifier] for identifier in used if identifier in hashes}
    unused_aliases = [identifier for identifier in unused if hashes.get(identifier) in used_hashes]
    unused_candidates = [identifier for identifier in unused if identifier not in unused_aliases]
    issues = []
    if unused_candidates:
        issues.append({'code': 'unused_video_candidates', 'block_id': None, 'severity': 'suggestion',
            'source_ids': unused_candidates,
            'reason': f'素材清单中有 {len(unused_candidates)} 个登记了本地文件的视频来源编号未用于当前分镜。'
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

    planned_sequences = _explanation_sequences(planned)
    rendered_sequences = _explanation_sequences(rendered, measured=True)
    keep_script = isinstance(context, Mapping) and context.get('content_type') == 'script'
    protection = ('保持给定文稿；' if keep_script else '') + '教程、连续推导或明确需要全动画的内容可以保留，不要求实拍比例。'
    advice = '请回看这些机制示意是否需要接回对应的实物、动作或结果，或沿用一个具体例子；'
    for sequence in planned_sequences:
        if sequence['shot_count'] >= CONSECUTIVE_EXPLANATION_SHOTS:
            weight = sequence['planned_weight']
            weight_text = f'，计划权重合计 {weight:g}（不是实测秒数）' if weight is not None else '，实际时长尚未由这些权重确定'
            issues.append({'code': 'consecutive_explanation_plan', 'severity': 'suggestion',
                'block_id': sequence['block_ids'][0] if sequence['block_ids'] else None, **sequence,
                'reason': f'分镜顺序中连续 {sequence["shot_count"]} 镜采用机制图或对象动画{weight_text}。'
                          + advice + protection})
    for sequence in rendered_sequences:
        if sequence['seconds'] >= LONG_EXPLANATION_SECONDS:
            issues.append({'code': 'long_explanation_sequence', 'severity': 'suggestion',
                'block_id': sequence['block_ids'][0] if sequence['block_ids'] else None, **sequence,
                'reason': f'当前渲染时间线中，连续 {sequence["shot_count"]} 镜机制图或对象动画共 {sequence["seconds"]:.1f} 秒。'
                          + advice + protection + '这是画面类型统计，不表示已观看或判定解释失败。'})

    image_groups = _repeated_views(planned, source_lookup, hashes)
    video_groups = _repeated_views(planned, source_lookup, hashes, video=True)
    repeated_images = [group for group in image_groups if group['shot_count'] > 1]
    repeated_videos = [group for group in video_groups if group['shot_count'] > 1]
    for code, groups, wording in (
            ('repeated_image_view', repeated_images, '使用同一来源、页码与相同或近似裁图'),
            ('repeated_video_selection', repeated_videos, '登记了相同的视频取片起点、计划长度与构图（不是实测重复播放时长）')):
        for group in groups:
            issues.append({'code': code, 'severity': 'suggestion',
                'block_id': group['shots'][0]['block_id'], **group,
                'reason': f'{group["shot_count"]} 镜{wording}。可核对每次是否在解释不同信息；'
                          '回顾、对照或逐步讲解所需的复用可以保留。'
                          + ('来源按采集时记录或显式提供的文件字节哈希匹配；本报告不重新读文件，也未做语义或像素相似度判断。' if group['identity_basis'] == 'sha256'
                             else '缺少文件哈希，当前仅按来源编号匹配，未确认跨来源内容去重。')})
    measured_by_kind = {kind: 0.0 for kind in _counts([])}
    for shot in rendered:
        seconds = _interval(shot)[2]
        if seconds is not None:
            measured_by_kind[_kind(shot)] += seconds
    used_sources = sorted({_identifier(shot.get('source_id')) for shot in planned
                           if _identifier(shot.get('source_id'))})
    coverage = {'available_videos': _identity_summary(available, hashes),
                'used_videos': _identity_summary(used, hashes),
                'unused_alias_source_ids': unused_aliases,
                'used_source_id_count': len(used_sources), 'used_source_ids': used_sources,
                'image_view_count': len(image_groups), 'video_selection_count': len(video_groups),
                'repeated_image_views': repeated_images, 'repeated_video_selections': repeated_videos,
                'recorded_hash_source_ids': sorted(set(recorded_hashes) & set(hashes)-set(supplied_hashes)),
                'supplied_hash_source_ids': sorted(supplied_hashes),
                'identity_scope': '独立视频数量依据采集时记录的content_sha256或调用方显式覆盖；本报告不重新读文件核对是否变化。缺哈希时总数未知。'
                                  '图片视图和视频选段按来源/页码/构图/取片登记匹配，不代表语义覆盖。'}

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
            'measured_seconds_by_kind': {kind: round(value, 3) for kind, value in measured_by_kind.items()} if measured else None,
            'longest_planned_explanation_shots': max((item['shot_count'] for item in planned_sequences), default=0),
            'longest_measured_explanation_seconds': max((item['seconds'] for item in rendered_sequences), default=0) if measured else None,
            'long_static_threshold_seconds': LONG_STATIC_SECONDS,
            'long_explanation_threshold_seconds': LONG_EXPLANATION_SECONDS,
        },
        'coverage': coverage,
        'planned_explanation_sequences': planned_sequences,
        'rendered_explanation_sequences': rendered_sequences,
        'issues': issues}
