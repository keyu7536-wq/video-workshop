"""Rejected content returns to editing without encoding or bypassing review."""
import copy
import tempfile
import unittest
from unittest.mock import patch

import paper_video as pv
import studio_engine as engine
import studio_server as server
from tests.test_plan_repair import BLOCKED, PASSED, SOURCES, fixed, setup_job


class PlanGateTests(unittest.TestCase):
    def assert_waiting_for_edit(self, job):
        state = pv.load_json(job / 'job.json')
        self.assertEqual(state['status'], 'review_needed')
        self.assertEqual(state['stage'], 'plan_review')
        self.assertIsNone(state['error'])
        self.assertNotIn('failed_action', state)

    def test_blocked_prepare_resume_and_render_stop_at_editable_plan_gate(self):
        cases = [('prepare', False), ('prepare', True), ('resume', False), ('resume', True), ('render', False)]
        for action, auto_render in cases:
            with self.subTest(action=action, auto_render=auto_render), tempfile.TemporaryDirectory() as folder:
                job, request, plan, review = setup_job(folder, {'auto_repair': False, 'auto_render': auto_render})
                state = pv.load_json(job / 'job.json'); state['failed_action'] = 'render'
                pv.save_json(job / 'job.json', state)
                original = (job / 'analysis/studio_plan.json').read_bytes()
                with patch.object(engine, 'collect', return_value=(SOURCES, [])), \
                        patch.object(engine, 'call_ai', return_value=copy.deepcopy(plan)) as ai, \
                        patch.object(engine, 'review_plan', return_value=copy.deepcopy(review)), \
                        patch.object(engine, 'export'), patch('studio_recovery.render_with_recovery') as render, \
                        patch('studio_delivery.export_formats') as formats, patch('paper_video.log'):
                    engine.execute(job, action)
                self.assert_waiting_for_edit(job)
                self.assertEqual(ai.call_count, 0 if action == 'render' else 1)
                render.assert_not_called(); formats.assert_not_called()
                self.assertEqual((job / 'analysis/studio_plan.json').read_bytes(), original)
                self.assertEqual(pv.load_json(job / 'request.json'), request)
                self.assertEqual(pv.load_json(job / 'review.json'), review)

    def test_either_not_ready_or_an_error_blocks_without_rerolling_review(self):
        for review in (BLOCKED, BLOCKED | {'ready': True}, BLOCKED | {'issues': []}):
            with self.subTest(review=review), tempfile.TemporaryDirectory() as folder:
                job, _, _, saved_review = setup_job(folder, {'auto_repair': False}, review)
                with patch.object(engine, 'call_ai') as ai, patch.object(engine, 'export'), \
                        patch('studio_recovery.render_with_recovery') as render, patch('paper_video.log'):
                    engine.execute(job, 'render'); engine.execute(job, 'render')
                self.assert_waiting_for_edit(job)
                ai.assert_not_called(); render.assert_not_called()
                self.assertEqual(pv.load_json(job / 'review.json'), saved_review)

    def test_auto_repair_enabled_still_has_one_rejected_candidate_and_no_encoding(self):
        with tempfile.TemporaryDirectory() as folder:
            job, request, original, review = setup_job(folder, {'auto_repair': True})
            with patch.object(engine, 'call_ai', side_effect=[fixed(), copy.deepcopy(BLOCKED)]) as ai, \
                    patch.object(engine, 'export'), patch('studio_recovery.render_with_recovery') as render, \
                    patch('paper_video.log'):
                engine.execute(job, 'render'); engine.execute(job, 'render')
            self.assert_waiting_for_edit(job)
            self.assertEqual(ai.call_count, 2)  # One candidate and its independent review.
            render.assert_not_called()
            self.assertEqual(pv.load_json(job / 'analysis/studio_plan.json'), original)
            self.assertEqual(pv.load_json(job / 'review.json'), review)
            self.assertEqual(pv.load_json(job / 'request.json'), request)

    def test_edited_plan_must_pass_fresh_review_before_encoding(self):
        with tempfile.TemporaryDirectory() as folder:
            job, _, _, _ = setup_job(folder, {'auto_repair': False})
            pv.save_json(job / 'analysis/studio_plan.json', fixed())
            with patch.object(engine, 'call_ai', return_value=copy.deepcopy(PASSED)) as ai, \
                    patch.object(engine, 'export'), \
                    patch('studio_recovery.render_with_recovery', side_effect=lambda j,r,p,s:p) as render, \
                    patch('studio_delivery.export_formats'), patch('paper_video.log'):
                engine.execute(job, 'render')
            ai.assert_called_once(); self.assertEqual(ai.call_args.args[-1], '_review')
            render.assert_called_once()
            self.assertEqual(pv.load_json(job / 'job.json')['status'], 'completed')

    def test_model_execution_error_is_still_a_failure_with_original_reason(self):
        with tempfile.TemporaryDirectory() as folder:
            job, _, _, review = setup_job(folder, {'auto_repair': False})
            review.pop('_plan_review_context_version'); pv.save_json(job / 'review.json', review)
            with patch.object(engine, 'review_plan', side_effect=RuntimeError('model unavailable')), \
                    patch('studio_recovery.render_with_recovery') as render:
                with self.assertRaisesRegex(RuntimeError, 'model unavailable'):
                    engine.execute(job, 'render')
            render.assert_not_called()
            state = pv.load_json(job / 'job.json')
            self.assertEqual(state['status'], 'failed'); self.assertEqual(state['error'], 'model unavailable')

    def test_snapshot_plan_gate_never_exports_or_retries_review_of_an_old_video(self):
        with tempfile.TemporaryDirectory() as folder:
            job, _, _, _ = setup_job(folder, {'auto_repair': False})
            pv.save_json(job / 'job.json', {'engine': 'studio', 'status': 'review_needed', 'stage': 'plan_review', 'error': None})
            (job / 'output/video.mp4').write_bytes(b'old video')
            pv.save_json(job / 'output/final_review.json', {'status': 'unavailable', 'issues': []})
            with patch('studio_revision.export_version_current', return_value=True):
                result = server.snapshot(job)
            self.assertEqual(result['stage'], 'plan_review')
            self.assertIn('plan', result)
            self.assertFalse(result['can_export'])
            self.assertIsNone(result['recovery'])

    def legacy_job(self, folder):
        job, request, plan, review = setup_job(folder, {'auto_repair': False})
        review['_plan_review_context_version'] = engine.PLAN_REVIEW_CONTEXT_VERSION - 1
        pv.save_json(job / 'review.json', review)
        pv.save_json(job / 'job.json', {'engine': 'studio', 'status': 'failed', 'stage': 'render',
            'failed_action': 'render', 'error': '文稿与分镜未通过内容复核：' + BLOCKED['issues'][0]['reason']})
        return job, request, plan, review

    def test_exact_legacy_gate_is_read_only_projection_with_original_status_recorded(self):
        with tempfile.TemporaryDirectory() as folder:
            job, _, _, _ = self.legacy_job(folder)
            paths = ('job.json', 'request.json', 'analysis/studio_plan.json', 'review.json')
            before = {name: (job / name).read_bytes() for name in paths}
            result = server.snapshot(job)
            self.assertEqual((result['status'], result['stage']), ('review_needed', 'plan_review'))
            self.assertEqual(result['recorded_status'], 'failed')
            self.assertFalse(result['review']['stale'])
            self.assertIsNone(result['error']); self.assertIsNone(result['recovery'])
            self.assertFalse(result['can_export'])
            self.assertEqual(before, {name: (job / name).read_bytes() for name in paths})

    def test_real_failures_and_changed_or_invalid_legacy_plans_are_never_projected(self):
        for change in ('ffmpeg', 'model', 'different_reason', 'plan', 'invalid', 'content_type', 'framework', 'brief', 'script'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as folder:
                job, request, plan, review = self.legacy_job(folder)
                state = pv.load_json(job / 'job.json')
                if change == 'ffmpeg': state['error'] = 'FFmpeg decoder failed'
                if change == 'model': state['error'] = 'model API unavailable'
                if change == 'different_reason': state['error'] += 'new unrelated error'
                if change == 'plan': plan['title'] = 'A changed plan'
                if change == 'invalid':
                    del plan['title']; review['_plan_hash'] = engine.plan_hash(plan)
                if change == 'content_type': request['content_type'] = 'paper'
                if change == 'framework': request['story_framework'] = 'source_order'
                if change == 'brief': request['_brief_revision'] = 'changed'
                if change == 'script': request['script'] = 'Different protected narration'
                for name, value in (('job.json', state), ('request.json', request),
                        ('analysis/studio_plan.json', plan), ('review.json', review)):
                    pv.save_json(job / name, value)
                before = (job / 'job.json').read_bytes()
                result = server.snapshot(job)
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['error'], state['error'])
                self.assertNotIn('recorded_status', result)
                self.assertEqual((job / 'job.json').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
