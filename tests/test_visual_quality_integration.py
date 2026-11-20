"""Snapshot diagnostics use current rendered evidence, never legacy guesswork."""
import copy
import tempfile
import unittest
from pathlib import Path

import paper_video as pv
from studio_engine import plan_hash
from studio_revision import file_hash, render_settings_hash
from studio_server import snapshot


class VisualQualitySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.job = Path(self.temporary.name)
        (self.job/'analysis').mkdir()
        (self.job/'output').mkdir()
        self.plan = {'blocks': [{'id': 'b1', 'narration': '现有文稿。',
                               'shots': [{'kind': 'image', 'source_id': 'i1', 'duration': 5}]}]}
        self.request = {'content_type': 'general', 'voice': 'zh-CN-YunyangNeural', 'rate': '+0%'}
        self.timeline = [{'block_id': 'b1', 'shot_index': 0, 'kind': 'image', 'file': 'figure.png',
                          'start': 0, 'end': 25, 'render_duration': 25}]
        pv.save_json(self.job/'job.json', {'status': 'completed', 'engine': 'studio'})
        pv.save_json(self.job/'request.json', self.request)
        pv.save_json(self.job/'analysis/studio_plan.json', self.plan)
        pv.save_json(self.job/'analysis/sources.json', [{'id': 'i1', 'type': 'image', 'file': 'figure.png'},
                                                     {'id': 'v1', 'type': 'video', 'file': 'demo.mp4'}])
        pv.save_json(self.job/'project.json', {'blocks': []})
        pv.save_json(self.job/'output/run_report.json', {'duration_seconds': 25})
        pv.save_json(self.job/'output/rendered_shots.json', self.timeline)
        (self.job/'output/video.mp4').write_bytes(b'encoded fixture, not a claimed playable video')

    def tearDown(self):
        self.temporary.cleanup()

    def receipt(self):
        pv.save_json(self.job/'output/render_receipt.json', {
            'plan_hash': plan_hash(self.plan), 'settings_hash': render_settings_hash(self.request),
            'video_sha256': file_hash(self.job/'output/video.mp4'),
            'project_sha256': file_hash(self.job/'project.json')})

    def assert_unmeasured(self):
        result = snapshot(self.job)
        quality = result['visual_quality']
        self.assertEqual(quality['status'], 'advisory')
        self.assertEqual(quality['statistics']['timing_status'], 'unavailable')
        self.assertIsNone(quality['statistics']['measured_shot_seconds'])
        self.assertFalse(any(issue['code'] == 'long_static_hold' for issue in quality['issues']))
        self.assertEqual(quality['statistics']['unused_video_source_ids'], ['v1'])
        return result

    def test_unversioned_legacy_export_does_not_certify_current_measurements(self):
        # Legacy compatibility may allow export, but no receipt binds the saved
        # timeline to the current editable plan. These are distinct decisions.
        result = self.assert_unmeasured()
        self.assertTrue(result['can_export'])

    def test_current_receipt_exposes_measured_advisory_without_mutating_plan(self):
        self.receipt()
        before = {path: path.read_bytes() for path in self.job.rglob('*') if path.is_file()}
        result = snapshot(self.job)
        self.assertTrue(result['can_export'])
        quality = result['visual_quality']
        self.assertNotIn('ready', quality)
        self.assertEqual(quality['statistics']['timing_status'], 'complete')
        holds = [issue for issue in quality['issues'] if issue['code'] == 'long_static_hold']
        self.assertEqual([issue['seconds'] for issue in holds], [25])
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_edited_plan_does_not_inherit_old_rendered_hold(self):
        self.receipt()
        edited = copy.deepcopy(self.plan)
        edited['blocks'][0]['narration'] = '用户修改后的文稿。'
        pv.save_json(self.job/'analysis/studio_plan.json', edited)
        self.assertFalse(self.assert_unmeasured()['can_export'])

    def test_changed_voice_settings_do_not_reuse_old_measured_hold(self):
        self.receipt()
        pv.save_json(self.job/'request.json', {**self.request, 'rate': '+50%'})
        self.assertFalse(self.assert_unmeasured()['can_export'])

    def test_replaced_video_bytes_do_not_reuse_old_timing(self):
        self.receipt()
        (self.job/'output/video.mp4').write_bytes(b'different output')
        self.assertFalse(self.assert_unmeasured()['can_export'])

    def test_changed_hash_bound_timeline_is_neither_current_measurement_nor_export(self):
        self.receipt()
        receipt_path=self.job/'output/render_receipt.json'
        receipt=pv.load_json(receipt_path)
        receipt['timeline_sha256']=file_hash(self.job/'output/rendered_shots.json')
        pv.save_json(receipt_path,receipt)
        self.assertEqual(snapshot(self.job)['visual_quality']['statistics']['timing_status'],'complete')
        changed=copy.deepcopy(self.timeline)
        changed[0].update(end=22,render_duration=22)
        pv.save_json(self.job/'output/rendered_shots.json',changed)
        result=self.assert_unmeasured()
        self.assertFalse(result['can_export'])
        self.assertEqual(result['rendered_segments'],[])

    def test_unreadable_timeline_leaves_plan_statistics_usable(self):
        self.receipt()
        (self.job/'output/rendered_shots.json').write_text('unfinished {', encoding='utf8')
        self.assert_unmeasured()


if __name__ == '__main__':
    unittest.main()
