"""Model-boundary regressions, not a substitute for an actual visual review.

The auditor fixtures express independently specified judgments. These tests
check settings/evidence delivery and release gating, never infer picture quality
from keywords, aspect ratios or shot kinds.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

import paper_video as pv
import studio_engine as engine
from studio_delivery import final_review


OK = {'ready': True, 'summary': 'Auditor response fixture, not a quality approval.', 'issues': []}


class VisualPolicyReviewTests(unittest.TestCase):
    def assert_renderer_visibility_contract(self,prompt):
        # This checks instructions crossing the model boundary, not whether
        # an image passes visual review by containing particular words.
        for rule in ('layout_style="immersive"', 'shot.label is not drawn',
                     'protect_content=true', 'chapter is not drawn',
                     'hidden metadata cannot restore missing axes, conditions or legends',
                     'Do not change the user-selected aspect ratio or layout'):
            self.assertIn(rule,prompt)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.job = Path(self.temp.name)
        for folder in ('analysis', 'output', 'analysis/final_frames'):
            (self.job / folder).mkdir(exist_ok=True)
        self.request = {'content_type': 'general', 'brief': 'Explain the observed event.',
                        'format': 'custom', 'width': 1600, 'height': 1000,
                        'layout_style': 'immersive', 'media_fit': 'contain',
                        'show_sources': False, 'show_disclosure': False, 'minimal_text': True}
        self.plan = {'blocks': [{'id': 'scope', 'narration': 'The report records the door opening twice.',
                                'evidence': [], 'shots': [{'kind': 'page', 'source_id': 'page1',
                                'bbox': [0, 0, 1, 1], 'duration': 4.2, 'label': 'Observed event'}]}]}
        self.frame = self.job / 'analysis/frame.png'
        canvas = Image.new('RGB', (960, 540), 'white')
        draw = ImageDraw.Draw(canvas)
        for row in range(28):
            draw.text((15, row * 18), 'This source paragraph describes events and related qualifications. ' * 2,
                      fill='black')
        canvas.save(self.frame)
        self.frames = [{'time': 2.1, 'label': 'Observed event', 'path': str(self.frame)}]
        self.timeline = [{'block_id': 'scope', 'kind': 'image', 'start': 0, 'end': 4.2,
                          'source_kind': 'page', 'file': str(self.frame)}]
        (self.job / 'output/video.mp4').write_bytes(b'video fixture; actual frame decoding is mocked')
        pv.save_json(self.job / 'project.json', {'width': 1600, 'height': 1000})
        pv.save_json(self.job / 'output/run_report.json', {'duration_seconds': 4.2})
        pv.save_json(self.job / 'output/rendered_shots.json', self.timeline)
        pv.save_json(self.job / 'analysis/final_frames/coverage.json', {'complete': True,
            'total_shots': 1, 'covered_shot_indices': [0], 'omitted_shot_indices': []})

    def tearDown(self):
        self.temp.cleanup()

    def final(self, auditor):
        with patch('studio_delivery.review_frames', return_value=self.frames):
            return final_review(self.job, self.request, self.plan, [], auditor, engine.REVIEW_SCHEMA)[0]

    def test_explicit_presentation_settings_reach_generation_and_every_auditor(self):
        for minimal in (True, False):
            with self.subTest(minimal_text=minimal):
                self.request['minimal_text'] = minimal
                prompts = [engine.prompt_for([], [], self.request)]
                def reviewer(prompt, *args):
                    prompts.append(prompt)
                    return copy.deepcopy(OK)
                with patch('studio_engine.call_ai', side_effect=reviewer):
                    engine.review_plan(self.job, self.plan, [], self.request)
                self.final(reviewer)
                expected = {key: self.request[key] for key in ('format', 'width', 'height',
                    'layout_style', 'media_fit', 'show_sources', 'show_disclosure', 'minimal_text')}
                self.assertEqual(len(prompts), 4)
                for prompt in prompts:
                    self.assert_renderer_visibility_contract(prompt)
                    line = next((line for line in prompt.splitlines() if line.startswith('视觉设置：')), None)
                    self.assertIsNotNone(line, 'A model stage lost the actual presentation settings')
                    self.assertEqual(json.loads(line.split('：', 1)[1]), expected)

    def test_plan_and_timing_repairs_receive_visibility_rules_and_keep_rejected_candidate_out(self):
        from studio_plan_repair import repair_plan_once
        from studio_recovery import render_with_recovery
        from studio_revision import file_hash
        sources=[{'id':'figure','type':'image','name':'Source comparison','file':str(self.frame)}]
        plan={'title':'Comparison','summary':'Source conditions matter.','source_issues':[],'unused_sources':[],
              'blocks':[{'id':'scope','chapter':'Comparison','narration':'Compare the two recorded conditions.',
                         'evidence':[],'shots':[{'kind':'image','source_id':'figure','in':0,'duration':4,
                         'bbox':[0,0,1,1],'focus':[],'keep_full':False,'label':'Comparison','card_text':'','cue':''}]}]}
        engine.validate(plan,sources)
        candidate=copy.deepcopy(plan);candidate['blocks'][0]['shots'][0]['label']='Condition A / B'
        # The independent auditor fixture rejects a metadata-only repair.
        # It does not pretend the mock candidate restored visible source pixels.
        rejected={'ready':False,'summary':'The missing conditions remain absent.','issues':[
            {'severity':'error','block_id':'scope','reason':'The crop lacks the conditions needed for the promised comparison; the changed label is hidden in this layout.'}]}
        request={**self.request,'auto_repair':True,'script':plan['blocks'][0]['narration']}
        pv.save_json(self.job/'analysis/studio_plan.json',plan);pv.save_json(self.job/'review.json',rejected)
        original=(self.job/'analysis/studio_plan.json').read_bytes();prompts=[]
        def ai(prompt,images,analysis,request,schema,suffix):
            prompts.append((suffix,prompt))
            return copy.deepcopy(rejected if suffix=='_review' else candidate)
        with patch('studio_engine.call_ai',side_effect=ai),patch('paper_video.log'):
            repaired,review=repair_plan_once(self.job,plan,sources,request,rejected)
        self.assertEqual(repaired,plan);self.assertFalse(review['ready'])
        self.assertEqual((self.job/'analysis/studio_plan.json').read_bytes(),original)
        plan_prompts=list(prompts);prompts.clear()
        def needs_more_visuals(job):
            pv.save_json(job/'output/timing_recovery.json',{'status':'needs_more_visuals',
                'code':'insufficient_visual_duration','project_sha256':file_hash(job/'project.json'),
                'issues':[{'block_id':'scope','shot_index':0,'missing_seconds':1}]})
            raise RuntimeError('Measured timing diagnostic fixture')
        with patch('studio_engine.call_ai',side_effect=ai),patch('studio_engine.render',side_effect=needs_more_visuals),patch('paper_video.log'):
            with self.assertRaisesRegex(ValueError,'未通过内容审核'):
                # The first scenario has exhausted this input's shared quota.
                # A different explicit brief isolates timing prompt coverage;
                # cross-stage quota persistence is tested separately.
                render_with_recovery(self.job,{**request,'brief':'Inspect timing repair instructions.'},plan,sources)
        self.assertEqual((self.job/'analysis/studio_plan.json').read_bytes(),original)
        self.assertTrue(any(suffix.startswith('_plan_repair_') for suffix,_ in plan_prompts))
        self.assertEqual(prompts[0][0],'_timing_repair')
        self.assertEqual(sum(suffix=='_review' for suffix,_ in prompts),1)
        for _,prompt in plan_prompts+prompts:self.assert_renderer_visibility_contract(prompt)

    def test_documented_hidden_fields_match_actual_immersive_subtitles(self):
        from studio_render import write_subtitles
        work=self.job/'subtitle_work';work.mkdir()
        shot={'start':0,'end':4,'protect_content':True,'label':'HIDDEN_LABEL',
              'chapter':'HIDDEN_CHAPTER','source':'Source metadata'}
        write_subtitles(self.job/'output',work,'Arial','TITLE',[shot],[(0,4,'Visible narration')],
                        4,1600,1000,self.request)
        ass=(work/'captions.ass').read_text('utf-8')
        self.assertNotIn('HIDDEN_LABEL',ass);self.assertNotIn('HIDDEN_CHAPTER',ass)
        self.assertIn('Visible narration',ass)

    def test_source_page_violation_survives_review_and_blocks_release(self):
        issue = {'severity': 'error', 'block_id': 'scope', 'reason':
                 'At 2.1 seconds the frame is a full paragraph, displayed for 4.2 seconds; '
                 'the narration describes one event. This conflicts with the requested minimal-text '
                 'presentation and does not visibly show that event. Use a corresponding figure '
                 'or a focused explanation; preserve the source quote in the evidence file.'}
        # Deliberately inconsistent ready=true proves that an explicit content
        # error cannot be accidentally accepted by the host's final aggregator.
        auditor = Mock(side_effect=[{**OK, 'issues': [issue]}, copy.deepcopy(OK)])
        before = copy.deepcopy(self.plan)
        result = self.final(auditor)
        self.assertFalse(result['ready'])
        self.assertTrue(result['review_complete'])
        self.assertEqual(result['issues'], [issue])
        self.assertEqual(pv.load_json(self.job / 'output/final_review.json')['issues'], [issue])
        self.assertEqual(self.plan, before)
        for call in auditor.call_args_list:
            self.assertEqual(call.args[1], [self.frame])
            self.assertIn(json.dumps(self.timeline, ensure_ascii=False), call.args[0])
            self.assertIn(self.plan['blocks'][0]['narration'], call.args[0])

    def test_all_auditors_distinguish_sourced_reports_from_promised_visual_observation(self):
        prompts = []
        def reviewer(prompt, *args):
            prompts.append(prompt)
            return copy.deepcopy(OK)
        with patch('studio_engine.call_ai', side_effect=reviewer):
            engine.review_plan(self.job, self.plan, [], self.request)
        self.final(reviewer)
        self.assertEqual(len(prompts), 3)
        for prompt in prompts:
            self.assertIn('Distinguish a sourced factual report from a promise', prompt)
            self.assertIn('does not have to independently prove every narrated fact', prompt)
            self.assertIn('wrong subject or condition', prompt)
            self.assertIn('false causal claim', prompt)
            self.assertIn('promised observation or essential comparison', prompt)
        self.assertIn('旁白承诺观众从该画面直接观察或比较', prompts[0])
        self.assertIn('不服务必要的原句讲解', prompts[0])
        self.assertIn('无关素材当成证据时ready=false', prompts[0])

    def test_final_gate_keeps_error_and_not_ready_as_independent_blockers(self):
        warning = {'severity': 'warning', 'block_id': 'scope',
                   'reason': 'Related illustration has an unnecessary secondary panel.'}
        error = {'severity': 'error', 'block_id': 'scope',
                 'reason': 'The narration promises a directly visible comparison, '
                           'but the crop removes the baseline needed to interpret it.'}
        for verdict in ({**OK, 'issues': [error]},
                        {**OK, 'ready': False, 'issues': [warning]},
                        {**OK, 'issues': [warning]}):
            with self.subTest(verdict=verdict):
                result = self.final(Mock(side_effect=[copy.deepcopy(verdict), copy.deepcopy(OK)]))
                self.assertEqual(result['ready'], verdict['ready'] and verdict['issues'] == [warning])
                self.assertEqual(result['issues'], verdict['issues'])
                self.assertTrue(result['review_complete'])

    def test_static_wide_figure_is_not_rejected_by_file_type_or_shape(self):
        canvas = Image.new('RGB', (960, 240), 'white')
        draw = ImageDraw.Draw(canvas)
        draw.line((80, 30, 80, 190, 900, 190), fill='black', width=4)
        draw.rectangle((220, 70, 360, 190), fill='blue')
        draw.rectangle((560, 110, 700, 190), fill='green')
        draw.text((220, 205), 'Observed A', fill='black')
        draw.text((560, 205), 'Observed B', fill='black')
        canvas.save(self.frame)
        result = self.final(Mock(return_value=copy.deepcopy(OK)))
        self.assertTrue(result['ready'])
        self.assertEqual(result['issues'], [])


if __name__ == '__main__':
    unittest.main()
