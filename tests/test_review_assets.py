"""Review receives images for manually selected pages, not just intake samples."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import pymupdf

import paper_video as pv
import studio_engine as engine
from studio_review_assets import prepare_review_assets


class ReviewAssetsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.job = Path(self.temp.name)
        (self.job / 'analysis').mkdir()
        self.pdf = self.job / 'paper.pdf'
        with pymupdf.open() as document:
            for color in [(1, 0, 0), (0, 1, 0), (0, 0, 1)]:
                page = document.new_page(width=200, height=200)
                page.draw_rect(page.rect, color=color, fill=color)
            document.save(self.pdf)
        self.old_image = self.job / 'analysis/page1.png'
        Image.new('RGB', (50, 50), 'red').save(self.old_image)
        self.images = [('p1', str(self.old_image))]
        self.sources = [{'id': 'p3', 'type': 'page', 'file': str(self.pdf), 'page': 3,
                         'name': 'Paper', 'text': 'The selected third page.'}]
        self.plan = {'blocks': [{'id': 'b1', 'narration': 'Explain the selected figure.', 'evidence': [],
            'shots': [{'kind': 'page', 'source_id': 'p3', 'bbox': [.2, .2, .8, .8]}]}]}

    def tearDown(self):
        self.temp.cleanup()

    def test_manual_unsampled_pdf_page_and_real_crop_reach_review_model(self):
        pv.save_json(self.job / 'analysis/images.json', self.images)
        seen = {}
        def reviewer(prompt, images, *_args):
            seen.update(prompt=prompt, images=images)
            return {'ready': True, 'summary': 'Fixture response, not a quality judgment.', 'issues': []}
        with patch.object(engine, 'call_ai', side_effect=reviewer):
            engine.review_plan(self.job, self.plan, self.sources, {'content_type': 'general'})
        # Full page, exact crop, and the existing optional intake sample.
        self.assertEqual(len(seen['images']), 3)
        with Image.open(seen['images'][0]) as image:
            self.assertEqual(image.convert('RGB').getpixel((10, 10)), (0, 0, 255))
        with Image.open(seen['images'][1]) as image:
            self.assertEqual(image.convert('RGB').getpixel((10, 10)), (0, 0, 255))
        self.assertIn('实际裁图', seen['prompt'])
        self.assertIn('p3', seen['prompt'])
        manifest = pv.load_json(self.job / 'analysis/review_assets.json')
        self.assertEqual(manifest['selected_source_ids'], ['p3'])
        self.assertEqual(manifest['crops'][0]['bbox'], [.2, .2, .8, .8])
        self.assertEqual(self.sources[0].get('preview'), None, 'Intake provenance is not rewritten')

    def test_selected_page_and_crop_replace_unused_samples_and_deduplicate(self):
        duplicate = {'id': 'b2', 'narration': 'Another use.', 'evidence': [],
                     'shots': [dict(self.plan['blocks'][0]['shots'][0])]}
        plan = {'blocks': self.plan['blocks'] + [duplicate]}
        images, scope = prepare_review_assets(self.job, plan, self.sources, self.images, max_images=2)
        self.assertEqual(len(images), 2)
        self.assertEqual(scope['omitted_prior'], ['p1'])
        self.assertEqual(len(scope['crops']), 1)
        self.assertEqual([s['block_id'] for s in scope['crops'][0]['shots']], ['b1', 'b2'])

    def test_crop_budget_omission_is_explicit_and_full_source_remains(self):
        images, scope = prepare_review_assets(self.job, self.plan, self.sources, self.images, max_images=1)
        self.assertEqual(len(images), 1)
        self.assertEqual(scope['selected_source_ids'], ['p3'])
        self.assertEqual(scope['omitted_crops'][0]['source_id'], 'p3')
        self.assertEqual(scope['crops'], [])

    def test_selected_sources_over_budget_fail_before_partial_review(self):
        sources = self.sources + [dict(self.sources[0], id='p2', page=2)]
        plan = {'blocks': [dict(self.plan['blocks'][0], shots=[
            self.plan['blocks'][0]['shots'][0], {'kind': 'page', 'source_id': 'p2', 'bbox': [0, 0, 1, 1]}])]}
        with self.assertRaisesRegex(ValueError, '超过1张预算'):
            prepare_review_assets(self.job, plan, sources, [], max_images=1)
        self.assertFalse((self.job / 'analysis/review_assets.json').exists())

    def test_selected_video_contact_sheet_is_preserved_before_optional_crop(self):
        frame = self.job / 'analysis/video.png'
        Image.new('RGB', (10, 10), 'green').save(frame)
        plan = {'blocks': [dict(self.plan['blocks'][0], shots=self.plan['blocks'][0]['shots'] + [
            {'kind': 'video', 'source_id': 'clip', 'in': 0, 'duration': 4}])]}
        images, scope = prepare_review_assets(self.job, plan, self.sources,
            self.images + [('clip · 视频拼图1', str(frame))], max_images=2)
        self.assertEqual(images[1][1], frame)
        self.assertEqual(len(scope['omitted_crops']), 1)
        self.assertIn('p1', scope['omitted_prior'])

    def test_image_crop_uses_actual_pixels_and_changed_bbox_has_new_asset(self):
        image_path = self.job / 'image.png'
        image = Image.new('RGB', (100, 50), 'red')
        image.paste((0, 0, 255), (50, 0, 100, 50)); image.save(image_path)
        sources = [{'id': 'image', 'type': 'image', 'file': str(image_path)}]
        shot = {'kind': 'image', 'source_id': 'image', 'bbox': [.5, 0, 1, 1]}
        plan = {'blocks': [{'id': 'b', 'shots': [shot]}]}
        images, scope = prepare_review_assets(self.job, plan, sources, [('image', str(image_path))])
        self.assertEqual(len(images), 2, 'Do not also attach the same original from intake')
        with Image.open(images[1][1]) as cropped:
            self.assertEqual(cropped.size, (50, 50))
            self.assertEqual(cropped.getpixel((0, 0)), (0, 0, 255))
        shot['bbox'] = [0, 0, .5, 1]
        changed, _ = prepare_review_assets(self.job, plan, sources, [])
        self.assertNotEqual(images[1][1], changed[1][1])
        with Image.open(changed[1][1]) as cropped:
            self.assertEqual(cropped.getpixel((0, 0)), (255, 0, 0))

    def test_registered_source_cannot_read_outside_job(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            outside = Path(elsewhere) / 'image.png'
            Image.new('RGB', (10, 10), 'red').save(outside)
            source = {'id': 'p3', 'type': 'page', 'file': str(outside), 'page': 1}
            with self.assertRaisesRegex(ValueError, '超出任务目录'):
                prepare_review_assets(self.job, self.plan, [source], [])

    def test_bad_page_or_bbox_fails_clearly_without_model_guessing(self):
        with self.assertRaisesRegex(ValueError, '页码超出范围'):
            prepare_review_assets(self.job, self.plan, [dict(self.sources[0], page=4)], [])
        for bbox in [[-.1, 0, 1, 1], [0, 0, 0, 1], [0, 0, float('nan'), 1]]:
            with self.subTest(bbox=bbox):
                self.plan['blocks'][0]['shots'][0]['bbox'] = bbox
                with self.assertRaisesRegex(ValueError, '审核裁切'):
                    prepare_review_assets(self.job, self.plan, self.sources, [])


if __name__ == '__main__':
    unittest.main()
