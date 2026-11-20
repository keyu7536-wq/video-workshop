"""Source-bound choices and independent understanding survive real host boundaries."""
import contextlib
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image

import paper_video as pv
import studio_engine as engine
import studio_server as server
from studio_delivery import final_review
from studio_media_index import POLICY_VERSION
from studio_revision import file_hash
from studio_storyboard import check_understanding


class StoryMediaPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.resources=contextlib.ExitStack()
        cls.addClassCleanup(cls.resources.close)
        cls.root=Path(cls.resources.enter_context(tempfile.TemporaryDirectory())).resolve()
        (cls.root/'jobs').mkdir()
        cls.resources.enter_context(patch.object(server,'ROOT',cls.root))
        cls.http=server.StudioHTTPServer(('127.0.0.1',0),server.Handler)
        cls.http.manager=server.Manager()
        cls.http.manager.start=Mock()
        thread=threading.Thread(target=cls.http.serve_forever,daemon=True)
        thread.start()
        cls.resources.callback(thread.join)
        cls.resources.callback(cls.http.server_close)
        cls.resources.callback(cls.http.shutdown)
        cls.base=f'http://127.0.0.1:{cls.http.server_port}'

    def setUp(self):
        self.job=self.root/'jobs'/self._testMethodName
        for folder in ('analysis','assets','output'):(self.job/folder).mkdir(parents=True)
        self.video=self.job/'assets/source.mp4'
        self.video.write_bytes(b'source bytes; video decoding is covered separately')
        self.source={'id':'v1','type':'video','file':str(self.video),'name':'原片',
                     'duration_seconds':10,'sample_times':[0,2,4,6,8]}
        self.sources=[self.source]
        self.segment={'id':'seg_1','source_id':'v1','start':0,'end':8,
                      'description':'采样帧可见物体位置不同。','visual_role':'demonstration',
                      'observed_times':[0,2,4,6],'limitations':['未核验连续动作。']}
        self.manifest={'policy_version':POLICY_VERSION,'status':'completed','segments':[self.segment],
                       'sources':[{'source_id':'v1','status':'indexed','video_sha256':file_hash(self.video),
                                   'sample_times':self.source['sample_times'],'duration_seconds':10}],
                       'gaps':[]}
        self.plan={'title':'观察状态','summary':'说明可见变化。','source_issues':[],'unused_sources':[],
                   'blocks':[{'id':'b1','chapter':'状态变化','narration':'这段只观察物体位置变化。','evidence':[],
                              'shots':[{'kind':'video','source_id':'v1','in':0,'duration':4,'bbox':[],
                                        'focus':[],'keep_full':False,'label':'','card_text':'','cue':'','diagram':None}]}]}
        self.story={'question':'INTERNAL_QUESTION','answer':'INTERNAL_ANSWER',
                    'example':{'description':'','source_ids':[],'illustrative':False},
                    'steps':[{'block_id':'b1','depends_on':'','change':'INTERNAL_CHANGE','why':''}]}
        self.request={'backend':'codex','content_type':'general','format':'custom','width':640,'height':360,
                      'layout_style':'immersive','media_fit':'contain','auto_repair':False,
                      'voice':'zh-CN-YunyangNeural','rate':'+0%','script':self.plan['blocks'][0]['narration']}
        self.write_inputs()
        self.http.manager.start.reset_mock()

    def write_inputs(self):
        pv.save_json(self.job/'analysis/studio_plan.json',self.plan)
        pv.save_json(self.job/'analysis/sources.json',self.sources)
        pv.save_json(self.job/'analysis/images.json',[])
        pv.save_json(self.job/'analysis/media_index.json',self.manifest)
        pv.save_json(self.job/'request.json',self.request)
        pv.save_json(self.job/'job.json',{'status':'prepared','engine':'studio'})

    def choice(self,**changes):
        plan=copy.deepcopy(self.plan)
        plan['blocks'][0]['shots'][0].update(media_segment_id='seg_1',selection_reason='INTERNAL_REASON',**changes)
        return plan

    def post_plan(self,plan):
        data=json.dumps({'job_id':self.job.name,'plan':plan},ensure_ascii=False).encode('utf8')
        request=Request(self.base+'/api/plan',data=data,headers={'Content-Type':'application/json'})
        try:
            with urlopen(request,timeout=10) as response:return response.status,json.load(response)
        except HTTPError as error:
            return error.code,json.load(error)

    def viewer(self,issue=False):
        return {'question':'物体呈现什么状态？','answer_from_video':'只能观察到位置改变。',
                'connections':[{'block_id':'b1','understood':not issue,
                                'explanation_from_video':'文稿只交代位置改变。',
                                'missing':'没有说明为何改变。' if issue else ''}],
                'issues':[{'severity':'error','block_id':'b1','reason':'主要解释缺少变化与原因的关系。'}] if issue else []}

    def test_legacy_validation_preserves_hash_and_missing_optional_fields(self):
        before=copy.deepcopy(self.plan);digest=engine.plan_hash(self.plan)
        engine.validate(self.plan,self.sources,self.request['script'])
        self.assertEqual(self.plan,before)
        self.assertEqual(engine.plan_hash(self.plan),digest)
        self.assertNotIn('story',self.plan)
        self.assertNotIn('media_segment_id',self.plan['blocks'][0]['shots'][0])

    def test_valid_media_binding_survives_actual_plan_endpoint_without_rewriting(self):
        plan=self.choice();plan['story']=copy.deepcopy(self.story)
        status,result=self.post_plan(plan)
        self.assertEqual(status,200,result)
        saved=pv.load_json(self.job/'analysis/studio_plan.json')
        self.assertEqual(saved['blocks'][0]['shots'][0]['media_segment_id'],'seg_1')
        self.assertEqual(saved['story'],self.story)
        self.assertEqual(saved['blocks'][0]['narration'],self.request['script'])
        self.http.manager.start.assert_not_called()

    def test_invalid_or_stale_binding_is_rejected_atomically_at_save(self):
        before={name:(self.job/name).read_bytes() for name in ('analysis/studio_plan.json','request.json','job.json')}
        for changes in ({'media_segment_id':'not-in-index'}, {'in':7,'duration':3},
                        {'in':1,'duration':1}, {'selection_reason':''}):
            with self.subTest(changes=changes):
                plan=self.choice();plan['blocks'][0]['shots'][0].update(changes)
                status,result=self.post_plan(plan)
                self.assertEqual(status,400,result)
                self.assertEqual({name:(self.job/name).read_bytes() for name in before},before)
        self.video.write_bytes(b'changed source content')
        status,result=self.post_plan(self.choice())
        self.assertEqual(status,400,result)
        self.assertIn('已变更',result['error'])
        self.assertEqual({name:(self.job/name).read_bytes() for name in before},before)

    def test_export_checks_index_and_does_not_copy_planning_hints_into_render_text(self):
        plan=self.choice();plan['story']=copy.deepcopy(self.story)
        plan['blocks'][0]['explanation']={'question':'INTERNAL_EXPLANATION','takeaway':'INTERNAL_TAKEAWAY',
                                          'visual_strategy':'demonstration','continuity':'INTERNAL_CONTINUITY'}
        engine.validate(plan,self.sources)
        config=engine.export(self.job,plan,self.sources,self.request)
        rendered=json.dumps(config,ensure_ascii=False)
        for marker in ('INTERNAL_QUESTION','INTERNAL_ANSWER','INTERNAL_CHANGE','INTERNAL_REASON',
                       'INTERNAL_EXPLANATION','INTERNAL_TAKEAWAY','INTERNAL_CONTINUITY'):
            self.assertNotIn(marker,rendered)
        self.video.write_bytes(b'replacement')
        with self.assertRaisesRegex(ValueError,'已变更'):
            engine.export(self.job,plan,self.sources,self.request)

    def test_viewer_cannot_silently_skip_duplicate_or_contradict_a_block(self):
        self.plan['story']=copy.deepcopy(self.story)
        invalid=[]
        empty=self.viewer();empty['connections']=[];invalid.append(empty)
        duplicate=self.viewer();duplicate['connections']*=2;invalid.append(duplicate)
        missing=self.viewer(True);missing['issues']=[];invalid.append(missing)
        for result in invalid:
            with self.subTest(result=result),self.assertRaises(ValueError):
                check_understanding(self.job,self.plan,self.request,Mock(return_value=result))

    def test_plan_viewer_sees_only_selected_compositions_and_error_changes_ready(self):
        self.plan['story']=copy.deepcopy(self.story)
        original=self.job/'analysis/original.png';preview=self.job/'analysis/composition.png'
        Image.new('RGB',(40,40),'red').save(original)
        Image.new('RGB',(40,40),'blue').save(preview)
        calls=[]
        def provider(prompt,images,*args):
            calls.append((prompt,images,args[-1]))
            return self.viewer(True) if args[-1]=='_understanding' else {'ready':True,'summary':'已有事实审核','issues':[]}
        assets=([('full source',original),('selected composition',preview)],
                {'compositions':[{'path':str(preview)}],'omitted_compositions':[]})
        with patch('studio_review_assets.prepare_review_assets',return_value=assets),patch.object(engine,'call_ai',side_effect=provider):
            result=engine.review_plan(self.job,self.plan,self.sources,self.request)
        self.assertFalse(result['ready'])
        self.assertEqual(calls[-1][1],[preview])
        self.assertNotIn('INTERNAL_ANSWER',calls[-1][0])
        self.assertEqual(result['issues'][0]['severity'],'error')

    def final_fixture(self):
        self.plan['story']=copy.deepcopy(self.story)
        frame=self.job/'analysis/encoded_frame.jpg'
        Image.new('RGB',(40,40),'blue').save(frame)
        (self.job/'output/video.mp4').write_bytes(b'encoded fixture bytes')
        pv.save_json(self.job/'project.json',{'blocks':[]})
        pv.save_json(self.job/'output/run_report.json',{'duration_seconds':4})
        pv.save_json(self.job/'output/rendered_shots.json',[{'block_id':'b1','shot_index':0,
                     'kind':'video','file':str(self.video),'start':0,'end':4}])
        (self.job/'analysis/final_frames').mkdir(exist_ok=True)
        pv.save_json(self.job/'analysis/final_frames/coverage.json',{'complete':True})
        return [{'time':2,'path':str(frame),'label':'encoded frame'}]

    def test_final_understanding_error_and_outage_cannot_receive_pass(self):
        frames=self.final_fixture();okay={'ready':True,'summary':'前两维复核通过','issues':[]}
        for last,status,complete in [(self.viewer(True),'completed',True),(TimeoutError('viewer unavailable'),'unavailable',False)]:
            with self.subTest(last=type(last).__name__),patch('studio_delivery.review_frames',return_value=frames):
                result,_=final_review(self.job,self.request,self.plan,self.sources,
                    Mock(side_effect=[copy.deepcopy(okay),copy.deepcopy(okay),last]),engine.REVIEW_SCHEMA)
                self.assertFalse(result['ready'])
                self.assertEqual(result['status'],status)
                self.assertEqual(result['review_complete'],complete)
                self.assertEqual(result['reviews'][-1]['stage'],'understanding')

    def test_repair_rejects_invalid_index_and_preserves_original_files(self):
        from studio_plan_repair import repair_plan_once
        self.request.update(auto_repair=True,_media_index=self.manifest)
        review={'ready':False,'summary':'需要修改镜头','issues':[{'severity':'error','block_id':'b1','reason':'画面对象不符。'}]}
        pv.save_json(self.job/'review.json',review)
        original=(self.job/'analysis/studio_plan.json').read_bytes()
        candidate=self.choice();candidate['blocks'][0]['shots'][0]['media_segment_id']='unknown'
        with patch.object(engine,'call_ai',return_value=candidate) as provider,patch.object(engine,'review_plan') as reviewer:
            plan,result=repair_plan_once(self.job,self.plan,self.sources,self.request,review)
        self.assertEqual(plan,self.plan)
        self.assertEqual(result['ready'],False)
        self.assertEqual((self.job/'analysis/studio_plan.json').read_bytes(),original)
        self.assertLessEqual(provider.call_count,3)
        self.assertGreater(provider.call_count,0)
        reviewer.assert_not_called()


if __name__=='__main__':
    unittest.main()
