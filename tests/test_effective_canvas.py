"""AI canvas descriptions follow renderer presets, preserving custom-size input."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import paper_video as pv
import studio_engine as engine
from studio_delivery import final_review
from studio_display import effective_display_settings
from studio_revision import render_settings_hash
from tests.test_plan_repair import BLOCKED, PASSED, setup_job


def visual_settings(prompt):
    payload=prompt.split('\n视觉设置：',1)[1]
    return json.JSONDecoder().raw_decode(payload)[0]


class EffectiveCanvasTests(unittest.TestCase):
    def test_presets_override_stale_custom_inputs_only_in_description_copy(self):
        for preset,size in [('portrait',(1080,1920)),('landscape',(1920,1080)),
                            ('square',(1080,1080)),('reference',(720,1280))]:
            with self.subTest(preset=preset):
                request={'format':preset,'width':1600,'height':1000,'layout_style':'immersive',
                         'media_fit':'contain','minimal_text':True,'private_unrelated':'keep out of prompt'}
                original=copy.deepcopy(request);fingerprint=render_settings_hash(request)
                actual=effective_display_settings(request)
                self.assertEqual((actual['width'],actual['height']),size)
                self.assertEqual(actual['format'],preset)
                self.assertEqual(actual['layout_style'],'immersive')
                self.assertNotIn('private_unrelated',actual)
                self.assertEqual(request,original)
                self.assertEqual(render_settings_hash(request),fingerprint)

    def test_custom_and_default_canvas_remain_consistent_with_existing_behavior(self):
        custom={'format':'custom','width':1600,'height':1000}
        self.assertEqual(effective_display_settings(custom),{**custom,'minimal_text':False})
        default={'width':1920,'height':1080}
        self.assertEqual(effective_display_settings(default),
                         {'format':'portrait','width':1080,'height':1920,'minimal_text':False})
        self.assertEqual(default,{'width':1920,'height':1080})
        with self.assertRaises(ValueError):effective_display_settings({'format':'custom','width':321,'height':720})

    def test_planning_receives_portrait_pixels_without_mutating_request(self):
        request={'format':'portrait','width':1920,'height':1080,'minimal_text':True}
        original=copy.deepcopy(request)
        actual=visual_settings(engine.prompt_for([],[],request))
        self.assertEqual((actual['format'],actual['width'],actual['height']),('portrait',1080,1920))
        self.assertEqual(request,original)

    def test_plan_review_receives_same_effective_canvas_and_new_context_version(self):
        request={'format':'portrait','width':1920,'height':1080,'minimal_text':True}
        original=copy.deepcopy(request)
        with tempfile.TemporaryDirectory() as folder:
            job=Path(folder);(job/'analysis').mkdir()
            with patch('studio_review_assets.prepare_review_assets',return_value=([],{})), \
                 patch.object(engine,'call_ai',return_value={'ready':True,'summary':'fixture','issues':[]}) as ai:
                result=engine.review_plan(job,{'blocks':[]},[],request)
        actual=visual_settings(ai.call_args.args[0])
        self.assertEqual((actual['width'],actual['height']),(1080,1920))
        self.assertEqual(result['_plan_review_context_version'],4)
        self.assertEqual(result['_effective_display_settings'],actual)
        self.assertEqual(request,original)

    def current_job(self, folder, settings=None):
        job,request,plan,review=setup_job(folder,{'auto_repair':False,**(settings or {})},PASSED)
        review['_effective_display_settings']=effective_display_settings(request)
        pv.save_json(job/'review.json',review)
        return job,request,plan,review

    def render_twice(self, job, verdict=PASSED):
        with patch.object(engine,'call_ai',return_value=copy.deepcopy(verdict)) as ai, \
             patch.object(engine,'export'), \
             patch('studio_recovery.render_with_recovery',side_effect=lambda j,r,p,s:p) as render, \
             patch('studio_delivery.export_formats'),patch('paper_video.log'):
            engine.execute(job,'render')
            engine.execute(job,'render')
        return ai,render

    def test_default_canvas_to_landscape_reviews_new_canvas_once_without_changing_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            job,request,plan,_=self.current_job(folder)
            request.update(format='landscape',width=1600,height=1000)
            pv.save_json(job/'request.json',request)
            ai,render=self.render_twice(job)
            ai.assert_called_once()
            self.assertEqual(ai.call_args.args[-1],'_review')
            self.assertEqual(visual_settings(ai.call_args.args[0])['width'],1920)
            self.assertEqual(pv.load_json(job/'review.json')['_effective_display_settings'],effective_display_settings(request))
            self.assertEqual(pv.load_json(job/'analysis/studio_plan.json'),plan)
            self.assertEqual(pv.load_json(job/'request.json'),request)
            self.assertEqual(render.call_count,2)

    def test_inactive_custom_dimensions_do_not_expire_preset_review(self):
        with tempfile.TemporaryDirectory() as folder:
            job,request,_,review=self.current_job(folder,{'format':'portrait','width':1600,'height':1000})
            request.update(width=1920,height=1080)
            pv.save_json(job/'request.json',request)
            ai,render=self.render_twice(job)
            ai.assert_not_called()
            self.assertEqual(render.call_count,2)
            self.assertEqual(pv.load_json(job/'review.json'),review)
            self.assertEqual(pv.load_json(job/'request.json'),request)

    def test_actual_custom_size_and_presentation_changes_each_expire_review(self):
        cases=[({'format':'custom','width':1600,'height':1000},{'width':1800}),
               ({'minimal_text':False},{'minimal_text':True}),
               ({'media_fit':'contain'},{'media_fit':'cover'}),
               ({'layout_style':'editorial'},{'layout_style':'immersive'}),
               ({'show_sources':False},{'show_sources':True}),
               ({'show_disclosure':False},{'show_disclosure':True})]
        for original,changed in cases:
            with self.subTest(changed=changed),tempfile.TemporaryDirectory() as folder:
                job,request,_,_=self.current_job(folder,original)
                request.update(changed)
                pv.save_json(job/'request.json',request)
                ai,render=self.render_twice(job)
                ai.assert_called_once()
                self.assertEqual(pv.load_json(job/'review.json')['_effective_display_settings'],effective_display_settings(request))
                self.assertEqual(render.call_count,2)

    def test_missing_display_binding_refreshes_current_context_once_and_preserves_rejection(self):
        with tempfile.TemporaryDirectory() as folder:
            job,request,_,review=self.current_job(folder)
            review.pop('_effective_display_settings')
            self.assertEqual(review['_plan_review_context_version'],engine.PLAN_REVIEW_CONTEXT_VERSION)
            pv.save_json(job/'review.json',review)
            ai,render=self.render_twice(job,BLOCKED)
            ai.assert_called_once()
            render.assert_not_called()
            checked=pv.load_json(job/'review.json')
            self.assertEqual(checked['_effective_display_settings'],effective_display_settings(request))
            self.assertFalse(checked['ready'])
            self.assertEqual(checked['issues'],BLOCKED['issues'])
            self.assertEqual(pv.load_json(job/'job.json')['status'],'review_needed')

    def test_both_final_review_dimensions_receive_effective_canvas_and_keep_hash_rules(self):
        request={'format':'portrait','width':1920,'height':1080,'minimal_text':True}
        original=copy.deepcopy(request);fingerprint=render_settings_hash(request);prompts=[]
        with tempfile.TemporaryDirectory() as folder:
            job=Path(folder);(job/'output').mkdir();(job/'analysis/final_frames').mkdir(parents=True)
            (job/'output/video.mp4').write_bytes(b'fixture: decoder replaced only for prompt inspection')
            pv.save_json(job/'project.json',{'width':1080,'height':1920,'blocks':[]})
            pv.save_json(job/'output/run_report.json',{'duration_seconds':1,'resolution':'1080x1920'})
            pv.save_json(job/'output/rendered_shots.json',[{'start':0,'end':1}])
            pv.save_json(job/'analysis/final_frames/coverage.json',{'complete':True,'total_shots':1})
            frame=job/'analysis/final_frames/00.jpg';Image.new('RGB',(16,24),'white').save(frame)
            def ai(prompt,*args):
                prompts.append(prompt)
                return {'ready':True,'summary':'fixture','issues':[]}
            with patch('studio_delivery.review_frames',return_value=[{'time':0,'path':str(frame)}]):
                result,_=final_review(job,request,{'blocks':[]},[],ai,engine.REVIEW_SCHEMA)
        self.assertEqual(len(prompts),2)
        for prompt in prompts:
            actual=visual_settings(prompt)
            self.assertEqual((actual['format'],actual['width'],actual['height']),('portrait',1080,1920))
        self.assertEqual(result['settings_hash'],fingerprint)
        self.assertEqual(request,original)


if __name__=='__main__':unittest.main()
