"""Only version-bound, applicable review evidence may block/repair a plan."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import paper_video as pv
import studio_engine as engine
import studio_plan_repair as repair
from studio_revision import file_hash,render_settings_hash
from studio_story import STORY_POLICY_VERSION
from studio_display import effective_display_settings
from tests.test_plan_repair import plan,SOURCES


class ReviewFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.job=Path(self.temp.name)
        for name in ('analysis','output','analysis/timing_recovery'):(self.job/name).mkdir(exist_ok=True)
        self.plan=plan()
        second=copy.deepcopy(self.plan['blocks'][0]);second['id']='b2'
        self.plan['blocks'].append(second)
        self.request={'auto_repair':True,'content_type':'general'}
        self.previous={'ready':True,'summary':'Original plan review','issues':[],
                       '_plan_hash':engine.plan_hash(self.plan),'_content_type':'general',
                       '_story_framework':'auto','_story_policy_version':STORY_POLICY_VERSION,
                       '_plan_review_context_version':engine.PLAN_REVIEW_CONTEXT_VERSION,
                       '_effective_display_settings':effective_display_settings(self.request)}
        for name,value in [('request.json',self.request),('review.json',self.previous),
                           ('analysis/studio_plan.json',self.plan),('analysis/sources.json',SOURCES),
                           ('job.json',{'status':'review_needed'}),('project.json',{'blocks':[]})]:
            pv.save_json(self.job/name,value)
        (self.job/'output/video.mp4').write_bytes(b'measured video fixture')
        self.issue={'severity':'error','block_id':'b1','reason':'The sampled table does not support this narration.'}
        self.final={'ready':False,'status':'completed','review_complete':True,'issues':[self.issue],
                    'reviews':[{'status':'completed','ready':False,'issues':[self.issue]}],
                    'plan_hash':engine.plan_hash(self.plan),'settings_hash':render_settings_hash(self.request),
                    'video_sha256':file_hash(self.job/'output/video.mp4'),
                    'project_sha256':file_hash(self.job/'project.json')}

    def test_candidate_feedback_requires_identical_narration_and_available_bound_review(self):
        for change in ('narration','block','missing_id','unavailable','wrong_hash'):
            with self.subTest(change=change):
                candidate=copy.deepcopy(self.plan);issue=copy.deepcopy(self.issue)
                review={'ready':False,'issues':[issue]}
                # Even when the flagged block is identical, changing narration
                # elsewhere invalidates transfer of the candidate's judgment.
                if change=='narration':candidate['blocks'][1]['narration']+=' Changed.'
                if change=='block':candidate['blocks'][0]['shots'][0]['label']='Changed'
                if change=='missing_id':issue['block_id']=''
                if change=='unavailable':review['status']='unavailable'
                review['_plan_hash']='stale' if change=='wrong_hash' else engine.plan_hash(candidate)
                folder=self.job/'analysis/timing_recovery';pv.save_json(folder/'rejected_review.json',review)
                result=repair.unchanged_block_feedback(self.job,self.plan,candidate,review,self.previous,SOURCES,folder)
                self.assertIsNone(result)
                self.assertEqual(pv.load_json(self.job/'review.json'),self.previous)

    def test_current_complete_final_errors_reach_repair_before_any_encoding(self):
        pv.save_json(self.job/'output/final_review.json',self.final)
        received=[]
        def reject_without_encoding(job,plan,sources,request,review):
            received.append(review)
            return plan,review
        with patch.object(repair,'repair_plan_once',side_effect=reject_without_encoding), \
             patch.object(engine,'export'),patch('studio_recovery.render_with_recovery') as render, \
             patch('studio_delivery.export_formats'),patch('paper_video.log'):
            engine.execute(self.job,'render')
        state=pv.load_json(self.job/'job.json')
        self.assertEqual((state['status'],state['stage']),('review_needed','plan_review'))
        self.assertIsNone(state['error'])
        render.assert_not_called()
        self.assertEqual(len(received),1);self.assertIn(self.issue,received[0]['issues'])
        self.assertFalse(received[0]['ready'])
        self.assertEqual(pv.load_json(self.job/'analysis/studio_plan.json'),self.plan)

    def test_stale_incomplete_or_unavailable_final_reviews_never_transfer(self):
        variants=[{'status':'unavailable'},{'status':'incomplete'},{'review_complete':False},
                  {'reviews':[{'status':'unavailable'}]},{'ready':True},{'issues':[]}]
        variants += [{key:'wrong'} for key in ('plan_hash','settings_hash','video_sha256','project_sha256')]
        for change in variants:
            with self.subTest(change=change):
                pv.save_json(self.job/'output/final_review.json',self.final|change)
                result=repair.current_final_feedback(self.job,self.plan,self.request,self.previous)
                self.assertEqual(result,self.previous)
                self.assertEqual(pv.load_json(self.job/'review.json'),self.previous)

    def test_repeated_render_of_rejected_current_version_does_not_reroll_or_encode(self):
        pv.save_json(self.job/'output/final_review.json',self.final)
        candidate=copy.deepcopy(self.plan);candidate['summary']='Candidate still needs correction.'
        rejected={'ready':False,'issues':[self.issue],'_plan_hash':engine.plan_hash(candidate)}
        with patch.object(engine,'call_ai',return_value=candidate) as generate, \
             patch.object(engine,'review_plan',return_value=rejected) as review, \
             patch.object(engine,'export'),patch('studio_recovery.render_with_recovery') as render, \
             patch('paper_video.log'):
            for _ in range(2):
                engine.execute(self.job,'render')
                state=pv.load_json(self.job/'job.json')
                self.assertEqual((state['status'],state['stage']),('review_needed','plan_review'))
                self.assertIsNone(state['error'])
        self.assertEqual(generate.call_count,3);review.assert_called_once();render.assert_not_called()
        self.assertFalse(pv.load_json(self.job/'review.json')['ready'])
        self.assertEqual(pv.load_json(self.job/'analysis/studio_plan.json'),self.plan)


if __name__=='__main__':unittest.main()
