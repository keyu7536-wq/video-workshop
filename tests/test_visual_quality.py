"""Advisory diagnostics must not turn timeline metadata into quality claims."""
import copy
import json
import unittest

from studio_visual_quality import visual_quality_report


class VisualQualityTests(unittest.TestCase):
    def plan(self, *shots):
        return {'ready': True, 'blocks': [{'id': 'b1', 'narration': '保留给定原文。', 'shots': list(shots)}]}

    def actual(self, index, start, end, file='crop.png', **extra):
        return {'block_id': 'b1', 'shot_index': index, 'kind': 'image', 'file': file,
                'start': start, 'end': end, **extra}

    def holds(self, result):
        return [issue for issue in result['issues'] if issue['code'] == 'long_static_hold']

    def test_empty_and_legacy_inputs_are_advisory_and_serializable(self):
        for plan, sources in [(None, None), ({}, []), ({'blocks': []}, []), (self.plan({'kind': 'image'}), [])]:
            result = visual_quality_report(plan, sources)
            self.assertEqual(result['status'], 'advisory')
            self.assertNotIn('ready', result)
            self.assertEqual(result['statistics']['timing_status'], 'unavailable')
            self.assertIsNone(result['statistics']['measured_shot_seconds'])
            json.dumps(result, ensure_ascii=False, allow_nan=False)

    def test_video_candidates_are_not_evidence_of_relevance_or_required_usage(self):
        sources = [{'id': 'v1', 'type': 'video', 'file': 'assets/a.mp4'},
                   {'id': 'v2', 'type': 'video', 'file': 'assets/b.mp4'},
                   {'id': 'v2', 'type': 'video', 'file': 'assets/b.mp4'},
                   {'id': 'remote', 'type': 'video', 'url': 'https://example.test/a.mp4'},
                   {'id': 'p1', 'type': 'page', 'file': 'paper.pdf'}]
        plan = self.plan({'kind': 'video', 'source_id': 'v1'}, {'kind': 'page', 'source_id': 'p1'})
        result = visual_quality_report(plan, sources)
        self.assertEqual(result['statistics']['available_video_count'], 2)
        self.assertEqual(result['statistics']['used_video_source_ids'], ['v1'])
        self.assertEqual(result['statistics']['unused_video_source_ids'], ['v2'])
        issue = result['issues'][0]
        self.assertEqual(issue['severity'], 'suggestion')
        self.assertEqual(issue['source_ids'], ['v2'])
        self.assertIn('尚未判断相关性', issue['reason'])
        self.assertIn('不要求插入', issue['reason'])

    def test_duration_weights_do_not_establish_actual_hold_seconds(self):
        plan = self.plan({'kind': 'page', 'source_id': 'p', 'duration': 60})
        for rendered in (None, [{'kind': 'image', 'duration': 60, 'file': 'crop.png'}]):
            result = visual_quality_report(plan, [], rendered)
            self.assertEqual(self.holds(result), [])
            self.assertIsNone(result['statistics']['measured_shot_seconds'])

    def test_measured_same_prepared_file_adjacency_is_combined(self):
        plan = self.plan({'kind': 'page', 'source_id': 'p'}, {'kind': 'page', 'source_id': 'p'})
        result = visual_quality_report(plan, [], [self.actual(0, 0, 10), self.actual(1, 10, 20)])
        self.assertEqual(len(self.holds(result)), 1)
        self.assertEqual(self.holds(result)[0]['seconds'], 20)
        self.assertEqual(self.holds(result)[0]['shot_index'], 0)
        self.assertEqual(self.holds(result)[0]['source_ids'], ['p'])

    def test_different_crops_files_or_discontinuous_intervals_do_not_combine(self):
        cases = [
            [self.actual(0, 0, 10, 'crop1.png'), self.actual(1, 10, 20, 'crop2.png')],
            [self.actual(0, 0, 10, bbox=[0, 0, .5, 1]), self.actual(1, 10, 20, bbox=[.5, 0, 1, 1])],
            [self.actual(0, 0, 10), self.actual(1, 11, 21)],
            [self.actual(0, 0, 10), self.actual(1, 8, 18)],
            [self.actual(0, 0, 10, file=''), self.actual(1, 10, 20, file='')],
        ]
        for actual in cases:
            with self.subTest(actual=actual):
                self.assertEqual(self.holds(visual_quality_report({}, [], actual)), [])

    def test_legacy_render_inherits_distinct_crops_from_matching_plan_shots(self):
        plan = self.plan({'kind': 'page', 'source_id': 'p', 'bbox': [0, 0, .5, 1]},
                         {'kind': 'page', 'source_id': 'p', 'bbox': [.5, 0, 1, 1]})
        result = visual_quality_report(plan, [], [self.actual(0, 0, 10), self.actual(1, 10, 20)])
        self.assertEqual(self.holds(result), [])

    def test_measured_video_between_static_shots_breaks_the_hold(self):
        actual = [self.actual(0, 0, 12), self.actual(1, 12, 15, file='action.mp4', kind='video'),
                  self.actual(2, 15, 27)]
        result = visual_quality_report({}, [], actual)
        self.assertEqual(self.holds(result), [])
        self.assertEqual(result['statistics']['rendered_shot_counts']['video'], 1)

    def test_staged_images_and_diagrams_are_not_pure_static_holds(self):
        plan = self.plan({'kind': 'page', 'visual_beats': [{'cue': '观察左侧'}]},
                         {'kind': 'diagram', 'diagram': {'nodes': [{'id': 'a'}]}},
                         {'kind': 'image'})
        actual = [self.actual(0, 0, 25), self.actual(1, 25, 50, kind='video'), self.actual(2, 50, 75)]
        result = visual_quality_report(plan, [], actual)
        self.assertEqual(result['statistics']['planned_shot_counts']['staged_image'], 1)
        self.assertEqual(result['statistics']['rendered_shot_counts']['diagram'], 1)
        self.assertEqual([issue['shot_index'] for issue in self.holds(result)], [2])

    def test_rendered_stage_metadata_is_respected_without_plan(self):
        for field in ('visual_beats', 'visual_timeline', 'visual_beat_timeline'):
            with self.subTest(field=field):
                actual = self.actual(0, 0, 25, **{field: [{'start': 0}]})
                self.assertEqual(self.holds(visual_quality_report({}, [], [actual])), [])

    def test_rendered_interval_overrides_planned_or_inconsistent_weight(self):
        result = visual_quality_report(self.plan({'kind': 'image', 'duration': 5}), [],
                                       [self.actual(0, 0, 26.3, render_duration=5)])
        self.assertEqual(self.holds(result)[0]['seconds'], 26.3)
        self.assertEqual(result['statistics']['timing_status'], 'complete')

    def test_measured_duration_without_timestamps_is_not_merged(self):
        actual = [{'kind': 'image', 'file': 'same.png', 'render_duration': 12},
                  {'kind': 'image', 'file': 'same.png', 'render_duration': 12},
                  {'kind': 'image', 'file': 'last.png', 'render_duration': 19}]
        result = visual_quality_report({}, [], actual)
        self.assertEqual([issue['seconds'] for issue in self.holds(result)], [19])
        self.assertEqual(result['statistics']['measured_shot_seconds'], 43)

    def test_missing_or_nonfinite_timing_is_partial_and_json_safe(self):
        actual = [self.actual(0, 0, 20), {'kind': 'image', 'duration': 99},
                  {'kind': 'image', 'render_duration': float('nan')},
                  {'kind': 'image', 'start': False, 'end': True}]
        result = visual_quality_report({}, [], actual)
        self.assertEqual(result['statistics']['timing_status'], 'partial')
        self.assertEqual(result['statistics']['measured_shot_count'], 1)
        json.dumps(result, allow_nan=False)

    def test_report_does_not_mutate_script_plan_sources_or_render(self):
        plan = self.plan({'kind': 'page', 'source_id': 'p', 'bbox': [0, 0, 1, 1]})
        sources = [{'id': 'v', 'type': 'video', 'file': 'v.mp4'}]
        actual = [self.actual(0, 0, 20)]
        before = copy.deepcopy((plan, sources, actual))
        result = visual_quality_report(plan, sources, actual)
        self.assertEqual((plan, sources, actual), before)
        self.assertTrue(plan['ready'])
        self.assertNotIn('ready', result)
        self.assertTrue(all(issue['severity'] == 'suggestion' for issue in result['issues']))

    def test_stale_diagram_metadata_does_not_turn_an_image_into_animation(self):
        plan=self.plan({'kind':'image','source_id':'p','diagram':{'title':'old'}})
        result=visual_quality_report(plan,[],[self.actual(0,0,20)])
        self.assertEqual(result['statistics']['planned_shot_counts']['image'],1)
        self.assertTrue(any(issue['code']=='long_static_hold' for issue in result['issues']))


if __name__ == '__main__':
    unittest.main()
