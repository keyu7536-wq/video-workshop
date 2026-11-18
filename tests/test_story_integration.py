"""Exercise persisted narrative choices and every model-facing workflow stage."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen

import paper_video as pv
import studio_engine as engine
from studio_server import Handler,StudioHTTPServer,Manager
from studio_revision import render_settings_hash
from studio_story import STORY_POLICY_VERSION,story_instruction
from test_studio import plan_fixture


class StoryIntegrationTests(unittest.TestCase):
    def test_editorial_requirements_invalidate_only_deliberately_changed_requests(self):
        import copy
        from studio_server import editorial_settings
        plan=plan_fixture();original={'brief':'原要求','script':plan['blocks'][0]['narration']+'\n'}
        same=editorial_settings(original,{'brief':'原要求','plan':plan},plan)
        self.assertEqual(same,original)
        self.assertEqual(render_settings_hash(same),render_settings_hash(original))
        changed=editorial_settings(original,{'brief':'新要求','plan':plan},plan)
        self.assertNotEqual(render_settings_hash(changed),render_settings_hash(original))
        edited=copy.deepcopy(plan);edited['blocks'][0]['narration']='人工修改后的正文。'
        updated=editorial_settings(changed,{'plan':edited},plan)
        self.assertEqual(updated['script'],'人工修改后的正文。')
        self.assertEqual(changed['script'],original['script'])
        with self.assertRaises(ValueError):editorial_settings(original,{'brief':[],'plan':plan},plan)

    def test_create_save_render_replan_preserve_choices_and_reject_invalid_before_write(self):
        with tempfile.TemporaryDirectory() as folder:
            job=Path(folder);(job/'analysis').mkdir()
            plan=plan_fixture();text=plan['blocks'][0]['narration']
            pv.save_json(job/'analysis/studio_plan.json',plan)
            pv.save_json(job/'analysis/sources.json',[{'id':'article','type':'text','text':text}])
            pv.save_json(job/'request.json',{'script':text,'story_framework':'auto'})
            pv.save_json(job/'job.json',{'status':'prepared'})
            server=StudioHTTPServer(('127.0.0.1',0),Handler);server.manager=Manager()
            server.manager.start=Mock()
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def post(endpoint,data):
                req=Request(f'http://127.0.0.1:{server.server_port}/api/{endpoint}',data=json.dumps(data).encode(),headers={'Content-Type':'application/json'})
                with urlopen(req) as response:return json.load(response)
            try:
                with patch('studio_server.job_path',return_value=job),patch('studio_server.engine.new_job',return_value=job) as create:
                    post('create',{'article':'An ordinary article.','story_framework':'source_order','content_type':'general'})
                    self.assertEqual(create.call_args.args[0]['story_framework'],'source_order')
                    for endpoint,framework in [('plan','source_order'),('render','problem_solution'),('replan','auto')]:
                        data={'job_id':'fixture','content_type':'general','story_framework':framework}
                        if endpoint=='plan':data['plan']=plan
                        post(endpoint,data)
                        saved=pv.load_json(job/'request.json')
                        self.assertEqual(saved['story_framework'],framework)
                        self.assertEqual(saved['script'],text)
                        self.assertEqual(saved['content_type'],'general')
                        before_plan=(job/'analysis/studio_plan.json').read_bytes()
                        with self.assertRaises(HTTPError):post(endpoint,data|{'story_framework':'unrecognized'})
                        self.assertEqual(pv.load_json(job/'request.json'),saved)
                        self.assertEqual((job/'analysis/studio_plan.json').read_bytes(),before_plan)
            finally:server.shutdown();server.server_close();thread.join()

    def test_all_stages_share_policy_and_user_context(self):
        from studio_research import dossier
        from studio_delivery import final_review
        request={'content_type':'general','story_framework':'source_order','brief':'面向初次接触的观众，轻松清晰','seconds':80}
        plan=plan_fixture();sources=[{'id':'article','type':'text','text':plan['blocks'][0]['narration']}]
        captured=[]
        def reviewer(prompt,*args):
            captured.append(prompt)
            return {'ready':True,'summary':'test fixture, not quality approval','issues':[]}
        with tempfile.TemporaryDirectory() as folder:
            job=Path(folder);(job/'analysis').mkdir();(job/'output').mkdir()
            def research_ai(prompt,*args):
                captured.append(prompt)
                return {'title':'文章','project_identity':'普通文章','story_angle':'原文论述','claims':[], 'missing_information':[],'visual_strategy':'相关素材'}
            dossier(job,request,sources,[],research_ai)
            self.assertIn(story_instruction(request,'dossier'),captured[-1])
            prompt=engine.prompt_for(sources,[],request)
            self.assertIn(story_instruction(request,'plan'),prompt)
            self.assertIn(request['brief'],prompt)
            with patch('studio_engine.call_ai',side_effect=reviewer):
                checked=engine.review_plan(job,plan,sources,request)
            self.assertEqual(checked['_story_framework'],'source_order')
            self.assertEqual(checked['_story_policy_version'],STORY_POLICY_VERSION)
            (job/'output/video.mp4').write_bytes(b'fixture')
            pv.save_json(job/'output/run_report.json',{});pv.save_json(job/'output/rendered_shots.json',[]);pv.save_json(job/'project.json',{})
            from PIL import Image
            frame=job/'analysis/frame.png';Image.new('RGB',(16,16),'green').save(frame)
            with patch('studio_delivery.review_frames',return_value=[{'time':0,'label':'fixture','path':str(frame)}]):final_review(job,request,plan,sources,reviewer,engine.REVIEW_SCHEMA)
            for prompt in captured[1:]:
                self.assertIn(story_instruction(request,'review'),prompt)
                self.assertIn(request['brief'],prompt)

    def test_policy_changes_invalidate_previous_review_without_changing_script(self):
        base={'script':'Preserve my words.'}
        self.assertEqual(render_settings_hash(base),render_settings_hash(base|{'story_framework':'auto'}))
        self.assertNotEqual(render_settings_hash(base),render_settings_hash(base|{'story_framework':'source_order'}))
        with patch('studio_story.STORY_POLICY_VERSION',STORY_POLICY_VERSION+1):changed=render_settings_hash(base)
        self.assertNotEqual(changed,render_settings_hash(base))

    def test_media_only_and_ordinary_facts_do_not_require_a_paper(self):
        from studio_research import dossier,DOSSIER
        from jsonschema import validate
        ai=Mock()
        with tempfile.TemporaryDirectory() as folder:
            job=Path(folder)
            result=dossier(job,{'content_type':'general'},[{'id':'image1','type':'image'}],[],ai)
            self.assertEqual(result['missing_information'],[])
            ai.assert_not_called()
            result['claims']=[{'topic':'event','explanation':'An event from an article.','source_id':'article','quote':'An event from an article.'}]
            validate(result,DOSSIER)
            paper=dossier(job,{'content_type':'paper'},[{'id':'image1','type':'image'}],[],ai)
            self.assertTrue(paper['missing_information'])


if __name__=='__main__':unittest.main()
