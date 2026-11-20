"""Adaptive base-rate UI persistence, export dispatch and receipt identity."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import make_video
import paper_video as pv
import studio_engine as engine
from studio_revision import file_hash, render_settings_hash, review_stale, export_version_current
from studio_voice import ADAPTIVE_PACING_VERSION
from tests import test_director_ui as fixtures


class AdaptivePacingReceiptTests(unittest.TestCase):
    def make_receipt_job(self, root, request):
        job = Path(root); (job/'analysis').mkdir(); (job/'output').mkdir()
        plan = {'title': '固定文稿', 'summary': '', 'source_issues': [], 'unused_sources': [],
                'blocks': [{'id': 'b1', 'chapter': '', 'narration': '原稿保持不变。',
                            'evidence': [], 'shots': [{'kind': 'card', 'source_id': '',
                            'in': 0, 'duration': 3, 'bbox': [], 'label': '',
                            'card_text': '原稿', 'cue': ''}]}]}
        engine.validate(plan, [])
        pv.save_json(job/'request.json', request)
        pv.save_json(job/'analysis/studio_plan.json', plan)
        pv.save_json(job/'project.json', {'voice': request['voice'], 'blocks': []})
        (job/'output/video.mp4').write_bytes(b'existing render bytes - no decoding in this identity test')
        proof = {'plan_hash': engine.plan_hash(plan), 'settings_hash': render_settings_hash(request),
                 'video_sha256': file_hash(job/'output/video.mp4'), 'project_sha256': file_hash(job/'project.json')}
        pv.save_json(job/'output/render_receipt.json', proof)
        return job, plan, proof

    def test_auto_base_rate_change_invalidates_existing_receipt_without_changing_plan(self):
        request = {'voice': 'zh-CN-YunyangNeural', 'rate': 'auto', 'adaptive_base_rate': '+40%'}
        with tempfile.TemporaryDirectory() as directory:
            job, plan, proof = self.make_receipt_job(directory, request)
            before = (job/'analysis/studio_plan.json').read_bytes()
            self.assertTrue(export_version_current(job))
            pv.save_json(job/'request.json', dict(request, adaptive_base_rate='+50%'))
            self.assertTrue(review_stale(job, proof, engine.plan_hash(plan)))
            self.assertFalse(export_version_current(job))
            self.assertEqual((job/'analysis/studio_plan.json').read_bytes(), before)

    def test_fixed_rate_and_local_voice_ignore_unused_adaptive_base_for_receipt(self):
        for voice, rate in (('zh-CN-YunyangNeural', '+50%'), ('qwen:Uncle_Fu', 'auto')):
            with self.subTest(voice=voice), tempfile.TemporaryDirectory() as directory:
                request = {'voice': voice, 'rate': rate, 'adaptive_base_rate': '+40%'}
                job, plan, proof = self.make_receipt_job(directory, request)
                pv.save_json(job/'request.json', dict(request, adaptive_base_rate='+50%'))
                self.assertFalse(review_stale(job, proof, engine.plan_hash(plan)))
                self.assertTrue(export_version_current(job))

    def test_legacy_auto_missing_base_matches_explicit_default_but_fixed_hash_is_unchanged(self):
        auto = {'voice': 'zh-CN-YunyangNeural', 'rate': 'auto'}
        self.assertEqual(render_settings_hash(auto), render_settings_hash(dict(auto, adaptive_base_rate='+40%')))
        fixed = dict(auto, rate='+50%')
        self.assertEqual(render_settings_hash(fixed), render_settings_hash(dict(fixed, adaptive_base_rate='+25%')))


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'Playwright is not installed')
class AdaptivePacingUITests(unittest.TestCase):
    setUpClass = classmethod(fixtures.DirectorUITests.setUpClass.__func__)
    write_plan = fixtures.DirectorUITests.write_plan
    open_job = fixtures.DirectorUITests.open_job

    def setUp(self):
        fixtures.DirectorUITests.setUp(self)
        self.request.update(voice='zh-CN-YunyangNeural', rate='auto', adaptive_base_rate='+40%')
        pv.save_json(self.job/'request.json', self.request)

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def test_render_sends_selected_base_then_reload_and_export_preserve_it(self):
        self.open_job()
        self.page.locator('[data-tab=create]').click()
        self.assertEqual(self.page.locator('#speechRate').input_value(), 'auto')
        self.assertEqual(self.page.locator('#adaptiveBaseRate').input_value(), '+40%')
        self.assertTrue(self.page.locator('#adaptiveBaseWrap').is_visible())
        self.page.locator('#adaptiveBaseRate').select_option('+50%')
        self.page.locator('[data-tab=editor]').click()
        with self.page.expect_response(lambda r: r.url.endswith('/api/render')) as event:
            self.page.locator('#renderButton').click()
        self.assertEqual(event.value.status, 200, event.value.text())
        speech = event.value.request.post_data_json['speech']
        self.assertEqual(speech, {'voice': 'zh-CN-YunyangNeural', 'rate': 'auto', 'adaptive_base_rate': '+50%'})
        saved = pv.load_json(self.job/'request.json')
        self.assertEqual(saved['adaptive_base_rate'], '+50%'); self.assertEqual(saved['rate'], 'auto')
        self.assertEqual(saved['script'], self.request['script'])
        self.http.manager.start.assert_called_once_with(self.job, 'render', '')
        self.page.reload(wait_until='networkidle')
        self.page.locator('[data-tab=create]').click()
        self.assertEqual(self.page.locator('#adaptiveBaseRate').input_value(), '+50%')
        self.assertEqual(self.page.locator('#speechRate').input_value(), 'auto')
        exported = engine.export(self.job, pv.load_json(self.job/'analysis/studio_plan.json'), self.sources, saved)
        self.assertEqual(exported['adaptive_base_rate'], '+50%')
        self.assertEqual(exported['rate'], 'auto')

    def test_new_task_create_sends_explicit_auto_base_and_restores_saved_task_over_preferences(self):
        self.page.goto(self.base, wait_until='networkidle')
        self.page.locator('#speechRate').select_option('auto')
        self.page.locator('#adaptiveBaseRate').select_option('+40%')
        self.page.locator('#article').fill('测试资料说明同一个模型在相同条件下的两组结果。请保留测试条件。')
        with self.page.expect_response(lambda r: r.url.endswith('/api/create')) as event:
            self.page.locator('#createButton').click()
        self.assertEqual(event.value.status, 200, event.value.text())
        sent = event.value.request.post_data_json
        self.assertEqual(sent['rate'], 'auto'); self.assertEqual(sent['adaptive_base_rate'], '+40%')
        created = self.root/'jobs'/event.value.json()['job_id']
        self.assertEqual(pv.load_json(created/'request.json')['adaptive_base_rate'], '+40%')
        self.page.evaluate("localStorage.setItem('video-workshop-settings',JSON.stringify({rate:'auto',adaptive_base_rate:'+50%'}))")
        self.page.goto(self.base+'/?job='+created.name, wait_until='networkidle')
        self.page.locator('[data-tab=create]').click()
        self.assertEqual(self.page.locator('#adaptiveBaseRate').input_value(), '+40%')

    def test_fixed_rate_hides_auto_base_and_server_rejects_invalid_adaptive_input_atomically(self):
        self.open_job(); self.page.locator('[data-tab=create]').click()
        self.page.locator('#speechRate').select_option('+50%')
        self.assertTrue(self.page.locator('#adaptiveBaseWrap').is_hidden())
        before = (self.job/'request.json').read_bytes()
        response = self.page.request.post(self.base+'/api/render', data=json.dumps({
            'job_id': self.job.name, 'speech': {'rate': 'auto', 'adaptive_base_rate': '+999%'}}),
            headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status, 400)
        self.assertEqual((self.job/'request.json').read_bytes(), before)
        self.http.manager.start.assert_not_called()

    def test_new_project_restores_local_base_or_default_instead_of_previous_task(self):
        self.request['adaptive_base_rate'] = '+50%'
        pv.save_json(self.job/'request.json', self.request)
        before = (self.job/'request.json').read_bytes()
        self.page.goto(self.base, wait_until='networkidle')
        for preferences in ({'rate': 'auto', 'adaptive_base_rate': '+40%'}, {'rate': 'auto'}):
            with self.subTest(preferences=preferences):
                self.page.evaluate('(settings)=>localStorage.setItem("video-workshop-settings",JSON.stringify(settings))', preferences)
                self.open_job()
                self.page.locator('[data-tab=create]').click()
                self.assertEqual(self.page.locator('#adaptiveBaseRate').input_value(), '+50%')
                self.page.locator('#newProject').click()
                self.assertEqual(self.page.locator('#speechRate').input_value(), 'auto')
                self.assertTrue(self.page.locator('#adaptiveBaseWrap').is_visible())
                self.assertEqual(self.page.locator('#adaptiveBaseRate').input_value(), '+40%')
                self.assertIsNone(self.page.evaluate('state.job'))
        self.assertEqual((self.job/'request.json').read_bytes(), before)
        self.assertEqual(self.posts, [])
        self.http.manager.start.assert_not_called()

    def test_fixed_rate_render_keeps_original_payload_without_unused_base(self):
        self.open_job(); self.page.locator('[data-tab=create]').click()
        self.page.locator('#adaptiveBaseRate').select_option('+50%')
        self.page.locator('#speechRate').select_option('+50%')
        self.page.locator('[data-tab=editor]').click()
        with self.page.expect_response(lambda r: r.url.endswith('/api/render')) as event:
            self.page.locator('#renderButton').click()
        self.assertEqual(event.value.status, 200, event.value.text())
        self.assertEqual(event.value.request.post_data_json['speech'],
                         {'voice': 'zh-CN-YunyangNeural', 'rate': '+50%'})
        self.assertEqual(pv.load_json(self.job/'request.json')['rate'], '+50%')

    def test_build_uses_selected_base_and_reports_actual_provider_timing(self):
        exported = engine.export(self.job, self.plan, self.sources, dict(self.request, adaptive_base_rate='+50%'))
        self.assertEqual(exported['adaptive_base_rate'], '+50%')
        narration = self.plan['blocks'][0]['narration']
        pcm = b'\0'*(3*make_video.RATE_HZ*2)
        words = [{'type': 'WordBoundary', 'text': narration, 'offset': 0, 'duration': 25_000_000}]
        timing = [{'text': narration, 'rate': '+50%', 'start': 0, 'audio_seconds': 3,
                   'pause_ms': 0, 'cached': False, 'provider': 'edge-tts',
                   'voice': self.request['voice'], 'base_rate': '+50%',
                   'pacing_version': ADAPTIVE_PACING_VERSION}]
        fake = AsyncMock(return_value=(pcm, words, False, timing))
        args = SimpleNamespace(project=str(self.job/'project.json'), voice=None, rate=None,
                               font=None, check=False, subtitles_only=False)
        with patch('studio_voice.make_adaptive_voice', fake), patch('make_video.make_voice', new=AsyncMock()) as fixed, patch('make_video.log'):
            # Playwright owns the main thread's event loop. The renderer uses
            # its normal asyncio entry point in an independent worker thread.
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(lambda: asyncio.run(make_video.build(args))).result(timeout=45)
        self.assertEqual(fake.await_count, 1)
        self.assertEqual(fake.await_args.args[:2], (narration, self.request['voice']))
        self.assertEqual(fake.await_args.kwargs['base_rate'], '+50%')
        fixed.assert_not_called()
        report = pv.load_json(self.job/'output/run_report.json')
        self.assertEqual(report['rate'], 'auto')
        self.assertEqual(report['adaptive_base_rate'], '+50%')
        self.assertEqual(report['adaptive_pacing_version'], ADAPTIVE_PACING_VERSION)
        self.assertEqual(report['voice'], self.request['voice'])


if __name__ == '__main__':
    unittest.main()
