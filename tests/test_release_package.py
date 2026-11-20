import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('release_builder',Path(__file__).resolve().parents[1]/'scripts/build_release.py')
release=importlib.util.module_from_spec(spec);spec.loader.exec_module(release)

class ReleaseTests(unittest.TestCase):
    def fixture(self,root):
        for name in release.REQUIRED:
            path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('public source\n',encoding='utf-8')

    def test_allowlist_omits_runtime_files_and_manifest_verifies_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'repo';root.mkdir();self.fixture(root)
            for name in ('jobs/private.mp4','assets/author-paper.pdf','tmp/credentials.json','audio_cache/private.wav','output/video.mp4','.venv/site.py','ui_settings.json','voice-overrides.txt','project.json','docs/private-review.md','studio/leftover-audition.html'):
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('private fixture',encoding='utf-8')
            result=release.build(root,Path(folder)/'dist','0.1.0')
            with zipfile.ZipFile(result['archive']) as archive:
                names=archive.namelist();self.assertEqual(len(names),len(release.REQUIRED)+1)
                self.assertTrue(all(n.startswith('video-workshop/') for n in names))
                self.assertFalse(any('private' in n or 'ui_settings' in n or 'audio_cache' in n for n in names))
                manifest=json.loads(archive.read('video-workshop/RELEASE_MANIFEST.json'))
                for item in manifest['files']:
                    target=Path(result['folder'])/item['path']
                    self.assertEqual(release.sha256(target),item['sha256'])
                    self.assertEqual(len(archive.read('video-workshop/'+item['path'])),item['size'])
            self.assertEqual(release.sha256(Path(result['archive'])),result['sha256'])

    def test_sensitive_content_blocks_without_echoing_value(self):
        values=['sk-'+'a'*32,'C:'+ '/Users/'+'private-person/project', '2026'+'0101_000000_studio_abcdef']
        for sensitive in values:
            with self.subTest(kind=sensitive[:3]),tempfile.TemporaryDirectory() as folder:
                root=Path(folder)/'repo';root.mkdir();self.fixture(root)
                (root/'README.md').write_text('fixture '+sensitive,encoding='utf-8')
                with self.assertRaises(ValueError) as caught:release.build(root,Path(folder)/'dist','0.1.0')
                self.assertIn('README.md',str(caught.exception))
                self.assertNotIn(sensitive,str(caught.exception))
                self.assertFalse((Path(folder)/'dist/video-workshop-0.1.0.zip').exists())

    def test_existing_deliverable_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'repo';root.mkdir();self.fixture(root)
            destination=Path(folder)/'dist';result=release.build(root,destination,'0.1.0')
            before=Path(result['archive']).read_bytes()
            with self.assertRaises(ValueError):release.build(root,destination,'0.1.0')
            self.assertEqual(Path(result['archive']).read_bytes(),before)

    def test_missing_required_file_fails_before_staging(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'repo';root.mkdir();self.fixture(root)
            (root/'studio_server.py').unlink()
            with self.assertRaisesRegex(ValueError,'studio_server.py'):release.build(root,Path(folder)/'dist','0.1.0')

    def test_same_source_produces_same_archive_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'repo';root.mkdir();self.fixture(root)
            first=release.build(root,Path(folder)/'first','0.1.0')
            second=release.build(root,Path(folder)/'second','0.1.0')
            self.assertEqual(first['sha256'],second['sha256'])

    def test_cli_default_version_comes_from_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'repo';root.mkdir();self.fixture(root)
            (root/'app_config.py').write_text("VERSION = '0.6.0'\n",encoding='utf-8')
            with patch.object(release,'ROOT',root),redirect_stdout(io.StringIO()):
                release.main(['--output',str(Path(folder)/'dist')])
            archive=Path(folder)/'dist/video-workshop-0.6.0.zip'
            with zipfile.ZipFile(archive) as bundle:
                manifest=json.loads(bundle.read('video-workshop/RELEASE_MANIFEST.json'))
                self.assertEqual(manifest['version'],'0.6.0')

    def test_cli_root_selects_that_sources_version(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'app_config.py').write_text("VERSION: str = '2.3.4-rc.1'\n",encoding='utf-8')
            with patch.object(release,'build',return_value={}) as build,redirect_stdout(io.StringIO()):
                release.main(['--root',str(root),'--output',str(root/'dist')])
            self.assertEqual(build.call_args.args,(root.resolve(),root/'dist','2.3.4-rc.1'))

    def test_cli_explicit_version_does_not_read_source_version(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with patch.object(release,'build',return_value={}) as build,redirect_stdout(io.StringIO()):
                release.main(['--root',str(root),'--version','3.2.1'])
            self.assertEqual(build.call_args.args[2],'3.2.1')

    def test_static_version_read_never_executes_module(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);marker=root/'executed'
            (root/'app_config.py').write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\nVERSION = '1.2.3'\n",encoding='utf-8')
            self.assertEqual(release.source_version(root),'1.2.3')
            self.assertFalse(marker.exists())

    def test_missing_invalid_or_dynamic_version_is_rejected_without_execution(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);marker=root/'executed'
            with self.assertRaisesRegex(ValueError,'app_config.py'):release.source_version(root)
            for source in ("NAME = 'fixture'", "VERSION = 'not-a-version'", 'VERSION = 123',
                           "VERSION = '1.2.3'\nVERSION = '2.3.4'", "VERSION = (",
                           f"VERSION = __import__('pathlib').Path({str(marker)!r}).touch() or '1.2.3'"):
                with self.subTest(source=source[:30]):
                    (root/'app_config.py').write_text(source,encoding='utf-8')
                    with self.assertRaisesRegex(ValueError,'app_config.py.*VERSION'):release.source_version(root)
                    self.assertFalse(marker.exists())

    def test_check_only_does_not_require_a_version(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);self.fixture(root)
            with redirect_stdout(io.StringIO()),self.assertRaises(SystemExit) as caught:
                release.main(['--root',str(root),'--check'])
            self.assertEqual(caught.exception.code,0)

if __name__=='__main__':unittest.main()
