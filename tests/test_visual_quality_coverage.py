"""Coverage diagnostics distinguish plan weights, actual intervals and identity."""
import copy
import json
import unittest

from studio_visual_quality import visual_quality_report


HASH_A, HASH_B = 'a'*64, 'b'*64


def example():
    kinds = ['image', 'diagram', 'diagram', 'motion', 'diagram', 'video', 'image']
    plan = {'blocks': [{'id': f'b{i}', 'narration': '固定的输入原稿。',
                       'shots': [{'kind': kind, 'source_id': 'v' if kind == 'video' else 'p' if kind == 'image' else '',
                                  'duration': 100, 'in': 0}]}
                      for i, kind in enumerate(kinds)]}
    durations = [26.2, 16.3, 16.7666666667, 17.0333333333, 14.7, 6.6, 54.4]
    actual, start = [], 0
    for i, (kind, seconds) in enumerate(zip(kinds, durations)):
        shot = {'block_id': f'b{i}', 'shot_index': 0, 'kind': 'video' if kind in ('diagram', 'motion') else kind,
                'start': start, 'end': start+seconds, 'file': f'prepared_{i}.mp4'}
        if kind in ('diagram', 'motion'):
            shot[kind] = {'title': '说明'}
        actual.append(shot); start += seconds
    return plan, actual


def still(identifier='p', bbox=None, **extra):
    return {'kind': 'page', 'source_id': identifier, 'bbox': bbox or [], **extra}


def plan_with(*shots):
    return {'blocks': [{'id': f'b{i}', 'shots': [shot]} for i, shot in enumerate(shots)]}


class VisualQualityCoverageTests(unittest.TestCase):
    def test_cross_block_mechanism_run_reports_64_point_8_seconds_not_as_static(self):
        plan, actual = example()
        report = visual_quality_report(plan, [], actual)
        issues = [item for item in report['issues'] if item['code'] == 'long_explanation_sequence']
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['shot_count'], 4)
        self.assertEqual(issues[0]['block_ids'], ['b1', 'b2', 'b3', 'b4'])
        self.assertEqual(issues[0]['seconds'], 64.8)
        self.assertAlmostEqual(issues[0]['start'], 26.2)
        self.assertAlmostEqual(issues[0]['end'], 91)
        self.assertEqual(report['statistics']['measured_seconds_by_kind']['video'], 6.6)
        self.assertEqual(report['statistics']['measured_shot_seconds'], 152)
        self.assertEqual(report['statistics']['longest_measured_explanation_seconds'], 64.8)
        self.assertFalse(any(item['code'] == 'long_static_hold' and item['block_id'] in ('b1','b2','b3','b4')
                             for item in report['issues']))

    def test_plan_counts_and_weights_never_claim_measured_time(self):
        plan, _ = example()
        report = visual_quality_report(plan, [])
        issue = next(item for item in report['issues'] if item['code'] == 'consecutive_explanation_plan')
        self.assertEqual(issue['planned_weight'], 400)
        self.assertNotIn('seconds', issue)
        self.assertIn('不是实测秒数', issue['reason'])
        self.assertIsNone(report['statistics']['longest_measured_explanation_seconds'])
        self.assertEqual(report['rendered_explanation_sequences'], [])
        plan['blocks'][2]['shots'][0]['duration'] = float('nan')
        report = visual_quality_report(plan, [])
        self.assertIsNone(report['planned_explanation_sequences'][0]['planned_weight'])
        json.dumps(report, allow_nan=False)

    def test_gaps_overlaps_unknown_timing_and_real_media_break_measured_sequences(self):
        plan, actual = example()
        for value in (None, 43.0, 42.0):
            changed = copy.deepcopy(actual)
            if value is None:
                changed[2].pop('start');changed[2].pop('end')
            else:
                changed[2]['start'] = value
            report = visual_quality_report(plan, [], changed)
            self.assertFalse(any(sequence['shot_count'] == 4 for sequence in report['rendered_explanation_sequences']))
        only_duration = [{'kind':'motion','render_duration':20}, {'kind':'diagram','render_duration':20}]
        report = visual_quality_report({}, [], only_duration)
        self.assertEqual([sequence['shot_count'] for sequence in report['rendered_explanation_sequences']], [1,1])

    def test_repeated_near_identical_page_crop_is_not_missed_due_to_export_filename(self):
        plan = plan_with(still(bbox=[.175,.067,.632,.178]), still(bbox=[.174,.068,.633,.178]),
                         still(bbox=[.643,.058,.831,.184]))
        sources = [{'id':'p','type':'page','page':5,'content_sha256':HASH_A}]
        result = visual_quality_report(plan, sources)
        repeated = result['coverage']['repeated_image_views']
        self.assertEqual(len(repeated), 1)
        self.assertEqual(repeated[0]['shot_count'], 2)
        self.assertEqual(repeated[0]['crop_match'], 'near_identical_metadata')
        self.assertEqual(result['coverage']['image_view_count'], 2)

    def test_crops_pages_and_highlights_remain_distinct(self):
        sources = [{'id':'p','type':'page','page':1,'content_sha256':HASH_A},
                   {'id':'q','type':'page','page':2,'content_sha256':HASH_A}]
        plan = plan_with(still('p',[0,0,.5,1]), still('p',[.5,0,1,1]), still('q',[0,0,.5,1]),
                         still('p',[0,0,.5,1],focus=[.1,.1,.3,.4]))
        self.assertEqual(visual_quality_report(plan,sources)['coverage']['repeated_image_views'], [])
        # Tiny distinct crops should not merge just because their edges are close.
        plan = plan_with(still('p',[.1,.1,.12,.12]),still('p',[.104,.1,.124,.12]))
        self.assertEqual(visual_quality_report(plan,sources)['coverage']['repeated_image_views'], [])

    def test_identical_file_aliases_share_independent_video_identity(self):
        sources = [{'id':'upload','type':'video','file':'a.mp4','content_sha256':HASH_A},
                   {'id':'author','type':'video','file':'b.mp4','content_sha256':HASH_A}]
        plan = plan_with({'kind':'video','source_id':'author','in':0,'duration':4})
        result = visual_quality_report(plan,sources)
        self.assertEqual(result['statistics']['available_video_count'], 2)
        self.assertEqual(result['coverage']['available_videos']['independent_count'], 1)
        self.assertEqual(result['coverage']['used_videos']['independent_count'], 1)
        self.assertEqual(result['coverage']['unused_alias_source_ids'], ['upload'])
        self.assertFalse(any(item['code']=='unused_video_candidates' for item in result['issues']))

    def test_missing_or_invalid_hashes_never_claim_complete_deduplication(self):
        sources = [{'id':'a','type':'video','file':'same.mp4','content_sha256':HASH_A},
                   {'id':'b','type':'video','file':'same.mp4','content_sha256':'fake'}]
        result = visual_quality_report({},sources)
        summary = result['coverage']['available_videos']
        self.assertEqual(summary['dedup_status'],'partial')
        self.assertEqual(summary['known_hash_count'],1)
        self.assertIsNone(summary['independent_count'])
        self.assertEqual(summary['unhashed_source_ids'],['b'])
        no_hash = visual_quality_report({},[{'id':'a','type':'video','file':'same.mp4'},
                                           {'id':'b','type':'video','file':'same.mp4'}])
        self.assertEqual(no_hash['coverage']['available_videos']['dedup_status'],'unavailable')
        self.assertIsNone(no_hash['coverage']['available_videos']['independent_count'])

    def test_explicit_hashes_override_intake_records_and_invalid_override_is_unknown(self):
        sources = [{'id':'a','type':'video','file':'a.mp4','content_sha256':HASH_A},
                   {'id':'b','type':'video','file':'b.mp4','content_sha256':HASH_A}]
        result = visual_quality_report({},sources,source_hashes={'b':HASH_B})
        self.assertEqual(result['coverage']['available_videos']['independent_count'],2)
        self.assertEqual(result['coverage']['recorded_hash_source_ids'],['a'])
        self.assertEqual(result['coverage']['supplied_hash_source_ids'],['b'])
        result = visual_quality_report({},sources,source_hashes={'b':None})
        self.assertIsNone(result['coverage']['available_videos']['independent_count'])

    def test_repeated_video_selection_reports_metadata_not_played_seconds(self):
        sources = [{'id':'a','type':'video','file':'a.mp4','content_sha256':HASH_A},
                   {'id':'b','type':'video','file':'b.mp4','content_sha256':HASH_A}]
        plan = plan_with({'kind':'video','source_id':'a','in':8,'duration':4,'bbox':[0,0,.5,1]},
                         {'kind':'video','source_id':'b','in':8,'duration':4,'bbox':[0,0,.5,1]},
                         {'kind':'video','source_id':'a','in':10,'duration':4,'bbox':[0,0,.5,1]},
                         {'kind':'video','source_id':'a','in':8,'duration':4,'bbox':[.5,0,1,1]})
        result = visual_quality_report(plan,sources)
        repeated = result['coverage']['repeated_video_selections']
        self.assertEqual(len(repeated),1)
        self.assertEqual(repeated[0]['shot_count'],2)
        self.assertEqual(repeated[0]['identity_basis'],'sha256')
        self.assertNotIn('seconds',repeated[0])
        self.assertEqual(result['coverage']['video_selection_count'],3)

    def test_unhashed_reuse_is_explicitly_source_id_based(self):
        plan = plan_with(still('a'),still('a'),still('b'))
        result = visual_quality_report(plan,[])
        repeat = result['coverage']['repeated_image_views'][0]
        self.assertEqual(repeat['source_ids'],['a'])
        self.assertEqual(repeat['identity_basis'],'source_id_unverified')
        issue = next(item for item in result['issues'] if item['code']=='repeated_image_view')
        self.assertIn('未确认跨来源内容去重',issue['reason'])

    def test_all_animation_script_is_advisory_and_does_not_mutate_inputs(self):
        plan,actual=example();plan['blocks']=plan['blocks'][1:5];actual=actual[1:5]
        sources=[{'id':'v','type':'video','file':'v.mp4','content_sha256':HASH_A}]
        context={'content_type':'script'};before=copy.deepcopy((plan,actual,sources,context))
        report=visual_quality_report(plan,sources,actual,context=context)
        self.assertEqual((plan,actual,sources,context),before)
        self.assertEqual(report['status'],'advisory')
        self.assertNotIn('ready',report)
        self.assertTrue(all(issue['severity']=='suggestion' for issue in report['issues']))
        issue=next(item for item in report['issues'] if item['code']=='long_explanation_sequence')
        self.assertIn('保持给定文稿',issue['reason'])
        self.assertIn('不要求实拍比例',issue['reason'])
        json.dumps(report,ensure_ascii=False,allow_nan=False)


if __name__=='__main__':unittest.main()
