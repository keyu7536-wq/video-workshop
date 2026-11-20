"""Local-only editor checks with isolated jobs, real HTTP and source pixels."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageDraw

import paper_video as pv
import studio_engine as engine
import studio_server as server


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'Playwright is not installed')
class DirectorUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.resources = contextlib.ExitStack()
        cls.addClassCleanup(cls.resources.close)
        cls.root = Path(cls.resources.enter_context(tempfile.TemporaryDirectory())).resolve()
        (cls.root / 'jobs').mkdir()
        (cls.root / 'uploads').mkdir()
        cls.resources.enter_context(patch.object(server, 'ROOT', cls.root))
        cls.resources.enter_context(patch.object(engine, 'ROOT', cls.root))
        cls.resources.enter_context(patch.object(server, 'INBOX', cls.root / 'uploads'))
        cls.http = server.StudioHTTPServer(('127.0.0.1', 0), server.Handler)
        cls.http.manager = server.Manager()
        cls.http.manager.start = Mock()
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.resources.callback(cls.thread.join)
        cls.resources.callback(cls.http.server_close)
        cls.resources.callback(cls.http.shutdown)
        cls.base = f'http://127.0.0.1:{cls.http.server_port}'
        cls.playwright = sync_playwright().start()
        cls.resources.callback(cls.playwright.stop)
        executable = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True,
                **({'executable_path': str(executable)} if executable.is_file() else {}))
        except Exception as exc:
            raise unittest.SkipTest(f'Headless browser unavailable: {exc}')
        cls.resources.callback(cls.browser.close)

    def setUp(self):
        self.http.manager.start.reset_mock()
        self.job = self.root / 'jobs' / self._testMethodName
        for folder in ('assets', 'analysis', 'output'):
            (self.job / folder).mkdir(parents=True)
        image = Image.new('RGB', (1000, 600), '#e8e8e8')
        draw = ImageDraw.Draw(image)
        draw.rectangle((100, 60, 499, 539), fill='#e63737')
        draw.rectangle((500, 60, 899, 539), fill='#285fe8')
        draw.rectangle((0, 0, 999, 599), outline='black', width=4)
        self.source = self.job / 'assets/chart.png'
        image.save(self.source)
        self.other_source = self.job / 'assets/other.png'
        Image.new('RGB', (800, 600), '#ffc13b').save(self.other_source)
        self.sources = [
            {'id': 'chart', 'type': 'image', 'name': '两组示意图', 'file': str(self.source)},
            {'id': 'other', 'type': 'image', 'name': '另一张示意图', 'file': str(self.other_source)},
        ]
        self.plan = {'title': '局部说明测试', 'summary': '编辑器测试', 'source_issues': [], 'unused_sources': [],
            'blocks': [{'id': 'b1', 'chapter': '两组对比',
                'narration': '先看全图。红色区域位于左侧。再看蓝色区域。最后比较两侧。',
                'evidence': [], 'shots': [{'kind': 'image', 'source_id': 'chart', 'in': 0,
                    'duration': 4, 'bbox': [.1, .1, .9, .9], 'focus': [], 'keep_full': False,
                    'label': '两组对比', 'card_text': '', 'cue': ''}]}]}
        self.request = {'backend': 'codex', 'content_type': 'general', 'format': 'custom',
            'width': 640, 'height': 360, 'layout_style': 'immersive', 'media_fit': 'contain',
            'brief': '保留文稿，查看两组对应位置。', 'script': self.plan['blocks'][0]['narration'],
            'seconds': 90, 'auto_repair': False, 'auto_render': False}
        pv.save_json(self.job / 'request.json', self.request)
        pv.save_json(self.job / 'job.json', {'engine': 'studio', 'status': 'prepared'})
        pv.save_json(self.job / 'analysis/sources.json', self.sources)
        self.write_plan()
        self.page = self.browser.new_page(viewport={'width': 1440, 'height': 1000})
        self.page.set_default_timeout(8000)
        self.addCleanup(self.page.close)
        self.errors = []
        self.posts = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.on('request', lambda request: self.posts.append(request)
                     if request.method == 'POST' else None)

    def tearDown(self):
        self.http.manager.start.assert_not_called()
        self.assertEqual(self.errors, [])

    def write_plan(self):
        pv.save_json(self.job / 'analysis/studio_plan.json', self.plan)

    def open_job(self):
        self.page.goto(self.base + '/?job=' + self.job.name, wait_until='networkidle')
        self.page.wait_for_selector('[data-shot="0,0"]', state='attached')

    def open_director(self):
        details = self.page.locator('#storyboard .director-fields').first
        if not details.evaluate('(node)=>node.open'):
            details.locator('summary').click()
        return details

    def open_shot(self):
        self.page.locator('[data-shot="0,0"]').click()
        self.page.wait_for_function("document.querySelector('#cropImage').naturalWidth===1000")

    def drag_crop(self, box):
        image = self.page.locator('#cropImage')
        image.scroll_into_view_if_needed()
        bounds = image.bounding_box()
        x0, y0, x1, y1 = box
        self.page.mouse.move(bounds['x'] + bounds['width'] * x0, bounds['y'] + bounds['height'] * y0)
        self.page.mouse.down()
        self.page.mouse.move(bounds['x'] + bounds['width'] * x1, bounds['y'] + bounds['height'] * y1, steps=4)
        self.page.mouse.up()
        return self.page.evaluate('[...state.crop]')

    def two_beats(self):
        self.page.locator('#visualBeatsEnabled').check()
        self.page.locator('[data-beat-label="0"]').fill('完整对比')
        self.page.locator('#addVisualBeat').click()
        self.page.locator('[data-beat-cue="1"]').fill('再看蓝色区域')
        self.page.locator('[data-beat-label="1"]').fill('蓝色区域')
        crop = self.drag_crop([.55, .15, .85, .85])
        self.page.locator('[data-beat-box="1"]').click()
        return crop

    def save_plan(self):
        with self.page.expect_response(lambda response: response.url.endswith('/api/plan')) as event:
            self.page.locator('#savePlan').click()
        response = event.value
        self.assertEqual(response.status, 200, response.text())
        self.page.wait_for_function('!state.dirty')
        return response.request.post_data_json

    def test_legacy_plan_stays_untouched_when_optional_director_is_only_opened(self):
        before = (self.job / 'analysis/studio_plan.json').read_bytes()
        self.open_job()
        save_disabled = self.page.locator('#savePlan').is_disabled()
        self.assertFalse(self.page.evaluate("Object.hasOwn(state.job.plan.blocks[0],'explanation')"))
        fields = self.open_director()
        self.assertFalse(fields.locator('[data-director-enabled]').is_checked())
        self.assertTrue(fields.locator('[data-director-fields]').is_hidden())
        self.assertFalse(self.page.evaluate('state.dirty'))
        self.assertEqual(self.page.locator('#savePlan').is_disabled(), save_disabled)
        self.assertFalse(self.page.evaluate("Object.hasOwn(state.job.plan.blocks[0],'explanation')"))
        self.assertEqual((self.job / 'analysis/studio_plan.json').read_bytes(), before)
        self.assertEqual(self.posts, [])

    def test_editing_four_explanation_fields_survives_post_and_reload_without_rewriting(self):
        self.open_job()
        fields = self.open_director()
        fields.locator('[data-director-enabled]').check()
        expected = {'question': '两组分别位于哪里？', 'takeaway': '红色在左，蓝色在右。',
                    'visual_strategy': 'comparison', 'continuity': '下一段沿用同一组颜色。'}
        for key, value in expected.items():
            field = fields.locator(f'[data-director="{key}"]')
            field.select_option(value) if key == 'visual_strategy' else field.fill(value)
        sent = self.save_plan()
        self.assertEqual(sent['plan']['blocks'][0]['explanation'], expected)
        saved = pv.load_json(self.job / 'analysis/studio_plan.json')
        self.assertEqual(saved['blocks'][0]['explanation'], expected)
        self.assertEqual(saved['blocks'][0]['narration'], self.request['script'])
        self.assertEqual(pv.load_json(self.job / 'request.json')['script'], self.request['script'])
        self.page.reload(wait_until='networkidle')
        fields = self.open_director()
        self.assertTrue(fields.locator('[data-director-enabled]').is_checked())
        for key, value in expected.items():
            self.assertEqual(fields.locator(f'[data-director="{key}"]').input_value(), value)

    def test_two_cue_views_use_dragged_source_coordinates_and_keep_base_crop_after_save(self):
        self.open_job(); self.open_shot()
        base_crop = self.plan['blocks'][0]['shots'][0]['bbox'][:]
        detail_crop = self.two_beats()
        self.assertTrue(self.page.locator('[data-beat-cue="0"]').is_disabled())
        self.page.locator('#shotForm button[type=submit]').click()
        self.assertTrue(self.page.locator('#shotDialog').is_hidden())
        sent = self.save_plan()
        shot = pv.load_json(self.job / 'analysis/studio_plan.json')['blocks'][0]['shots'][0]
        self.assertEqual(shot['source_id'], 'chart')
        self.assertEqual(shot['bbox'], base_crop)
        self.assertEqual(shot['focus'], [])
        self.assertIsNone(shot['column_crop'])
        self.assertEqual(shot['visual_beats'], sent['plan']['blocks'][0]['shots'][0]['visual_beats'])
        self.assertEqual([beat['cue'] for beat in shot['visual_beats']], ['', '再看蓝色区域'])
        self.assertEqual(shot['visual_beats'][0]['bbox'], base_crop)
        for actual, expected in zip(shot['visual_beats'][1]['bbox'], detail_crop):
            self.assertAlmostEqual(actual, expected, places=8)
        self.assertEqual(pv.load_json(self.job / 'analysis/studio_plan.json')['blocks'][0]['narration'], self.request['script'])
        self.page.reload(wait_until='networkidle'); self.open_shot()
        self.assertTrue(self.page.locator('#visualBeatsEnabled').is_checked())
        self.assertEqual(self.page.locator('[data-beat-cue="1"]').input_value(), '再看蓝色区域')

    def test_invalid_missing_duplicate_and_reversed_cues_do_not_modify_or_save_the_plan(self):
        self.open_job()
        before = (self.job / 'analysis/studio_plan.json').read_bytes()
        original = self.page.evaluate('JSON.stringify(state.job.plan)')
        for cues in (['不在文稿'], ['区域'], ['再看蓝色区域', '红色区域位于左侧']):
            with self.subTest(cues=cues):
                self.open_shot()
                self.page.locator('#visualBeatsEnabled').check()
                for index, cue in enumerate(cues, 1):
                    self.page.locator('#addVisualBeat').click()
                    self.page.locator(f'[data-beat-cue="{index}"]').fill(cue)
                self.page.locator('#shotForm button[type=submit]').click()
                self.assertTrue(self.page.locator('#shotDialog').is_visible())
                self.assertIn('唯一出现', self.page.locator('#toast').inner_text())
                self.assertFalse(self.page.evaluate('state.dirty'))
                self.assertEqual(self.page.evaluate('JSON.stringify(state.job.plan)'), original)
                self.page.keyboard.press('Escape')
        self.assertEqual((self.job / 'analysis/studio_plan.json').read_bytes(), before)
        self.assertEqual(self.posts, [])

    def test_real_preview_get_renders_source_pixels_and_reports_a_missing_source(self):
        self.open_job(); self.open_shot(); detail_crop = self.two_beats()
        with self.page.expect_response(lambda r: '/api/visual-preview?' in r.url) as event:
            self.page.locator('[data-beat-preview="1"]').click()
        response = event.value
        self.assertEqual(response.status, 200, response.text())
        query = parse_qs(urlparse(response.url).query)
        self.assertEqual(query['id'], ['chart'])
        self.assertEqual(query['job'], [self.job.name])
        self.assertEqual(json.loads(query['beats'][0])[1]['bbox'], detail_crop)
        self.page.wait_for_function("!document.querySelector('#visualBeatPreview').hidden && document.querySelector('#visualBeatPreview').naturalWidth>0")
        source_url = self.page.locator('#visualBeatPreview').get_attribute('src')
        self.assertIn('/analysis/visual_previews/', source_url)
        pixels = self.page.request.get(self.base + source_url)
        self.assertEqual(pixels.status, 200)
        image = Image.open(io.BytesIO(pixels.body())).convert('RGB')
        self.assertEqual(image.size, (640, 360))
        raw_pixels = image.tobytes()
        self.assertGreater(sum(b > 150 and b > r * 1.6
                               for r, b in zip(raw_pixels[0::3], raw_pixels[2::3])), 2000)
        self.assertIn('字幕和切换时间在配音后确定', self.page.locator('#visualBeatHint').inner_text())
        self.source.unlink()  # Only the temporary fixture, never a real task source.
        with self.page.expect_response(lambda r: '/api/visual-preview?' in r.url) as failed:
            self.page.locator('[data-beat-preview="1"]').click()
        self.assertGreaterEqual(failed.value.status, 400)
        expected = failed.value.json()['error']
        self.page.wait_for_function('(message)=>document.querySelector("#visualBeatHint").textContent===message', arg=expected)
        self.assertTrue(self.page.locator('#visualBeatPreview').is_hidden())
        self.assertEqual(self.posts, [])

    def test_changing_source_clears_stages_and_does_not_redisplay_the_old_source_preview(self):
        self.open_job(); self.open_shot(); self.two_beats()
        self.page.locator('[data-beat-preview="1"]').click()
        self.page.wait_for_function("!document.querySelector('#visualBeatPreview').hidden && document.querySelector('#visualBeatPreview').naturalWidth>0")
        self.page.locator('#shotSource').select_option('other')
        self.assertFalse(self.page.locator('#visualBeatsEnabled').is_checked())
        self.assertEqual(self.page.locator('[data-beat-cue]').count(), 0)
        self.page.locator('#visualBeatsEnabled').check()
        self.assertEqual(self.page.locator('[data-beat-cue]').count(), 1)
        self.assertEqual(self.page.locator('[data-beat-cue="0"]').input_value(), '')
        self.assertTrue(self.page.locator('#visualBeatPreview').is_hidden())
        self.assertEqual(self.page.locator('#visualBeatHint').inner_text(), '')
        self.page.locator('#shotForm button[type=submit]').click()
        self.save_plan()
        shot = pv.load_json(self.job / 'analysis/studio_plan.json')['blocks'][0]['shots'][0]
        self.assertEqual(shot['source_id'], 'other')
        self.assertEqual(shot['bbox'], [0, 0, 1, 1])
        self.assertEqual(shot['visual_beats'], [{'cue': '', 'bbox': [0, 0, 1, 1], 'label': ''}])

    def test_explanation_and_stage_labels_are_displayed_as_text_without_html_injection(self):
        question = '\"><img src=x onerror="window.directorInjected=true">'
        takeaway = '</textarea><script>window.directorInjected=true</script>'
        self.plan['blocks'][0]['explanation'] = {'question': question, 'takeaway': takeaway,
            'visual_strategy': 'context', 'continuity': '<b>保留为文本</b>'}
        self.plan['blocks'][0]['shots'][0]['visual_beats'] = [
            {'cue': '', 'bbox': [.1, .1, .9, .9], 'label': '<img src=x>'}]
        self.write_plan(); self.open_job()
        fields = self.open_director()
        self.assertEqual(fields.locator('[data-director="question"]').input_value(), question)
        self.assertEqual(fields.locator('[data-director="takeaway"]').input_value(), takeaway)
        self.assertEqual(fields.locator('img,script').count(), 0)
        self.open_shot()
        self.assertEqual(self.page.locator('[data-beat-label="0"]').input_value(), '<img src=x>')
        self.assertEqual(self.page.locator('#visualBeatRows img,#visualBeatRows script').count(), 0)
        self.assertFalse(self.page.evaluate('Boolean(window.directorInjected)'))
        self.assertEqual(self.posts, [])

    def test_late_preview_response_cannot_replace_the_new_sources_empty_preview(self):
        self.open_job(); self.open_shot(); self.two_beats()
        pending = []
        def delay_response(route):
            pending.append((route, route.fetch()))
        self.page.route('**/api/visual-preview?*', delay_response)
        with self.page.expect_request('**/api/visual-preview?*'):
            self.page.locator('[data-beat-preview="1"]').click()
        self.page.locator('#shotSource').select_option('other')
        self.page.locator('#visualBeatsEnabled').check()
        self.assertEqual(len(pending), 1)
        route, response = pending[0]
        self.assertEqual(response.status, 200)
        with self.page.expect_response(lambda r: '/api/visual-preview?' in r.url) as delivered:
            route.fulfill(response=response)
        delivered.value.finished()
        self.page.wait_for_load_state('networkidle')
        self.assertEqual(self.page.locator('#shotSource').input_value(), 'other')
        self.assertTrue(self.page.locator('#visualBeatPreview').is_hidden())
        self.assertEqual(self.page.locator('#visualBeatHint').inner_text(), '')
        self.assertEqual(self.page.locator('[data-beat-cue]').count(), 1)
        self.assertEqual(self.posts, [])

    def test_director_and_detail_controls_fit_desktop_tablet_and_narrow_viewports(self):
        self.open_job()
        for width in (1440, 768, 390):
            with self.subTest(width=width):
                self.page.set_viewport_size({'width': width, 'height': 1000})
                fields = self.open_director()
                fields.locator('[data-director-enabled]').check()
                fields.locator('[data-director="question"]').fill('两组各在什么位置？')
                fields.locator('[data-director="takeaway"]').fill('红色在左，蓝色在右。')
                self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), width + 1)
                for name in ('question', 'takeaway', 'visual_strategy', 'continuity'):
                    rect = fields.locator(f'[data-director="{name}"]').bounding_box()
                    self.assertGreater(rect['width'], 30)
                    self.assertGreaterEqual(rect['x'], 0)
                    self.assertLessEqual(rect['x'] + rect['width'], width + 1)
                self.open_shot(); self.two_beats()
                dialog = self.page.locator('#shotDialog')
                self.assertLessEqual(dialog.evaluate('(n)=>n.scrollWidth-n.clientWidth'), 1)
                self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), width + 1)
                self.assertTrue(self.page.locator('[data-beat-preview="1"]').is_visible())
                self.page.keyboard.press('Escape')


if __name__ == '__main__':
    unittest.main()
