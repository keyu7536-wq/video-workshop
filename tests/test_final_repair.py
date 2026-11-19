"""Final-video repairs share a persisted cap and preserve usable artifacts."""
import copy
import json
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import paper_video as pv
import studio_engine as engine
from studio_final_repair import repair_final_video
from studio_repair_budget import reserve_attempt,finish_attempt,ensure_budget
from tests.test_plan_repair import setup_job,SOURCES,PASSED


class FinalRepairTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.job,self.request,self.plan,self.review=setup_job(temporary.name,{'final_review':True},PASSED)
        (self.job/'assets').mkdir()
        pv.save_json(self.job/'project.json',{'version':'original'})
        (self.job/'output/video.mp4').write_bytes(b'original encoded bytes')
        self.final={'ready':False,'status':'completed','review_complete':True,'round':1,
                    'issues':[{'severity':'error','block_id':'b1','reason':'original visible error'}]}
        pv.save_json(self.job/'output/final_review.json',self.final)
        self.before={name:(self.job/name).read_bytes() for name in ('project.json','analysis/studio_plan.json','review.json','output/video.mp4')}

    def candidate(self,index):
        value=copy.deepcopy(self.plan);value['blocks'][0]['shots'][0]['label']=f'candidate {index}'
        return value

    def patches(self,generate,audit):
        stack=ExitStack();self.addCleanup(stack.close)
        ai=stack.enter_context(patch.object(engine,'call_ai',side_effect=generate))
        def review(job,plan,*args):
            result=copy.deepcopy(PASSED);pv.save_json(job/'review.json',result);return result
        stack.enter_context(patch.object(engine,'review_plan',side_effect=review))
        def export(job,plan,*args):pv.save_json(job/'project.json',{'candidate':plan['blocks'][0]['shots'][0]['label']})
        stack.enter_context(patch.object(engine,'export',side_effect=export))
        def render(job,request,plan,sources):
            # The downstream stage sees this candidate without getting a new
            # allowance merely because the host changed its own plan.
            state=ensure_budget(job,plan,sources,request)
            self.assertLessEqual(state['remaining'],2)
            (job/'output/video.mp4').write_bytes(plan['blocks'][0]['shots'][0]['label'].encode())
            return plan
        rendered=stack.enter_context(patch('studio_recovery.render_with_recovery',side_effect=render))
        checked=stack.enter_context(patch('studio_delivery.final_review',side_effect=audit))
        stack.enter_context(patch('paper_video.log'))
        return ai,rendered,checked

    def test_third_revision_uses_latest_feedback_then_keeps_accepted_media(self):
        prompts=[];counter=[0]
        def generate(prompt,*args):prompts.append(prompt);return self.candidate(len(prompts))
        def audit(job,request,plan,*args):
            counter[0]+=1
            result={**self.final,'ready':counter[0]==3,'round':counter[0]+1,
                'issues':[] if counter[0]==3 else [{'severity':'error','block_id':'b1','reason':f'visible error {counter[0]}'}]}
            pv.save_json(job/'output/final_review.json',result);return result,[]
        ai,rendered,_=self.patches(generate,audit)
        accepted,final=repair_final_video(self.job,self.request,self.plan,SOURCES,self.final)
        self.assertTrue(final['ready']);self.assertEqual(accepted,self.candidate(3))
        self.assertEqual(ai.call_count,3);self.assertEqual(rendered.call_count,3)
        self.assertIn('visible error 1',prompts[1]);self.assertIn('candidate 1',prompts[1])
        self.assertIn('visible error 2',prompts[2]);self.assertIn('candidate 2',prompts[2])
        self.assertEqual((self.job/'output/video.mp4').read_bytes(),b'candidate 3')
        self.assertEqual(ensure_budget(self.job,accepted,SOURCES,self.request)['remaining'],0)

    def test_three_rejections_restore_original_and_repeated_click_does_not_reset(self):
        counter=[0]
        def generate(*args):counter[0]+=1;return self.candidate(counter[0])
        ai,_,_=self.patches(generate,lambda *args:({**self.final,'issues':[{'severity':'error','block_id':'b1','reason':f'error {counter[0]}'}]},[]))
        for _ in range(2):
            value,final=repair_final_video(self.job,self.request,self.plan,SOURCES,self.final)
            self.assertEqual(value,self.plan);self.assertFalse(final['ready'])
            self.assertEqual(self.before,{name:(self.job/name).read_bytes() for name in self.before})
        self.assertEqual(ai.call_count,3)

    def test_two_prior_stage_attempts_leave_only_one_final_revision(self):
        for stage in ('schema','plan'):
            ticket=reserve_attempt(self.job,self.plan,SOURCES,self.request,stage)
            finish_attempt(self.job,ticket,'accepted',candidate=self.plan,candidate_validated=True)
        ai,_,_=self.patches(lambda *args:self.candidate(3),lambda *args:(self.final,[]))
        repair_final_video(self.job,self.request,self.plan,SOURCES,self.final)
        self.assertEqual(ai.call_count,1)
        self.assertEqual(ensure_budget(self.job,self.plan,SOURCES,self.request)['remaining'],0)

    def test_unavailable_final_review_resumes_same_encoded_candidate_without_generation(self):
        unavailable={**self.final,'ready':False,'status':'unavailable','review_complete':False,'unavailable_reason':'temporary service outage'}
        ai,rendered,checked=self.patches(lambda *args:self.candidate(1),lambda *args:(unavailable,[]))
        repair_final_video(self.job,self.request,self.plan,SOURCES,self.final)
        self.assertEqual(self.before,{name:(self.job/name).read_bytes() for name in self.before})
        checked.side_effect=lambda *args:({**self.final,'ready':True,'issues':[]},[])
        # The original-plan gate must not trap a pending later-stage review.
        pv.save_json(self.job/'review.json',{'ready':False,'issues':[{'severity':'error','block_id':'b1','reason':'original error'}]})
        with patch('studio_delivery.export_formats'):
            engine.execute(self.job,'render')
        accepted=pv.load_json(self.job/'analysis/studio_plan.json')
        self.assertEqual(pv.load_json(self.job/'job.json')['status'],'completed')
        self.assertEqual(accepted,self.candidate(1))
        self.assertEqual(ai.call_count,1);self.assertEqual(rendered.call_count,1)
        self.assertEqual(checked.call_count,2)
        self.assertEqual(ensure_budget(self.job,accepted,SOURCES,self.request)['remaining'],2)

    def test_pending_timing_review_is_routed_before_original_plan_rejection(self):
        candidate=self.candidate(1)
        ticket=reserve_attempt(self.job,self.plan,SOURCES,self.request,'timing')
        finish_attempt(self.job,ticket,'review_unavailable',candidate=candidate,candidate_validated=True)
        pv.save_json(self.job/'review.json',{'ready':False,'issues':[{'severity':'error','block_id':'b1','reason':'old rejected plan'}]})
        request={**self.request,'final_review':False};pv.save_json(self.job/'request.json',request)
        with patch('studio_recovery.render_with_recovery',return_value=candidate) as timing, \
             patch('studio_plan_repair.repair_plan_once') as plan_repair, \
             patch('studio_delivery.export_formats'),patch('paper_video.log'):
            engine.execute(self.job,'render')
        timing.assert_called_once();plan_repair.assert_not_called()
        self.assertEqual(pv.load_json(self.job/'job.json')['status'],'completed')

    def test_render_failure_restores_project_video_and_plan(self):
        ai,rendered,_=self.patches(lambda *args:self.candidate(1),lambda *args:({**self.final,'ready':True},[]))
        def failed(*args):
            (self.job/'output/video.mp4').write_bytes(b'partial broken encode')
            raise RuntimeError('encoder stopped')
        rendered.side_effect=failed
        accepted,final=repair_final_video(self.job,self.request,self.plan,SOURCES,self.final)
        self.assertEqual(accepted,self.plan);self.assertFalse(final['ready'])
        self.assertEqual(self.before,{name:(self.job/name).read_bytes() for name in self.before})
        self.assertEqual(ai.call_count,1)

    def test_bad_json_gets_a_bounded_correction_but_timeout_stops(self):
        for error,expected in ((json.JSONDecodeError('invalid model JSON','?',0),2),(TimeoutError('service timeout'),1)):
            with self.subTest(error=type(error).__name__),tempfile.TemporaryDirectory() as folder:
                job,request,value,_=setup_job(folder,{'final_review':True},PASSED)
                (job/'assets').mkdir();pv.save_json(job/'project.json',{})
                (job/'output/video.mp4').write_bytes(b'original')
                pv.save_json(job/'output/final_review.json',self.final)
                candidate=copy.deepcopy(value);candidate['blocks'][0]['shots'][0]['label']='修正后的图'
                with patch.object(engine,'call_ai',side_effect=[error,candidate]) as ai, \
                     patch.object(engine,'review_plan',return_value=copy.deepcopy(PASSED)), \
                     patch.object(engine,'export'),patch('studio_recovery.render_with_recovery',side_effect=lambda j,r,p,s:p), \
                     patch('studio_delivery.final_review',return_value=({**self.final,'ready':True,'issues':[]},[])), \
                     patch('paper_video.log'):
                    _,result=repair_final_video(job,request,value,SOURCES,self.final)
                self.assertEqual(ai.call_count,expected)
                self.assertEqual(result['ready'],expected==2)
                self.assertEqual(ensure_budget(job,value,SOURCES,request)['remaining'],3-expected)

    def test_engine_pending_resume_does_not_rereview_an_earlier_complete_rejection(self):
        from studio_revision import file_hash,render_settings_hash
        generations=iter([self.candidate(1),self.candidate(2),self.candidate(1)])
        audit_count=[0];plan_audits=[0]
        def audit(job,request,plan,*args):
            audit_count[0]+=1
            # A second call would simulate reviewer nondeterminism. The exact
            # already rejected candidate must never get that second chance.
            result={**self.final,'ready':audit_count[0]>1,
                    'issues':[] if audit_count[0]>1 else self.final['issues'],
                    'plan_hash':engine.plan_hash(plan),'settings_hash':render_settings_hash(request),
                    'video_sha256':file_hash(job/'output/video.mp4'),
                    'project_sha256':file_hash(job/'project.json')}
            pv.save_json(job/'output/final_review.json',result)
            return result,[]
        ai,rendered,checked=self.patches(lambda *args:next(generations),audit)
        def plan_review(job,plan,*args):
            plan_audits[0]+=1
            if plan_audits[0]==2:raise RuntimeError('review service unavailable')
            result=(dict(self.final) if plan_audits[0]==3 else copy.deepcopy(PASSED))
            result['_plan_hash']=engine.plan_hash(plan)
            pv.save_json(job/'review.json',result)
            return result
        with patch.object(engine,'review_plan',side_effect=plan_review),patch('studio_delivery.export_formats'):
            repair_final_video(self.job,self.request,self.plan,SOURCES,self.final)
            self.assertEqual(ensure_budget(self.job,self.plan,SOURCES,self.request)['used'],2)
            engine.execute(self.job,'render')
        self.assertEqual(ai.call_count,3)
        self.assertEqual(plan_audits[0],3)
        self.assertEqual(rendered.call_count,1)
        self.assertEqual(checked.call_count,1)
        self.assertEqual(pv.load_json(self.job/'job.json')['status'],'review_needed')
        self.assertEqual(self.before,{name:(self.job/name).read_bytes() for name in self.before})

    def test_saved_rejection_requires_bound_candidate_and_archived_media(self):
        from studio_final_repair import _saved_rejections
        from studio_revision import file_hash,render_settings_hash
        candidate=self.candidate(1);digest=engine.plan_hash(candidate)
        ticket=reserve_attempt(self.job,self.plan,SOURCES,self.request,'final')
        folder=Path(ticket['folder']);rendered=folder/'rendered'
        for name,data in (('analysis/studio_plan.json',candidate),('project.json',{'candidate':1})):
            (rendered/name).parent.mkdir(parents=True,exist_ok=True);pv.save_json(rendered/name,data)
        (rendered/'output').mkdir();(rendered/'output/video.mp4').write_bytes(b'candidate video')
        rejection={**self.final,'plan_hash':digest,'settings_hash':render_settings_hash(self.request),
                   'video_sha256':file_hash(rendered/'output/video.mp4'),
                   'project_sha256':file_hash(rendered/'project.json')}
        finish_attempt(self.job,ticket,'rejected',candidate=candidate,review=rejection,candidate_validated=True)
        self.assertEqual(_saved_rejections(self.job,self.plan,SOURCES,self.request)[digest],rejection)
        baseline={path:path.read_bytes() for path in folder.rglob('*') if path.is_file()}
        for change in ('candidate','plan_hash','settings_hash','incomplete','video','project'):
            with self.subTest(change=change):
                for path,data in baseline.items():path.write_bytes(data)
                if change=='candidate':pv.save_json(folder/'candidate.json',self.candidate(9))
                elif change in ('plan_hash','settings_hash'):
                    pv.save_json(folder/'candidate_review.json',{**rejection,change:'stale'})
                elif change=='incomplete':
                    pv.save_json(folder/'candidate_review.json',{**rejection,'review_complete':False})
                elif change=='video':(rendered/'output/video.mp4').write_bytes(b'changed media')
                else:pv.save_json(rendered/'project.json',{'candidate':'changed'})
                self.assertEqual(_saved_rejections(self.job,self.plan,SOURCES,self.request),{})


if __name__=='__main__':unittest.main()
