import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from PIL import Image
import paper_video as pv
from studio_delivery import final_review
from studio_engine import REVIEW_SCHEMA,plan_hash
from studio_revision import file_hash,render_settings_hash


class ReviewAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.job=Path(self.temporary.name)
        (self.job/'output').mkdir();(self.job/'analysis').mkdir()
        self.video=self.job/'output/video.mp4';self.video.write_bytes(b'encoded video fixture')
        pv.save_json(self.job/'project.json',{'blocks':[]})
        pv.save_json(self.job/'output/run_report.json',{'duration_seconds':1})
        pv.save_json(self.job/'output/rendered_shots.json',[{'start':0,'end':1}])
        self.frame=self.job/'analysis/frame.jpg';Image.new('RGB',(16,16),'white').save(self.frame)
        self.frames=[{'time':0.5,'label':'sample','path':str(self.frame)}]
        (self.job/'analysis/final_frames').mkdir()
        pv.save_json(self.job/'analysis/final_frames/coverage.json',{'complete':True,'total_shots':1,
            'covered_shot_indices':[0],'omitted_shot_indices':[],'reason':'fixture covers its only shot'})
        self.request={'content_type':'general','voice':'zh-CN-YunxiNeural'}
        self.plan={'blocks':[]}
        self.ok={'ready':True,'summary':'reviewed fixture','issues':[]}

    def tearDown(self):self.temporary.cleanup()

    def review(self,ai):
        with patch('studio_delivery.review_frames',return_value=self.frames):
            return final_review(self.job,self.request,self.plan,[],ai,REVIEW_SCHEMA)

    def assert_versions(self,result):
        versions={'video_sha256':file_hash(self.video),'plan_hash':plan_hash(self.plan),
            'settings_hash':render_settings_hash(self.request),'project_sha256':file_hash(self.job/'project.json')}
        for entry in [result,*result['reviews']]:
            for key,value in versions.items():self.assertEqual(entry[key],value)

    def test_first_external_failure_is_unavailable_not_passed_and_skips_repeated_request(self):
        reason='provider temporarily unreachable: '+('detail '*400)+'END OF ORIGINAL REASON'
        ai=Mock(side_effect=RuntimeError(reason));before=self.video.read_bytes()
        result,frames=self.review(ai)
        self.assertEqual(ai.call_count,1)
        self.assertFalse(result['ready']);self.assertFalse(result['review_complete'])
        self.assertEqual(result['status'],'unavailable')
        self.assertEqual([r['status'] for r in result['reviews']],['unavailable','skipped'])
        self.assertEqual(result['unavailable_reason'],'RuntimeError: '+reason)
        self.assertEqual(result['reviews'][0]['reason'],'RuntimeError: '+reason)
        self.assertEqual(result['issues'],[])
        self.assertTrue(all(r['frames']==0 and r['prepared_frames']==1 for r in result['reviews']))
        self.assertEqual(frames,self.frames);self.assertEqual(self.video.read_bytes(),before)
        self.assertEqual(pv.load_json(self.job/'output/final_review.json'),result)
        self.assert_versions(result)

    def test_second_failure_preserves_successful_dimension_and_its_issues(self):
        issue={'severity':'warning','block_id':'b1','reason':'Small caption in sampled frame'}
        first={**self.ok,'issues':[issue]}
        result,_=self.review(Mock(side_effect=[first,TimeoutError('story service timed out')]))
        self.assertEqual([r['status'] for r in result['reviews']],['completed','unavailable'])
        self.assertEqual(result['reviews'][0]['summary'],first['summary'])
        self.assertEqual(result['reviews'][0]['frames'],1)
        self.assertEqual(result['issues'],[issue]);self.assertFalse(result['ready'])
        self.assertEqual(pv.load_json(self.job/'output/review_1_visual.json'),result['reviews'][0])
        self.assert_versions(result)

    def test_invalid_json_and_invalid_schema_are_recorded_as_unavailable(self):
        for failure in [json.JSONDecodeError('invalid provider JSON','broken',0),{'ready':True}]:
            with self.subTest(failure=type(failure).__name__):
                ai=Mock(side_effect=failure) if isinstance(failure,Exception) else Mock(return_value=failure)
                result,_=self.review(ai)
                self.assertEqual(ai.call_count,1)
                self.assertEqual(result['status'],'unavailable');self.assertFalse(result['ready'])
                self.assertTrue(result['unavailable_reason'])
                self.assertEqual(result['reviews'][0]['issues'],[])

    def test_completed_review_with_content_error_remains_content_failure(self):
        issue={'severity':'error','block_id':'b1','reason':'Wrong source image'}
        result,_=self.review(Mock(side_effect=[{**self.ok,'issues':[issue]},self.ok]))
        self.assertTrue(result['review_complete']);self.assertEqual(result['status'],'completed')
        self.assertFalse(result['ready']);self.assertEqual(result['issues'],[issue])
        self.assertNotIn('unavailable_reason',result)

    def test_review_can_be_retried_on_same_video_after_provider_recovers(self):
        self.review(Mock(side_effect=ConnectionError('offline')))
        before=self.video.read_bytes()
        result,_=self.review(Mock(return_value=self.ok))
        self.assertTrue(result['ready']);self.assertTrue(result['review_complete'])
        self.assertEqual(result['status'],'completed')
        self.assertEqual(self.video.read_bytes(),before)
        self.assertTrue(all('no auditory listening' in r['inspection'] for r in result['reviews']))
        self.assert_versions(result)

    def test_missing_video_stops_before_model_or_frame_review(self):
        self.video.unlink();ai=Mock()
        with patch('studio_delivery.review_frames') as frames:
            with self.assertRaisesRegex(RuntimeError,'找不到可审核的成片'):
                final_review(self.job,self.request,self.plan,[],ai,REVIEW_SCHEMA)
        frames.assert_not_called();ai.assert_not_called()
        self.assertFalse((self.job/'output/final_review.json').exists())

    def test_frame_decoder_failure_is_not_misclassified_as_external_unavailability(self):
        ai=Mock()
        with patch('studio_delivery.review_frames',side_effect=RuntimeError('frame decoder failure')):
            with self.assertRaisesRegex(RuntimeError,'frame decoder failure'):
                final_review(self.job,self.request,self.plan,[],ai,REVIEW_SCHEMA)
        ai.assert_not_called();self.assertFalse((self.job/'output/final_review.json').exists())

    def test_empty_or_unreadable_frame_evidence_stops_review(self):
        ai=Mock();self.frames=[]
        with self.assertRaisesRegex(RuntimeError,'成片抽帧为空'):self.review(ai)
        self.frames=[{'time':0.5,'path':str(self.frame)}];self.frame.write_bytes(b'not an image')
        with self.assertRaisesRegex(RuntimeError,'成片抽帧无法读取'):self.review(ai)
        ai.assert_not_called();self.assertFalse((self.job/'output/final_review.json').exists())

    def test_changed_video_during_review_cannot_receive_old_hash_approval(self):
        def reviewer(*args):
            self.video.write_bytes(b'changed encoding')
            return self.ok
        with self.assertRaisesRegex(RuntimeError,'审核期间发生变化'):self.review(reviewer)
        self.assertFalse((self.job/'output/final_review.json').exists())

    def test_external_error_redacts_credentials_without_truncating_reason(self):
        with patch.dict('os.environ',{'PAPER_VIDEO_API_KEY':'private-fixture-value'}):
            result,_=self.review(Mock(side_effect=RuntimeError('token private-fixture-value unavailable')))
        self.assertIn('[REDACTED]',result['unavailable_reason'])
        self.assertNotIn('private-fixture-value',json.dumps(result))


if __name__=='__main__':unittest.main()
