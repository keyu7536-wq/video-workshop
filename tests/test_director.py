import copy
from types import MappingProxyType
import unittest

from jsonschema import Draft202012Validator

from studio_director import (
    DIRECTOR_POLICY_VERSION, EXPLANATION_SCHEMA, VISUAL_STRATEGIES,
    director_instruction, normalize_explanation, validate_explanation,
)


def explanation(**changes):
    value = {'question': '这一步改变了什么？', 'takeaway': '阀门关闭后流量下降，比较条件保持一致。',
             'visual_strategy': 'comparison', 'continuity': '沿用上一段同一装置的方向。'}
    value.update(changes)
    return value


class ExplanationMetadataTests(unittest.TestCase):
    def test_legacy_absent_or_null_metadata_is_accepted_without_fabricating_intent(self):
        block = {'narration': '保留旧稿。'}
        self.assertIsNone(normalize_explanation(block.get('explanation')))
        self.assertIsNone(validate_explanation(None))
        self.assertEqual(block, {'narration': '保留旧稿。'})
        Draft202012Validator(EXPLANATION_SCHEMA).validate(None)

    def test_schema_and_validator_accept_all_visual_tasks_and_an_empty_transition(self):
        Draft202012Validator.check_schema(EXPLANATION_SCHEMA)
        validator = Draft202012Validator(EXPLANATION_SCHEMA)
        for strategy in VISUAL_STRATEGIES:
            with self.subTest(strategy=strategy):
                value = explanation(visual_strategy=strategy, continuity='')
                validator.validate(value)
                self.assertEqual(normalize_explanation(value), value)

    def test_normalization_copies_metadata_and_preserves_user_text_elsewhere(self):
        value = explanation(question='  为什么先关阀门？  ', continuity='\n')
        before = copy.deepcopy(value)
        normalized = normalize_explanation(MappingProxyType(value))
        self.assertEqual(normalized['question'], '为什么先关阀门？')
        self.assertEqual(normalized['continuity'], '')
        normalized['takeaway'] = '另一个编辑结果'
        self.assertEqual(value, before)

    def test_wrong_types_missing_fields_and_unknown_fields_are_rejected(self):
        invalid = ['', [], False, 0, explanation(question=None), explanation(takeaway=True),
                   explanation(continuity=[]), explanation(visual_strategy='Comparison'),
                   explanation(visual_strategy=' comparison '), explanation(visual_strategy='invented'),
                   explanation(extra='not allowed')]
        missing = explanation(); del missing['question']; invalid.append(missing)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_explanation(value)

    def test_empty_questions_or_takeaways_are_not_silently_replaced(self):
        for field in ('question', 'takeaway'):
            for text in ('', ' \n\t'):
                with self.subTest(field=field, text=text), self.assertRaisesRegex(ValueError, field):
                    normalize_explanation(explanation(**{field: text}))

    def test_each_field_limit_matches_the_schema_without_shared_field_aliases(self):
        fields = EXPLANATION_SCHEMA['anyOf'][1]['properties']
        self.assertEqual([fields[name]['maxLength'] for name in ('question', 'takeaway', 'continuity')],
                         [240, 320, 240])
        self.assertIsNot(fields['question'], fields['continuity'])
        validator = Draft202012Validator(EXPLANATION_SCHEMA)
        for field in ('question', 'takeaway', 'continuity'):
            limit = fields[field]['maxLength']
            valid = explanation(**{field: '字' * limit})
            validator.validate(valid); validate_explanation(valid)
            invalid = explanation(**{field: '字' * (limit + 1)})
            self.assertFalse(validator.is_valid(invalid))
            with self.assertRaisesRegex(ValueError, field):
                validate_explanation(invalid)


class DirectorInstructionTests(unittest.TestCase):
    def test_protected_script_has_priority_without_echoing_or_mutating_the_request(self):
        for framework in ('auto', 'problem_solution', 'source_order'):
            request = {'script': '私有原稿。\n\n顺序不变。', 'story_framework': framework,
                       'content_type': 'paper', 'api_key': 'private-key-not-for-prompt'}
            before = copy.deepcopy(request)
            for stage in ('plan', 'review'):
                result = director_instruction(request, stage)
                self.assertIn('逐字保留文字与顺序', result)
                self.assertNotIn('私有原稿', result)
                self.assertNotIn('private-key', result)
                self.assertNotIn('连续性设计不授权重排原文论述', result)
            self.assertEqual(request, before)

    def test_general_and_automatic_inputs_are_not_forced_into_research(self):
        general = director_instruction({'content_type': 'general'})
        automatic = director_instruction({})
        ordered = director_instruction({'content_type': 'general', 'story_framework': 'source_order'})
        self.assertIn('按文章、事件或过程本身规划', general)
        self.assertIn('PDF 不自动等于论文', automatic)
        self.assertIn('连续性设计不授权重排原文论述', ordered)

    def test_excerpt_does_not_acquire_a_full_paper_obligation(self):
        for stage in ('plan', 'review'):
            result = director_instruction({'content_type': 'paper', 'delivery_scope': 'opening_sample'}, stage)
            self.assertIn('只规划和审核所选内容', result)
            self.assertNotIn('完整论文解读要尽早', result)

    def test_review_distinguishes_intent_from_actual_evidence_and_legacy_compatibility(self):
        result = director_instruction({}, 'review')
        self.assertIn('具体 block/shot 和证据', result)
        self.assertIn('缺失或为 null 不单独判错', result)
        self.assertIn('字段完整也不代表通过', result)
        self.assertIn('事实错误、误导或关键关系无法理解', result)
        self.assertIn('不按关键词、镜头比例或美感打分', result)

    def test_source_and_beat_constraints_are_present_without_provider_specific_claims(self):
        result = director_instruction({'backend': 'example', 'model': 'example'})
        self.assertIn('下载过不等于看过', result)
        self.assertIn('相对完整原始页面或图片', result)
        self.assertIn('必须位于该镜头 bbox 裁图内', result)
        self.assertIn('首个 cue 为空', result)
        self.assertIn('唯一出现且按先后排列', result)
        self.assertIn('不得与 focus 或 column_crop 同用', result)
        self.assertIn('不强制某种镜头类型、固定章节或视频使用比例', result)
        self.assertNotIn('example', result)
        self.assertLess(len(result), 2400)
        self.assertIsInstance(DIRECTOR_POLICY_VERSION, int)

    def test_invalid_requests_and_stages_fail_instead_of_silently_changing_route(self):
        for request in ([], None, {'content_type': 'invented'}, {'story_framework': ''}, {'script': []}):
            with self.subTest(request=request), self.assertRaises(ValueError):
                director_instruction(request)
        with self.assertRaises(ValueError):
            director_instruction({}, 'unknown')


if __name__ == '__main__':
    unittest.main()
