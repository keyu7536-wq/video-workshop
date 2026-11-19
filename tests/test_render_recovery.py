import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import paper_video as pv
import studio_engine as engine
from studio_recovery import render_with_recovery
from studio_revision import file_hash
from studio_display import effective_display_settings
from test_studio import plan_fixture


class RenderRecoveryTests(unittest.TestCase):
    def test_three_measured_rounds_use_last_candidate_and_fresh_diagnostic(self):
        candidates=[]
        for number in range(1,4):
            candidate=copy.deepcopy(self.plan)
            candidate['blocks'][0]['shots'][0]['label']=f'修订{number}'
            candidates.append(candidate)
        def render(job):
            self.calls+=1
            if self.calls<=3:
                pv.save_json(job/'output/timing_recovery.json',{'status':'needs_more_visuals',
                    'code':'insufficient_visual_duration','project_sha256':file_hash(job/'project.json'),
                    'issues':[{'block_id':'b1','missing_seconds':5-self.calls}]})
                raise RuntimeError(f'gap {self.calls}')
        def export(job,plan,*args):pv.save_json(job/'project.json',{'plan_hash':engine.plan_hash(plan)})
        with patch.object(engine,'render',side_effect=render),patch.object(engine,'call_ai',side_effect=candidates) as ai,\
             patch.object(engine,'review_plan',return_value={'ready':True,'issues':[]}),\
             patch.object(engine,'export',side_effect=export),patch('paper_video.log'):
            result=render_with_recovery(self.job,{'auto_repair':True},self.plan,self.sources)
        self.assertEqual(result,candidates[-1]);self.assertEqual(self.calls,4);self.assertEqual(ai.call_count,3)
        for index,call in enumerate(ai.call_args_list):
            previous=self.plan if index==0 else candidates[index-1]
            self.assertIn(json.dumps(previous,ensure_ascii=False),call.args[0])
            self.assertIn(f'"missing_seconds": {4-index}',call.args[0])

    def test_exhausted_measured_repairs_restore_original_plan_project_and_media(self):
        originals={name:(self.job/name).read_bytes() for name in ('project.json','analysis/studio_plan.json','review.json')}
        for name in ('output/video.mp4','output/narration.wav','assets/visual_00_00.png'):
            (self.job/name).write_bytes(b'previous usable media');originals[name]=b'previous usable media'
        def render(job):
            self.calls+=1
            (job/'output/narration.wav').write_bytes(b'new incomplete narration')
            pv.save_json(job/'output/timing_recovery.json',{'status':'needs_more_visuals',
                'code':'insufficient_visual_duration','project_sha256':file_hash(job/'project.json'),
                'issues':[{'block_id':'b1','missing_seconds':4}]})
            raise RuntimeError('still needs more visuals')
        def export(job,plan,*args):
            pv.save_json(job/'project.json',{'candidate':self.calls})
            (job/'assets/visual_00_00.png').write_bytes(b'candidate image')
        fixed=copy.deepcopy(self.plan);fixed['summary']='candidate'
        with patch.object(engine,'render',side_effect=render),patch.object(engine,'call_ai',return_value=fixed) as ai,\
             patch.object(engine,'review_plan',return_value={'ready':True,'issues':[]}),\
             patch.object(engine,'export',side_effect=export),patch('paper_video.log'):
            with self.assertRaisesRegex(RuntimeError,'still needs more visuals|3'):
                render_with_recovery(self.job,{'auto_repair':True},self.plan,self.sources)
        self.assertEqual(ai.call_count,3);self.assertEqual(self.calls,4)
        for name,original in originals.items():self.assertEqual((self.job/name).read_bytes(),original,name)
        self.assertTrue((self.job/'output/timing_recovery.json').is_file())

    def test_other_stage_consumption_leaves_only_one_timing_generation(self):
        from studio_repair_budget import reserve_attempt,finish_attempt,ensure_budget
        request={'auto_repair':True}
        for stage in ('plan','final'):
            ticket=reserve_attempt(self.job,self.plan,self.sources,request,stage)
            finish_attempt(self.job,ticket,'rejected',candidate=self.plan,candidate_validated=True)
        def render(job):
            self.calls+=1
            pv.save_json(job/'output/timing_recovery.json',{'status':'needs_more_visuals',
                'code':'insufficient_visual_duration','project_sha256':file_hash(job/'project.json'),
                'issues':[{'block_id':'b1','missing_seconds':4}]})
            raise RuntimeError('measured shortage remains')
        with patch.object(engine,'render',side_effect=render),patch.object(engine,'call_ai',return_value=copy.deepcopy(self.plan)) as ai,\
             patch.object(engine,'review_plan',return_value={'ready':True,'issues':[]}),\
             patch.object(engine,'export'),patch('paper_video.log'):
            with self.assertRaisesRegex(RuntimeError,'measured shortage'):
                render_with_recovery(self.job,request,self.plan,self.sources)
            self.assertEqual(ai.call_count,1)
            with self.assertRaisesRegex(RuntimeError,'measured shortage'):
                render_with_recovery(self.job,request,self.plan,self.sources)
            self.assertEqual(ai.call_count,1,'Another click must not reset the shared quota')
        self.assertEqual(ensure_budget(self.job,self.plan,self.sources,request)['used'],3)

    def test_unavailable_timing_candidate_is_only_reviewed_again_without_generation(self):
        from studio_repair_budget import ensure_budget
        request={'auto_repair':True};fixed=copy.deepcopy(self.plan);fixed['summary']='Retained candidate'
        original=(self.job/'analysis/studio_plan.json').read_bytes()
        unavailable={'ready':False,'status':'unavailable','review_complete':False,'issues':[]}
        with patch.object(engine,'render',side_effect=self.shortage),patch.object(engine,'call_ai',return_value=fixed) as ai,\
             patch.object(engine,'review_plan',return_value=unavailable) as review,patch('paper_video.log'):
            with self.assertRaisesRegex(RuntimeError,'审核暂不可用'):
                render_with_recovery(self.job,request,self.plan,self.sources)
        self.assertEqual(ai.call_count,1);self.assertEqual(review.call_count,1)
        self.assertEqual((self.job/'analysis/studio_plan.json').read_bytes(),original)
        budget=ensure_budget(self.job,self.plan,self.sources,request)
        self.assertEqual(budget['used'],1)
        baseline=Path(budget['attempts'][0]['folder'])/'plan_before.json'
        saved_baseline=baseline.read_bytes();changed=copy.deepcopy(self.plan)
        changed['blocks'][0]['narration']='Tampered protected baseline.'
        pv.save_json(baseline,changed)
        with patch.object(engine,'call_ai') as ai,patch.object(engine,'review_plan') as review:
            with self.assertRaisesRegex(ValueError,'保护基线已改变'):
                render_with_recovery(self.job,request,self.plan,self.sources)
        ai.assert_not_called();review.assert_not_called()
        baseline.write_bytes(saved_baseline)
        with patch.object(engine,'render') as render,patch.object(engine,'call_ai') as ai,\
             patch.object(engine,'review_plan',return_value={'ready':True,'issues':[]}) as review,\
             patch.object(engine,'export'),patch('paper_video.log'):
            result=render_with_recovery(self.job,request,self.plan,self.sources)
        self.assertEqual(result,fixed);ai.assert_not_called();review.assert_called_once();render.assert_called_once()
        self.assertEqual(ensure_budget(self.job,result,self.sources,request)['used'],1)

    def test_repeated_identical_rejected_candidate_does_not_reroll_its_review(self):
        fixed=copy.deepcopy(self.plan);fixed['summary']='Rejected timing candidate'
        rejection={'ready':False,'issues':[{'severity':'error','block_id':'b1','reason':'unrelated picture'}]}
        with patch.object(engine,'render',side_effect=self.shortage),patch.object(engine,'call_ai',return_value=fixed) as ai,\
             patch.object(engine,'review_plan',return_value=rejection) as review,patch('paper_video.log'):
            with self.assertRaisesRegex(ValueError,'未通过内容审核'):
                render_with_recovery(self.job,{'auto_repair':True},self.plan,self.sources)
        self.assertEqual(ai.call_count,3);review.assert_called_once();self.assertEqual(self.calls,1)

    def test_malformed_responses_can_be_corrected_within_the_same_budget(self):
        fixed=copy.deepcopy(self.plan)
        with patch.object(engine,'render',side_effect=self.shortage),\
             patch.object(engine,'call_ai',side_effect=[ValueError('invalid JSON'),{'blocks':None},fixed]) as ai,\
             patch.object(engine,'review_plan',return_value={'ready':True,'issues':[]}) as review,\
             patch.object(engine,'export'),patch('paper_video.log'):
            result=render_with_recovery(self.job,{'auto_repair':True},self.plan,self.sources)
        self.assertEqual(result,self.plan);self.assertEqual(ai.call_count,3);review.assert_called_once()
        self.assertEqual(self.calls,2)
        self.assertIn('invalid JSON',ai.call_args_list[1].args[0])
        self.assertIn('blocks',ai.call_args_list[2].args[0])

    def test_transient_generation_failure_stops_without_spending_all_rounds(self):
        from studio_repair_budget import ensure_budget
        request={'auto_repair':True}
        with patch.object(engine,'render',side_effect=self.shortage),\
             patch.object(engine,'call_ai',side_effect=TimeoutError('temporary service timeout')) as ai,\
             patch.object(engine,'review_plan') as review,patch('paper_video.log'):
            with self.assertRaisesRegex(TimeoutError,'timeout'):
                render_with_recovery(self.job,request,self.plan,self.sources)
        ai.assert_called_once();review.assert_not_called()
        self.assertEqual(ensure_budget(self.job,self.plan,self.sources,request)['used'],1)

    def test_nested_final_timing_pending_resumes_its_own_protected_narration(self):
        from studio_final_repair import repair_final_video
        from studio_repair_budget import ensure_budget
        request={'auto_repair':True,'final_review':True,'content_type':'general'}
        for name,value in (('request.json',request),('analysis/sources.json',self.sources),
                           ('analysis/images.json',[]),('job.json',{'status':'review_needed'})):
            pv.save_json(self.job/name,value)
        (self.job/'output/video.mp4').write_bytes(b'previous valid video')
        before={name:(self.job/name).read_bytes() for name in
                ('analysis/studio_plan.json','project.json','review.json','output/video.mp4')}
        final={'ready':False,'status':'completed','review_complete':True,'round':1,
               'issues':[{'severity':'error','block_id':'b1','reason':'Explain the missing relation.'}]}
        pv.save_json(self.job/'output/final_review.json',final)
        candidate=copy.deepcopy(self.plan)
        # General content revision may change generated narration. The nested
        # timing-only revision must preserve this newer narration exactly.
        candidate['blocks'][0]['narration']+=' More explanation.'
        candidate['blocks'][0]['shots'][0]['label']='Final candidate'
        timed=copy.deepcopy(candidate);timed['blocks'][0]['shots'][0]['label']='Timing candidate'
        unavailable={'ready':False,'status':'unavailable','review_complete':False,'issues':[]}
        def export(job,plan,*args):pv.save_json(job/'project.json',{'plan_hash':engine.plan_hash(plan)})
        with patch.object(engine,'call_ai',side_effect=[candidate,timed]) as ai,\
             patch.object(engine,'review_plan',side_effect=[{'ready':True,'issues':[]},unavailable]),\
             patch.object(engine,'export',side_effect=export),patch.object(engine,'render',side_effect=self.shortage),\
             patch('paper_video.log'):
            result,_=repair_final_video(self.job,request,self.plan,self.sources,final)
        self.assertEqual(result,self.plan);self.assertEqual(ai.call_count,2)
        self.assertEqual(before,{name:(self.job/name).read_bytes() for name in before})
        budget=ensure_budget(self.job,self.plan,self.sources,request)
        self.assertEqual([(a['stage'],a['status']) for a in budget['attempts']],
                         [('final','render_failed'),('timing','review_unavailable')])
        def render(job):(job/'output/video.mp4').write_bytes(b'accepted resumed video')
        with patch.object(engine,'call_ai') as ai,patch.object(engine,'review_plan',return_value={'ready':True,'issues':[]}) as reviewer,\
             patch.object(engine,'export',side_effect=export),patch.object(engine,'render',side_effect=render) as rendered,\
             patch('studio_delivery.final_review',return_value=({**final,'ready':True,'issues':[]},[])),\
             patch('studio_delivery.export_formats'),patch('paper_video.log'):
            engine.execute(self.job,'render')
        ai.assert_not_called();reviewer.assert_called_once();rendered.assert_called_once()
        self.assertEqual(pv.load_json(self.job/'analysis/studio_plan.json'),timed)
        self.assertEqual(pv.load_json(self.job/'job.json')['status'],'completed')
        self.assertEqual((self.job/'output/video.mp4').read_bytes(),b'accepted resumed video')
        self.assertEqual(ensure_budget(self.job,timed,self.sources,request)['used'],2)

    def test_direct_engine_run_is_busy_before_network_or_model_work(self):
        pv.save_json(self.job/'job.json',{'status':'prepared'})
        pv.save_json(self.job/'request.json',{})
        def observe(*args):
            state=pv.load_json(self.job/'job.json')
            self.assertEqual(state['status'],'processing')
            self.assertEqual(state['last_action'],'prepare')
            raise RuntimeError('simulated upstream failure')
        with patch.object(engine,'collect',side_effect=observe),patch('paper_video.log'):
            with self.assertRaisesRegex(RuntimeError,'simulated upstream failure'):
                engine.execute(self.job,'prepare')
        self.assertEqual(pv.load_json(self.job/'job.json')['status'],'failed')

    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.job=Path(self.temporary.name)
        for directory in ('analysis','output','assets'):(self.job/directory).mkdir()
        self.plan=plan_fixture();engine.validate(self.plan,[{'id':'article','type':'text','text':self.plan['blocks'][0]['narration']}])
        self.sources=[{'id':'article','type':'text','text':self.plan['blocks'][0]['narration'],'name':'article'}]
        pv.save_json(self.job/'project.json',{'fixture':True})
        pv.save_json(self.job/'analysis/studio_plan.json',self.plan)
        pv.save_json(self.job/'review.json',{'ready':True,'issues':[],'_plan_hash':engine.plan_hash(self.plan)})
        self.calls=0

    def shortage(self,job):
        self.calls+=1
        if self.calls==1:
            pv.save_json(job/'output/timing_recovery.json',{'status':'needs_more_visuals','code':'insufficient_visual_duration',
                'project_sha256':file_hash(job/'project.json'),'issues':[{'block_id':'b1','missing_seconds':4}]})
            raise RuntimeError('演示只有3秒，画面还缺4秒。')

    def test_program_repairs_shots_once_for_either_model_backend(self):
        for backend in ('codex','api'):
            with self.subTest(backend=backend):
                self.calls=0;fixed=copy.deepcopy(self.plan);fixed['blocks'][0]['shots'][0]['label']='问题示意'
                with patch.object(engine,'render',side_effect=self.shortage),patch.object(engine,'call_ai',return_value=fixed) as ai,\
                     patch.object(engine,'review_plan',return_value={'ready':True,'issues':[]}),patch.object(engine,'export') as export:
                    result=render_with_recovery(self.job,{'backend':backend},self.plan,self.sources)
                self.assertEqual(self.calls,2);self.assertEqual(ai.call_count,1);export.assert_called_once()
                self.assertEqual(ai.call_args.args[3]['backend'],backend)
                self.assertEqual(result['blocks'][0]['narration'],self.plan['blocks'][0]['narration'])
                self.assertTrue((self.job/'analysis/timing_recovery/plan_before.json').is_file())

    def test_narration_changes_are_rejected_before_saving_or_second_render(self):
        fixed=copy.deepcopy(self.plan);fixed['blocks'][0]['narration']='模型擅自缩短了原文。'
        with patch.object(engine,'render',side_effect=self.shortage),patch.object(engine,'call_ai',return_value=fixed),\
             patch.object(engine,'export') as export:
            with self.assertRaisesRegex(ValueError,'修改了原文稿'):
                render_with_recovery(self.job,{},self.plan,self.sources)
        self.assertEqual(self.calls,1);export.assert_not_called()
        self.assertEqual(pv.load_json(self.job/'analysis/studio_plan.json'),self.plan)

    def motion_plan(self):
        text='先介绍研究。随后展示运动。接着看比较。'
        self.sources=[{'id':'article','type':'text','text':text},
                      {'id':'motion','type':'video','duration_seconds':12,'sample_times':[7,8]}]
        first=copy.deepcopy(self.plan['blocks'][0]['shots'][0])
        first.update(duration=1,card_text='研究背景')
        motion={**first,'kind':'video','source_id':'motion','in':7,'duration':4,
                'keep_full':True,'card_text':'','cue':'展示运动'}
        last={**first,'card_text':'比较结果','cue':'接着看比较'}
        self.plan['blocks'][0].update(narration=text,evidence=[{'source_id':'article','quote':text}],
                                     shots=[first,motion,last])
        pv.save_json(self.job/'analysis/studio_plan.json',self.plan)
        return text

    def test_measured_candidates_reach_host_repair_and_resolve_with_same_complete_action(self):
        import make_video as mv
        import studio_render as sr
        text=self.motion_plan()
        events=[{'type':'WordBoundary','text':token,'offset':int(start*10_000_000),
                 'duration':2_000_000} for token,start in
                [('先介绍研究',0),('随后',1),('展示运动',2.2),('接着看比较',5)]]
        words=mv.words_with_positions(text,events)
        cues=mv.measured_cue_candidates(text,words)
        fixed=copy.deepcopy(self.plan)
        fixed['blocks'][0]['shots'][1]['cue']=next(c['cue'] for c in cues if c['start']==1)
        diagnostic={'status':'needs_more_visuals','code':'insufficient_visual_duration',
                    'project_sha256':file_hash(self.job/'project.json'),
                    'issues':[{'block_id':'b1','reason':'complete_clip_exceeds_cue_window',
                               'required_source_duration':4,'available_cue_window':2.8}],
                    'blocks':[{'id':'b1','start':0,'end':8.3,'cue_time_basis':'block_local',
                               'cue_candidates':cues}]}
        def render(job):
            self.calls+=1
            if self.calls==1:
                pv.save_json(job/'output/timing_recovery.json',diagnostic)
                raise RuntimeError('complete action does not fit')
            saved=pv.load_json(job/'analysis/studio_plan.json')
            selected=sr.choose_shots(saved['blocks'][0],8.3,words,30)
            self.assertEqual((selected[1]['local_start'],selected[2]['local_start']),(1,5))
            self.assertEqual((selected[1]['in'],selected[1]['duration'],selected[1]['keep_full']),(7,4,True))
        with patch.object(engine,'render',side_effect=render),patch.object(engine,'call_ai',return_value=fixed) as ai, \
             patch.object(engine,'review_plan',return_value={'ready':True,'issues':[]}) as review, \
             patch.object(engine,'export'),patch('paper_video.log'):
            result=render_with_recovery(self.job,{'script':text},self.plan,self.sources)
        self.assertEqual(self.calls,2);ai.assert_called_once();review.assert_called_once()
        self.assertIn(json.dumps(diagnostic,ensure_ascii=False),ai.call_args.args[0])
        self.assertEqual(result['blocks'][0]['narration'],text)
        self.assertEqual(result['blocks'][0]['shots'][2]['cue'],'接着看比较')

    def test_recovery_cannot_disable_or_shorten_a_complete_source_range(self):
        text=self.motion_plan()
        for change in ({'keep_full':False},{'duration':3},{'in':7.5},
                       {'kind':'card','source_id':'','keep_full':False,'card_text':'静态解释'}):
            with self.subTest(change=change):
                self.calls=0;fixed=copy.deepcopy(self.plan)
                fixed['blocks'][0]['shots'][1].update(change)
                with patch.object(engine,'render',side_effect=self.shortage), \
                     patch.object(engine,'call_ai',return_value=fixed) as ai, \
                     patch.object(engine,'review_plan') as review,patch.object(engine,'export') as export, \
                     patch('paper_video.log'):
                    with self.assertRaisesRegex(ValueError,'截短了要求完整保留的动作'):
                        # Each subcase is a different user input, not four clicks
                        # attempting to evade the same three-generation quota.
                        render_with_recovery(self.job,{'script':text,'brief':str(change)},self.plan,self.sources)
                self.assertEqual(self.calls,1);ai.assert_called_once()
                review.assert_not_called();export.assert_not_called()
                self.assertEqual(pv.load_json(self.job/'analysis/studio_plan.json'),self.plan)

    def test_one_candidate_shot_cannot_replace_two_required_occurrences(self):
        text=self.motion_plan()
        original=self.plan['blocks'][0]['shots']
        original[2]={**original[1],'cue':'接着看比较'}
        pv.save_json(self.job/'analysis/studio_plan.json',self.plan)
        fixed=copy.deepcopy(self.plan);fixed['blocks'][0]['shots'].pop()
        with patch.object(engine,'render',side_effect=self.shortage), \
             patch.object(engine,'call_ai',return_value=fixed),patch.object(engine,'review_plan') as review, \
             patch.object(engine,'export') as export,patch('paper_video.log'):
            with self.assertRaisesRegex(ValueError,'截短了要求完整保留的动作'):
                render_with_recovery(self.job,{'script':text},self.plan,self.sources)
        self.assertEqual(self.calls,1);review.assert_not_called();export.assert_not_called()

    def test_rejected_repair_restores_previous_review(self):
        before=(self.job/'review.json').read_bytes()
        def reject(*args):
            result={'ready':False,'issues':[{'severity':'error','reason':'画面无关'}]}
            pv.save_json(self.job/'review.json',result);return result
        with patch.object(engine,'render',side_effect=self.shortage),patch.object(engine,'call_ai',return_value=copy.deepcopy(self.plan)),\
             patch.object(engine,'review_plan',side_effect=reject):
            with self.assertRaisesRegex(ValueError,'未通过内容审核'):
                render_with_recovery(self.job,{},self.plan,self.sources)
        self.assertEqual((self.job/'review.json').read_bytes(),before)
        self.assertEqual(self.calls,1)

    def test_rejected_candidate_transfers_only_error_on_exactly_unchanged_block(self):
        from studio_story import STORY_POLICY_VERSION
        original_block=self.plan['blocks'][0]
        second=copy.deepcopy(original_block);second['id']='reading'
        self.plan['blocks'].append(second)
        pv.save_json(self.job/'analysis/studio_plan.json',self.plan)
        accepted={'ready':True,'issues':[],'_plan_hash':engine.plan_hash(self.plan),
                  '_content_type':'auto','_story_framework':'auto','_story_policy_version':STORY_POLICY_VERSION,
                  '_plan_review_context_version':engine.PLAN_REVIEW_CONTEXT_VERSION,
                  '_effective_display_settings':effective_display_settings({})}
        pv.save_json(self.job/'review.json',accepted)
        fixed=copy.deepcopy(self.plan);fixed['blocks'][0]['shots'][0]['label']='Adjusted opening'
        eligible={'severity':'error','block_id':'reading','reason':'This result table does not show training examples.'}
        issues=[eligible,{'severity':'error','block_id':'b1','reason':'Changed opening picture is unrelated.'},
                {'severity':'error','block_id':'','reason':'Global transition concern.'}]
        rejected={'ready':False,'issues':issues,'_plan_hash':engine.plan_hash(fixed)}
        with patch.object(engine,'render',side_effect=self.shortage),patch.object(engine,'call_ai',return_value=fixed), \
             patch.object(engine,'review_plan',return_value=rejected),patch('paper_video.log'):
            with self.assertRaisesRegex(ValueError,'未通过内容审核'):
                render_with_recovery(self.job,{},self.plan,self.sources)
        active=pv.load_json(self.job/'review.json')
        self.assertFalse(active['ready']);self.assertEqual(active['issues'],[eligible])
        self.assertEqual(active['_plan_hash'],engine.plan_hash(self.plan))
        self.assertFalse(active['original_plan_fully_rereviewed'])
        proof=pv.load_json(self.job/'analysis/timing_recovery/unchanged_block_feedback.json')
        self.assertEqual(proof['original_plan_hash'],engine.plan_hash(self.plan))
        self.assertEqual(proof['candidate_plan_hash'],engine.plan_hash(fixed))
        self.assertEqual(proof['unchanged_blocks'][0]['block_id'],'reading')
        self.assertEqual(pv.load_json(self.job/'analysis/timing_recovery/rejected_review.json'),rejected)
        self.assertEqual(pv.load_json(self.job/'analysis/studio_plan.json'),self.plan)
        # The next render must now repair the content before reaching encoding.
        pv.save_json(self.job/'request.json',{'auto_repair':True})
        pv.save_json(self.job/'job.json',{'status':'failed'})
        pv.save_json(self.job/'analysis/sources.json',self.sources)
        with patch('studio_plan_repair.repair_plan_once',return_value=(self.plan,active)) as repair, \
             patch.object(engine,'export'),patch.object(engine,'render') as render,patch('paper_video.log'):
            engine.execute(self.job,'render')
        state=pv.load_json(self.job/'job.json')
        self.assertEqual((state['status'],state['stage']),('review_needed','plan_review'))
        self.assertIsNone(state['error'])
        repair.assert_called_once();self.assertEqual(repair.call_args.args[4]['issues'],[eligible])
        render.assert_not_called()

    def test_unrelated_failure_keeps_actual_error_and_never_calls_model(self):
        pv.save_json(self.job/'output/timing_recovery.json',{'status':'needs_more_visuals','code':'insufficient_visual_duration',
            'project_sha256':file_hash(self.job/'project.json'),'issues':[{}]})
        with patch.object(engine,'render',side_effect=RuntimeError('FFmpeg is unavailable')),patch.object(engine,'call_ai') as ai:
            with self.assertRaisesRegex(RuntimeError,'FFmpeg is unavailable'):
                render_with_recovery(self.job,{},self.plan,self.sources)
        ai.assert_not_called()

    def test_failed_second_attempt_is_not_retried_in_a_loop(self):
        def render(job):
            self.shortage(job)
            raise RuntimeError('second render still needs visuals')
        with patch.object(engine,'render',side_effect=render),patch.object(engine,'call_ai',return_value=copy.deepcopy(self.plan)) as ai,\
             patch.object(engine,'review_plan',return_value={'ready':True,'issues':[]}),patch.object(engine,'export'):
            with self.assertRaisesRegex(RuntimeError,'second render'):
                render_with_recovery(self.job,{},self.plan,self.sources)
        self.assertEqual(self.calls,2);self.assertEqual(ai.call_count,1)

    def receipt(self,request=None):
        from studio_revision import render_settings_hash
        pv.save_json(self.job/'request.json',request or {})
        pv.save_json(self.job/'analysis/sources.json',self.sources)
        pv.save_json(self.job/'job.json',{'status':'review_needed','engine':'studio'})
        (self.job/'output/video.mp4').write_bytes(b'unchanged encoded fixture')
        pv.save_json(self.job/'output/render_receipt.json',{
            'plan_hash':engine.plan_hash(self.plan),'settings_hash':render_settings_hash(request or {}),
            'project_sha256':file_hash(self.job/'project.json'),'video_sha256':file_hash(self.job/'output/video.mp4')})

    def test_retry_review_uses_existing_video_without_encoding(self):
        self.receipt();before=(self.job/'output/video.mp4').read_bytes()
        with patch.object(engine,'render') as render,patch('studio_delivery.final_review',return_value=({'ready':True},[])) as review,\
             patch('studio_delivery.export_formats'):
            engine.execute(self.job,'review')
        render.assert_not_called();review.assert_called_once()
        self.assertEqual((self.job/'output/video.mp4').read_bytes(),before)
        self.assertEqual(pv.load_json(self.job/'job.json')['status'],'completed')

    def test_retry_review_refuses_changed_voice_or_video(self):
        for change in ('voice','video'):
            with self.subTest(change=change):
                self.receipt()
                if change=='voice':pv.save_json(self.job/'request.json',{'voice':'different'})
                else:
                    video=self.job/'output/video.mp4';before=video.stat()
                    video.write_bytes(b'different encoded content')
                    # Reproduce same-size writes whose modification timestamp
                    # is retained/coarsened, without relying on CI timing.
                    os.utime(video,ns=(before.st_atime_ns,before.st_mtime_ns))
                    self.assertEqual((video.stat().st_size,video.stat().st_mtime_ns),
                                     (before.st_size,before.st_mtime_ns))
                with patch('studio_delivery.final_review') as review:
                    with self.assertRaisesRegex(ValueError,'与成片不一致'):engine.execute(self.job,'review')
                review.assert_not_called()

    def test_export_failure_can_retry_export_without_encoding(self):
        self.receipt();pv.save_json(self.job/'output/final_review.json',{'ready':True})
        with patch('studio_delivery.export_formats',side_effect=RuntimeError('temporary export failure')):
            with self.assertRaisesRegex(RuntimeError,'temporary export'):engine.execute(self.job,'export')
        self.assertEqual(pv.load_json(self.job/'job.json')['failed_action'],'export')
        with patch.object(engine,'render') as render,patch('studio_delivery.export_formats'):
            engine.execute(self.job,'export')
        render.assert_not_called()
        result=pv.load_json(self.job/'job.json')
        self.assertEqual(result['status'],'completed');self.assertNotIn('failed_action',result)

    def test_export_rejects_a_video_that_no_longer_matches_its_receipt(self):
        from studio_server import snapshot
        self.receipt();pv.save_json(self.job/'job.json',{'engine':'studio','status':'completed'})
        (self.job/'output/video.mp4').write_bytes(b'changed after generation')
        self.assertFalse(snapshot(self.job)['can_export'])
        with patch('studio_delivery.export_formats') as export:
            with self.assertRaisesRegex(ValueError,'与生成记录不一致'):engine.execute(self.job,'export')
        export.assert_not_called()

    def test_rejected_content_revision_keeps_accepted_plan_review(self):
        self.receipt({'final_review':True,'auto_repair':True})
        pv.save_json(self.job/'analysis/images.json',[])
        candidate=copy.deepcopy(self.plan);candidate['summary']='候选修订'
        accepted={'ready':True,'summary':'original accepted','issues':[],'_plan_hash':engine.plan_hash(self.plan)}
        def reviewer(job,plan,*args):
            result=({'ready':False,'summary':'candidate rejected','issues':[{'severity':'error','reason':'不相关画面'}]}
                if plan['summary']=='候选修订' else accepted)
            pv.save_json(job/'review.json',result);return result
        final={'ready':False,'issues':[{'severity':'error','reason':'需要调整画面'}]}
        with patch.object(engine,'review_plan',side_effect=reviewer),patch.object(engine,'export'),\
             patch('studio_recovery.render_with_recovery',return_value=self.plan),\
             patch('studio_delivery.final_review',return_value=(final,[])),patch('studio_delivery.export_formats'),\
             patch.object(engine,'call_ai',return_value=candidate):
            engine.execute(self.job,'render')
        self.assertEqual(pv.load_json(self.job/'analysis/studio_plan.json'),self.plan)
        self.assertEqual(pv.load_json(self.job/'review.json'),accepted)


if __name__=='__main__':unittest.main()
