"""Chinese progress must not turn successful work into a code-page failure."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import make_video
import paper_video
import studio_local_voice
import studio_server


ROOT = Path(__file__).resolve().parents[1]
MESSAGE = '中文进度：已完成 1/2，路径 café/视频。'


class ConsoleOutputTests(unittest.TestCase):
    def test_library_loggers_handle_cp1252_without_changing_the_selected_encoding(self):
        for logger in (paper_video.log, make_video.log):
            with self.subTest(logger=logger.__module__):
                raw = io.BytesIO()
                stream = io.TextIOWrapper(raw, encoding='cp1252', errors='strict')
                try:
                    with patch.object(sys, 'stdout', stream): logger(MESSAGE)
                    self.assertEqual(stream.encoding, 'cp1252')
                    expected = (MESSAGE + '\n').encode('cp1252', 'backslashreplace')
                    self.assertEqual(raw.getvalue().replace(b'\r\n', b'\n'), expected)
                finally:
                    stream.detach()

    def test_utf8_capture_and_absent_pythonw_console_remain_supported(self):
        for logger in (paper_video.log, make_video.log):
            stream = io.StringIO()
            with patch.object(sys, 'stdout', stream): logger(MESSAGE)
            self.assertEqual(stream.getvalue(), MESSAGE + '\n')
            with patch.object(sys, 'stdout', None): logger(MESSAGE)

    def test_cli_configures_stdout_and_stderr_independently_when_one_is_absent(self):
        from studio_console import configure_console
        for name, absent in (('stdout', 'stderr'), ('stderr', 'stdout')):
            with self.subTest(stream=name):
                raw = io.BytesIO(); stream = io.TextIOWrapper(raw, encoding='cp1252', errors='strict')
                try:
                    with patch.object(sys, name, stream), patch.object(sys, absent, None):
                        configure_console()
                        stream.write(MESSAGE); stream.flush()
                    self.assertEqual(stream.encoding, 'utf-8')
                    self.assertEqual(raw.getvalue().decode('utf-8'), MESSAGE)
                finally:
                    stream.detach()
        with patch.object(sys, 'stdout', None), patch.object(sys, 'stderr', None):
            configure_console()

    def test_real_output_io_errors_are_not_silently_discarded(self):
        class BrokenOutput:
            def write(self, _):
                raise BrokenPipeError('disconnected output')
        with patch.object(sys, 'stdout', BrokenOutput()):
            with self.assertRaisesRegex(BrokenPipeError, 'disconnected output'):
                paper_video.log(MESSAGE)

    def test_imported_loggers_in_real_legacy_codepage_process_do_not_fail(self):
        script = ('import sys,paper_video,make_video\n'
            'assert sys.stdout.encoding == "cp1252"\n'
            f'paper_video.log({MESSAGE!r})\nmake_video.log({MESSAGE!r})\n')
        result = subprocess.run([sys.executable, '-c', script], cwd=ROOT,
            env={**os.environ, 'PYTHONIOENCODING': 'cp1252:strict', 'PYTHONUTF8': '0'},
            capture_output=True, timeout=15,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0, result.stderr.decode('ascii', 'replace'))
        self.assertEqual(result.stdout.replace(b'\r\n', b'\n'),
            ((MESSAGE + '\n') * 2).encode('cp1252', 'backslashreplace'))

    def test_cli_help_uses_utf8_even_when_inherited_pipe_is_cp1252(self):
        for filename in ('make_video.py', 'paper_video.py', 'studio_server.py'):
            with self.subTest(cli=filename):
                result = subprocess.run([sys.executable, str(ROOT / filename), '--help'], cwd=ROOT,
                    env={**os.environ, 'PYTHONIOENCODING': 'cp1252:strict', 'PYTHONUTF8': '0'},
                    capture_output=True, timeout=15,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self.assertEqual(result.returncode, 0, result.stderr.decode('ascii', 'replace'))
                text = result.stdout.decode('utf-8')
                self.assertTrue(any('\u4e00' <= char <= '\u9fff' for char in text))

    def test_browser_fallback_keeps_address_on_legacy_console(self):
        raw = io.BytesIO(); stream = io.TextIOWrapper(raw, encoding='cp1252', errors='strict')
        try:
            with patch.object(sys, 'stdout', stream), patch('studio_server.webbrowser.open', return_value=False):
                self.assertFalse(studio_server.open_workbench('http://127.0.0.1:8780'))
            self.assertIn(b'http://127.0.0.1:8780', raw.getvalue())
        finally:
            stream.detach()

    def test_local_voice_forwarding_preserves_utf8_disk_log_on_legacy_console(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); project = root / 'project.json'; project.write_text('{}')
            cache = root / 'audio_cache/qwen'; cache.mkdir(parents=True)
            (cache / 'manifest.json').write_text(json.dumps({'blocks': [{'id': 'b1'}]}))
            process = SimpleNamespace(stdout=io.StringIO(MESSAGE + '\n'), wait=lambda: 0)
            raw = io.BytesIO(); stream = io.TextIOWrapper(raw, encoding='cp1252', errors='strict')
            try:
                with patch.dict(os.environ, {'VIDEO_WORKSHOP_VOICE_PYTHON': sys.executable}), \
                        patch.object(studio_local_voice.subprocess, 'Popen', return_value=process), \
                        patch.object(sys, 'stdout', stream):
                    self.assertIn('b1', studio_local_voice.prepare(project))
                self.assertEqual((root / 'voice_worker.log').read_text(encoding='utf-8'), MESSAGE + '\n')
                self.assertIn(b'\\u4e2d', raw.getvalue())
            finally:
                stream.detach()


if __name__ == '__main__':
    unittest.main()
