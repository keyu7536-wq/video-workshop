import unittest

from studio_story import STORY_POLICY_VERSION, normalize_framework, story_instruction, _route


class StoryFrameworkTests(unittest.TestCase):
    def test_policy_has_a_version_for_review_invalidation(self):
        self.assertIsInstance(STORY_POLICY_VERSION, int)
        self.assertGreaterEqual(STORY_POLICY_VERSION, 5)

    def test_supported_frameworks_and_invalid_values(self):
        self.assertEqual(normalize_framework(None), 'auto')
        for value in ('auto', 'problem_solution', 'source_order'):
            self.assertEqual(normalize_framework(value), value)
        for value in ('', 'invented', 'AUTO', 1, True, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_framework(value)

    def test_supplied_narration_overrides_every_selected_order(self):
        for framework in ('auto', 'problem_solution', 'source_order'):
            request = {'story_framework':framework, 'content_type':'paper', 'script':'先说结果。\n\n再说明方法。'}
            self.assertEqual(_route(request), ('protected', 'paper'))
            for stage in ('dossier', 'plan', 'review'):
                result = story_instruction(request, stage)
                self.assertIn('逐字保留原有文字与先后顺序', result)
                self.assertNotIn('按以下五步保持相对顺序', result)

    def test_explicit_source_order_does_not_receive_paper_default(self):
        request = {'story_framework':'source_order', 'content_type':'paper'}
        self.assertEqual(_route(request), ('source_order', 'paper'))
        result = story_instruction(request)
        self.assertIn('沿主要原文的论述先后', result)
        self.assertNotIn('常见方法 → 具体问题 → 本文解决什么', result)

    def test_general_content_never_gets_paper_five_step_template(self):
        for framework in ('auto', 'problem_solution', 'source_order'):
            result = story_instruction({'content_type':'general', 'story_framework':framework})
            self.assertNotIn('常见方法 → 具体问题 → 本文解决什么', result)
        chosen = story_instruction({'content_type':'general', 'story_framework':'problem_solution'})
        self.assertIn('不要求五章或固定讲述顺序', chosen)
        self.assertIn('合并或重排', chosen)
        self.assertIn('不编造实验', chosen)

    def test_paper_default_has_flexible_coverage_and_mechanism_requirements(self):
        result = story_instruction({'content_type':'paper'})
        self.assertIn('常见方法、具体问题、本文解决什么、方法如何起作用、效果如何', result)
        self.assertIn('不是固定顺序、固定段数或每项等长的模板', result)
        self.assertIn('合并、重排和分配篇幅', result)
        self.assertNotIn('按以下五步保持相对顺序', result)
        self.assertNotIn('默认使用五步主线', result)
        self.assertIn('瓶颈产生在哪一步、为什么产生', result)
        self.assertIn('同一任务、基线、指标和实验条件', result)
        self.assertIn('brief 中明确指定', result)

    def test_automatic_framework_uses_audience_runtime_and_pacing(self):
        for content in ('paper', 'general', 'auto'):
            result = story_instruction({'content_type':content})
            self.assertIn('目标观众、目标时长和观看节奏', result)
            self.assertIn('因果解释、时间顺序、事件叙事', result)
            self.assertNotIn('按以下五步保持相对顺序', result)
            review = story_instruction({'content_type':content}, 'review')
            self.assertIn('不以五类信息出现的顺序、段数或篇幅相等作为通过条件', review)

    def test_automatic_type_is_conditional_not_extension_inference(self):
        result = story_instruction({'content_type':'auto', 'files':[{'name':'article.pdf'}]})
        self.assertIn('PDF 格式、文件名、技术术语本身都不是论文证据', result)
        self.assertIn('若是普通文章', result)
        self.assertEqual(_route({'script':' \n', 'content_type':'auto'}), ('auto', 'auto'))

    def test_stage_guidance_is_distinct_and_respects_existing_schema(self):
        request = {'content_type':'paper'}
        dossier = story_instruction(request, 'dossier')
        plan = story_instruction(request, 'plan')
        review = story_instruction(request, 'review')
        self.assertIn('暂不写口播', dossier)
        self.assertIn('missing_information', dossier)
        self.assertIn('每段匹配能支持当前观点的素材', plan)
        self.assertIn('block_id/实际段落及具体证据', review)
        self.assertIn('不能只看是否出现', review)
        self.assertIn('只检查其实际覆盖部分', review)
        with self.assertRaises(ValueError):
            story_instruction(request, 'reviev')

    def test_same_policy_for_all_providers_without_embedding_private_data(self):
        base = {'content_type':'paper', 'story_framework':'problem_solution'}
        codex = story_instruction(base | {'backend':'codex', 'key':'private-value', 'brief':'正文要求'})
        api = story_instruction(base | {'backend':'api', 'api_base':'https://example.invalid/v1', 'model':'example', 'key':'different-value'})
        self.assertEqual(codex, api)
        self.assertNotIn('private-value', codex)
        self.assertNotIn('example.invalid', api)

    def test_malformed_input_rejected_before_prompt_generation(self):
        for request in (None, [], {'content_type':'invented'}, {'script':['text']}):
            with self.subTest(request=request), self.assertRaises(ValueError):
                story_instruction(request)

    def test_excerpt_scope_applies_to_all_stages_without_expanding_script(self):
        for stage in ('dossier', 'plan', 'review'):
            result = story_instruction({'content_type':'paper', 'delivery_scope':'opening_sample', 'script':'原文第一句。'}, stage)
            self.assertIn('当前任务是开场节选', result)
            self.assertIn('逐字保留原有文字与先后顺序', result)
            self.assertNotIn('按以下五步保持相对顺序', result)
        review = story_instruction({'content_type':'paper', 'script':'原文第一句。'}, 'review')
        self.assertIn('不能单凭该缺项判为叙事错误', review)


if __name__ == '__main__':
    unittest.main()
