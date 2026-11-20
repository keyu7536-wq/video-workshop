"""Visual indexing uses current bytes and observed times, with optional failure."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

import studio_media_index as mi


class MediaIndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.job=Path(self.temporary.name).resolve()
        (self.job/'assets').mkdir()
        self.video=self.job/'assets/video.mp4'
        self.video.write_bytes(b'current video bytes; decode is independently exercised in integration')
        self.source={'id':'v1','type':'video','file':str(self.video),'duration_seconds':10,
                     'sample_times':[0,2,4,6,8]}
        self.sources=[self.source]
        self.response={'segments':[{'id':'candidate','source_id':'v1','start':0,'end':5,
                         'description':'采样帧中可见蓝色物体位置不同。','visual_role':'demonstration',
                         'observed_times':[0,2,4],'limitations':['帧间动作未观察。']}],'gaps':[]}
        def sample(path,folder,times):
            folder.mkdir(parents=True,exist_ok=True)
            target=folder/'sample.jpg'
            Image.new('RGB',(40,40),'blue').save(target)
            return [target]
        self.sample=patch.object(mi,'_sample_video',side_effect=sample).start()
        self.addCleanup(patch.stopall)
        self.ai=Mock(side_effect=lambda *args:copy.deepcopy(self.response))

    def build(self,ai=None,sources=None):
        return mi.build_media_index(self.job,{'backend':'codex'},self.sources if sources is None else sources,
                                    [('v1 视频拼图 0','old-sheet.jpg')],ai or self.ai)

    def shot(self,report,**changes):
        return {'kind':'video','source_id':'v1','in':0,'duration':4,
                'media_segment_id':report['segments'][0]['id'],
                'selection_reason':'展示旁白正在描述的物体位置。',**changes}

    def test_valid_segments_are_source_bound_and_do_not_mutate_inputs(self):
        before=copy.deepcopy(self.sources)
        report=self.build()
        self.assertEqual(report['status'],'completed')
        self.assertEqual(report['coverage']['indexed_video_count'],1)
        segment=report['segments'][0]
        self.assertEqual(segment['observed_times'],[0,2,4])
        self.assertFalse(segment['continuous_motion_verified'])
        self.assertFalse(segment['audio_reviewed'])
        self.assertIn(mi.OBSERVATION_LIMIT,segment['limitations'])
        self.assertEqual(self.sources,before)
        self.assertTrue((self.job/'analysis/media_index.json').is_file())
        prompt,images,folder,request,schema,suffix=self.ai.call_args.args
        self.assertEqual(schema,mi.INDEX_SCHEMA)
        self.assertEqual(len(images),1)
        self.assertIn('不能凭文件名',prompt)
        self.assertIn('sampled_frames',mi.media_index_context(report))
        mi.validate_media_selection(self.shot(report),report,self.sources,self.job)

    def test_cached_current_result_skips_sampling_and_provider(self):
        first=self.build();second=self.build()
        self.assertEqual(self.ai.call_count,1)
        self.assertEqual(self.sample.call_count,1)
        self.assertTrue(second['sources'][0]['cache_hit'])
        self.assertEqual(first['segments'],second['segments'])

    def test_changed_video_bytes_invalidate_cache_and_segment_ids(self):
        old=self.build()
        self.video.write_bytes(b'replacement video with different bytes')
        with self.assertRaisesRegex(ValueError,'已变更'):
            mi.validate_media_selection(self.shot(old),old,self.sources,self.job)
        new=self.build()
        self.assertEqual(self.ai.call_count,2)
        self.assertEqual(self.sample.call_count,2)
        self.assertNotEqual(old['segments'][0]['id'],new['segments'][0]['id'])

    def test_sampling_list_and_policy_changes_invalidate_cache(self):
        self.build()
        self.source['sample_times']=[0,2,4,6,8,9]
        self.build()
        with patch.object(mi,'POLICY_VERSION',mi.POLICY_VERSION+1):
            self.build()
        self.assertEqual(self.ai.call_count,3)

    def test_corrupt_cache_rebuilds_instead_of_reusing_or_staying_blocked(self):
        self.build()
        cache=next((self.job/'analysis/media_index').glob('*/index.json'))
        cache.write_text('broken {',encoding='utf8')
        report=self.build()
        self.assertEqual(self.ai.call_count,2)
        self.assertEqual(report['status'],'completed')
        self.assertTrue(report['sources'][0]['cache_rebuilt_from_invalid'])

    def test_invalid_unobserved_or_out_of_range_model_spans_never_become_choices(self):
        changes=[{'observed_times':[1]}, {'observed_times':[0,0]}, {'source_id':'another'},
                 {'start':6,'end':10}, {'end':11}, {'start':5,'end':5},
                 {'description':'   '}, {'start':float('nan')}]
        for change in changes:
            with self.subTest(change=change):
                response=copy.deepcopy(self.response)
                response['segments'][0].update(change)
                report=self.build(Mock(return_value=response))
                self.assertEqual(report['segments'],[])
                self.assertEqual(report['status'],'unavailable')
                self.assertEqual(report['gaps'][0]['code'],'invalid_result')

    def test_empty_samples_missing_source_or_decode_failure_are_explicit_gaps(self):
        for change in ({'sample_times':[]},{'file':str(self.job/'missing.mp4')},{'duration_seconds':1000}):
            with self.subTest(change=change):
                report=self.build(sources=[{**self.source,**change}])
                self.assertEqual(report['segments'],[])
                self.assertEqual(report['gaps'][0]['code'],'invalid_source')
        self.sample.side_effect=RuntimeError('unreadable media')
        report=self.build()
        self.assertEqual(report['gaps'][0]['code'],'sampling_failed')
        self.ai.assert_not_called()

    def test_provider_outage_does_not_fabricate_success_or_call_every_video(self):
        sources=[self.source,{**self.source,'id':'v2'}]
        provider=Mock(side_effect=ConnectionError('service unavailable'))
        report=self.build(provider,sources)
        self.assertEqual(report['status'],'unavailable')
        self.assertEqual(report['segments'],[])
        self.assertEqual(provider.call_count,1)
        self.assertEqual([item['code'] for item in report['gaps']],['unavailable','unavailable'])
        recovered=self.build()
        self.assertEqual(recovered['status'],'completed')

    def test_source_changed_during_provider_call_is_not_cached_as_success(self):
        def provider(*args):
            self.video.write_bytes(b'changed while looking')
            return copy.deepcopy(self.response)
        report=self.build(provider)
        self.assertEqual(report['segments'],[])
        self.assertEqual(report['gaps'][0]['code'],'invalid_result')

    def test_bounded_sample_subset_only_exposes_attached_times(self):
        self.source.update(duration_seconds=100,sample_times=list(range(100)))
        self.response['segments'][0].update(start=0,end=60,observed_times=[0])
        report=self.build()
        times=self.sample.call_args.args[2]
        self.assertEqual(len(times),18)
        self.assertTrue(set(times)<=set(self.source['sample_times']))
        self.assertEqual(times[0],0);self.assertEqual(times[-1],99)
        self.assertEqual(report['status'],'partial')
        self.assertIn('sampling_budget',[item['code'] for item in report['gaps']])
        with self.assertRaisesRegex(ValueError,'本轮实际附图'):
            bad=copy.deepcopy(self.response);bad['segments'][0]['observed_times']=[1]
            mi.validate_index_result(bad,self.source,times,'test')

    def test_video_count_budget_and_no_video_case(self):
        sources=[{**self.source,'id':f'v{index}'} for index in range(17)]
        def provider(prompt,*args):
            identifier=json.loads(prompt.split('\n')[-1])['source_id']
            result=copy.deepcopy(self.response);result['segments'][0]['source_id']=identifier
            return result
        report=self.build(provider,sources)
        self.assertEqual(report['coverage']['indexed_video_count'],16)
        self.assertEqual(report['sources'][-1]['status'],'budget_omitted')
        self.assertEqual(self.sample.call_count,16)
        empty=self.build(sources=[])
        self.assertEqual(empty['status'],'not_applicable')

    def test_optional_manual_choice_survives_unavailable_index(self):
        mi.validate_media_selection({'kind':'video'},None,self.sources,self.job)
        report=self.build()
        for changes in ({'in':5,'duration':1},{'in':1,'duration':.5}, {'source_id':'v2'},
                        {'media_segment_id':'missing'}, {'kind':'image'}, {'selection_reason':''}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                mi.validate_media_selection(self.shot(report,**changes),report,self.sources,self.job)

    def test_relative_source_path_supported_with_job_context(self):
        self.source['file']='assets/video.mp4'
        report=self.build()
        mi.validate_media_selection(self.shot(report),report,self.sources,self.job)
        with self.assertRaisesRegex(ValueError,'当前任务目录'):
            mi.validate_media_selection(self.shot(report),report,self.sources)

    def test_no_identifiable_segment_remains_an_explicit_gap(self):
        report=self.build(Mock(return_value={'segments':[],'gaps':['画面看不清。']}))
        self.assertEqual(report['status'],'partial')
        self.assertEqual(report['segments'],[])
        self.assertIn('no_segments',[item['code'] for item in report['gaps']])


if __name__=='__main__':
    unittest.main()
