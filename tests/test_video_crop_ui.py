"""Isolated workbench edits against real HTTP and a synthetic four-panel clip."""
import importlib.util
import io
from pathlib import Path
import shutil
import unittest

from PIL import Image, ImageDraw

import make_video
import paper_video as pv
from tests import test_director_ui as fixtures


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'Playwright is not installed')
class VideoCropUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.DirectorUITests.setUpClass.__func__(cls)
        picture = Image.new('RGB', (640, 360), '#d02020')
        draw = ImageDraw.Draw(picture)
        for left, color in ((160, '#20b040'), (320, '#2040d0'), (480, '#e0c020')):
            draw.rectangle((left, 0, left+159, 359), fill=color)
        still = cls.root/'four-panels.png'; picture.save(still)
        cls.video = cls.root/'four-panels.mp4'
        make_video.run([make_video.ffmpeg_path(), '-v', 'error', '-y', '-loop', '1', '-i', str(still),
                        '-t', '4', '-r', '10', '-an', '-c:v', 'libx264', '-threads', '1',
                        '-pix_fmt', 'yuv420p', str(cls.video)], timeout=30)

    write_plan = fixtures.DirectorUITests.write_plan
    open_job = fixtures.DirectorUITests.open_job
    drag_crop = fixtures.DirectorUITests.drag_crop
    save_plan = fixtures.DirectorUITests.save_plan
    tearDown = fixtures.DirectorUITests.tearDown

    def setUp(self):
        fixtures.DirectorUITests.setUp(self)
        video = self.job/'assets/demo.mp4'; shutil.copyfile(self.video, video)
        self.sources.append({'id': 'demo', 'type': 'video', 'name': '四栏演示', 'file': str(video),
                             'duration_seconds': 4, 'sample_times': [0, .5, 1, 1.5, 2, 2.5, 3, 3.5]})
        self.plan['blocks'][0]['shots'][0].update(kind='video', source_id='demo', bbox=[], duration=3)
        pv.save_json(self.job/'analysis/sources.json', self.sources)
        self.write_plan()

    def open_video(self):
        with self.page.expect_response(lambda response: '/api/video-crop-preview?' in response.url) as event:
            self.page.locator('[data-shot="0,0"]').click()
        self.assertEqual(event.value.status, 200, event.value.text())
        self.page.wait_for_function("document.querySelector('#cropImage').naturalWidth===640 && !document.querySelector('#videoCropImage').hidden && document.querySelector('#videoCropImage').naturalWidth===640")
        return event.value.json()

    def refresh_preview(self):
        with self.page.expect_response(lambda response: '/api/video-crop-preview?' in response.url) as event:
            self.page.locator('#refreshVideoCrop').click()
        self.assertEqual(event.value.status, 200, event.value.text())
        self.page.wait_for_function("!document.querySelector('#videoCropImage').hidden && document.querySelector('#videoCropImage').complete")
        return event.value.json()

    def test_open_video_decodes_actual_original_and_crop_with_bounded_scope(self):
        self.open_job(); result = self.open_video()
        self.assertEqual(result['metadata']['inspection'], 'sampled_frames_only')
        self.assertLessEqual(len(result['frames']), 3)
        self.assertEqual(result['metadata']['canvas'], [640, 360])
        response = self.page.request.get(self.base+'/files/'+self.job.name+'/'+result['frames'][0]['original'])
        self.assertEqual(response.status, 200)
        with Image.open(io.BytesIO(response.body())) as image:
            self.assertEqual(image.size, (640, 360))
            self.assertGreater(image.getpixel((80, 180))[0], 150)
            self.assertGreater(image.getpixel((240, 180))[1], 140)
            self.assertGreater(image.getpixel((400, 180))[2], 150)
        self.assertIn('静态抽帧', self.page.locator('#videoCropStatus').inner_text())
        self.assertTrue(self.page.locator('#focusControl').is_hidden())
        self.assertTrue(self.page.locator('#keepControl').is_visible())

    def test_decode_failure_returns_an_actionable_json_error(self):
        self.open_job();self.open_video()
        from unittest.mock import patch
        with patch('studio_video_crop._extract',side_effect=RuntimeError('fixture decode failed')):
            response=self.page.request.get(self.base+'/api/video-crop-preview',params={
                'job':self.job.name,'id':'demo','in':'0','duration':'1','bbox':'[]'})
        self.assertEqual(response.status,400)
        self.assertIn('fixture decode failed',response.json()['error'])

    def test_subtitle_safe_preview_save_and_restore(self):
        self.open_job();self.open_video()
        with self.page.expect_response(lambda r:'/api/video-crop-preview?' in r.url) as response:
            self.page.locator('#videoSubtitleSafe').check()
        self.assertTrue(response.value.json()['metadata']['protect_content'])
        self.page.locator('#shotForm button[type=submit]').click()
        self.assertTrue(self.page.evaluate('state.job.plan.blocks[0].shots[0].subtitle_safe'))
        self.page.evaluate('openShot(0,0)')
        self.assertTrue(self.page.locator('#videoSubtitleSafe').is_checked())

    def test_dragged_panel_is_saved_reopened_and_previewed_without_changing_source(self):
        self.open_job(); self.open_video()
        crop = self.drag_crop([.25, 0, .5, 1])
        self.assertTrue(self.page.locator('#videoCropImage').is_hidden())
        result = self.refresh_preview()
        frame = result['frames'][0]
        response = self.page.request.get(self.base+'/files/'+self.job.name+'/'+frame['preview'])
        with Image.open(io.BytesIO(response.body())) as image:
            self.assertGreater(image.getpixel((320, 180))[1], 140)
            self.assertLess(max(image.getpixel((100, 180))), 30)
        self.page.locator('#shotForm button[type=submit]').click()
        sent = self.save_plan()
        shot = pv.load_json(self.job/'analysis/studio_plan.json')['blocks'][0]['shots'][0]
        self.assertEqual(shot['source_id'], 'demo'); self.assertEqual(shot['kind'], 'video')
        self.assertEqual(shot['bbox'], crop)
        self.assertEqual(shot['bbox'], sent['plan']['blocks'][0]['shots'][0]['bbox'])
        self.assertEqual(shot['focus'], []); self.assertEqual(shot['in'], 0)
        self.assertEqual(pv.load_json(self.job/'request.json')['script'], self.request['script'])
        self.page.reload(wait_until='networkidle'); self.open_video()
        self.assertEqual(self.page.evaluate('state.crop'), crop)

    def test_time_changes_hide_previous_preview_and_refresh_the_selected_interval(self):
        self.open_job(); self.open_video()
        self.page.locator('#shotDuration').fill('1')
        self.page.locator('#shotIn').fill('2')
        self.assertTrue(self.page.locator('#videoCropImage').is_hidden())
        self.assertIn('范围已修改', self.page.locator('#videoCropStatus').inner_text())
        result = self.refresh_preview()
        self.assertEqual(result['metadata']['requested_window'], [2, 3])
        self.assertEqual(result['metadata']['sampled_times'], [2, 2.5])
        self.assertIn('2.00 秒', self.page.locator('#videoCropStatus').inner_text())

    def test_late_preview_for_old_time_cannot_reappear_after_range_edit(self):
        self.open_job(); self.open_video()
        pending = []
        self.page.route('**/api/video-crop-preview?*', lambda route: pending.append(route))
        with self.page.expect_request('**/api/video-crop-preview?*'):
            self.page.locator('#refreshVideoCrop').click()
        self.page.locator('#shotIn').fill('1')
        self.assertEqual(len(pending), 1)
        route = pending[0]
        response = route.fetch()
        with self.page.expect_response(lambda r: '/api/video-crop-preview?' in r.url):
            route.fulfill(response=response)
        self.page.wait_for_load_state('networkidle')
        self.assertTrue(self.page.locator('#videoCropImage').is_hidden())
        self.assertIn('范围已修改', self.page.locator('#videoCropStatus').inner_text())

    def test_invalid_crop_cannot_be_persisted_by_official_plan_endpoint(self):
        self.open_job(); self.open_video()
        before = (self.job/'analysis/studio_plan.json').read_bytes()
        self.drag_crop([.1, .1, .12, .4])
        self.page.locator('#shotForm button[type=submit]').click()
        if self.page.locator('#shotDialog').is_visible():
            self.assertIn('8%', self.page.locator('#toast').inner_text())
        else:
            with self.page.expect_response(lambda r: r.url.endswith('/api/plan')) as event:
                self.page.locator('#savePlan').click()
            self.assertEqual(event.value.status, 400)
            self.assertIn('8%', event.value.json()['error'])
        self.assertEqual((self.job/'analysis/studio_plan.json').read_bytes(), before)

    def test_invalid_time_preview_reports_failure_and_has_no_previous_crop_visible(self):
        self.open_job(); self.open_video()
        self.page.locator('#shotIn').fill('3.8')
        with self.page.expect_response(lambda r: '/api/video-crop-preview?' in r.url) as event:
            self.page.locator('#refreshVideoCrop').click()
        self.assertEqual(event.value.status, 400)
        self.page.wait_for_function('(message)=>document.querySelector("#videoCropStatus").textContent===message',
                                    arg=event.value.json()['error'])
        self.assertTrue(self.page.locator('#videoCropImage').is_hidden())
        self.assertEqual(self.posts, [])

    def test_switching_to_old_image_keeps_image_crop_and_no_video_controls(self):
        self.open_job(); self.open_video()
        self.page.locator('#shotSource').select_option('chart')
        self.page.wait_for_function("document.querySelector('#cropImage').naturalWidth===1000")
        self.assertTrue(self.page.locator('#videoCropPanel').is_hidden())
        self.assertTrue(self.page.locator('#focusControl').is_visible())
        crop = self.drag_crop([.1, .1, .9, .9])
        self.page.locator('#shotForm button[type=submit]').click(); self.save_plan()
        shot = pv.load_json(self.job/'analysis/studio_plan.json')['blocks'][0]['shots'][0]
        self.assertEqual(shot['kind'], 'image'); self.assertEqual(shot['source_id'], 'chart')
        self.assertEqual(shot['bbox'], crop)
        self.page.reload(wait_until='networkidle')
        self.page.locator('[data-shot="0,0"]').click()
        self.assertTrue(self.page.locator('#videoCropPanel').is_hidden())
        self.assertEqual(self.page.evaluate('state.crop'), crop)


if __name__ == '__main__':
    unittest.main()
