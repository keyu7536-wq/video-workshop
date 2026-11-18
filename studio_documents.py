"""Extract complete PDF text while bounding visual previews independently.

Extraction does not establish reading order, image understanding or scientific
correctness. Unselected pages remain available to later on-demand rendering.
"""
from __future__ import annotations

import json
from pathlib import Path
import re


_CONTENTS = re.compile(r'^\s*(?:table of contents|contents|目\s*录)\s*$', re.I | re.M)
_SECTION = re.compile(
    r'^\s*(?:\d{1,2}[.\s]+[A-Z][^\n]{2,100}|'
    r'abstract|introduction|conclusion[s]?|discussion|references|appendix(?:\s+[A-Z])?)\s*$',
    re.M,
)
_FIGURE = re.compile(r'^\s*(?:figure|fig\.?|table|图|表)\s*\d', re.I | re.M)


def _spread(candidates, count):
    """Select stable, evenly spaced members, preserving both ends when possible."""
    candidates = sorted(set(candidates))
    if count <= 0 or not candidates:
        return []
    if len(candidates) <= count:
        return candidates
    if count == 1:
        return [candidates[len(candidates) // 2]]
    return [candidates[round(i * (len(candidates) - 1) / (count - 1))]
            for i in range(count)]


def _sample_pages(pages, outline, limit):
    count = len(pages)
    if count <= limit:
        return list(range(count))
    if not limit:
        return []
    selected = {0}
    if limit > 1:
        selected.add(count - 1)

    def add(candidates, budget):
        remaining = sorted(set(candidates) - selected)
        selected.update(_spread(remaining, min(budget, limit - len(selected))))

    # Protect coverage across the entire document before assigning semantic slots.
    add(_spread(range(count), max(2, limit // 3)), limit)
    add([i for i, p in enumerate(pages) if _CONTENTS.search(p['text'][:4000])], 2)
    if outline:
        top_level = min(item['level'] for item in outline)
        section_pages = sorted({item['page'] - 1 for item in outline
                                if item['level'] == top_level})
    else:
        section_pages = [i for i, p in enumerate(pages)
                         if _SECTION.search(p['text'][:1200])]
    section_budget = max(1, limit // 4)
    before = len(selected)
    # Keep the first context/overview sections as well as later section starts.
    add(section_pages[:min(3, section_budget)], section_budget)
    add(section_pages, max(0, section_budget - (len(selected) - before)))
    add([i for i, p in enumerate(pages) if _FIGURE.search(p['text'])], max(1, limit // 3))
    # Fill remaining gaps by farthest distance, rather than taking early pages.
    while len(selected) < limit:
        candidate = max((i for i in range(count) if i not in selected),
                        key=lambda i: (min(abs(i - j) for j in selected), -i))
        selected.add(candidate)
    return sorted(selected)


def inspect_document(path, analysis, max_previews=24, max_pages=400,
                     max_characters=2_000_000):
    """Return ``(pages, preview_images)`` with complete text and sampled PNGs.

    Each page is ``{'page': int, 'text': str, 'file': str | None}``; ``file``
    is relative to ``analysis``. Preview Paths are absolute, sorted by page.
    All text is saved as the list ``paper_pages.json``. The outline and explicit
    visual/text coverage live in ``document_manifest.json``. No OCR is performed.
    Page and text budgets are checked before creating or rendering any preview.
    """
    import pymupdf

    for name, value, minimum in [('max_previews', max_previews, 0),
                                 ('max_pages', max_pages, 1),
                                 ('max_characters', max_characters, 1)]:
        if type(value) is not int or value < minimum:
            raise ValueError(f'{name} must be an integer >= {minimum}')
    path, analysis = Path(path).resolve(), Path(analysis).resolve()
    with pymupdf.open(path) as document:
        if document.needs_pass:
            raise ValueError('PDF is password protected; provide a readable copy.')
        count = len(document)
        if not 1 <= count <= max_pages:
            raise ValueError(f'PDF page count {count} exceeds supported range 1-{max_pages}.')
        outline = [{'level': level, 'title': title, 'page': page}
                   for level, title, page in document.get_toc(simple=True)
                   if 1 <= page <= count]
        pages, characters = [], 0
        for number, page in enumerate(document, 1):
            text = page.get_text('text', sort=False)
            characters += len(text)
            if characters > max_characters:
                raise ValueError(f'PDF text exceeds max_characters={max_characters}; '
                                 'no previews were rendered.')
            pages.append({'page': number, 'text': text, 'file': None})
        if not any(page['text'].strip() for page in pages):
            raise ValueError('PDF has no extractable text (possibly scanned). '
                             'OCR is not included; provide a text PDF or run OCR first.')
        selected = _sample_pages(pages, outline, min(max_previews, count))
        analysis.mkdir(parents=True, exist_ok=True)
        preview_images = []
        if selected:
            (analysis / 'pages').mkdir(exist_ok=True)
        for index in selected:
            relative = f'pages/page_{index + 1:03}.png'
            target = analysis / relative
            document[index].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False).save(target)
            pages[index]['file'] = relative
            preview_images.append(target)

    empty_pages = [p['page'] for p in pages if not p['text'].strip()]
    caveats = ['Text extraction covers every page; no OCR was performed.',
               'PDF content order is retained (sort=False), not guaranteed semantic reading order.',
               'Rendered previews are not evidence that a person or model visually reviewed them.']
    if len(selected) < count:
        caveats.append('Visual previews are sampled across the document; unrendered pages '
                       'and their figures have not been visually inspected.')
    if empty_pages:
        caveats.append('Pages without extractable text may be blank or scanned; '
                       'their visual content requires separate inspection or OCR.')
    manifest = {
        'source_file': str(path), 'total_pages': count, 'character_count': characters,
        'text_coverage': 'all_pages_extracted', 'text_pages_processed': count,
        'pages_without_text': empty_pages, 'outline': outline,
        'image_sample_pages': [i + 1 for i in selected],
        'preview_count': len(preview_images), 'preview_scale': 1.5,
        'sampling': 'title, full-document spacing, contents, section starts and figure/table captions',
        'limits': {'max_previews': max_previews, 'max_pages': max_pages,
                   'max_characters': max_characters}, 'caveats': caveats,
    }
    (analysis / 'paper_pages.json').write_text(
        json.dumps(pages, ensure_ascii=False, indent=2), encoding='utf-8')
    (analysis / 'document_manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return pages, preview_images
