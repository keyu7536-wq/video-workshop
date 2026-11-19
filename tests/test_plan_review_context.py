"""Review-material upgrades refresh only the plan gate, once per context version."""
import copy
import tempfile
import unittest
from unittest.mock import patch

import paper_video as pv
import studio_engine as engine
from studio_revision import file_hash, render_settings_hash, review_stale
from tests.test_plan_repair import BLOCKED, PASSED, setup_job


class PlanReviewContextTests(unittest.TestCase):
    def test_old_failed_context_rechecks_once_then_reuses_current_review(self):
        for old_version in (None, engine.PLAN_REVIEW_CONTEXT_VERSION - 1):
            with self.subTest(old_version=old_version), tempfile.TemporaryDirectory() as folder:
                job, request, plan, review = setup_job(folder, {'auto_repair': False})
                if old_version is None:
                    review.pop('_plan_review_context_version')
                else:
                    review['_plan_review_context_version'] = old_version
                pv.save_json(job / 'review.json', review)
                original_plan = (job / 'analysis/studio_plan.json').read_bytes()
                with patch.object(engine, 'call_ai', return_value=copy.deepcopy(PASSED)) as ai, \
                     patch.object(engine, 'export'), \
                     patch('studio_recovery.render_with_recovery', side_effect=lambda j,r,p,s:p) as render, \
                     patch('studio_delivery.export_formats'), patch('paper_video.log'):
                    engine.execute(job, 'render')
                    engine.execute(job, 'render')
                ai.assert_called_once()
                self.assertEqual(ai.call_args.args[-1], '_review')
                self.assertEqual(render.call_count, 2)
                current = pv.load_json(job / 'review.json')
                self.assertTrue(current['ready'])
                self.assertEqual(current['_plan_review_context_version'], engine.PLAN_REVIEW_CONTEXT_VERSION)
                self.assertEqual((job / 'analysis/studio_plan.json').read_bytes(), original_plan)

    def test_current_failed_context_is_not_rerolled_to_seek_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            job, _, _, review = setup_job(folder, {'auto_repair': False})
            with patch.object(engine, 'call_ai') as ai, patch.object(engine, 'export'), \
                 patch('studio_recovery.render_with_recovery') as render, patch('paper_video.log'):
                for _ in range(2):
                    engine.execute(job, 'render')
                    state=pv.load_json(job/'job.json')
                    self.assertEqual((state['status'],state['stage']),('review_needed','plan_review'))
                    self.assertIsNone(state['error'])
            ai.assert_not_called()
            render.assert_not_called()
            self.assertEqual(pv.load_json(job / 'review.json'), review)

    def test_context_refresh_preserves_a_new_rejection_and_stops_encoding(self):
        with tempfile.TemporaryDirectory() as folder:
            job, _, _, review = setup_job(folder, {'auto_repair': False})
            review.pop('_plan_review_context_version')
            pv.save_json(job / 'review.json', review)
            with patch.object(engine, 'call_ai', return_value=copy.deepcopy(BLOCKED)) as ai, \
                 patch.object(engine, 'export'), patch('studio_recovery.render_with_recovery') as render, \
                 patch('paper_video.log'):
                for _ in range(2):
                    engine.execute(job, 'render')
                    state=pv.load_json(job/'job.json')
                    self.assertEqual((state['status'],state['stage']),('review_needed','plan_review'))
                    self.assertIsNone(state['error'])
            ai.assert_called_once()
            render.assert_not_called()
            current = pv.load_json(job / 'review.json')
            self.assertFalse(current['ready'])
            self.assertEqual(current['issues'], BLOCKED['issues'])
            self.assertEqual(current['_plan_review_context_version'], engine.PLAN_REVIEW_CONTEXT_VERSION)

    def test_context_upgrade_does_not_expire_current_encoded_review(self):
        with tempfile.TemporaryDirectory() as folder:
            job, request, plan, _ = setup_job(folder, review=PASSED)
            pv.save_json(job / 'project.json', {'blocks': []})
            (job / 'output/video.mp4').write_bytes(b'encoded fixture')
            final = {'plan_hash': engine.plan_hash(plan),
                     'settings_hash': render_settings_hash(request),
                     'project_sha256': file_hash(job / 'project.json'),
                     'video_sha256': file_hash(job / 'output/video.mp4')}
            with patch.object(engine, 'PLAN_REVIEW_CONTEXT_VERSION', engine.PLAN_REVIEW_CONTEXT_VERSION + 1):
                self.assertFalse(review_stale(job, final, engine.plan_hash(plan)))


if __name__ == '__main__':
    unittest.main()
