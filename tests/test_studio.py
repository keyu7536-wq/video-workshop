import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request,urlopen

import studio_engine as se
import studio_render as sr
from studio_server import Handler,Manager,StudioHTTPServer


def plan_fixture():
    return {'title':'解释一个概念','summary':'清晰说明一个概念','source_issues':[],'unused_sources':[],
        'blocks':[{'id':'b1','chapter':'提出问题','narration':'先把问题讲清楚，再给出具体的解释。',
            'evidence':[{'source_id':'article','quote':'先把问题讲清楚，再给出具体的解释。'}],
            'shots':[{'kind':'card','source_id':'','in':0,'duration':8,'bbox':[],'label':'先明确问题','card_text':'先把问题讲清楚，再给出具体的解释。','cue':''}]}]}


class StudioEngineTests(unittest.TestCase):
    def setUp(self):
        self.plan=plan_fixture();self.sources=[{'id':'article','type':'text','text':'先把问题讲清楚，再给出具体的解释。','name':'文章'}]
    def test_article_without_paper_or_video(self): se.validate(self.plan,self.sources)
    def test_user_script_is_preserved(self):
        se.validate(self.plan,self.sources,'先把问题讲清楚，再给出具体的解释。')
        with self.assertRaisesRegex(ValueError,'改写'):
            se.validate(self.plan,self.sources,'这是另一段文案。')
    def test_fabricated_quote_is_rejected(self):
        self.plan['blocks'][0]['evidence'][0]['quote']='研究已经证明百分之百成功'
        with self.assertRaisesRegex(ValueError,'引文不匹配'):se.validate(self.plan,self.sources)
    def test_unrelated_video_is_not_used(self):
        video={'id':'v1','type':'video','duration_seconds':10,'sample_times':[0,2,4]}
        self.sources.append(video);self.plan['unused_sources']=['v1']
        self.plan['blocks'][0]['shots'][0].update(kind='video',source_id='v1',duration=4)
        with self.assertRaisesRegex(ValueError,'不相关'):se.validate(self.plan,self.sources)
    def test_visuals_are_optional_not_forced(self):
        self.plan['source_issues']=['提供的视频与文章无关，未采用。'];self.plan['unused_sources']=['v1']
        se.validate(self.plan,self.sources)
    def test_invalid_crop_reports_shot_coordinates_and_bounds_for_repair(self):
        self.sources.append({'id':'p1','type':'page','text':''})
        self.plan['blocks'][0]['shots'][0].update(kind='page',source_id='p1',bbox=[.172,.854,.829,.899])
        with self.assertRaises(ValueError) as caught:se.validate(self.plan,self.sources)
        message=str(caught.exception)
        for detail in ('b1','1','0.854','0.899','0.08','0.05'):
            self.assertIn(detail,message)
    def test_exact_minimum_crop_is_not_rejected_by_floating_point_roundoff(self):
        self.sources.append({'id':'p1','type':'page','text':''})
        self.plan['blocks'][0]['shots'][0].update(kind='page',source_id='p1',bbox=[.1,.45,.18,.5])
        se.validate(self.plan,self.sources)
    def test_dynamic_layout(self):
        for width,height in [(1080,1920),(1920,1080),(720,1280)]:
            values=sr.layout(width,height);x,y,w,h=values['media']
            self.assertLessEqual(x+w,width);self.assertLessEqual(y+h,height)
    def test_video_only_shortage_requests_related_visuals_without_text_cards(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            shots=[{'kind':'video','file':'source.mp4','duration':3,'render_duration':8,'start':0,'end':8,'chapter':'概念','label':'演示','summary':'先说明问题，再解释方法。'}]
            with self.assertRaises(sr.NeedsMoreVisualsError):
                sr.replace_video_holds(shots,root,root,30)
            self.assertFalse(list(root.glob('continuation_*.png')))
    def test_plan_change_invalidates_review_hash(self):
        old=se.plan_hash(self.plan);self.plan['blocks'][0]['narration']='修改后的文案。'
        self.assertNotEqual(old,se.plan_hash(self.plan))
    def test_caption_never_puts_only_punctuation_on_last_line(self):
        lines=sr.caption_lines('论文对六类单项任务做了三百六十次测试，',18)
        self.assertGreater(len(lines[-1]),1)
        self.assertEqual(''.join(lines),'论文对六类单项任务做了三百六十次测试，')
    def test_complete_action_not_cut_to_short_narration(self):
        shots=[{'kind':'video','file':'demo.mp4','duration':6,'keep_full':True},{'kind':'image','file':'fig.png','duration':3}]
        result=sr.choose_shots({'id':'b','narration':'讲解','shots':shots},8,[],30)
        self.assertGreaterEqual(result[0]['render_duration'],6)
        self.assertGreaterEqual(result[1]['render_duration'],1.2)
    def test_publication_assets_and_config(self):
        with tempfile.TemporaryDirectory() as folder:
            job=Path(folder);(job/'assets').mkdir()
            config=se.export(job,self.plan,self.sources,{'format':'landscape'})
            self.assertEqual((config['width'],config['height']),(1920,1080))
            self.assertFalse((job/'assets/cover.png').exists())
            self.assertIsNone(config['outro'])
            self.assertFalse(json.loads((job/'publishing.json').read_text(encoding='utf-8'))['automatically_published'])
    def test_publication_description_excludes_production_notes(self):
        self.plan['summary']='逐字保留用户文案，安排8段，总计90秒。'
        description=se.publication_description(self.plan)
        self.assertNotIn('逐字保留',description)
        self.assertIn('先把问题讲清楚',description)


class StudioHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=StudioHTTPServer(('127.0.0.1',0),Handler);cls.server.manager=Manager()
        cls.url=f'http://127.0.0.1:{cls.server.server_port}';cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.thread.join()
    def test_local_studio_page(self):
        with urlopen(self.url) as response:self.assertIn('视频工坊',response.read().decode())
    def test_second_server_cannot_take_the_active_port(self):
        with self.assertRaises(OSError):StudioHTTPServer(('127.0.0.1',self.server.server_port),Handler)
    def test_range_requests_for_reference_video(self):
        # Byte-serving fixture, no downloaded paper/video or personal job required.
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'fixture.mp4').write_bytes(b'\0\0\0\x18ftyp'+bytes(128))
            with patch('studio_server.WEB',root):
                with urlopen(Request(self.url+'/fixture.mp4',headers={'Range':'bytes=0-63'})) as response:
                    data=response.read();self.assertEqual(response.status,206);self.assertEqual(len(data),64);self.assertEqual(data[4:8],b'ftyp')
    def test_cross_site_post_is_rejected(self):
        request=Request(self.url+'/api/cancel',data=b'{}',headers={'Content-Type':'application/json','Origin':'https://other.example'})
        with self.assertRaises(HTTPError) as error:urlopen(request)
        self.assertEqual(error.exception.code,400);error.exception.close()
    def test_file_traversal_is_rejected(self):
        with self.assertRaises(HTTPError) as error:urlopen(self.url+'/files/../project.json')
        self.assertEqual(error.exception.code,400);error.exception.close()

    def test_render_request_persists_display_choices(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            job=Path(folder);(job/'analysis').mkdir();(job/'analysis/studio_plan.json').write_text('{}')
            (job/'job.json').write_text(json.dumps({'engine':'studio','status':'prepared'}))
            (job/'request.json').write_text(json.dumps({'voice':'zh-CN-YunxiNeural','format':'portrait'}))
            display={'format':'custom','width':1600,'height':1000,'layout_style':'immersive','media_fit':'cover','show_sources':False,'minimal_text':True}
            body=json.dumps({'job_id':'example','display':display}).encode()
            with patch('studio_server.job_path',return_value=job),patch.object(self.server.manager,'start') as start:
                with urlopen(Request(self.url+'/api/render',data=body,headers={'Content-Type':'application/json'})) as response:
                    self.assertEqual(response.status,200)
                saved=json.loads((job/'request.json').read_text(encoding='utf-8'))
                self.assertEqual({k:saved[k] for k in display},display);self.assertEqual(saved['voice'],'zh-CN-YunxiNeural')
                start.assert_called_once_with(job,'render','')


if __name__=='__main__':unittest.main()
