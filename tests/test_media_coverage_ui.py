"""Execute the production research panel in a real, isolated browser document."""
import contextlib
import importlib.util
from pathlib import Path
import unittest

from studio_media_catalog import coverage_summary


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'Playwright is not installed')
class MediaCoverageUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.resources = contextlib.ExitStack()
        cls.addClassCleanup(cls.resources.close)
        runtime = sync_playwright().start()
        cls.resources.callback(runtime.stop)
        executable = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
        cls.browser = runtime.chromium.launch(headless=True,
            **({'executable_path': str(executable)} if executable.is_file() else {}))
        cls.resources.callback(cls.browser.close)
        app = (Path(__file__).resolve().parents[1]/'studio/app.js').read_text(encoding='utf-8')
        # Test the real renderer and escaping helper, without unrelated server polling.
        helper = next(line for line in app.splitlines() if line.startswith('const escapeHTML ='))
        start = app.index('function renderResearch(){')
        end = app.index('function renderFinalReview(){', start)
        cls.script = helper + '\n' + app[start:end]

    def setUp(self):
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        self.page.set_content('<main id="researchPanel"></main>')
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.evaluate('''() => {
            window.state = {job: null};
            window.$ = id => document.getElementById(id);
            window.executed = false;
        }''')
        self.page.add_script_tag(content=self.script)

    def render(self, job):
        self.page.evaluate('job => {state.job = job; renderResearch();}', job)
        self.assertEqual(self.errors, [])
        return self.page.locator('#researchPanel').text_content()

    @staticmethod
    def job(records, sources=None):
        return {'id': 'test-job',
                'research_manifest': {'media_coverage': coverage_summary(records, sources or [])},
                'media_inventory': {'records': records}}

    def test_fetch_status_is_visible_without_deep_read_dossier(self):
        records = [
            {'url': 'https://example.org/demo.mp4', 'status': 'downloaded_and_sampled'},
            {'url': 'https://example.org/copy.mp4', 'status': 'duplicate'},
            {'url': 'https://example.org/private', 'status': 'unavailable', 'reason': '需要登录'},
            {'url': 'https://example.org/extra', 'status': 'not_reviewed', 'reason': '预算已用完'},
        ]
        text = self.render(self.job(records, [{'id': 'v', 'type': 'video', 'content_sha256': 'a'*64}]))
        self.assertIn('素材获取', text)
        self.assertIn('发现 4 个链接', text)
        self.assertIn('新获取 1 项', text)
        self.assertIn('合并重复 1 项', text)
        self.assertIn('获取失败 1 项', text)
        self.assertIn('未尝试 1 项', text)
        self.assertIn('可用视频 1 份', text)
        self.assertIn('需要登录', text)
        self.assertNotIn('研究笔记', text)
        self.assertEqual(self.page.locator('#researchPanel a').get_attribute('href'),
                         '/files/test-job/analysis/research/media_inventory.json')

    def test_old_manifest_does_not_claim_verified_deduplication(self):
        text = self.render({'id': 'old', 'research_manifest': {'records': [], 'gaps': []}})
        self.assertNotIn('按文件内容去重', text)
        self.assertNotIn('可用视频 0', text)
        self.assertNotIn('已获取并抽帧', text)

    def test_missing_hash_is_explicitly_unknown_instead_of_zero(self):
        sources = [{'id': 'v1', 'type': 'video', 'content_sha256': 'a'*64},
                   {'id': 'v2', 'type': 'video'}]
        text = self.render(self.job([], sources))
        self.assertIn('未核对素材是否重复', text)
        self.assertNotIn('按文件内容去重', text)
        self.assertNotIn('可用视频 0', text)
        self.assertNotIn('可用视频 1', text)

    def test_empty_new_inventory_can_report_verified_zero(self):
        text = self.render(self.job([]))
        self.assertIn('发现 0 个链接', text)
        self.assertIn('可用视频 0 份', text)

    def test_all_external_text_and_job_identifier_are_escaped(self):
        attack = '<img src=x onerror="window.executed=true"><script>window.executed=true</script>'
        records = [{'status': attack, 'label': attack, 'reason': attack},
                   {'status': 'failed', 'url': attack, 'reason': attack}]
        job = self.job(records)
        job['id'] = 'x/"?><svg onload="window.executed=true">'
        job['research_manifest']['gaps'] = [attack]
        job['research'] = {
            'title': attack, 'project_identity': attack, 'story_angle': attack,
            'claims': [{'topic': attack, 'explanation': attack, 'quote': attack}],
            'missing_information': [attack],
        }
        text = self.render(job)
        self.assertGreaterEqual(text.count(attack), 10)
        self.assertEqual(self.page.locator('#researchPanel img, #researchPanel script, #researchPanel svg').count(), 0)
        self.assertFalse(self.page.evaluate('window.executed'))
        for link in self.page.locator('#researchPanel a').all():
            self.assertTrue(link.get_attribute('href').startswith('/files/x%2F%22%3F%3E%3Csvg'))
            self.assertIsNone(link.get_attribute('onload'))

    def test_changing_to_job_without_research_clears_previous_results(self):
        self.render(self.job([{'url': 'https://example.org/demo.mp4', 'status': 'failed'}]))
        self.assertIn('素材获取', self.page.locator('#researchPanel').text_content())
        self.assertEqual(self.render({'id': 'plain'}), '')
        self.assertEqual(self.render(None), '')


if __name__ == '__main__':
    unittest.main()
