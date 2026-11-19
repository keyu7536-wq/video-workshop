"""Duration choices reach planning/review without rewriting protected scripts."""
import copy
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch

import paper_video as pv
import studio_engine as engine
import studio_server as server
from studio_duration import duration_settings, duration_review_stale, effective_duration_settings
from studio_plan_repair import repair_fingerprint
from studio_revision import render_settings_hash
from studio_display import DISPLAY_KEYS
from studio_story import normalize_framework, STORY_POLICY_VERSION
from tests.test_plan_repair import setup_job, PASSED, BLOCKED, SOURCES
from tests import test_job_recovery as recovery_tests
from tests import test_visual_policy_review as visual_tests


class DurationSettingsTests(unittest.TestCase):
    def test_target_defaults_and_adaptive_memory(self):
        self.assertEqual(effective_duration_settings({}),{'duration_mode':'target','seconds':100})
        self.assertEqual(effective_duration_settings({'seconds':'90'}),{'duration_mode':'target','seconds':90})
        self.assertEqual(effective_duration_settings({'duration_mode':'adaptive','seconds':90}),{'duration_mode':'adaptive'})
        for mode in ('',None,'auto',[],True):
            with self.subTest(mode=mode),self.assertRaises(ValueError):effective_duration_settings({'duration_mode':mode})
        for seconds in (True,29,601,90.5,'bad','90.5'):
            with self.subTest(seconds=seconds),self.assertRaises(ValueError):effective_duration_settings({'seconds':seconds})

    def test_effective_edits_only_and_protected_text_preserved(self):
        old={'script':'原始文案\n\n保持原句。','seconds':90}
        same=duration_settings(old,{'duration_mode':'target','seconds':90})
        self.assertNotIn('_duration_revision',same)
        adaptive=duration_settings(old,{'duration_mode':'adaptive'})
        self.assertIn('_duration_revision',adaptive)
        changed_memory=duration_settings(adaptive,{'seconds':120})
        self.assertEqual(changed_memory['_duration_revision'],adaptive['_duration_revision'])
        self.assertEqual(changed_memory['script'],old['script'])
        self.assertFalse(duration_review_stale({'_duration_settings':{'duration_mode':'adaptive'}},changed_memory))
        self.assertTrue(duration_review_stale({},adaptive))
        self.assertFalse(duration_review_stale({},old))

    def test_legacy_render_hash_unchanged_and_material_edits_invalidate(self):
        old={'seconds':90,'rate':'+25%'}
        keys=('voice','rate','voice_style','delivery_scope','content_type',*DISPLAY_KEYS)
        settings={k:old.get(k) for k in keys}
        settings.update(story_framework=normalize_framework(old.get('story_framework')),story_policy_version=STORY_POLICY_VERSION)
        legacy=hashlib.sha256(json.dumps(settings,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(render_settings_hash(old),legacy)
        self.assertEqual(render_settings_hash(duration_settings(old,{'duration_mode':'target','seconds':90})),legacy)
        adaptive=duration_settings(old,{'duration_mode':'adaptive'})
        self.assertNotEqual(render_settings_hash(adaptive),legacy)
        self.assertEqual(render_settings_hash(adaptive),render_settings_hash(duration_settings(adaptive,{'seconds':120})))
        self.assertNotEqual(render_settings_hash(duration_settings(old,{'seconds':120})),legacy)

    def test_repair_fingerprint_ignores_adaptive_memory_not_effective_duration(self):
        with tempfile.TemporaryDirectory() as folder:
            _,request,plan,_=setup_job(folder)
            self.assertEqual(repair_fingerprint(plan,SOURCES,request),repair_fingerprint(plan,SOURCES,request|{'duration_mode':'target'}))
            adaptive=request|{'duration_mode':'adaptive','seconds':90}
            self.assertEqual(repair_fingerprint(plan,SOURCES,adaptive),repair_fingerprint(plan,SOURCES,adaptive|{'seconds':120}))
            self.assertNotEqual(repair_fingerprint(plan,SOURCES,request),repair_fingerprint(plan,SOURCES,adaptive))

    def test_mode_change_rechecks_once_but_never_rewrites_existing_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            job,request,plan,review=setup_job(folder,{'auto_repair':False},PASSED)
            request=duration_settings(request,{'duration_mode':'adaptive'})
            pv.save_json(job/'request.json',request)
            original=(job/'analysis/studio_plan.json').read_bytes()
            with patch.object(engine,'call_ai',return_value=copy.deepcopy(BLOCKED)) as ai, \
                 patch.object(engine,'export'),patch('studio_recovery.render_with_recovery') as render,patch('paper_video.log'):
                engine.execute(job,'render');engine.execute(job,'render')
            ai.assert_called_once();render.assert_not_called()
            self.assertEqual((job/'analysis/studio_plan.json').read_bytes(),original)
            self.assertEqual(pv.load_json(job/'review.json')['_duration_settings'],{'duration_mode':'adaptive'})


class DurationPromptTests(unittest.TestCase):
    setUp=visual_tests.VisualPolicyReviewTests.setUp
    tearDown=visual_tests.VisualPolicyReviewTests.tearDown
    final=visual_tests.VisualPolicyReviewTests.final

    def test_adaptive_reaches_plan_and_all_auditors_without_a_numeric_budget(self):
        self.request.update(duration_mode='adaptive',seconds=90,script='保持这段文案。')
        prompts=[engine.prompt_for([],[],self.request)]
        def reviewer(prompt,*args):
            prompts.append(prompt)
            return copy.deepcopy(PASSED)
        with patch.object(engine,'call_ai',side_effect=reviewer):
            result=engine.review_plan(self.job,self.plan,[],self.request)
        self.final(reviewer)
        self.assertEqual(len(prompts),4)
        for prompt in prompts:
            self.assertIn('自适应，没有固定秒数目标',prompt)
            self.assertNotIn('"seconds":',prompt)
            self.assertNotIn('目标约90秒',prompt)
        self.assertIn('逐字保留',prompts[0])
        self.assertEqual(result['_duration_settings'],{'duration_mode':'adaptive'})

    def test_omitted_rate_uses_config_default_but_local_voice_does_not_claim_rate_speedup(self):
        with patch('app_config.load_config',return_value={'default_rate':'+50%','default_voice':'zh-CN-YunyangNeural'}):
            edge=engine.prompt_for([],[],{})
            local=engine.prompt_for([],[],{'voice':'qwen:Uncle_Fu','rate':'+50%'})
        self.assertIn('每秒6.8汉字',edge)
        self.assertIn('每秒4.5汉字',local)
        self.assertIn('目标约100秒',edge)


class DurationHTTPTests(unittest.TestCase):
    setUp=recovery_tests.RecoveryHTTPTests.setUp
    job=recovery_tests.RecoveryHTTPTests.job
    post=recovery_tests.RecoveryHTTPTests.post

    def test_create_persists_mode_and_rejects_invalid_choices_before_job_creation(self):
        with patch.object(engine,'ROOT',self.root):
            status,data=self.post('/api/create',{'article':'An article.','duration_mode':'adaptive','seconds':90})
            self.assertEqual(status,200,data)
            saved=pv.load_json(self.root/'jobs'/data['job_id']/'request.json')
            self.assertEqual((saved['duration_mode'],saved['seconds']),('adaptive',90))
            count=len(list((self.root/'jobs').iterdir()))
            for settings in ({'duration_mode':'unknown'},{'duration_mode':'target','seconds':29},{'seconds':601}):
                status,_=self.post('/api/create',{'article':'An article.',**settings})
                self.assertEqual(status,400)
            self.assertEqual(len(list((self.root/'jobs').iterdir())),count)

    def test_edit_render_resume_replan_save_duration_and_preserve_words(self):
        for endpoint,action in (('/api/plan',None),('/api/render','render'),('/api/resume','resume'),('/api/replan','prepare')):
            with self.subTest(endpoint=endpoint):
                job=self.job(name='duration_'+endpoint.rsplit('/',1)[-1])
                plan=pv.load_json(job/'analysis/studio_plan.json')
                engine.validate(plan,[])
                pv.save_json(job/'analysis/studio_plan.json',plan)
                request=pv.load_json(job/'request.json');request['script']=plan['blocks'][0]['narration']
                pv.save_json(job/'request.json',request)
                before=(job/'analysis/studio_plan.json').read_bytes()
                data={'job_id':job.name,'duration_mode':'adaptive','seconds':90}
                if action is None:data['plan']=copy.deepcopy(plan)
                status,result=self.post(endpoint,data)
                self.assertEqual(status,200,result)
                saved=pv.load_json(job/'request.json')
                self.assertEqual(saved['duration_mode'],'adaptive');self.assertIn('_duration_revision',saved)
                self.assertEqual(saved['script'],request['script'])
                self.assertEqual((job/'analysis/studio_plan.json').read_bytes(),before)
                if action:self.assertEqual(self.http.manager.start.call_args.args[:2],(job,action))
                original_request=(job/'request.json').read_bytes()
                status,_=self.post(endpoint,data|{'duration_mode':'invalid'})
                self.assertEqual(status,400)
                self.assertEqual((job/'request.json').read_bytes(),original_request)

    def test_snapshot_flags_actual_duration_changes_but_legacy_target_stays_current(self):
        job=self.job();plan=pv.load_json(job/'analysis/studio_plan.json')
        review={'ready':True,'issues':[],'_plan_hash':engine.plan_hash(plan),'_story_framework':'auto'}
        pv.save_json(job/'review.json',review)
        self.assertFalse(server.snapshot(job)['review']['stale'])
        request=duration_settings(pv.load_json(job/'request.json'),{'duration_mode':'adaptive'})
        pv.save_json(job/'request.json',request)
        self.assertTrue(server.snapshot(job)['review']['stale'])
        review['_duration_settings']=effective_duration_settings(request);pv.save_json(job/'review.json',review)
        request=duration_settings(request,{'seconds':120});pv.save_json(job/'request.json',request)
        self.assertFalse(server.snapshot(job)['review']['stale'])


if __name__=='__main__':unittest.main()
