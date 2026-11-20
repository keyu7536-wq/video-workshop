"""Bounded anonymous YouTube/Vimeo downloads using yt-dlp metadata extraction.

Only one public, non-live, non-DRM video is accepted. yt-dlp never downloads
files or executes postprocessors here: selected plain HTTPS MP4 streams use
our metered transport. HLS/DASH fragment assembly and browser credentials are
deliberately unsupported. A separate process bounds extraction and decoding.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_BYTES = 120 * 1024**2
MAX_DURATION = 900
MAX_HEIGHT = 720
METADATA_BYTES = 8 * 1024**2
VERSION = 1


class HostedMediaError(RuntimeError):
    def __init__(self, code, reason, status='rejected'):
        super().__init__(reason)
        self.code, self.status = code, status


def normalize_hosted_url(url):
    """Identify exactly one video without resolving pages or following embeds."""
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 32 for c in url):
        raise ValueError('公开视频链接无效。')
    parsed = urlparse(url.strip())
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError('公开视频端口无效。') from exc
    if parsed.scheme not in ('https', 'http') or parsed.username or parsed.password or port not in (None, 80, 443):
        raise ValueError('仅支持无账号信息的公共 HTTP(S) 单视频链接。')
    host = (parsed.hostname or '').lower()
    query = parse_qs(parsed.query, keep_blank_values=True)
    if any(key in query for key in ('list', 'playlist', 'listType', 'channel')):
        raise ValueError('不接受播放列表或频道链接。')
    video_id = None
    if host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtube-nocookie.com', 'www.youtube-nocookie.com'):
        if parsed.path == '/watch' and len(query.get('v', [])) == 1:
            video_id = query['v'][0]
        else:
            match = re.fullmatch(r'/(?:embed|shorts)/([A-Za-z0-9_-]{11})/?', parsed.path)
            video_id = match[1] if match else None
    elif host == 'youtu.be':
        match = re.fullmatch(r'/([A-Za-z0-9_-]{11})/?', parsed.path)
        video_id = match[1] if match else None
    if video_id and re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        return {'provider': 'youtube', 'id': video_id, 'url': 'https://www.youtube.com/watch?v='+video_id}
    if host in ('vimeo.com', 'www.vimeo.com', 'player.vimeo.com'):
        pattern = r'/video/(\d{1,12})/?' if host == 'player.vimeo.com' else r'/(\d{1,12})(?:/([a-fA-F0-9]{10,32}))?/?'
        match = re.fullmatch(pattern, parsed.path)
        if match:
            secret = match[2] if host != 'player.vimeo.com' and match.lastindex == 2 else None
            if 'h' in query:
                if len(query['h']) != 1 or not re.fullmatch(r'[a-fA-F0-9]{10,32}', query['h'][0]):
                    raise ValueError('Vimeo 单视频访问标识无效。')
                secret = query['h'][0]
            target = f'https://player.vimeo.com/video/{match[1]}' if secret else f'https://vimeo.com/{match[1]}'
            if secret:
                target += '?' + urlencode({'h': secret})
            return {'provider': 'vimeo', 'id': match[1], 'url': target}
    raise ValueError('仅支持 YouTube/Vimeo 的单视频页面或嵌入链接；不支持直播入口。')


def _limits(max_bytes, timeout, max_duration, max_height):
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BYTES:
        raise ValueError('单个公开视频的传输预算须为 1 至 120 MiB。')
    for value, low, high, name in ((timeout, 1, 180, '超时'), (max_duration, 1, MAX_DURATION, '时长')):
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(name + '限制无效。')
    if type(max_height) is not int or not 144 <= max_height <= MAX_HEIGHT:
        raise ValueError('公开托管视频最高分辨率须为 144 至 720 行。')
    return {'max_bytes': max_bytes, 'timeout': timeout, 'max_duration': max_duration,
            'max_height': max_height, 'max_width': math.ceil(max_height*16/9)}


def _public(url):
    from studio_research import public_url
    if urlparse(url).scheme != 'https':
        raise HostedMediaError('unsafe_url', '托管视频请求只接受公共 HTTPS 地址。')
    try:
        return public_url(url)
    except ValueError as exc:
        raise HostedMediaError('unsafe_url', str(exc)) from exc


class _Redirect(HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 1

    def __init__(self, before_request=None):
        self.before_request = before_request

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _public(newurl)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected:
            for key in ('Cookie', 'Authorization', 'Proxy-Authorization'):
                redirected.remove_header(key)
        return redirected

    def http_error_302(self, req, fp, code, msg, headers):
        # urllib's default handler drains redirect bodies without a byte cap.
        # Close them immediately; every followed URL is checked and bounded.
        location = headers.get('location') or headers.get('uri')
        if not location:
            return None
        target = urljoin(req.full_url, location)
        chain = getattr(req, '_hosted_redirect_chain', [req.full_url])
        if target in chain or len(chain) >= 4:
            fp.close()
            raise HostedMediaError('redirect_limit', '平台重定向循环或超过 3 跳。')
        try:
            redirected = self.redirect_request(req, fp, code, msg, headers, target)
            if redirected is None:
                raise HostedMediaError('redirect_rejected', '平台重定向不受支持。')
            redirected._hosted_redirect_chain = [*chain, target]
            if self.before_request:
                self.before_request()
        finally:
            fp.close()
        return self.parent.open(redirected, timeout=req.timeout)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


class _Transport:
    """One byte/deadline budget includes metadata, media and failed reads."""
    def __init__(self, limits, progress=None):
        self.limits = limits
        self.deadline = time.monotonic() + limits['timeout']
        self.received = self.metadata_bytes = self.requests = 0
        self.progress = progress
        self.opener = build_opener(_Redirect(self.reserve_request))

    def reserve_request(self):
        self.remaining_time()
        self.requests += 1
        if self.requests > 32:
            raise HostedMediaError('request_limit', '超过单视频 32 次请求上限。')

    def remaining_time(self):
        remaining = self.deadline-time.monotonic()
        if remaining <= 0:
            raise HostedMediaError('timeout', '公开视频读取达到时间上限。', 'timeout')
        return min(15, remaining)

    def charge(self, size, metadata):
        self.received += size
        if metadata:
            self.metadata_bytes += size
        if self.progress:
            self.progress(self.received)

    def read(self, url, *, headers=None, data=None, method='GET', output=None):
        _public(url)
        self.reserve_request()
        if method not in ('GET', 'POST', 'HEAD') or (data is not None and (not isinstance(data, bytes) or len(data) > 1024**2)):
            raise HostedMediaError('request_rejected', '视频平台请求方法或正文不受支持。')
        safe_headers = {key: value for key, value in (headers or {}).items()
                        if key.lower() in ('user-agent', 'accept', 'accept-language', 'content-type', 'referer', 'origin', 'range')}
        safe_headers['Accept-Encoding'] = 'identity'
        safe_headers.setdefault('User-Agent', 'Mozilla/5.0 VideoWorkshop/1')
        request = Request(url, data=data, headers=safe_headers, method=method)
        try:
            response = self.opener.open(request, timeout=self.remaining_time())
        except HTTPError as exc:
            exc.close()
            status = 'access_restricted' if exc.code in (401, 403, 407, 429) else 'http_error'
            raise HostedMediaError(status, f'公开平台返回 HTTP {exc.code}，未重试或使用登录凭据。', 'unavailable') from exc
        metadata = output is None
        with response:
            _public(response.geturl())
            encoding = response.headers.get('Content-Encoding', '').lower()
            if encoding not in ('', 'identity'):
                raise HostedMediaError('unsupported_encoding', '平台未提供可限额读取的未压缩响应。')
            remaining = self.limits['max_bytes']-self.received
            if metadata:
                remaining = min(remaining, METADATA_BYTES-self.metadata_bytes, 4*1024**2)
            length = response.headers.get('Content-Length', '')
            expected = int(length) if str(length).isdigit() else None
            if remaining <= 0 or (expected is not None and expected > remaining):
                raise HostedMediaError('byte_limit', '平台响应超过剩余传输预算。')
            parts, size = [], 0
            reader = getattr(response, 'read1', None) or response.read
            while method != 'HEAD':
                self.remaining_time()
                if size == expected:
                    break
                if remaining <= 0:
                    raise HostedMediaError('byte_limit', '平台响应达到传输上限，停止读取。')
                raw = getattr(getattr(response, 'fp', None), 'raw', None)
                sock = getattr(raw, '_sock', None)
                if sock is not None:
                    sock.settimeout(self.remaining_time())
                try:
                    chunk = reader(min(64*1024, remaining))
                except Exception as exc:
                    partial = getattr(exc, 'partial', b'')
                    if isinstance(partial, bytes):
                        self.charge(len(partial), metadata)
                    raise
                if not chunk:
                    break
                self.charge(len(chunk), metadata)
                size += len(chunk); remaining -= len(chunk)
                if remaining < 0:
                    raise HostedMediaError('byte_limit', '平台响应读取超过传输上限。')
                if output is None:
                    parts.append(chunk)
                else:
                    output.write(chunk)
            if method != 'HEAD' and expected is not None and size != expected:
                raise HostedMediaError('incomplete_response', '公开视频响应未完整读取。', 'failed')
            return b''.join(parts), response.geturl(), dict(response.headers), getattr(response, 'status', 200)


class _Logger:
    def __init__(self):
        self.messages = []

    def debug(self, message):
        pass

    info = debug

    def warning(self, message):
        if len(self.messages) < 12:
            # Signed CDN URLs and extractor stacks are not useful provenance.
            self.messages.append(re.sub(r'https?://\S+', '[URL]', str(message))[:500])

    error = warning


def _extract_info(identity, transport, logger):
    try:
        import yt_dlp
        from yt_dlp import globals as yglobals
        from yt_dlp.networking import Response
        from yt_dlp.version import __version__
    except ImportError as exc:
        raise HostedMediaError('dependency_missing', '缺少可选依赖 yt-dlp；请安装 yt-dlp>=2026.10.26。', 'unavailable') from exc
    # This runs only in the isolated worker. Do not discover/import locally
    # installed yt-dlp plugins or consult browser/configuration directories.
    yglobals.plugin_dirs.value = []
    yglobals.all_plugins_loaded.value = True
    from yt_dlp.extractor.youtube import YoutubeIE
    from yt_dlp.extractor.vimeo import VimeoIE

    class GuardedYoutubeDL(yt_dlp.YoutubeDL):
        def urlopen(self, req):
            url = req if isinstance(req, str) else req.url
            headers = {} if isinstance(req, str) else dict(req.headers)
            data = None if isinstance(req, str) else req.data
            method = 'GET' if isinstance(req, str) else req.method
            body, final, returned_headers, status = transport.read(url, headers=headers, data=data, method=method)
            return Response(io.BytesIO(body), final, returned_headers, status)

    options = {'quiet': True, 'no_warnings': True, 'logger': logger,
               'noplaylist': True, 'ignoreerrors': False, 'cachedir': False,
               'cookiefile': None, 'cookiesfrombrowser': None, 'usenetrc': False,
               'username': None, 'password': None, 'videopassword': None,
               'geo_bypass': False, 'nocheckcertificate': False,
               'socket_timeout': 15, 'retries': 0, 'extractor_retries': 0,
               'fragment_retries': 0, 'file_access_retries': 0,
               'allow_unplayable_formats': False, 'enable_file_urls': False,
               'js_runtimes': {}, 'remote_components': [], 'skip_download': True,
               'postprocessors': [], 'writesubtitles': False, 'writeautomaticsub': False}
    with GuardedYoutubeDL(options, auto_init=False) as downloader:
        downloader.add_info_extractor(YoutubeIE() if identity['provider'] == 'youtube' else VimeoIE())
        info = downloader.extract_info(identity['url'], download=False, process=False)
    return info, __version__


def _select_format(info, identity, limits):
    if not isinstance(info, dict) or info.get('_type', 'video') != 'video' or 'entries' in info:
        raise HostedMediaError('not_single_video', '平台结果不是一个可下载的独立视频。')
    if str(info.get('id', '')) != identity['id']:
        raise HostedMediaError('identity_mismatch', '平台返回的视频身份与作者链接不一致。')
    if info.get('is_live') or info.get('was_live') or info.get('live_status') not in (None, 'not_live'):
        raise HostedMediaError('live_video', '不下载直播、预告直播或直播回放。')
    if info.get('has_drm'):
        raise HostedMediaError('drm', '不下载 DRM 保护视频。')
    if info.get('availability') not in (None, 'public', 'unlisted') or (info.get('age_limit') or 0) > 0:
        raise HostedMediaError('access_restricted', '视频需要登录、年龄验证或其他访问权限。', 'unavailable')
    duration = info.get('duration')
    if type(duration) not in (int, float) or not math.isfinite(duration) or not 1 <= duration <= limits['max_duration']:
        raise HostedMediaError('duration_limit', '视频时长未知或超出本次时长上限。')
    eligible = []
    for format_info in info.get('formats') or []:
        if not isinstance(format_info, dict):
            continue
        width, height = format_info.get('width'), format_info.get('height')
        if (format_info.get('has_drm') or format_info.get('fragments') or format_info.get('manifest_url')
                or format_info.get('protocol') not in ('https', 'http')
                or urlparse(format_info.get('url', '')).scheme != 'https'
                or format_info.get('ext') != 'mp4' or format_info.get('vcodec') in (None, 'none')):
            continue
        if (type(width) not in (int, float) or type(height) not in (int, float)
                or not 2 <= width <= limits['max_width'] or not 2 <= height <= limits['max_height']):
            continue
        known_size = format_info.get('filesize')
        if known_size is not None and (type(known_size) not in (int, float) or not 0 < known_size <= limits['max_bytes']):
            continue
        eligible.append(format_info)
    if not eligible:
        raise HostedMediaError('no_supported_format', '未找到限额内的非 DRM HTTPS MP4 单流；分片、登录或额外验证未启用。', 'unavailable')
    return max(eligible, key=lambda f: (f['height'], f['width'], f.get('acodec') not in (None, 'none')))


def _inspect_media(path, limits):
    import imageio_ffmpeg
    from studio_media import response_format
    with path.open('rb') as stream:
        kind, suffix = response_format('', '', stream.read(1024), 'video')
    if kind != 'video' or suffix != '.mp4':
        raise HostedMediaError('invalid_media', '下载结果不是 MP4 视频。', 'failed')
    reader = imageio_ffmpeg.read_frames(str(path), pix_fmt='rgb24')
    try:
        meta = next(reader)
        next(reader)  # Require an actual decoded frame, not metadata alone.
    finally:
        reader.close()
    width, height = meta['size']; duration = meta.get('duration')
    if not (2 <= width <= limits['max_width'] and 2 <= height <= limits['max_height']):
        raise HostedMediaError('resolution_limit', '实际解码尺寸超过允许范围。')
    if type(duration) not in (int, float) or not math.isfinite(duration) or not 1 <= duration <= limits['max_duration']+.05:
        raise HostedMediaError('duration_limit', '实际视频时长未知或超出允许范围。')
    return {'width': width, 'height': height, 'duration_seconds': duration, 'fps': meta.get('fps')}


def _perform(identity, staged, limits, progress=None):
    transport = _Transport(limits, progress)
    logger = _Logger()
    result = {'status': 'failed', 'normalized_url': identity['url'], 'provider': identity['provider'],
              'video_id': identity['id'], 'metadata': {}, 'version': VERSION}
    try:
        info, library_version = _extract_info(identity, transport, logger)
        selected = _select_format(info, identity, limits)
        result['metadata'] = {'title': str(info.get('title', ''))[:500], 'uploader': str(info.get('uploader', ''))[:240],
                              'duration_seconds': info['duration'], 'format_id': str(selected.get('format_id', ''))[:80],
                              'yt_dlp_version': library_version, 'source_audio_present': selected.get('acodec') not in (None, 'none')}
        with staged.open('xb') as stream:
            _, final, headers, _ = transport.read(selected['url'], headers=selected.get('http_headers'), output=stream)
        mime = next((value for key, value in headers.items() if key.lower() == 'content-type'), '')
        if 'html' in mime.lower() or ('video/' not in mime.lower() and mime.lower().split(';')[0] not in ('', 'application/octet-stream', 'binary/octet-stream')):
            raise HostedMediaError('invalid_media', '媒体请求返回的不是视频内容。', 'failed')
        result['metadata'].update(_inspect_media(staged, limits))
        from studio_revision import file_hash
        result.update(status='downloaded', reason_code='ok', reason='', bytes=staged.stat().st_size,
                      sha256=file_hash(staged), media_host=urlparse(final).hostname)
    except HostedMediaError as exc:
        result.update(status=exc.status, reason_code=exc.code, reason=str(exc))
    except Exception as exc:
        text = re.sub(r'https?://\S+', '[URL]', str(exc))[:1200]
        restricted = any(term in text.lower() for term in ('sign in', 'log in', 'login', 'not a bot', 'captcha',
                                                           'authentication', 'private video', 'age-restricted'))
        if restricted:
            text = text.split('Use --cookies')[0].strip()
            result.update(status='unavailable', reason_code='access_restricted',
                          reason='平台要求登录或人机验证；未导入 Cookie、未重试。' + text)
        else:
            result.update(status='unavailable' if isinstance(exc, (URLError, TimeoutError)) else 'failed',
                          reason_code='extract_or_download_failed', reason=f'{type(exc).__name__}: {text}')
    if result['status'] != 'downloaded':
        staged.unlink(missing_ok=True)
    result.update(received_bytes=transport.received, budget_charge_bytes=transport.received,
                  byte_accounting='measured response body bytes, including metadata and failed reads',
                  warnings=logger.messages, limits=limits,
                  inspection=('downloaded and one frame decoded; not reviewed for content or motion'
                              if result['status'] == 'downloaded' else 'no downloaded video accepted; no content or motion review'),
                  received_bytes_known=True, credentials_used=False)
    return result


def _stop_worker(process):
    if os.name == 'nt':
        try:
            killed = subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=5,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if killed.returncode:
                process.kill()
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=5)


def download_hosted_media(url, target, *, max_bytes=MAX_BYTES, timeout=90, max_duration=MAX_DURATION, max_height=MAX_HEIGHT):
    """Atomically publish one MP4; failed calls leave any existing target intact.

    Always returns a JSON-compatible status record. Caller must charge
    ``budget_charge_bytes`` even on failure. A killed/crashed worker is charged
    its full allowance because its final read count cannot be established.
    ``target`` is the caller-owned .mp4 path; it is never derived from a title.
    """
    base = {'status': 'rejected', 'requested_url': url if isinstance(url, str) else '',
            'received_bytes': 0, 'received_bytes_known': True, 'budget_charge_bytes': 0, 'version': VERSION}
    try:
        identity = normalize_hosted_url(url)
        limits = _limits(max_bytes, timeout, max_duration, max_height)
        target = Path(target).absolute()
        if target.suffix.lower() != '.mp4' or target.is_symlink():
            raise ValueError('托管视频输出须为本地 MP4 文件，不接受符号链接。')
    except (ValueError, TypeError) as exc:
        return {**base, 'reason_code': 'invalid_input', 'reason': str(exc)}
    base.update(normalized_url=identity['url'], provider=identity['provider'], video_id=identity['id'])
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix='.hosted-', dir=target.parent)).resolve()
    except OSError as exc:
        return {**base, 'status': 'failed', 'reason_code': 'destination_error', 'reason': f'无法创建下载临时目录：{exc}'}
    worker_started = False
    outcome = None
    try:
        from paper_video import save_json, load_json
        from studio_process import console_python
        request = folder/'request.json'; result_path = folder/'result.json'; staged = folder/'video.mp4'
        save_json(request, {'identity': identity, 'limits': limits, 'staged': str(staged)})
        environment = {key: value for key, value in os.environ.items() if key not in ('OPENAI_API_KEY', 'PAPER_VIDEO_API_KEY')}
        environment.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
        process = subprocess.Popen([console_python(), '-X', 'utf8', str(Path(__file__).resolve()),
                                    '--worker', str(request), str(result_path)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   env=environment, start_new_session=os.name != 'nt',
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        worker_started = True
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _stop_worker(process)
            outcome = {**base, 'status': 'timeout', 'reason_code': 'timeout', 'reason': '公开视频任务达到总时间上限，已停止。',
                       'received_bytes_known': False, 'budget_charge_bytes': max_bytes,
                       'byte_accounting': 'conservative full allowance after worker termination'}
            return outcome
        if process.returncode or not result_path.is_file():
            outcome = {**base, 'status': 'failed', 'reason_code': 'worker_failed', 'reason': f'公开视频进程异常退出（{process.returncode}）。',
                       'received_bytes_known': False, 'budget_charge_bytes': max_bytes,
                       'byte_accounting': 'conservative full allowance after worker failure'}
            return outcome
        result = load_json(result_path)
        if not isinstance(result, dict) or type(result.get('budget_charge_bytes')) is not int or not 0 <= result['budget_charge_bytes'] <= max_bytes:
            raise RuntimeError('公开视频进程未返回有效传输计量。')
        if result.get('status') == 'downloaded':
            from studio_revision import file_hash
            if (not staged.is_file() or staged.is_symlink() or staged.stat().st_size != result.get('bytes')
                    or file_hash(staged) != result.get('sha256')):
                raise RuntimeError('下载文件与元数据不一致。')
            staged.replace(target)
            result['file'] = str(target)
        outcome = {**base, **result}
        return outcome
    except Exception as exc:
        outcome = {**base, 'status': 'failed', 'reason_code': 'worker_failed', 'reason': f'{type(exc).__name__}: {exc}',
                   'received_bytes_known': not worker_started, 'budget_charge_bytes': max_bytes if worker_started else 0,
                   'byte_accounting': 'conservative full allowance after incomplete worker result' if worker_started else 'worker not started; zero response bytes'}
        return outcome
    finally:
        try:
            if folder.is_relative_to(target.parent.resolve()) and folder.name.startswith('.hosted-'):
                shutil.rmtree(folder)
        except OSError as exc:
            # A Windows file lock must not replace a completed download or its
            # measured failure record with a cleanup exception. The caller can
            # display this explicit leftover without claiming cleanup succeeded.
            if outcome is not None:
                outcome.update(cleanup_warning=f'下载临时目录清理失败（{type(exc).__name__}）。',
                               cleanup_directory=str(folder))


def _worker_main(request_path, result_path):
    from paper_video import load_json, save_json
    data = load_json(Path(request_path))
    result = _perform(data['identity'], Path(data['staged']), data['limits'])
    save_json(Path(result_path), result)


if __name__ == '__main__' and len(sys.argv) == 4 and sys.argv[1] == '--worker':
    _worker_main(sys.argv[2], sys.argv[3])
