"""Content identity for new intake. Never rewrite already-authored source IDs."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path


def file_digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def origin(source):
    return {key: source[key] for key in (
        'id', 'name', 'url', 'requested_url', 'parent_url', 'provenance',
        'reuse', 'media_context') if source.get(key)}


def merge_origin(canonical, duplicate):
    """Retain every observed origin without upgrading its reuse/verification status."""
    origins = canonical.setdefault('origins', [origin(canonical)])
    for entry in duplicate.get('origins', [origin(duplicate)]):
        if entry and entry not in origins:
            origins.append(entry)
    aliases = canonical.setdefault('source_aliases', [])
    for identifier in [duplicate.get('id'), *duplicate.get('source_aliases', [])]:
        if identifier and identifier != canonical['id'] and identifier not in aliases:
            aliases.append(identifier)


def content_index(sources):
    """Hash actual bytes once during intake; PDF pages are separate logical sources."""
    index = {}
    for source in sources:
        if source.get('type') not in ('image', 'video') or not source.get('file'):
            continue
        try:
            digest = file_digest(source['file'])
        except OSError:
            # A missing file must never acquire verified identity from a stale hash.
            source.pop('content_sha256', None)
            continue
        source['content_sha256'] = digest
        source.setdefault('origins', [origin(source)])
        index.setdefault((source['type'], digest), source)
    return index


def deduplicate_new_sources(sources, images):
    """Only call before generating a new plan. Keep files; merge candidate identities."""
    index = content_index(sources)
    kept, aliases = [], {}
    for source in sources:
        canonical = index.get((source.get('type'), source.get('content_sha256')))
        if canonical is not None and canonical is not source:
            merge_origin(canonical, source)
            aliases[source['id']] = canonical['id']
        else:
            kept.append(source)
    sources[:] = kept
    # Contact-sheet labels begin with the owning source ID and a separator.
    images[:] = [(label, path) for label, path in images
                 if not any(label == alias or label.startswith(alias + ' ')
                            for alias in aliases)]
    return aliases


def coverage_summary(records, sources):
    statuses = {}
    for record in records:
        status = record.get('status', 'discovered')
        statuses[status] = statuses.get(status, 0) + 1
    media = [s for s in sources if s.get('type') in ('video', 'image')]
    videos = [s for s in media if s['type'] == 'video']
    def digest(source):
        value=source.get('content_sha256')
        return value.lower() if isinstance(value,str) and re.fullmatch(r'[0-9a-fA-F]{64}',value) else None
    known = {digest(s) for s in videos if digest(s)}
    unknown = [s['id'] for s in videos if not digest(s)]
    return {
        'discovered': len(records), 'sampled': statuses.get('downloaded_and_sampled', 0),
        'not_reviewed': statuses.get('not_reviewed', 0), 'duplicates': statuses.get('duplicate', 0),
        'unavailable': sum(statuses.get(s, 0) for s in ('failed', 'inspection_failed', 'unavailable')),
        'status_counts': statuses, 'available_videos': len(videos),
        'independent_videos': len(known) if not unknown else None,
        'unverified_video_ids': unknown,
        'available_images': sum(s['type'] == 'image' for s in media),
        'inventory': 'analysis/research/media_inventory.json',
        'scope': '已获取文件并抽帧不等于已逐帧审看；仅完全相同的文件按字节去重。',
    }
