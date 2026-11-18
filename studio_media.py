"""Static author-media discovery. No page scripts or repository code are executed."""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import PurePosixPath
import re
from urllib.parse import urljoin, urlparse, urlunparse

IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp'}
VIDEO_SUFFIXES = {'.mp4', '.webm', '.mov', '.m4v'}
MAX_DISCOVERED = 1600


def web_url(value, base):
    value = (value or '').strip()
    if not value or value.startswith('#'):
        return None
    result = urljoin(base, value)
    parsed = urlparse(result)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        return None
    # GitHub blob pages are HTML wrappers, not the linked image/video bytes.
    parts = parsed.path.split('/')
    if parsed.hostname == 'github.com' and len(parts) >= 6 and parts[3] == 'blob':
        result = urlunparse(('https', 'raw.githubusercontent.com',
                             '/'.join(parts[:3] + parts[4:]), '', parsed.query, ''))
    return result


def kind_hint(url, mime=''):
    mime = mime.lower().split(';', 1)[0].strip()
    suffix = PurePosixPath(urlparse(url).path).suffix.lower()
    if mime.startswith('video/') or suffix in VIDEO_SUFFIXES:
        return 'video'
    if mime.startswith('image/') or suffix in IMAGE_SUFFIXES:
        return 'image'
    return ''


def hosted_player(url):
    host = (urlparse(url).hostname or '').lower()
    return any(host == domain or host.endswith('.' + domain)
               for domain in ('youtube.com', 'youtube-nocookie.com', 'youtu.be', 'vimeo.com'))


def github_attachment(url):
    parsed = urlparse(url)
    return parsed.hostname == 'github.com' and parsed.path.startswith('/user-attachments/assets/')


class MediaHTML(HTMLParser):
    """Finite HTML links with their author-provided labels and surrounding heading."""
    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url = url
        self.links, self.media, self.scripts = [], [], []
        self.media_items, self.linked_pages = [], []
        self.heading = ''
        self._heading_tag = None
        self._heading_text = []
        self._anchor = None
        self._video = []
        self.omitted = 0

    def add_media(self, value, hint='', origin='html_media', label='', embedded=False):
        url = web_url(value, self.url)
        if not url:
            return
        if len(self.media_items) >= MAX_DISCOVERED:
            self.omitted += 1
            return
        self.media.append(url)
        self.media_items.append({'url': url, 'media_type_hint': hint or kind_hint(url),
                                 'discovery_kind': origin, 'label': label.strip()[:240],
                                 'context': self.heading[:400], 'embedded_page': embedded})

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if re.fullmatch(r'h[1-6]', tag):
            self._heading_tag, self._heading_text = tag, []
        if tag == 'a' and attrs.get('href'):
            url = web_url(attrs['href'], self.url)
            if url:
                self.links.append(url)
                self._anchor = {'url': url, 'attrs': attrs, 'text': []}
        if tag == 'script' and attrs.get('src'):
            url = web_url(attrs['src'], self.url)
            if url:
                self.scripts.append(url)
        if tag == 'video':
            self._video.append(attrs.get('aria-label') or attrs.get('title') or '')
        label = attrs.get('alt') or attrs.get('aria-label') or attrs.get('title') or (self._video[-1] if self._video else '')
        if tag in ('img', 'video', 'source'):
            hint = 'image' if tag == 'img' else 'video' if tag == 'video' or self._video else kind_hint('', attrs.get('type', ''))
            for name in ('src', 'data-src', 'data-original', 'data-lazy-src', 'data-src-sm', 'data-src-hevc', 'data-src-sm-hevc'):
                if attrs.get(name):
                    self.add_media(attrs[name], hint, 'html_' + tag, label)
            for name in ('poster', 'data-poster', 'data-poster-sm', 'data-still', 'data-still-sm', 'data-end', 'data-end-sm'):
                if attrs.get(name):
                    self.add_media(attrs[name], 'image', 'html_poster', label)
            for name in ('srcset', 'data-srcset'):
                values = attrs.get(name) or ''
                # A data URI contains commas; treating its base64 tail as a
                # relative HTTP path would invent a request not in the page.
                if values.lstrip().lower().startswith('data:'):
                    continue
                for item in values.split(','):
                    if item.strip():
                        self.add_media(item.strip().split()[0], 'image', 'html_srcset', label)
        if tag in ('iframe', 'embed'):
            url = web_url(attrs.get('src') or attrs.get('data-src'), self.url)
            if url:
                hint = kind_hint(url, attrs.get('type', ''))
                if hint:
                    self.add_media(url, hint, 'html_' + tag, label)
                else:
                    self.add_media(url, '', 'html_' + tag, label, embedded=True)
                    self.linked_pages.append({'url': url, 'kind': 'embedded_page', 'label': label})

    def handle_data(self, data):
        if self._heading_tag:
            self._heading_text.append(data)
        if self._anchor is not None:
            self._anchor['text'].append(data)

    def handle_endtag(self, tag):
        if tag == self._heading_tag:
            self.heading = ' '.join(''.join(self._heading_text).split())[:400]
            self._heading_tag = None
        if tag == 'video' and self._video:
            self._video.pop()
        if tag == 'a' and self._anchor is not None:
            link, self._anchor = self._anchor, None
            url, attrs = link['url'], link['attrs']
            label = ' '.join(''.join(link['text']).split())[:240]
            hint = kind_hint(url, attrs.get('type', ''))
            if hint or github_attachment(url) or 'download' in attrs:
                if urlparse(url).path.lower().endswith('.pdf') or attrs.get('type') == 'application/pdf':
                    self.linked_pages.append({'url': url, 'kind': 'supplement_document', 'label': label})
                else:
                    self.add_media(url, hint, 'html_link', label)
            elif hosted_player(url):
                self.add_media(url, '', 'author_player_link', label, embedded=True)
            elif re.search(r'\bsupplement(?:ary|al)?\b|\bappendix\b|\b(?:more|additional|all)\s+(?:videos|demos)\b|补充材料|附录', label, re.I):
                kind = 'supplement_document' if urlparse(url).path.lower().endswith('.pdf') else 'supplement_page'
                self.linked_pages.append({'url': url, 'kind': kind, 'label': label})


def readme_media(text, raw_url):
    """HTML, inline/reference Markdown and author-pasted media attachment URLs."""
    page = MediaHTML(raw_url)
    page.feed(text)
    definitions = {}
    for match in re.finditer(r'^\s{0,3}\[([^\]]+)\]:\s*(?:<([^>]+)>|(\S+))', text, re.M):
        definitions[' '.join(match[1].lower().split())] = match[2] or match[3]

    def add(value, label, is_image=False):
        url = web_url(value, raw_url)
        if not url:
            return
        hint = kind_hint(url)
        if is_image or hint or github_attachment(url):
            page.add_media(url, 'image' if is_image and not hint else hint, 'readme_markdown', label)
        elif hosted_player(url):
            page.add_media(url, '', 'readme_player_link', label, embedded=True)

    for match in re.finditer(r'(!?)\[([^\]\n]*)\]\(\s*(?:<([^>]+)>|([^\s)]+))[^\n)]*\)', text):
        add(match[3] or match[4], match[2], bool(match[1]))
    for match in re.finditer(r'(!?)\[([^\]\n]+)\](?:\[([^\]\n]*)\])?(?!\()', text):
        key = ' '.join((match[3] or match[2]).lower().split())
        if key in definitions:
            add(definitions[key], match[2], bool(match[1]))
    for match in re.finditer(r'https?://[^\s<>"\]\)]+', text):
        add(match[0].rstrip('.,;'), '')
    return page


def response_format(url, mime, data, hint=''):
    """Only actual supported media enters sources; HTML/error bodies never do."""
    mime = mime.lower().split(';', 1)[0].strip()
    head = data[:1024].lstrip().lower()
    if 'html' in mime or head.startswith((b'<!doctype html', b'<html', b'<?xml')):
        raise ValueError('来源返回HTML/XML页面，不是可下载媒体')
    formats = {'image/png': ('image', '.png'), 'image/jpeg': ('image', '.jpg'),
               'image/webp': ('image', '.webp'), 'video/mp4': ('video', '.mp4'),
               'video/webm': ('video', '.webm'), 'video/quicktime': ('video', '.mov'),
               'video/x-m4v': ('video', '.m4v')}
    result = formats.get(mime)
    if result is None and mime in ('application/octet-stream', 'binary/octet-stream', ''):
        if data.startswith(b'\x89PNG\r\n\x1a\n'): result = ('image', '.png')
        elif data.startswith(b'\xff\xd8\xff'): result = ('image', '.jpg')
        elif data[:4] == b'RIFF' and data[8:12] == b'WEBP': result = ('image', '.webp')
        elif data[4:8] == b'ftyp': result = ('video', '.mp4')
        elif data.startswith(b'\x1a\x45\xdf\xa3'): result = ('video', '.webm')
    if result is None:
        raise ValueError('响应不是受支持的图片或视频 MIME/文件头：' + mime)
    if hint in ('image', 'video') and result[0] != hint:
        raise ValueError('响应媒体类型与作者标签声明不一致')
    return result
