"""Review receives images for manually selected pages, not just intake samples."""
import json
import copy
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
        self.job = Path(self.temp.name).resolve()
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
        # Full page, exact crop, actual-canvas preview, and optional intake sample.
        self.assertEqual(len(seen['images']), 4)
        with Image.open(seen['images'][0]) as image:
            self.assertEqual(image.convert('RGB').getpixel((10, 10)), (0, 0, 255))
        with Image.open(seen['images'][1]) as image:
            self.assertEqual(image.convert('RGB').getpixel((10, 10)), (0, 0, 255))
        self.assertIn('实际裁图', seen['prompt'])
        self.assertIn('p3', seen['prompt'])
        self.assertIn('构图预览', seen['prompt'])
        manifest = pv.load_json(self.job / 'analysis/review_assets.json')
        self.assertEqual(manifest['selected_source_ids'], ['p3'])
        self.assertEqual(manifest['crops'][0]['bbox'], [.2, .2, .8, .8])
        self.assertEqual(self.sources[0].get('preview'), None, 'Intake provenance is not rewritten')

    def test_pdf_composition_matches_export_focus_and_protected_canvas_geometry(self):
        from studio_render import media_geometry
        # A word intersects the left edge; panel_clip must shift both the crop
        # and its focus coordinates exactly as the actual export does.
        paper=self.job/'word_boundary.pdf'
        with pymupdf.open() as doc:
            page=doc.new_page(width=200,height=200)
            page.draw_rect(page.rect,color=(0,0,1),fill=(0,0,1))
            page.insert_text((39,80),'Boundary',fontsize=8)
            doc.save(paper)
        self.sources[0].update(file=str(paper),page=1)
        shot=self.plan['blocks'][0]['shots'][0]
        shot.update(focus=[.3,.3,.7,.7],duration=4,label='Figure',cue='',keep_full=False,card_text='',**{'in':0})
        request={'format':'custom','width':1000,'height':1600,'layout_style':'immersive','media_fit':'cover'}
        before=paper.read_bytes();original=copy.deepcopy(self.plan)
        images,scope=prepare_review_assets(self.job,self.plan,self.sources,[],request=request)
        item=scope['compositions'][0]
        self.assertEqual(item['fit'],'contain');self.assertTrue(item['protect_content'])
        expected=list(media_geometry(1000,1600,request,True));expected[2]-=expected[2]%2;expected[3]-=expected[3]%2
        self.assertEqual(item['media_box'],expected)
        self.assertEqual(item['canvas'],[1000,1600])
        self.assertLess(item['image_canvas_area_ratio'],1)
        self.assertGreater(item['focus_canvas_area_ratio'],0)
        (self.job/'assets').mkdir()
        plan={**self.plan,'title':'Figure','summary':'A figure.','source_issues':[],'unused_sources':[]}
        plan['blocks'][0]['chapter']='Figure'
        config=engine.export(self.job,plan,self.sources,request)
        actual=config['blocks'][0]['shots'][0]
        for a,b in zip(item['focus_crop_normalized'],actual['focus']):self.assertAlmostEqual(a,b,places=7)
        with Image.open(item['path']) as image:
            self.assertEqual(image.size,(1000,1600))
            self.assertEqual(image.getpixel((0,0)),(8,14,21))
            x,y,_,_=item['focus_rect']
            self.assertEqual(image.getpixel((int(x)+1,int(y)+1)),(84,214,189))
        self.assertEqual(paper.read_bytes(),before)
        self.assertEqual(self.plan['blocks'][0]['shots'],original['blocks'][0]['shots'])

    def test_image_crop_focus_and_cover_are_separate_from_source_selection(self):
        image_path=self.job/'wide.png';Image.new('RGB',(800,200),'blue').save(image_path)
        source={'id':'image','type':'image','file':str(image_path)}
        shot={'kind':'image','source_id':'image','bbox':[.25,0,.75,1],'focus':[.375,.25,.625,.75]}
        plan={'blocks':[{'id':'b','shots':[shot]}]}
        request={'format':'custom','width':1000,'height':1600,'layout_style':'immersive','media_fit':'cover'}
        _,scope=prepare_review_assets(self.job,plan,[source],[],request=request)
        item=scope['compositions'][0]
        self.assertEqual(item['fit'],'cover');self.assertFalse(item['protect_content'])
        self.assertEqual(item['focus_crop_normalized'],[.25,.25,.75,.75])
        self.assertEqual(item['visible_image_rect'],[0,0,1000,1600])
        self.assertEqual(item['image_canvas_area_ratio'],1)
        self.assertEqual(item['focus_rect'],[0,400,1000,800])
        self.assertEqual(scope['crops'][0]['bbox'],[.25,0,.75,1])

    def test_composition_budget_and_changed_canvas_or_focus_are_explicit(self):
        request={'format':'portrait','layout_style':'immersive'}
        _,scope=prepare_review_assets(self.job,self.plan,self.sources,[],max_images=2,request=request)
        self.assertEqual(scope['compositions'],[])
        self.assertEqual(len(scope['omitted_compositions']),1)
        self.assertEqual(len(scope['originals']),1);self.assertEqual(len(scope['crops']),1)
        _,first=prepare_review_assets(self.job,self.plan,self.sources,[],request=request)
        _,changed=prepare_review_assets(self.job,self.plan,self.sources,[],request=request|{'format':'landscape'})
        self.assertNotEqual(first['compositions'][0]['path'],changed['compositions'][0]['path'])
        self.plan['blocks'][0]['shots'][0]['focus']=[.3,.3,.7,.7]
        _,focused=prepare_review_assets(self.job,self.plan,self.sources,[],request=request)
        self.assertNotEqual(first['compositions'][0]['path'],focused['compositions'][0]['path'])

    def test_same_composition_is_deduplicated_without_losing_shot_coverage(self):
        self.plan['blocks'].append({'id':'b2','shots':copy.deepcopy(self.plan['blocks'][0]['shots'])})
        images,scope=prepare_review_assets(self.job,self.plan,self.sources,[],max_images=3,request={'format':'portrait'})
        self.assertEqual(len(images),3)
        self.assertEqual([s['block_id'] for s in scope['compositions'][0]['shots']],['b1','b2'])

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
