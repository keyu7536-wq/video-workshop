"""Conservative adaptive Edge pacing with lossless text and sample-based timing.

Fixed-rate synthesis remains in make_video.make_voice. This module uses the
same selected voice for every phrase; it does not claim emotion control, change
pronunciation text, or normalize each phrase's loudness independently.
"""
import re


DEFAULT_ADAPTIVE_BASE_RATE = '+40%'
ADAPTIVE_PACING_VERSION = 2
_MIN_PHRASE = 28
_TARGET_PHRASE = 110
_LONG_SENTENCE = 180
_CLOSERS = '\"\'”’）)]】」』'
_DENSE = re.compile(r'\d|[A-Z]{2,}|参数|损失|梯度|基线|成功率|百分之|相比|对照')
_ABBREVIATION = re.compile(r'(?:\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|No|Fig|Figs|Eq|Eqs|vs|etc)|\be\.g|\bi\.e|\bet\s+al)$', re.I)


def normalize_base_rate(value):
    """Canonicalize an adaptive baseline, separate from the fixed-rate option."""
    if value is None:
        return DEFAULT_ADAPTIVE_BASE_RATE
    if not isinstance(value, str) or not re.fullmatch(r'[+-]?\d+%', value.strip()):
        raise ValueError('自动语速基准须为百分数字符串，例如 +40%。')
    number = int(value.strip()[:-1])
    if not -30 <= number <= 80:
        raise ValueError('自动语速基准须在 -30%～+80% 之间。')
    return f'{number:+d}%'


def _length(text):
    return len(re.sub(r'\s', '', text))


def _url_prefix(text, index):
    return bool(re.search(r'(?:https?://|www\.)[^\s<>\"“”]*$', text[:index], re.I))


def _sentence_dot(text, index):
    before = text[index - 1] if index else ''
    after = text[index + 1] if index + 1 < len(text) else ''
    if before == '.' or after == '.':
        return False
    if before.isdigit() and after.isdigit():
        return False
    if after and re.match(r'[A-Za-z0-9_/]', after):
        return False
    prefix = text[:index]
    if _ABBREVIATION.search(prefix):
        return False
    if re.search(r'([A-Za-z](?:\.[A-Za-z])+|\b[A-Z])$', prefix):
        return False
    return True


def _long_clauses(text):
    """Only long sentences need comma-level cuts; never cut numeric groups."""
    if _length(text) <= _LONG_SENTENCE:
        return [text]
    parts = []
    start = 0
    depth = 0
    for index, char in enumerate(text):
        if char in '([{（【「『“':
            depth += 1
        elif char in ')]}）】」』”':
            depth = max(0, depth - 1)
        if depth or char not in ',，;；:：':
            continue
        before = text[index - 1] if index else ''
        after = text[index + 1] if index + 1 < len(text) else ''
        if before.isdigit() and after.isdigit() or char == ':' and after == '/' or _url_prefix(text, index):
            continue
        if _length(text[start:index + 1]) >= _TARGET_PHRASE:
            parts.append(text[start:index + 1])
            start = index + 1
    tail = text[start:]
    if tail:
        if parts and _length(tail) < _MIN_PHRASE:
            parts[-1] += tail
        else:
            parts.append(tail)
    return parts


def _phrase_chunks(line):
    sentences = []
    start = index = 0
    while index < len(line):
        char = line[index]
        terminal = char in '。！？!?' or char == '.' and _sentence_dot(line, index)
        if char in '!?' and _url_prefix(line, index):
            terminal = False
        if terminal:
            end = index + 1
            while end < len(line) and (line[end] in '。！？!?' + _CLOSERS or line[end].isspace()):
                end += 1
            sentences.extend(_long_clauses(line[start:end]))
            start = index = end
        else:
            index += 1
    if start < len(line):
        sentences.extend(_long_clauses(line[start:]))
    phrases = []
    current = ''
    for sentence in sentences:
        if current and _length(current) >= _MIN_PHRASE and _length(current + sentence) > _TARGET_PHRASE:
            phrases.append(current)
            current = ''
        current += sentence
    if current:
        if phrases and _length(current) < _MIN_PHRASE:
            phrases[-1] += current
        else:
            phrases.append(current)
    return phrases


def delivery_segments(text, base_rate=DEFAULT_ADAPTIVE_BASE_RATE):
    """Plan longer phrases, preserving every input character for spoken text.

    Paragraph boundaries remain explicit. Unbroken tokens are left intact even
    when long; a size target is not permission to split a word or a number.
    Blank input has no speech segments. No phonetic rewriting is performed.
    """
    baseline = normalize_base_rate(base_rate)
    if not isinstance(text, str):
        raise ValueError('配音文案须为文本。')
    if not text.strip():
        return []
    chunks = []
    prefix = ''
    for line in text.splitlines(keepends=True):
        if not line.strip():
            if chunks:
                chunks[-1] += line
            else:
                prefix += line
            continue
        content = line.rstrip('\r\n\u2028\u2029')
        ending = line[len(content):]
        phrases = _phrase_chunks(content)
        phrases[0] = prefix + phrases[0]
        prefix = ''
        phrases[-1] += ending
        chunks.extend(phrases)
    result = []
    for index, chunk in enumerate(chunks):
        dense = bool(_DENSE.search(chunk))
        question = bool(re.search(r'[?？][' + re.escape(_CLOSERS) + r']*\s*$', chunk))
        adjustment = -6 if dense else -2 if question else 0
        paragraph = bool(re.search(r'[\r\n\u2028\u2029]\s*$', chunk))
        pause = 160 if paragraph else 60 if chunk.rstrip().endswith((',', '，', ';', '；', ':', '：')) else 100 if dense else 90 if question else 70
        result.append({'text': chunk, 'rate': f'{int(baseline[:-1]) + adjustment:+d}%',
                       'pause_ms': pause if index + 1 < len(chunks) else 0,
                       'reason': '数字或术语' if dense else '问题' if question else '叙述'})
    return result


async def make_adaptive_voice(text, voice, cache, ffmpeg, base_rate=DEFAULT_ADAPTIVE_BASE_RATE):
    """Return PCM, unchanged-duration boundary events, cache status and plan.

    Offsets are computed from integer PCM samples including inserted silence.
    Existing Edge caches remain reusable because their text/voice/rate key is
    unchanged; pause lengths and pacing_version belong to this assembly plan.
    """
    from make_video import make_voice, RATE_HZ
    baseline = normalize_base_rate(base_rate)
    plan = delivery_segments(text, baseline)
    if not plan:
        raise ValueError('配音文案不能为空。')
    pieces, events, hits, timing = [], [], [], []
    samples = 0
    for segment in plan:
        pcm, words, hit = await make_voice(segment['text'], voice, segment['rate'], cache, ffmpeg)
        if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2:
            raise ValueError('配音返回的单声道 16-bit PCM 为空或样本长度无效。')
        offset = (samples * 10_000_000 + RATE_HZ // 2) // RATE_HZ
        events.extend({**word, 'offset': word['offset'] + offset} if 'offset' in word else dict(word)
                      for word in words)
        audio_samples = len(pcm) // 2
        pause_samples = (segment['pause_ms'] * RATE_HZ + 500) // 1000
        timing.append({**segment, 'start': samples / RATE_HZ, 'audio_seconds': audio_samples / RATE_HZ,
                       'cached': hit, 'provider': 'edge-tts', 'voice': voice,
                       'base_rate': baseline, 'pacing_version': ADAPTIVE_PACING_VERSION,
                       'start_sample': samples, 'audio_samples': audio_samples, 'pause_samples': pause_samples})
        pieces.extend((pcm, b'\x00' * pause_samples * 2))
        samples += audio_samples + pause_samples
        hits.append(hit)
    return b''.join(pieces), events, all(hits), timing
