"""Bounded discovery from an explicit public query, with provider provenance.

Search results are candidates, not verified author identities. The caller must
match the paper/title and authors before reading a suggested project. No private
source text or fallback query is accepted by this module.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import ipaddress
import re
import threading
import time
from urllib.parse import parse_qs, quote, urljoin, urlparse
import xml.etree.ElementTree as ET


LIMIT = 1024**2
MAX_RESULTS = 10
ATOM = '{http://www.w3.org/2005/Atom}'
ARXIV = '{http://arxiv.org/schemas/atom}'
_arxiv_lock = threading.Lock()
_arxiv_ready_at = 0.0


class SearchResults(list):
    """A normal URL list plus serializable diagnostics for search.json."""
    def __init__(self):
        super().__init__()
        self.attempts = []
        self.provenance = []


def _pause_for_arxiv():
    # One API request per search, spaced across calls in the current process.
    global _arxiv_ready_at
    with _arxiv_lock:
        delay = _arxiv_ready_at-time.monotonic()
        if delay > 0:
            time.sleep(delay)
        _arxiv_ready_at = time.monotonic()+3.0


def _candidate_url(value):
    value = value.strip().rstrip('.,;')
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or '').lower()
        if (parsed.scheme not in ('http', 'https') or not host or
                parsed.username or parsed.password or parsed.port not in (None, 80, 443)):
            return None
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
            return None
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            return None
    except ValueError:
        return None
    # DNS/public-address/redirect checks remain mandatory when the caller
    # actually fetches this candidate through studio_research.fetch.
    return value


class _DDGResults(HTMLParser):
    def __init__(self, base):
        super().__init__()
        self.base = base
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag != 'a' or 'result__a' not in attrs.get('class', '').split():
            return
        link = urljoin(self.base, attrs.get('href', ''))
        actual = parse_qs(urlparse(link).query).get('uddg', [link])[0]
        host = (urlparse(actual).hostname or '').lower()
        if host == 'duckduckgo.com' or host.endswith('.duckduckgo.com'):
            return
        actual = _candidate_url(actual)
        if actual and actual not in self.links:
            self.links.append(actual)


def _normalized_title(text):
    return ' '.join(re.findall(r'\w+', text.casefold(), flags=re.UNICODE))


def _read_attempt(result, provider, query, endpoint, fetch):
    attempt = {
        'provider': provider, 'query': query, 'request_url': endpoint,
        'accessed_at': datetime.now(timezone.utc).isoformat(),
    }
    result.attempts.append(attempt)
    try:
        data, final, mime = fetch(endpoint, LIMIT)
        if len(data) > LIMIT:
            raise ValueError('Search response exceeded the 1 MiB cap.')
        attempt.update(resolved_url=final, content_type=mime, bytes=len(data),
                       sha256=hashlib.sha256(data).hexdigest())
        return attempt, data
    except Exception as exc:
        attempt.update(status='error', reason=f'{type(exc).__name__}: {exc}'[:1600])
        if isinstance(getattr(exc, 'code', None), int):
            attempt['http_status'] = exc.code
        return attempt, None


def _add(result, url, provider, **metadata):
    actual = _candidate_url(url)
    if actual and actual not in result and len(result) < MAX_RESULTS:
        result.append(actual)
        result.provenance.append({'url': actual, 'provider': provider, **metadata})


def _arxiv_candidates(result, query, data, attempt):
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('Unexpected XML document/entity declaration.')
    tree = ET.fromstring(data)
    if tree.tag != ATOM+'feed':
        raise ValueError('arXiv did not return an Atom feed.')
    entries = tree.findall(ATOM+'entry')
    matched = []
    for entry in entries[:5]:
        title = ' '.join(entry.findtext(ATOM+'title', '').split())
        if _normalized_title(title) != _normalized_title(query):
            continue
        record = entry.findtext(ATOM+'id', '')
        if not _candidate_url(record):
            continue
        authors = [' '.join(n.findtext(ATOM+'name', '').split())
                   for n in entry.findall(ATOM+'author')]
        metadata = {'record_url': record, 'title': title, 'authors': authors}
        matched.append(metadata)
        # The author's own submitted comments/abstract can identify an external
        # project even when an older PDF did not print that URL.
        for field, tag in (('comment', ARXIV+'comment'), ('summary', ATOM+'summary')):
            text = entry.findtext(tag, '')
            for url in re.findall(r'https?://[^\s<>"\]\)]+', text):
                _add(result, url, 'arxiv_title', field=field, **metadata)
        # Keep the exact version's landing page as provenance/a candidate;
        # never treat the PDF link itself as a newly found author project.
        for link in entry.findall(ATOM+'link'):
            if link.get('rel') == 'alternate':
                _add(result, link.get('href', ''), 'arxiv_title',
                     field='alternate', **metadata)
    attempt['matched_records'] = matched
    if result:
        attempt.update(status='ok', result_count=len(result))
    else:
        attempt.update(status='no_exact_match' if entries else 'empty',
                       reason='No exact normalized title match with usable public links.')


def search_public(query, fetch):
    """Try at most two providers with the same explicitly public query.

    The injected fetch preserves TLS verification, public-URL/redirect checks,
    bounded transport retries, and a response cap. Challenge pages and HTTP
    access decisions are recorded; no challenge is solved or retried here.
    """
    if not isinstance(query, str) or len(query) > 240:
        raise ValueError('Public search query must be a string of at most 240 characters.')
    query = ' '.join(query.split())
    result = SearchResults()
    if not query:
        return result
    endpoint = 'https://html.duckduckgo.com/html/?q='+quote(query, safe='')
    attempt, data = _read_attempt(result, 'duckduckgo_html', query, endpoint, fetch)
    if data is not None:
        text = data.decode('utf-8', errors='replace')
        lower = text.lower()
        if any(marker in lower for marker in
               ('challenge-form', 'anomaly.js', 'anomaly-modal', '<title>captcha')):
            attempt.update(status='challenge',
                           reason='Provider returned an access challenge; not solved or retried.')
        else:
            page = _DDGResults(attempt['resolved_url'])
            page.feed(text)
            for url in page.links[:MAX_RESULTS]:
                _add(result, url, 'duckduckgo_html', field='result_link')
            attempt.update(status='ok' if result else 'empty', result_count=len(result))
            if not result:
                attempt['reason'] = 'No recognized result links in the search response.'
    if result:
        return result
    # Title matching deliberately does not turn an ambiguous topic into a
    # claimed paper identity. The caller's later title/author check still runs.
    phrase = query.replace('"', ' ').replace('\\', ' ')
    endpoint = ('https://export.arxiv.org/api/query?search_query='+
                quote('ti:"'+phrase+'"', safe='')+'&max_results=5')
    _pause_for_arxiv()
    attempt, data = _read_attempt(result, 'arxiv_title', query, endpoint, fetch)
    if data is not None:
        try:
            _arxiv_candidates(result, query, data, attempt)
        except (ValueError, ET.ParseError) as exc:
            attempt.update(status='invalid_response', reason=f'{type(exc).__name__}: {exc}'[:1600])
    return result
