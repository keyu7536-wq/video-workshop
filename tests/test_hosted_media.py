"""Offline hosted-media limits, metered transport and atomic worker publication."""
import copy
import hashlib
from http.client import IncompleteRead
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request

import studio_hosted_media as hm


URL = 'https://www.youtube.com/watch?v=xAXvfVTgqr0'
IDENTITY = hm.normalize_hosted_url(URL)


def info():
    return {'id': IDENTITY['id'], 'title': 'Author demonstration', 'uploader': 'Author',
            'duration': 10, 'live_status': 'not_live', 'availability': 'public',
            'formats': [{'format_id': 'mp4-720', 'url': 'https://cdn.example/video.mp4',
                         'ext': 'mp4', 'protocol': 'https', 'width': 1280, 'height': 720,
                         'vcodec': 'h264', 'acodec': 'aac', 'filesize': 500}]}


class Response(io.BytesIO):
    def __init__(self, data, length=True, url='https://cdn.example/file', mime='application/octet-stream'):
        super().__init__(data)
        self.headers = {'Content-Type': mime}
        if length:
            self.headers['Content-Length'] = str(len(data))
        self.url = url
        self.status = 200
        self.requested_sizes = []

    def read1(self, size):
        self.requested_sizes.append(size)
        return self.read(size)

    def geturl(self):
        return self.url


class HostedURLTests(unittest.TestCase):
    def test_youtube_embed_short_and_watch_normalize_to_one_identity(self):
        for url in (URL, 'https://youtu.be/xAXvfVTgqr0?t=3',
                    'https://www.youtube-nocookie.com/embed/xAXvfVTgqr0?rel=0',
                    'https://m.youtube.com/shorts/xAXvfVTgqr0'):
            with self.subTest(url=url):
                self.assertEqual(hm.normalize_hosted_url(url), IDENTITY)

    def test_vimeo_numeric_and_explicit_public_unlisted_embed(self):
        self.assertEqual(hm.normalize_hosted_url('https://player.vimeo.com/video/123456')['url'], 'https://vimeo.com/123456')
        result = hm.normalize_hosted_url('https://player.vimeo.com/video/123456?h=abcdef0123&autoplay=1')
        self.assertEqual(result, {'provider': 'vimeo', 'id': '123456', 'url': 'https://player.vimeo.com/video/123456?h=abcdef0123'})

    def test_playlist_live_channels_credentials_and_lookalike_hosts_are_rejected(self):
        for url in (URL+'&list=123', 'https://youtube.com/playlist?list=123',
                    'https://youtube.com/live/xAXvfVTgqr0', 'https://youtube.com/@channel',
                    'https://youtube.com.evil.example/watch?v=xAXvfVTgqr0',
                    'https://user:pass@youtube.com/watch?v=xAXvfVTgqr0',
                    'https://127.0.0.1/watch?v=xAXvfVTgqr0', 'file:///video.mp4',
                    'https://youtube.com:8443/watch?v=xAXvfVTgqr0', URL+'&v=abcdefghijk'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                hm.normalize_hosted_url(url)


class HostedMetadataTests(unittest.TestCase):
    def test_selects_bounded_direct_video_and_allows_video_without_audio(self):
        data = info(); data['age_limit'] = None
        oversized = copy.deepcopy(data['formats'][0]); oversized.update(width=3840, height=2160)
        data['formats'].append(oversized)
        data['formats'][0]['acodec'] = 'none'
        selected = hm._select_format(data, IDENTITY, hm._limits(1000, 30, 900, 720))
        self.assertEqual(selected['height'], 720); self.assertEqual(selected['acodec'], 'none')

    def test_rejects_live_drm_playlist_restricted_identity_and_unknown_duration(self):
        for changes, code in (({'is_live': True}, 'live_video'), ({'was_live': True}, 'live_video'),
                              ({'live_status': 'is_upcoming'}, 'live_video'), ({'has_drm': True}, 'drm'),
                              ({'_type': 'playlist', 'entries': []}, 'not_single_video'),
                              ({'availability': 'needs_auth'}, 'access_restricted'),
                              ({'age_limit': 18}, 'access_restricted'), ({'id': 'different'}, 'identity_mismatch'),
                              ({'duration': None}, 'duration_limit'), ({'duration': 901}, 'duration_limit')):
            with self.subTest(changes=changes), self.assertRaises(hm.HostedMediaError) as caught:
                hm._select_format(dict(info(), **changes), IDENTITY, hm._limits(1000, 30, 900, 720))
            self.assertEqual(caught.exception.code, code)

    def test_fragments_manifests_unsafe_protocol_drm_and_over_budget_formats_are_not_used(self):
        for changes in ({'protocol': 'm3u8_native'}, {'fragments': [{'url': 'x'}]},
                        {'manifest_url': 'https://cdn.example/master.mpd'}, {'has_drm': True},
                        {'url': 'file:///private.mp4'}, {'filesize': 1001}, {'height': 721},
                        {'width': 1281}, {'vcodec': 'none'}, {'ext': 'webm'}):
            data = info(); data['formats'][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(hm.HostedMediaError) as caught:
                hm._select_format(data, IDENTITY, hm._limits(1000, 30, 900, 720))
            self.assertEqual(caught.exception.code, 'no_supported_format')

    def test_invalid_limits_reject_before_starting_worker(self):
        for changes in ({'max_bytes': 0}, {'max_bytes': hm.MAX_BYTES+1}, {'timeout': 181},
                        {'timeout': float('nan')}, {'max_duration': 901}, {'max_height': 1080}):
            with self.subTest(changes=changes), patch('subprocess.Popen') as start:
                result = hm.download_hosted_media(URL, 'unused.mp4', **changes)
                self.assertEqual(result['status'], 'rejected'); start.assert_not_called()


class HostedTransportTests(unittest.TestCase):
    def transport(self, cap=1000):
        result = hm._Transport(hm._limits(cap, 30, 900, 720))
        result.opener = Mock()
        return result

    def test_metadata_and_media_share_one_measured_budget_and_credentials_are_stripped(self):
        transport = self.transport()
        transport.opener.open.side_effect = [Response(b'metadata'), Response(b'media')]
        with patch('studio_hosted_media._public') as public:
            transport.read('https://example.com/info', headers={'Cookie': 'private', 'Authorization': 'secret'})
            output = io.BytesIO(); transport.read('https://example.com/media', output=output)
        self.assertEqual(transport.received, 13); self.assertEqual(transport.metadata_bytes, 8)
        self.assertEqual(output.getvalue(), b'media')
        self.assertEqual(public.call_count, 4)
        request = transport.opener.open.call_args_list[0].args[0]
        self.assertIsNone(request.get_header('Cookie')); self.assertIsNone(request.get_header('Authorization'))
        self.assertEqual(request.get_header('Accept-encoding'), 'identity')

    def test_known_oversize_and_http_denial_charge_measured_zero_without_retry(self):
        transport = self.transport(10)
        transport.opener.open.return_value = Response(b'a'*11)
        with patch('studio_hosted_media._public'), self.assertRaises(hm.HostedMediaError):
            transport.read('https://example.com/file')
        self.assertEqual(transport.received, 0)
        transport.opener.open.side_effect = HTTPError(URL, 403, 'Forbidden', {}, io.BytesIO())
        with patch('studio_hosted_media._public'), self.assertRaises(hm.HostedMediaError) as caught:
            transport.read(URL)
        self.assertEqual(caught.exception.code, 'access_restricted'); self.assertEqual(transport.received, 0)
        self.assertEqual(transport.opener.open.call_count, 2)

    def test_unknown_length_stops_without_reading_past_remaining_budget(self):
        transport = self.transport(10); response = Response(b'a'*20, length=False)
        transport.opener.open.return_value = response
        with patch('studio_hosted_media._public'), self.assertRaises(hm.HostedMediaError) as caught:
            transport.read(URL)
        self.assertEqual(caught.exception.code, 'byte_limit'); self.assertEqual(transport.received, 10)
        self.assertEqual(response.requested_sizes, [10])

    def test_partial_failed_read_bytes_are_not_lost(self):
        transport = self.transport()
        response = Response(b'placeholder'); response.read1 = Mock(side_effect=IncompleteRead(b'partial', 8))
        transport.opener.open.return_value = response
        with patch('studio_hosted_media._public'), self.assertRaises(IncompleteRead):
            transport.read(URL)
        self.assertEqual(transport.received, 7)

    def test_private_resolved_address_is_rejected_before_open(self):
        transport = self.transport()
        with patch('studio_research.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]):
            with self.assertRaises(hm.HostedMediaError) as caught:
                transport.read('https://cdn.example/video.mp4')
        self.assertEqual(caught.exception.code, 'unsafe_url'); transport.opener.open.assert_not_called()

    def test_redirect_target_is_checked_and_never_downgraded_to_http(self):
        with self.assertRaises(hm.HostedMediaError):
            hm._Redirect().redirect_request(Request(URL), None, 302, 'Found', {}, 'http://example.com/video')
        with patch('studio_hosted_media._public', side_effect=hm.HostedMediaError('unsafe_url', 'private')) as check:
            with self.assertRaises(hm.HostedMediaError):
                hm._Redirect().redirect_request(Request(URL), None, 302, 'Found', {}, 'https://127.0.0.1/video')
            check.assert_called_once_with('https://127.0.0.1/video')

    def test_redirect_body_is_not_drained_and_hops_are_bounded(self):
        before = Mock(); redirect = hm._Redirect(before)
        redirect.parent = Mock()
        request = Request(URL); request.timeout = 5
        body = Mock()
        with patch('studio_hosted_media._public'):
            redirect.http_error_302(request, body, 302, 'Found', {'location': 'https://example.com/next'})
        body.read.assert_not_called(); body.close.assert_called_once(); before.assert_called_once()
        followed = redirect.parent.open.call_args.args[0]
        self.assertEqual(followed._hosted_redirect_chain, [URL, 'https://example.com/next'])
        followed._hosted_redirect_chain = [URL, 'https://example.com/a', 'https://example.com/b', 'https://example.com/next']
        with self.assertRaises(hm.HostedMediaError) as caught:
            redirect.http_error_302(followed, Mock(), 302, 'Found', {'location': 'https://example.com/fourth'})
        self.assertEqual(caught.exception.code, 'redirect_limit')

    def test_exceeded_request_deadline_stops_before_opening_another_connection(self):
        transport = self.transport(); transport.deadline = 0
        with patch('studio_hosted_media._public'), self.assertRaises(hm.HostedMediaError) as caught:
            transport.read(URL)
        self.assertEqual(caught.exception.code, 'timeout'); transport.opener.open.assert_not_called()


class HostedWorkerTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec('yt_dlp'), 'Optional yt-dlp is not installed')
    def test_installed_library_embedding_disables_plugins_credentials_and_remote_runtime(self):
        import importlib
        import yt_dlp
        from yt_dlp import globals as globals_
        module = importlib.import_module('yt_dlp.YoutubeDL')
        old_dirs, old_loaded = globals_.plugin_dirs.value, globals_.all_plugins_loaded.value
        captured = {}
        def extract(downloader, url, *, download, process):
            captured.update(downloader.params)
            self.assertFalse(download); self.assertFalse(process); self.assertEqual(url, URL)
            return info()
        try:
            with patch.object(module, 'load_all_plugins', side_effect=AssertionError('must not load user plugins')), \
                 patch.object(yt_dlp.YoutubeDL, 'extract_info', autospec=True, side_effect=extract):
                result, version = hm._extract_info(IDENTITY, hm._Transport(hm._limits(1000, 30, 900, 720)), hm._Logger())
            self.assertEqual(result['id'], IDENTITY['id']); self.assertTrue(version)
            self.assertEqual(captured['js_runtimes'], {}); self.assertEqual(captured['remote_components'], set())
            for key in ('cookiefile', 'cookiesfrombrowser', 'username', 'password', 'videopassword'):
                self.assertIsNone(captured[key])
            for key in ('usenetrc', 'geo_bypass', 'nocheckcertificate', 'allow_unplayable_formats', 'enable_file_urls'):
                self.assertFalse(captured[key])
            self.assertEqual(captured['extractor_retries'], 0)
        finally:
            globals_.plugin_dirs.value, globals_.all_plugins_loaded.value = old_dirs, old_loaded

    def test_real_mp4_is_decoded_and_actual_resolution_overrides_claimed_metadata(self):
        import make_video
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory); source = folder/'source.mp4'
            make_video.run([make_video.ffmpeg_path(), '-v', 'error', '-y', '-f', 'lavfi', '-i',
                            'color=c=blue:size=320x180:rate=10', '-t', '2', '-an', '-c:v', 'libx264',
                            '-threads', '1', '-pix_fmt', 'yuv420p', str(source)], timeout=30)
            payload = source.read_bytes(); data = info(); data['duration'] = 2
            data['formats'][0].update(filesize=len(payload), width=128, height=72, acodec='none')
            def read(transport, url, **kwargs):
                kwargs['output'].write(payload); transport.charge(len(payload), False)
                return b'', url, {'Content-Type': 'video/mp4'}, 200
            with patch('studio_hosted_media._extract_info', return_value=(data, 'fixture')), patch.object(hm._Transport, 'read', read):
                good = hm._perform(IDENTITY, folder/'good.mp4', hm._limits(1024**2, 30, 900, 360))
                rejected = hm._perform(IDENTITY, folder/'bad.mp4', hm._limits(1024**2, 30, 900, 144))
            self.assertEqual(good['status'], 'downloaded'); self.assertEqual(good['metadata']['width'], 320)
            self.assertEqual(good['metadata']['duration_seconds'], 2)
            self.assertFalse(good['metadata']['source_audio_present'])
            self.assertEqual(rejected['reason_code'], 'resolution_limit')
            self.assertFalse((folder/'bad.mp4').exists())
            self.assertEqual(rejected['budget_charge_bytes'], len(payload))

    def test_success_includes_metadata_charge_hash_and_actual_decode_dimensions(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'video.mp4'; payload = b'0000ftyp' + b'valid fixture data'
            def extract(identity, transport, logger):
                transport.charge(9, True)
                return info(), 'fixture'
            def read(transport, url, **kwargs):
                transport.charge(len(payload), False); kwargs['output'].write(payload)
                return b'', url, {'Content-Type': 'video/mp4'}, 200
            with patch('studio_hosted_media._extract_info', side_effect=extract), \
                 patch.object(hm._Transport, 'read', read), \
                 patch('studio_hosted_media._inspect_media', return_value={'width': 1280, 'height': 720, 'duration_seconds': 10}):
                result = hm._perform(IDENTITY, target, hm._limits(1000, 30, 900, 720))
            self.assertEqual(result['status'], 'downloaded'); self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(result['budget_charge_bytes'], 9+len(payload))
            self.assertEqual(result['sha256'], hashlib.sha256(payload).hexdigest())
            self.assertEqual(result['metadata']['format_id'], 'mp4-720')
            self.assertFalse(result['credentials_used'])

    def test_metadata_failure_returns_consumed_bytes_and_no_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'video.mp4'
            def extract(identity, transport, logger):
                transport.charge(123, True)
                raise hm.HostedMediaError('access_restricted', 'login needed', 'unavailable')
            with patch('studio_hosted_media._extract_info', side_effect=extract):
                result = hm._perform(IDENTITY, target, hm._limits(1000, 30, 900, 720))
            self.assertEqual(result['budget_charge_bytes'], 123); self.assertFalse(target.exists())
            self.assertEqual(result['status'], 'unavailable'); self.assertEqual(result['reason'], 'login needed')
            self.assertEqual(result['inspection'], 'no downloaded video accepted; no content or motion review')

    def test_real_observed_bot_challenge_is_unavailable_and_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'video.mp4'
            def extract(identity, transport, logger):
                transport.charge(1322522, True)
                raise RuntimeError('ERROR: Sign in to confirm you are not a bot. Use --cookies-from-browser for authentication.')
            with patch('studio_hosted_media._extract_info', side_effect=extract) as extraction:
                result = hm._perform(IDENTITY, target, hm._limits(2*1024**2, 30, 900, 720))
            extraction.assert_called_once()
            self.assertEqual(result['status'], 'unavailable'); self.assertEqual(result['reason_code'], 'access_restricted')
            self.assertNotIn('--cookies-from-browser', result['reason'])
            self.assertEqual(result['budget_charge_bytes'], 1322522)
            self.assertFalse(target.exists())

    def test_download_failure_cleans_partial_file_and_keeps_consumed_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'video.mp4'
            def read(transport, url, **kwargs):
                kwargs['output'].write(b'partial'); transport.charge(7, False)
                raise OSError('connection ended')
            with patch('studio_hosted_media._extract_info', return_value=(info(), 'fixture')), patch.object(hm._Transport, 'read', read):
                result = hm._perform(IDENTITY, target, hm._limits(1000, 30, 900, 720))
            self.assertEqual(result['status'], 'failed'); self.assertEqual(result['budget_charge_bytes'], 7)
            self.assertFalse(target.exists())

    def test_html_error_body_cannot_be_published_as_mp4(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'video.mp4'
            def read(transport, url, **kwargs):
                kwargs['output'].write(b'<html>denied</html>'); transport.charge(19, False)
                return b'', url, {'Content-Type': 'text/html'}, 200
            with patch('studio_hosted_media._extract_info', return_value=(info(), 'fixture')), patch.object(hm._Transport, 'read', read):
                result = hm._perform(IDENTITY, target, hm._limits(1000, 30, 900, 720))
            self.assertEqual(result['reason_code'], 'invalid_media'); self.assertFalse(target.exists())

    def test_missing_dependency_is_visible_without_network_or_fake_media(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('sys.modules', {'yt_dlp': None}):
            result = hm._perform(IDENTITY, Path(directory)/'video.mp4', hm._limits(1000, 30, 900, 720))
        self.assertEqual(result['reason_code'], 'dependency_missing')
        self.assertEqual(result['status'], 'unavailable'); self.assertEqual(result['budget_charge_bytes'], 0)

    def test_atomic_success_and_failed_attempt_leave_no_staging_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'final.mp4'; target.write_bytes(b'old')
            def process(command, **kwargs):
                request = json.loads(Path(command[-2]).read_text(encoding='utf-8'))
                staged = Path(request['staged']); staged.write_bytes(b'new video')
                result = {'status': 'downloaded', 'bytes': 9, 'sha256': hashlib.sha256(b'new video').hexdigest(),
                          'received_bytes': 15, 'budget_charge_bytes': 15}
                Path(command[-1]).write_text(json.dumps(result), encoding='utf-8')
                return SimpleNamespace(wait=lambda timeout: None, returncode=0)
            with patch('subprocess.Popen', side_effect=process):
                result = hm.download_hosted_media(URL, target, max_bytes=1000)
            self.assertEqual(result['status'], 'downloaded'); self.assertEqual(target.read_bytes(), b'new video')
            self.assertEqual(list(Path(directory).glob('.hosted-*')), [])
            with patch('subprocess.Popen', side_effect=OSError('start failed')):
                failed = hm.download_hosted_media(URL, target, max_bytes=1000)
            self.assertEqual(failed['status'], 'failed'); self.assertEqual(target.read_bytes(), b'new video')
            self.assertEqual(failed['budget_charge_bytes'], 0)
            self.assertEqual(list(Path(directory).glob('.hosted-*')), [])

    def test_timed_out_worker_is_stopped_and_budget_conservatively_charged(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'final.mp4'; target.write_bytes(b'old')
            worker = Mock(); worker.wait.side_effect = subprocess.TimeoutExpired('worker', 1)
            with patch('subprocess.Popen', return_value=worker), patch('studio_hosted_media._stop_worker') as stop:
                result = hm.download_hosted_media(URL, target, max_bytes=1000, timeout=1)
            stop.assert_called_once_with(worker)
            self.assertEqual(result['status'], 'timeout'); self.assertEqual(result['budget_charge_bytes'], 1000)
            self.assertFalse(result['received_bytes_known'])
            self.assertEqual(target.read_bytes(), b'old'); self.assertEqual(list(Path(directory).glob('.hosted-*')), [])

    def test_locked_cleanup_preserves_published_video_and_measured_success(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'final.mp4'; target.write_bytes(b'old')
            def process(command, **kwargs):
                request = json.loads(Path(command[-2]).read_text(encoding='utf-8'))
                Path(request['staged']).write_bytes(b'new video')
                Path(command[-1]).write_text(json.dumps({
                    'status': 'downloaded', 'bytes': 9, 'sha256': hashlib.sha256(b'new video').hexdigest(),
                    'received_bytes': 15, 'budget_charge_bytes': 15}), encoding='utf-8')
                return SimpleNamespace(wait=lambda timeout: None, returncode=0)
            with patch('subprocess.Popen', side_effect=process), \
                 patch('studio_hosted_media.shutil.rmtree', side_effect=PermissionError('locked')):
                result = hm.download_hosted_media(URL, target, max_bytes=1000)
            self.assertEqual(result['status'], 'downloaded')
            self.assertEqual(result['budget_charge_bytes'], 15)
            self.assertEqual(target.read_bytes(), b'new video')
            self.assertIn('PermissionError', result['cleanup_warning'])
            self.assertTrue(Path(result['cleanup_directory']).is_relative_to(Path(directory)))
            self.assertTrue(Path(result['cleanup_directory']).is_dir())

    def test_locked_cleanup_preserves_original_start_failure_and_zero_charge(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'final.mp4'; target.write_bytes(b'old')
            with patch('subprocess.Popen', side_effect=OSError('start failed')), \
                 patch('studio_hosted_media.shutil.rmtree', side_effect=PermissionError('locked')):
                result = hm.download_hosted_media(URL, target, max_bytes=1000)
            self.assertEqual(result['status'], 'failed')
            self.assertIn('start failed', result['reason'])
            self.assertEqual(result['budget_charge_bytes'], 0)
            self.assertEqual(target.read_bytes(), b'old')
            self.assertIn('PermissionError', result['cleanup_warning'])


if __name__ == '__main__':
    unittest.main()
