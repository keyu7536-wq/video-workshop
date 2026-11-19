"""The three candidate generations are shared by every repair stage."""
import copy
from pathlib import Path
import tempfile
import unittest

import paper_video as pv
from studio_repair_budget import (ensure_budget,reserve_attempt,finish_attempt,pending_attempt,
                                 generation_retryable,register_plan,budget_summary)


SOURCES=[{'id':'text','type':'text','text':'An attributable source.'}]
REQUEST={'content_type':'general','auto_repair':True}


class RepairBudgetTests(unittest.TestCase):
    def test_host_generated_initial_plan_alias_does_not_reset_spent_quota(self):
        with tempfile.TemporaryDirectory() as root:
            job=Path(root)
            register_plan(job,None,{'value':0},SOURCES,REQUEST)
            ticket=reserve_attempt(job,{'value':0},SOURCES,REQUEST,'schema')
            finish_attempt(job,ticket,'invalid_candidate',candidate=['invalid'])
            state=register_plan(job,None,{'value':99},SOURCES,REQUEST)
            self.assertEqual(state['used'],1)
            self.assertEqual(state['budget_id'],ticket['budget_id'])
            self.assertEqual(reserve_attempt(job,{'value':99},SOURCES,REQUEST,'plan')['number'],2)

    def test_read_only_summary_reports_old_version_without_mutating_or_resetting(self):
        with tempfile.TemporaryDirectory() as root:
            job=Path(root);plan={'value':0}
            self.assertIsNone(budget_summary(job,plan,SOURCES,REQUEST))
            self.assertEqual(list(job.iterdir()),[])
            ticket=reserve_attempt(job,plan,SOURCES,REQUEST,'plan')
            finish_attempt(job,ticket,'review_unavailable',candidate={'value':1},candidate_validated=True)
            before={str(path.relative_to(job)):path.read_bytes() for path in job.rglob('*') if path.is_file()}
            summary=budget_summary(job,plan,SOURCES,REQUEST)
            self.assertEqual((summary['used'],summary['remaining'],summary['pending_review'],summary['stale']),(1,2,True,False))
            self.assertTrue(budget_summary(job,{'value':'manual'},SOURCES,REQUEST)['stale'])
            self.assertTrue(budget_summary(job,plan,SOURCES,REQUEST|{'brief':'new input'})['stale'])
            self.assertEqual(before,{str(path.relative_to(job)):path.read_bytes() for path in job.rglob('*') if path.is_file()})

    def test_generation_error_classifier_never_blindly_retries_external_failures(self):
        for error in (TimeoutError('timeout'),RuntimeError('model exited with code 1'),
                      RuntimeError('unknown failure'),ValueError('HTTP 429 rate limit'),
                      ValueError('API 模式需要填写 API Key'),ValueError('API 地址须为 https'),
                      RuntimeError('Codex 本次未返回有效计划，请确认网络和账户额度正常。')):
            with self.subTest(error=str(error)):self.assertFalse(generation_retryable(error))
        for error in (ValueError('protected script changed'),RuntimeError('invalid JSON'),
                      RuntimeError('Codex 本次结果必须是 JSON 对象，未使用之前的输出。')):
            with self.subTest(error=str(error)):self.assertTrue(generation_retryable(error))

    def test_legacy_unavailable_candidate_resumes_without_extra_reservation(self):
        from studio_plan_repair import repair_fingerprint
        from studio_engine import plan_hash
        with tempfile.TemporaryDirectory() as root:
            job=Path(root);plan={'value':0};candidate={'value':1}
            fingerprint=repair_fingerprint(plan,SOURCES,REQUEST)
            folder=job/'analysis/plan_repair'/fingerprint;folder.mkdir(parents=True)
            pv.save_json(folder/'original_plan.json',plan);pv.save_json(folder/'candidate.json',candidate)
            pv.save_json(folder/'attempt.json',{'fingerprint':fingerprint,'status':'review_unavailable',
                         'attempts':1,'candidate_validated':True,'candidate_plan_hash':plan_hash(candidate)})
            ticket=pending_attempt(job,plan,SOURCES,REQUEST,'plan')
            self.assertEqual(ticket['number'],1)
            self.assertIsNone(reserve_attempt(job,plan,SOURCES,REQUEST,'plan'))
            finish_attempt(job,ticket,'accepted',review={'ready':True})
            self.assertEqual(ensure_budget(job,candidate,SOURCES,REQUEST)['used'],1)

    def test_corrupt_ledger_cannot_reset_the_quota(self):
        with tempfile.TemporaryDirectory() as root:
            job=Path(root);ticket=reserve_attempt(job,None,SOURCES,REQUEST,'schema')
            (job/'analysis/repair_budget.json').write_text('{bad',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'不能重置'):
                reserve_attempt(job,None,SOURCES,REQUEST,'schema')
            self.assertEqual(pv.load_json(Path(ticket['folder'])/'attempt.json')['number'],1)

    def test_three_generations_shared_across_stages_and_known_candidates(self):
        with tempfile.TemporaryDirectory() as root:
            job=Path(root);plan={'value':0}
            for number,stage in enumerate(('schema','plan','timing'),1):
                ticket=reserve_attempt(job,plan,SOURCES,REQUEST,stage,{'error':'specific'})
                self.assertEqual(ticket['number'],number)
                plan={'value':number}
                finish_attempt(job,ticket,'accepted',candidate=plan,candidate_validated=True)
            self.assertIsNone(reserve_attempt(job,plan,SOURCES,REQUEST,'final'))
            state=ensure_budget(job,plan,SOURCES,REQUEST)
            self.assertEqual(state['used'],3);self.assertEqual(state['remaining'],0)
            self.assertEqual(len(state['known_plan_hashes']),4)

    def test_concurrent_reservations_cannot_exceed_shared_limit(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as root:
            job=Path(root)
            with ThreadPoolExecutor(max_workers=5) as pool:
                tickets=list(pool.map(lambda _:reserve_attempt(job,None,SOURCES,REQUEST,'schema'),range(5)))
            reserved=[ticket for ticket in tickets if ticket]
            self.assertEqual(sorted(ticket['number'] for ticket in reserved),[1,2,3])
            self.assertEqual(len({ticket['folder'] for ticket in reserved}),3)
            self.assertEqual(ensure_budget(job,None,SOURCES,REQUEST)['used'],3)

    def test_failures_count_and_repeated_click_cannot_reset(self):
        with tempfile.TemporaryDirectory() as root:
            job=Path(root)
            for _ in range(3):
                ticket=reserve_attempt(job,None,SOURCES,REQUEST,'schema')
                finish_attempt(job,ticket,'generation_failed',reason='process exited')
            self.assertIsNone(reserve_attempt(job,None,SOURCES,REQUEST,'schema'))
            self.assertEqual(ensure_budget(job,None,SOURCES,REQUEST)['used'],3)

    def test_pending_review_reuses_candidate_without_new_generation(self):
        with tempfile.TemporaryDirectory() as root:
            job=Path(root);plan={'value':0};candidate={'value':1}
            ticket=reserve_attempt(job,plan,SOURCES,REQUEST,'plan')
            finish_attempt(job,ticket,'review_unavailable',candidate=candidate,candidate_validated=True,
                           reason='review service unavailable')
            resumed=pending_attempt(job,plan,SOURCES,REQUEST,'plan')
            self.assertEqual(resumed['number'],1)
            self.assertEqual(pv.load_json(Path(resumed['folder'])/'candidate.json'),candidate)
            self.assertIsNone(reserve_attempt(job,plan,SOURCES,REQUEST,'final'))
            finish_attempt(job,resumed,'accepted',review={'ready':True})
            self.assertEqual(ensure_budget(job,candidate,SOURCES,REQUEST)['used'],1)

    def test_tampered_pending_candidate_is_not_reviewed_or_reset(self):
        with tempfile.TemporaryDirectory() as root:
            job=Path(root);plan={'value':0}
            ticket=reserve_attempt(job,plan,SOURCES,REQUEST,'plan')
            finish_attempt(job,ticket,'review_unavailable',candidate={'value':1},candidate_validated=True)
            pv.save_json(Path(ticket['folder'])/'candidate.json',{'value':'tampered'})
            with self.assertRaisesRegex(ValueError,'candidate|候选'):
                pending_attempt(job,plan,SOURCES,REQUEST,'plan')
            self.assertEqual(ensure_budget(job,plan,SOURCES,REQUEST)['used'],1)

    def test_material_changes_reset_but_toggles_policy_and_idle_values_do_not(self):
        import studio_plan_repair as repair
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root:
            job=Path(root);plan={'value':0}
            request=REQUEST|{'duration_mode':'adaptive','seconds':90,'format':'portrait','width':720,'height':1280}
            ticket=reserve_attempt(job,plan,SOURCES,request,'plan')
            finish_attempt(job,ticket,'rejected')
            changed=request|{'seconds':600,'width':1920,'height':1080,'auto_render':True,'auto_repair':False,'key':'secret'}
            with patch.object(repair,'REPAIR_POLICY_VERSION',999):
                state=ensure_budget(job,plan,[SOURCES[0]|{'accessed_at':'tomorrow'}],changed)
            self.assertEqual(state['budget_id'],ticket['budget_id']);self.assertEqual(state['used'],1)
            for current_plan,current_sources,current_request in (
                ({'value':'user edit'},SOURCES,request),
                (plan,[SOURCES[0]|{'text':'New material.'}],request),
                (plan,SOURCES,request|{'rate':'+50%'}),
            ):
                changed_state=ensure_budget(job,current_plan,current_sources,current_request)
                self.assertEqual(changed_state['used'],0)
                self.assertNotEqual(changed_state['budget_id'],ticket['budget_id'])

    def test_legacy_matching_attempt_consumes_one_and_preserves_candidate(self):
        from studio_plan_repair import repair_fingerprint
        with tempfile.TemporaryDirectory() as root:
            job=Path(root);plan={'value':0};candidate={'value':1}
            fingerprint=repair_fingerprint(plan,SOURCES,REQUEST)
            folder=job/'analysis/plan_repair'/fingerprint
            folder.mkdir(parents=True)
            pv.save_json(folder/'original_plan.json',plan);pv.save_json(folder/'candidate.json',candidate)
            pv.save_json(folder/'attempt.json',{'fingerprint':fingerprint,'status':'rejected','attempts':1})
            state=ensure_budget(job,plan,SOURCES,REQUEST)
            self.assertEqual(state['used'],1)
            self.assertEqual(reserve_attempt(job,plan,SOURCES,REQUEST,'final')['number'],2)

    def test_unrelated_legacy_attempt_does_not_spend_new_input(self):
        from studio_plan_repair import repair_fingerprint
        with tempfile.TemporaryDirectory() as root:
            job=Path(root);old={'value':0};current={'value':'new manual plan'}
            fingerprint=repair_fingerprint(old,SOURCES,REQUEST)
            folder=job/'analysis/plan_repair'/fingerprint
            folder.mkdir(parents=True)
            pv.save_json(folder/'original_plan.json',old)
            pv.save_json(folder/'attempt.json',{'fingerprint':fingerprint,'status':'rejected','attempts':1})
            self.assertEqual(ensure_budget(job,current,SOURCES,REQUEST)['used'],0)

    def test_same_plan_legacy_record_does_not_charge_changed_request_or_sources(self):
        from studio_plan_repair import repair_fingerprint
        for sources,request in ((SOURCES,REQUEST|{'brief':'new focus'}),
                                (SOURCES,REQUEST|{'format':'landscape'}),
                                ([SOURCES[0]|{'text':'changed source'}],REQUEST)):
            with self.subTest(request=request,sources=sources),tempfile.TemporaryDirectory() as root:
                job=Path(root);plan={'value':0}
                fingerprint=repair_fingerprint(plan,SOURCES,REQUEST)
                folder=job/'analysis/plan_repair'/fingerprint;folder.mkdir(parents=True)
                pv.save_json(folder/'original_plan.json',plan)
                pv.save_json(folder/'attempt.json',{'fingerprint':fingerprint,'status':'rejected','attempts':1})
                self.assertEqual(ensure_budget(job,plan,sources,request)['used'],0)

    def test_unbound_old_schema_timing_final_files_do_not_spend_new_input(self):
        with tempfile.TemporaryDirectory() as root:
            job=Path(root);p0={'value':0};p1={'value':1};p2={'value':2};p3={'value':3}
            for name,value in (
                ('analysis/codex_result_fix.json',p1),('analysis/timing_recovery/plan_before.json',p1),
                ('analysis/timing_recovery/candidate.json',p2),
                ('analysis/plan_before_repair.json',p2),('analysis/codex_result_render_repair.json',p3),
            ):
                (job/name).parent.mkdir(parents=True,exist_ok=True)
                pv.save_json(job/name,value)
            state=ensure_budget(job,p3,SOURCES,REQUEST)
            self.assertEqual(state['used'],0)
            self.assertEqual(reserve_attempt(job,p3,SOURCES,REQUEST,'final')['number'],1)


if __name__=='__main__':unittest.main()
