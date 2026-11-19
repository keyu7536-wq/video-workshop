"""Real execute branches with deterministic model fixtures and no media render."""
import copy
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import paper_video as pv
import studio_engine as engine
from studio_plan_repair import repair_fingerprint,repair_plan_once
from studio_story import STORY_POLICY_VERSION
from studio_display import effective_display_settings


TEXT='这段说明依据来源展开，演示和结果需要分别解释。'
SOURCES=[{'id':'article','type':'text','name':'原文','text':TEXT}]
BLOCKED={'ready':False,'summary':'开场画面标签与所讲观点不一致。',
         'issues':[{'severity':'error','block_id':'b1','reason':'改正开场的错误标签，保留正确文稿。'}]}
PASSED={'ready':True,'summary':'候选通过测试审核夹具。','issues':[]}


def plan():
    value={'title':'研究解释','summary':'讲清方法与证据。','source_issues':[],'unused_sources':[],
           'blocks':[{'id':'b1','chapter':'研究范围','narration':TEXT,
                      'evidence':[{'source_id':'article','quote':TEXT}],
                      'shots':[{'kind':'card','source_id':'','in':0,'duration':8,'bbox':[],
                                'label':'错误标签','card_text':'已有资料的解释。','cue':''}]}]}
    engine.validate(value,SOURCES)
    return value


def fixed():
    value=plan();value['blocks'][0]['shots'][0]['label']='演示范围'
    return value


def setup_job(root,request=None,review=None):
    job=Path(root);(job/'analysis').mkdir();(job/'output').mkdir()
    request={'auto_repair':True,'auto_render':False,'final_review':False,
             'content_type':'general','story_framework':'auto',**(request or {})}
    value=plan();original_review=copy.deepcopy(review or BLOCKED)
    original_review.update(_plan_hash=engine.plan_hash(value),_content_type='general',
                          _story_framework='auto',_story_policy_version=STORY_POLICY_VERSION,
                          _plan_review_context_version=engine.PLAN_REVIEW_CONTEXT_VERSION,
                          _effective_display_settings=effective_display_settings(request))
    for name,data in [('request.json',request),('job.json',{'status':'prepared'}),
                      ('analysis/studio_plan.json',value),('analysis/sources.json',SOURCES),
                      ('analysis/images.json',[]),('review.json',original_review)]:
        pv.save_json(job/name,data)
    return job,request,value,original_review


class PlanRepairTests(unittest.TestCase):
    def test_prepare_repairs_rejected_initial_plan_before_export(self):
        with tempfile.TemporaryDirectory() as folder:
            job,request,_,_=setup_job(folder)
            calls=[]
            def ai(prompt,images,analysis,request,schema,suffix):
                calls.append(suffix)
                if suffix=='_studio':return plan()
                if suffix.startswith('_plan_repair_'):
                    self.assertIn(BLOCKED['issues'][0]['reason'],prompt)
                    return fixed()
                return copy.deepcopy(PASSED if any(s.startswith('_plan_repair_') for s in calls) else BLOCKED)
            with patch('studio_engine.collect',return_value=(SOURCES,[])), \
                 patch('studio_engine.call_ai',side_effect=ai),patch('studio_engine.export') as export, \
                 patch('paper_video.log'):
                engine.execute(job,'prepare')
            self.assertEqual(sum(s.startswith('_plan_repair_') for s in calls),1)
            self.assertEqual(calls.count('_review'),2)
            self.assertEqual(export.call_args.args[1]['blocks'][0]['shots'][0]['label'],'演示范围')
            self.assertEqual(pv.load_json(job/'analysis/studio_plan.json'),fixed())
            self.assertTrue(pv.load_json(job/'review.json')['ready'])
            self.assertEqual(pv.load_json(job/'analysis/plan_repair_latest.json')['status'],'accepted')

    def test_render_of_prepared_old_policy_rechecks_then_repairs_before_render(self):
        with tempfile.TemporaryDirectory() as folder:
            job,request,original,old=setup_job(folder)
            old['_story_policy_version']=5;pv.save_json(job/'review.json',old)
            calls=[]
            def ai(prompt,images,analysis,request,schema,suffix):
                calls.append(suffix)
                if suffix.startswith('_plan_repair_'):return fixed()
                return copy.deepcopy(BLOCKED if calls.count('_review')==1 else PASSED)
            with patch('studio_engine.call_ai',side_effect=ai),patch('studio_engine.export') as export, \
                 patch('studio_recovery.render_with_recovery',side_effect=lambda j,r,p,s:p) as render, \
                 patch('studio_delivery.export_formats'),patch('paper_video.log'):
                engine.execute(job,'render')
            self.assertEqual(calls[0],'_review')
            self.assertEqual(calls.count('_review'),2)
            self.assertEqual(render.call_count,1)
            self.assertEqual(render.call_args.args[2]['blocks'][0]['shots'][0]['label'],'演示范围')
            self.assertTrue(export.called)
            self.assertEqual(pv.load_json(job/'review.json')['_story_policy_version'],STORY_POLICY_VERSION)

    def test_rejected_candidate_restores_bytes_and_identical_retry_does_not_call_ai(self):
        with tempfile.TemporaryDirectory() as folder:
            job,request,original,review=setup_job(folder)
            before=[(job/path).read_bytes() for path in ('analysis/studio_plan.json','review.json')]
            with patch('studio_engine.call_ai',side_effect=[fixed(),copy.deepcopy(BLOCKED)]) as ai,patch('paper_video.log'):
                returned,checked=repair_plan_once(job,original,SOURCES,request,review)
                repair_plan_once(job,original,SOURCES,request,review)
            self.assertEqual(ai.call_count,2)
            self.assertEqual(returned,original);self.assertEqual(checked,review)
            self.assertEqual(before,[(job/path).read_bytes() for path in ('analysis/studio_plan.json','review.json')])
            folder=next((job/'analysis/plan_repair').iterdir())
            self.assertTrue((folder/'candidate.json').is_file())
            self.assertTrue((folder/'candidate_review.json').is_file())
            self.assertEqual(pv.load_json(folder/'attempt.json')['status'],'rejected')

    def test_disabled_and_unavailable_review_never_generate_candidate(self):
        with tempfile.TemporaryDirectory() as folder:
            job,request,original,review=setup_job(folder)
            for settings,checked in [(request|{'auto_repair':False},review),
                                     (request,review|{'status':'unavailable'}),
                                     (request,review|{'review_complete':False}),
                                     (request,copy.deepcopy(PASSED))]:
                with patch('studio_engine.call_ai') as ai:
                    repair_plan_once(job,original,SOURCES,settings,checked)
                ai.assert_not_called()
            self.assertFalse((job/'analysis/plan_repair').exists())

    def test_protected_script_and_visual_policy_are_validated_before_new_review(self):
        for protected in (True,False):
            with self.subTest(protected=protected),tempfile.TemporaryDirectory() as folder:
                settings={'script':TEXT} if protected else {'minimal_text':True}
                job,request,original,review=setup_job(folder,settings)
                candidate=fixed()
                if protected:candidate['blocks'][0]['narration']+='添加的新结论。'
                else:candidate['blocks'][0]['shots'][0]['card_text']='过量文字'*20
                before=(job/'review.json').read_bytes()
                with patch('studio_engine.call_ai',return_value=candidate) as ai, \
                     patch('studio_engine.review_plan') as recheck,patch('paper_video.log'):
                    result,_=repair_plan_once(job,original,SOURCES,request,review)
                self.assertEqual(result,original);ai.assert_called_once();recheck.assert_not_called()
                self.assertEqual((job/'review.json').read_bytes(),before)
                self.assertEqual(pv.load_json(job/'analysis/plan_repair_latest.json')['status'],'invalid_candidate')

    def test_material_changes_allow_new_attempt_but_toggles_timestamps_and_keys_do_not(self):
        original=plan();request={'auto_repair':True,'brief':'解释机制。'}
        base=repair_fingerprint(original,SOURCES,request)
        self.assertEqual(base,repair_fingerprint(original,[SOURCES[0]|{'accessed_at':'tomorrow'}],
                         request|{'auto_repair':False,'auto_render':True,'key':'secret'}))
        for changed_plan,changed_sources,changed_request in [
            (fixed(),SOURCES,request),(original,[SOURCES[0]|{'text':TEXT+'补充资料。'}],request),
            (original,SOURCES,request|{'brief':'侧重适用范围。'}),
            (original,SOURCES,request|{'rate':'+50%'})]:
            self.assertNotEqual(base,repair_fingerprint(changed_plan,changed_sources,changed_request))
        with tempfile.TemporaryDirectory() as folder:
            job,request,original,review=setup_job(folder)
            with patch('studio_engine.call_ai',side_effect=[fixed(),copy.deepcopy(BLOCKED),fixed(),copy.deepcopy(PASSED)]) as ai,patch('paper_video.log'):
                repair_plan_once(job,original,SOURCES,request,review)
                result,_=repair_plan_once(job,original,SOURCES,request|{'brief':'更换关注点。'},review)
            self.assertEqual(ai.call_count,4);self.assertEqual(result,fixed())

    def test_candidate_review_outage_restores_original_review_and_keeps_candidate(self):
        with tempfile.TemporaryDirectory() as folder:
            job,request,original,review=setup_job(folder)
            before=(job/'review.json').read_bytes()
            with patch('studio_engine.call_ai',side_effect=[fixed(),RuntimeError('review service unavailable')]),patch('paper_video.log'):
                result,checked=repair_plan_once(job,original,SOURCES,request,review)
            self.assertEqual(result,original);self.assertEqual(checked,review)
            self.assertEqual((job/'review.json').read_bytes(),before)
            folder=next((job/'analysis/plan_repair').iterdir())
            self.assertTrue((folder/'candidate.json').is_file())
            self.assertIn('unavailable',pv.load_json(folder/'attempt.json')['reason'])

    def test_constraint_policy_change_allows_one_new_attempt_without_global_story_invalidation(self):
        import studio_plan_repair as repair
        policy=repair.REPAIR_POLICY_VERSION
        with tempfile.TemporaryDirectory() as folder:
            job,request,original,review=setup_job(folder,{'script':TEXT})
            invalid=fixed();invalid['blocks'][0]['narration']+='Unauthorized change.'
            initial=repair_fingerprint(original,SOURCES,request)
            with patch('studio_engine.call_ai',return_value=invalid) as first,patch('paper_video.log'):
                repair_plan_once(job,original,SOURCES,request,review)
                repair_plan_once(job,original,SOURCES,request,review)
            first.assert_called_once()
            self.assertEqual(pv.load_json(job/'analysis/plan_repair_latest.json')['status'],'invalid_candidate')
            with patch.object(repair,'REPAIR_POLICY_VERSION',policy+1):
                self.assertNotEqual(repair_fingerprint(original,SOURCES,request),initial)
                with patch('studio_engine.call_ai',return_value=fixed()) as generate, \
                     patch('studio_engine.review_plan',return_value=copy.deepcopy(BLOCKED)) as recheck, \
                     patch('paper_video.log'):
                    repair_plan_once(job,original,SOURCES,request,review)
                    repair_plan_once(job,original,SOURCES,request,review)
                generate.assert_called_once();recheck.assert_called_once()
                self.assertEqual(pv.load_json(job/'analysis/plan_repair_latest.json')['repair_policy_version'],policy+1)
            self.assertEqual(repair_fingerprint(original,SOURCES,request),initial)
            self.assertEqual(pv.load_json(job/'review.json')['_story_policy_version'],STORY_POLICY_VERSION)
            self.assertEqual(len(list((job/'analysis/plan_repair').iterdir())),2)

    def test_external_review_outages_retry_only_same_candidate_on_later_click(self):
        for outcome in (RuntimeError('review service unavailable'),
                        {'ready':False,'status':'unavailable','summary':'service down','issues':[]}):
            with self.subTest(outcome=type(outcome).__name__),tempfile.TemporaryDirectory() as folder:
                job,request,original,review=setup_job(folder,{'script':TEXT})
                before=(job/'review.json').read_bytes()
                def unavailable(*args):
                    # Simulate a reviewer that overwrites the active review before
                    # reporting unavailability; rollback must still preserve it.
                    pv.save_json(job/'review.json',{'status':'unavailable'})
                    if isinstance(outcome,Exception):raise outcome
                    return copy.deepcopy(outcome)
                with patch('studio_engine.call_ai',return_value=fixed()) as generate, \
                     patch('studio_engine.review_plan',side_effect=unavailable) as first_review,patch('paper_video.log'):
                    repair_plan_once(job,original,SOURCES,request,review)
                generate.assert_called_once();first_review.assert_called_once()
                self.assertEqual((job/'review.json').read_bytes(),before)
                with patch('studio_engine.call_ai') as regenerate, \
                     patch('studio_engine.validate',wraps=engine.validate) as validate, \
                     patch('studio_engine.review_plan',return_value=copy.deepcopy(PASSED)) as second_review,patch('paper_video.log'):
                    accepted,checked=repair_plan_once(job,original,SOURCES,request,review)
                regenerate.assert_not_called();second_review.assert_called_once()
                self.assertEqual(validate.call_args.args[2],TEXT)
                self.assertEqual(accepted,fixed());self.assertTrue(checked['ready'])
                record=pv.load_json(job/'analysis/plan_repair_latest.json')
                self.assertEqual(record['status'],'accepted');self.assertEqual(record['attempts'],1)
                self.assertEqual(record['review_attempts'],2)

    def test_resume_does_not_review_tampered_candidate_or_reroll_rejected_content(self):
        for tamper in (True,False):
            with self.subTest(tamper=tamper),tempfile.TemporaryDirectory() as folder:
                job,request,original,review=setup_job(folder)
                with patch('studio_engine.call_ai',return_value=fixed()), \
                     patch('studio_engine.review_plan',side_effect=RuntimeError('service down')),patch('paper_video.log'):
                    repair_plan_once(job,original,SOURCES,request,review)
                folder=next((job/'analysis/plan_repair').iterdir())
                if tamper:
                    candidate=fixed();candidate['summary']='Artifact modified after validation'
                    pv.save_json(folder/'candidate.json',candidate)
                with patch('studio_engine.call_ai') as generate, \
                     patch('studio_engine.review_plan',return_value=copy.deepcopy(BLOCKED)) as recheck,patch('paper_video.log'):
                    repair_plan_once(job,original,SOURCES,request,review)
                    repair_plan_once(job,original,SOURCES,request,review)
                generate.assert_not_called()
                self.assertEqual(recheck.call_count,0 if tamper else 1)
                self.assertEqual(pv.load_json(job/'analysis/studio_plan.json'),original)

    def test_generation_failure_is_distinct_and_does_not_claim_content_repair(self):
        with tempfile.TemporaryDirectory() as folder:
            job,request,original,review=setup_job(folder)
            with patch('studio_engine.call_ai',side_effect=RuntimeError('generation service down')),patch('paper_video.log'):
                result,_=repair_plan_once(job,original,SOURCES,request,review)
            record=pv.load_json(job/'analysis/plan_repair_latest.json')
            self.assertEqual(result,original)
            self.assertEqual(record['status'],'generation_failed')
            self.assertEqual(record['review_attempts'],0)

    def test_ready_false_without_error_issue_still_gets_one_repair(self):
        with tempfile.TemporaryDirectory() as folder:
            job,request,original,review=setup_job(folder,review=BLOCKED|{'issues':[]})
            with patch('studio_engine.call_ai',side_effect=[fixed(),copy.deepcopy(PASSED)]) as ai,patch('paper_video.log'):
                result,_=repair_plan_once(job,original,SOURCES,request,review)
            self.assertEqual(ai.call_count,2);self.assertEqual(result,fixed())


if __name__=='__main__':unittest.main()
