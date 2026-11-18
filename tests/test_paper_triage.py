"""Paper-screening intent travels through existing requests without a new schema.

Model calls and encoded-frame acquisition are mocked: these checks verify
request/prompt integration and deterministic guards, not editorial quality.
"""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request, urlopen

from PIL import Image

import paper_video as pv
import studio_engine as engine
from studio_server import Handler, StudioHTTPServer, Manager
from studio_story import STORY_POLICY_VERSION


BRIEF = '用途：论文初筛，帮助决定是否精读。我关注低数据条件下的方法机制与适用边界，不需要评分。'
NARRATION = '这段说明依据来源展开，尚未验证的条件需要保留。'


def fixture_plan():
    return {'title':'理解研究范围','summary':'说明来源中的方法与边界。',
            'source_issues':[],'unused_sources':[], 'blocks':[{
                'id':'b1','chapter':'研究范围','narration':NARRATION,
                'evidence':[{'source_id':'article','quote':NARRATION}],
                'shots':[{'kind':'card','source_id':'','in':0,'duration':8,'bbox':[],
                          'label':'研究范围','card_text':NARRATION,'cue':''}]}]}


def sources():
    return [{'id':'article','type':'text','name':'材料','text':NARRATION}]


class PaperTriageTests(unittest.TestCase):
    def test_create_api_persists_optional_purpose_in_brief_without_forcing_framework(self):
        with tempfile.TemporaryDirectory() as folder:
            server=StudioHTTPServer(('127.0.0.1',0),Handler);server.manager=Manager()
            server.manager.start=Mock()
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with patch('studio_engine.ROOT',Path(folder)):
                    data={'article':NARRATION,'brief':BRIEF,'content_type':'auto',
                          'story_framework':'source_order','auto_render':False}
                    request=Request(f'http://127.0.0.1:{server.server_port}/api/create',
                                    data=json.dumps(data).encode(),headers={'Content-Type':'application/json'})
                    with urlopen(request) as response:result=json.load(response)
                    saved=pv.load_json(Path(folder)/'jobs'/result['job_id']/'request.json')
                self.assertEqual(saved['brief'],BRIEF)
                self.assertEqual(saved['story_framework'],'source_order')
                self.assertEqual(saved['content_type'],'auto')
                self.assertNotIn('purpose',saved)
                self.assertFalse(saved['auto_render'])
                self.assertEqual(server.manager.start.call_args.args[1],'prepare')
            finally:
                server.shutdown();server.server_close();thread.join()

    def test_triage_brief_and_contract_reach_dossier_plan_and_both_reviews(self):
        from studio_research import dossier
        from studio_delivery import final_review
        request={'brief':BRIEF,'content_type':'paper','story_framework':'source_order','seconds':90}
        captured=[]
        def research_ai(prompt,*args):
            captured.append(('dossier',prompt))
            return {'title':'研究范围','project_identity':'来源测试夹具','story_angle':'按来源理解',
                    'claims':[],'missing_information':[],'visual_strategy':'来源对应画面'}
        def review_ai(prompt,*args):
            captured.append(('review',prompt))
            return {'ready':True,'summary':'测试夹具，不代表质量批准','issues':[]}
        with tempfile.TemporaryDirectory() as folder:
            job=Path(folder);(job/'analysis').mkdir();(job/'output').mkdir()
            plan=fixture_plan()
            dossier(job,request,sources(),[],research_ai)
            captured.append(('plan',engine.prompt_for(sources(),[],request)))
            with patch('studio_engine.call_ai',side_effect=review_ai):
                result=engine.review_plan(job,plan,sources(),request)
            self.assertEqual(result['_story_policy_version'],STORY_POLICY_VERSION)
            self.assertEqual(result['_story_framework'],'source_order')
            (job/'output/video.mp4').write_bytes(b'mocked encoded artifact, not a video')
            pv.save_json(job/'project.json',{})
            pv.save_json(job/'output/run_report.json',{})
            pv.save_json(job/'output/rendered_shots.json',[])
            frame=job/'analysis/frame.png';Image.new('RGB',(16,16),'green').save(frame)
            with patch('studio_delivery.review_frames',return_value=[{'time':0,'label':'fixture','path':str(frame)}]):
                final_review(job,request,plan,sources(),review_ai,engine.REVIEW_SCHEMA)
            self.assertEqual(len(captured),5)
            for stage,prompt in captured:
                self.assertIn(BRIEF,prompt)
                self.assertIn('paper triage',prompt)
                self.assertIn('用户显式选择“按原文顺序”',prompt)
                self.assertIn('score',prompt)

    def test_screening_purpose_does_not_authorize_script_rewriting_or_five_sections(self):
        plan=fixture_plan()
        request={'brief':BRIEF,'content_type':'paper','script':NARRATION,'delivery_scope':'opening_sample'}
        engine.validate(plan,sources(),NARRATION)
        self.assertEqual(len(plan['blocks']),1)
        prompt=engine.prompt_for(sources(),[],request)
        self.assertIn('逐字保留原有文字与先后顺序',prompt)
        self.assertIn('当前任务是开场节选',prompt)
        changed=copy.deepcopy(plan);changed['blocks'][0]['narration']+='因此这篇必须精读。'
        with self.assertRaisesRegex(ValueError,'改写|遗漏'):
            engine.validate(changed,sources(),NARRATION)

    def test_general_article_still_validates_and_invented_score_evidence_is_rejected(self):
        plan=fixture_plan();engine.validate(plan,sources())
        request={'brief':'按事件顺序解释这篇普通文章。','content_type':'general'}
        prompt=engine.prompt_for(sources(),[],request)
        self.assertIn('本任务明确为通用讲解',prompt)
        self.assertIn('未选择此用途',prompt)
        invalid=copy.deepcopy(plan)
        invalid['blocks'][0]['evidence'][0]['quote']='这篇论文的精读价值是满分十分。'
        with self.assertRaisesRegex(ValueError,'引文不匹配'):
            engine.validate(invalid,sources())
        invalid=copy.deepcopy(plan);invalid['reading_value_score']=10
        with self.assertRaisesRegex(ValueError,'分镜格式'):
            engine.validate(invalid,sources())


if __name__=='__main__':
    unittest.main()
