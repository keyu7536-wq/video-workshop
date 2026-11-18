"""Isolated HTTP and real-browser checks for review checkpoints and clip playback."""
import contextlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import paper_video as pv
import studio_engine as engine
import studio_server as server


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'Playwright is not installed')
class ShotPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        from make_video import ffmpeg_path
        cls.resources=contextlib.ExitStack();cls.addClassCleanup(cls.resources.close)
        cls.root=Path(cls.resources.enter_context(tempfile.TemporaryDirectory())).resolve()
        (cls.root/'jobs').mkdir();(cls.root/'uploads').mkdir()
        cls.resources.enter_context(patch.object(server,'ROOT',cls.root))
        cls.resources.enter_context(patch.object(engine,'ROOT',cls.root))
        cls.resources.enter_context(patch.object(server,'INBOX',cls.root/'uploads'))
        cls.resources.enter_context(patch.object(server,'PROTOCOL_VERSION',11))
        cls.fixture=cls.root/'source.mp4'
        subprocess.run([ffmpeg_path(),'-v','error','-y','-f','lavfi','-i',
            'testsrc2=size=320x180:rate=30','-t','5','-c:v','libx264','-pix_fmt','yuv420p',
            '-movflags','+faststart',str(cls.fixture)],check=True,capture_output=True)
        cls.http=server.StudioHTTPServer(('127.0.0.1',0),server.Handler)
        cls.http.manager=server.Manager();cls.http.manager.start=Mock()
        cls.thread=threading.Thread(target=cls.http.serve_forever,daemon=True);cls.thread.start()
        cls.resources.callback(cls.thread.join);cls.resources.callback(cls.http.server_close)
        cls.resources.callback(cls.http.shutdown)
        cls.base=f'http://127.0.0.1:{cls.http.server_port}'
        cls.playwright=sync_playwright().start();cls.resources.callback(cls.playwright.stop)
        executable=Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
        cls.browser=cls.playwright.chromium.launch(headless=True,
            **({'executable_path':str(executable)} if executable.is_file() else {}))
        cls.resources.callback(cls.browser.close)

    def setUp(self):
        self.http.manager.start.reset_mock()
        self.job=self.root/'jobs'/self._testMethodName
        for folder in ('assets','analysis','output'):(self.job/folder).mkdir(parents=True)
        shutil.copyfile(self.fixture,self.job/'assets/source.mp4')
        self.plan={'title':'检查点测试','summary':'测试','source_issues':[],'unused_sources':[],
            'blocks':[{'id':'b1','chapter':'演示','narration':'先看动作，再看变化。','evidence':[],
                'shots':[{'kind':'video','source_id':'v1','in':1,'duration':1.4,'bbox':[],
                    'label':'动作演示','card_text':'','cue':''},
                    {'kind':'card','source_id':'','in':0,'duration':1,'bbox':[],
                    'label':'变化','card_text':'变化过程','cue':'再看变化'}]}]}
        self.request={'backend':'codex','format':'landscape','brief':'检查动作过程',
            'article':'原始文章正文','script':'','links':['https://author.example/paper.pdf'],
            'auto_render':False,'auto_repair':False,'final_review':True,'seconds':90,
            'files':[{'path':str(self.job/'assets/source.mp4'),'name':'原始演示.mp4'}]}
        pv.save_json(self.job/'request.json',self.request)
        pv.save_json(self.job/'job.json',{'engine':'studio','status':'prepared'})
        pv.save_json(self.job/'analysis/studio_plan.json',self.plan)
        pv.save_json(self.job/'analysis/sources.json',[{'id':'v1','type':'video','name':'原始演示',
            'file':str(self.job/'assets/source.mp4'),'duration_seconds':5,'sample_times':[1,2,3,4]}])
        self.page=self.browser.new_page(viewport={'width':1440,'height':1000})
        self.page.set_default_timeout(8000)
        self.addCleanup(self.page.close);self.errors=[]
        self.page.on('pageerror',lambda error:self.errors.append(str(error)))

    def open_job(self):
        self.page.goto(self.base+'/?job='+self.job.name,wait_until='networkidle')
        self.page.wait_for_selector('[data-shot="0,0"]',state='attached')

    def test_source_clip_plays_actual_frames_and_stops_at_selected_end(self):
        self.open_job();self.page.locator('[data-shot="0,0"]').click()
        self.page.wait_for_function("document.querySelector('#shotVideoPreview')?.readyState>=2")
        self.assertIn('原素材',self.page.locator('#shotVideoHint').inner_text())
        self.page.locator('#playSourceClip').click()
        self.page.wait_for_function("document.querySelector('#shotVideoPreview').currentTime>1.25")
        self.page.wait_for_function("document.querySelector('#shotVideoPreview').paused && document.querySelector('#shotVideoPreview').currentTime>=2.35",timeout=6000)
        position=self.page.locator('#shotVideoPreview').evaluate('(v)=>v.currentTime')
        self.assertLessEqual(position,2.45);self.assertEqual(self.errors,[])

    def test_creative_brief_edit_survives_save_and_requires_new_version(self):
        from studio_revision import render_settings_hash
        self.open_job();self.page.locator('#editorBriefWrap summary').click()
        old_hash=render_settings_hash(pv.load_json(self.job/'request.json'))
        self.page.locator('#editorBrief').fill('讲清原理，开头用论文身份图。')
        self.page.locator('#savePlan').click()
        self.page.wait_for_function("document.querySelector('#toast').textContent.includes('已保存')")
        saved=pv.load_json(self.job/'request.json')
        self.assertEqual(saved['brief'],'讲清原理，开头用论文身份图。')
        self.assertNotEqual(render_settings_hash(saved),old_hash)
        self.page.reload(wait_until='networkidle')
        self.assertEqual(self.page.locator('#editorBrief').input_value(),saved['brief'])
        self.assertEqual(self.errors,[])

    def test_active_progress_uses_current_three_stage_logs(self):
        self.open_job()
        self.page.route('**/api/status',lambda route:route.fulfill(status=200,content_type='application/json',
            body=json.dumps({'active':True,'job_id':self.job.name,'logs':['【文稿与分镜】复核事实与镜头']})))
        self.page.evaluate('poll()')
        self.assertEqual(self.page.locator('#taskTitle').inner_text(),'文稿与分镜 · 复核事实与镜头')
        self.assertEqual(self.page.locator('#progressFill').evaluate('(e)=>e.style.width'),'50%')

    def test_edited_shot_and_narration_survive_save_and_reopen(self):
        self.open_job();self.page.locator('[data-narration="0"]').fill('先看动作，再看变化。修改后逐步解释。')
        self.page.locator('[data-shot="0,0"]').click()
        self.page.locator('#shotIn').fill('2');self.page.locator('#shotDuration').fill('1.2')
        self.page.locator('#shotLabel').fill('修改后的动作')
        self.page.locator('#shotForm button[type="submit"]').click()
        self.page.locator('#savePlan').click()
        self.page.wait_for_function("document.querySelector('#toast').textContent.includes('已保存')")
        self.page.reload(wait_until='networkidle');self.page.locator('[data-shot="0,0"]').click()
        self.assertEqual(self.page.locator('#shotIn').input_value(),'2')
        self.assertEqual(self.page.locator('#shotDuration').input_value(),'1.2')
        self.assertEqual(self.page.locator('#shotLabel').input_value(),'修改后的动作')
        saved=pv.load_json(self.job/'analysis/studio_plan.json')
        self.assertTrue(saved['blocks'][0]['narration'].endswith('逐步解释。'))
        self.assertEqual(self.errors,[])

    def test_failed_input_can_be_edited_into_new_job_preserving_old_job(self):
        (self.job/'analysis/studio_plan.json').unlink()
        pv.save_json(self.job/'job.json',{'engine':'studio','status':'failed','error':'HTTP 429','failed_action':'prepare'})
        before={name:(self.job/name).read_bytes() for name in ('request.json','job.json')}
        self.page.goto(self.base+'/?job='+self.job.name,wait_until='networkidle')
        self.page.locator('#editInputs').click()
        self.assertEqual(self.page.locator('#article').input_value(),'原始文章正文')
        self.assertEqual(self.page.locator('#links').input_value(),self.request['links'][0])
        self.assertFalse(self.page.locator('#autoRender').is_checked())
        self.assertFalse(self.page.locator('#autoRepair').is_checked())
        self.assertIn('新建任务',self.page.locator('#createButton').inner_text())
        self.page.locator('.link-details summary').click()
        self.page.locator('#links').fill('https://mirror.example/paper.pdf')
        with self.page.expect_response(lambda r:r.url.endswith('/api/create')) as response:
            self.page.locator('#createButton').click()
        self.assertEqual(response.value.status,200,response.value.text())
        new=self.root/'jobs'/response.value.json()['job_id'];self.assertNotEqual(new,self.job)
        saved=pv.load_json(new/'request.json')
        self.assertEqual(saved['links'],['https://mirror.example/paper.pdf'])
        self.assertEqual(saved['files'],self.request['files'])
        self.assertFalse(saved['auto_repair']);self.assertFalse(saved['auto_render'])
        self.assertEqual(before,{name:(self.job/name).read_bytes() for name in before})
        self.http.manager.start.assert_called_once();self.assertEqual(self.errors,[])

    def make_render(self):
        from studio_revision import file_hash,render_settings_hash
        shutil.copyfile(self.fixture,self.job/'output/video.mp4')
        pv.save_json(self.job/'project.json',{'blocks':[]})
        pv.save_json(self.job/'output/run_report.json',{'duration_seconds':5,'resolution':'320x180','fps':30,'shots':2})
        pv.save_json(self.job/'output/rendered_shots.json',[{'start':0,'end':2,'label':'第一段','kind':'video'},
            {'start':2,'end':5,'label':'实际补充镜头','kind':'image'}])
        pv.save_json(self.job/'output/render_receipt.json',{'plan_hash':engine.plan_hash(self.plan),
            'settings_hash':render_settings_hash(self.request),'video_sha256':file_hash(self.job/'output/video.mp4'),
            'project_sha256':file_hash(self.job/'project.json')})
        pv.save_json(self.job/'job.json',{'engine':'studio','status':'completed'})

    def test_final_segments_use_measured_timeline_and_play_within_it(self):
        self.make_render();self.open_job()
        self.assertEqual(self.page.locator('[data-segment]').count(),2)
        self.page.locator('[data-segment="1"]').click()
        self.page.wait_for_function("document.querySelector('#segmentVideo')?.readyState>=2")
        self.page.locator('#playFinalClip').click()
        self.page.wait_for_function("document.querySelector('#segmentVideo').currentTime>2.25")
        self.assertIn('2.00',self.page.locator('#segmentHint').inner_text())
        self.assertEqual(self.errors,[])
        self.plan['blocks'][0]['narration']+='新改动';pv.save_json(self.job/'analysis/studio_plan.json',self.plan)
        changed=self.page.request.get(self.base+'/api/job?id='+self.job.name).json()
        self.assertEqual(changed['rendered_segments'],[])

    def test_server_defaults_and_attachment_reuse_validation(self):
        response=self.page.request.post(self.base+'/api/create',data={'article':'测试文章','seconds':60})
        self.assertEqual(response.status,200,response.text())
        request=pv.load_json(self.root/'jobs'/response.json()['job_id']/'request.json')
        self.assertFalse(request['auto_render']);self.assertFalse(request['auto_repair'])
        for selection in ([True],[-1],[1],['0']):
            response=self.page.request.post(self.base+'/api/create',data={'article':'测试','from_job':self.job.name,'reuse_files':selection})
            self.assertEqual(response.status,400,response.text())

    def test_explicit_repair_choice_is_persisted_when_rendering(self):
        response=self.page.request.post(self.base+'/api/render',data={'job_id':self.job.name,'auto_repair':True,'final_review':True})
        self.assertEqual(response.status,200,response.text())
        self.assertTrue(pv.load_json(self.job/'request.json')['auto_repair'])
        self.http.manager.start.assert_called_once_with(self.job,'render','')

    def test_gui_render_passes_explicit_repair_choice_after_human_edits(self):
        self.open_job();self.page.locator('[data-narration="0"]').fill('先看动作，再看变化。保留我的改动。')
        self.page.locator('[data-tab="create"]').click();self.page.locator('#autoRepair').check()
        self.page.locator('[data-tab="editor"]').click()
        with self.page.expect_response(lambda r:r.url.endswith('/api/render')) as response:
            self.page.locator('#renderButton').click()
        self.assertEqual(response.value.status,200,response.value.text())
        self.assertTrue(pv.load_json(self.job/'request.json')['auto_repair'])
        self.assertTrue(pv.load_json(self.job/'analysis/studio_plan.json')['blocks'][0]['narration'].endswith('我的改动。'))
        self.http.manager.start.assert_called_once_with(self.job,'render','')
        self.assertEqual(self.errors,[])

    def test_card_and_diagram_are_honest_editable_previews(self):
        self.open_job();self.page.locator('[data-shot="0,1"]').click()
        self.page.locator('#shotCardText').fill('实际卡片内容')
        self.assertIn('实际卡片内容',self.page.locator('#generatedVisualPreview').inner_text())
        self.page.locator('[data-close="shotDialog"]').click()
        self.plan['blocks'][0]['shots'][1].update(kind='diagram',card_text='',diagram={
            'nodes':[{'id':'input','label':'输入','x':.3,'y':.4},{'id':'output','label':'输出','x':.7,'y':.4}],
            'edges':[{'id':'flow','source':'input','target':'output','label':'传递'}],
            'stages':[{'cue':'','active_nodes':['input'],'active_edges':[]},
                      {'cue':'再看变化','active_nodes':['output'],'active_edges':['flow']}]})
        pv.save_json(self.job/'analysis/studio_plan.json',self.plan)
        self.page.reload(wait_until='networkidle');self.page.locator('[data-shot="0,1"]').click()
        preview=self.page.locator('#generatedVisualPreview').inner_text()
        self.assertIn('输入 → 输出',preview);self.assertIn('实际动画在配音后生成',preview)
        self.assertEqual(self.page.locator('#shotDurationLabel').inner_text(),'停留权重')
        self.assertIn('再看变化',preview);self.assertEqual(self.errors,[])

    def test_planned_duration_labels_distinguish_clip_seconds_from_weights(self):
        from PIL import Image
        image=self.job/'assets/figure.png';Image.new('RGB',(120,80),'teal').save(image)
        sources=pv.load_json(self.job/'analysis/sources.json')
        sources.append({'id':'image1','type':'image','name':'测试图片','file':str(image),'text':''})
        pv.save_json(self.job/'analysis/sources.json',sources)
        self.open_job()
        self.assertIn('取片 1.4 秒',self.page.locator('[data-shot="0,0"]').inner_text())
        self.assertIn('停留权重 1',self.page.locator('[data-shot="0,1"]').inner_text())
        self.assertIn('实际时长配音后确定',self.page.locator('[data-shot="0,1"]').inner_text())
        self.page.locator('[data-shot="0,0"]').click()
        original=self.page.locator('#shotDuration').input_value()
        for source in ('v1','image1','card','v1'):
            self.page.locator('#shotSource').select_option(source)
            label=self.page.locator('#shotDurationLabel').inner_text()
            hint=self.page.locator('#shotDurationHint').inner_text()
            if source=='v1':
                self.assertEqual(label,'取片秒数');self.assertIn('原视频',hint)
                self.assertNotIn('按权重',hint)
            else:
                self.assertEqual(label,'停留权重');self.assertIn('实际时长配音后确定',hint)
                self.assertIn('同段',hint)
            self.assertEqual(self.page.locator('#shotDuration').input_value(),original)
        self.assertEqual(self.errors,[])

    def test_replan_after_deleting_old_cue_creates_protected_script_in_new_job(self):
        revised='  先观察当前场景。\n保留这句新文案，不再使用旧的转场短语。  '
        self.open_job();self.page.locator('[data-narration="0"]').fill(revised)
        before={name:(self.job/name).read_bytes() for name in ('job.json','request.json','analysis/studio_plan.json')}
        requests=[]
        self.page.on('request',lambda request:requests.append(request.url) if request.method=='POST' else None)
        with self.page.expect_response(lambda response:response.request.method=='POST' and '/api/' in response.url) as response:
            self.page.locator('#replan').click()
        result=response.value
        self.assertEqual(result.status,200,result.text())
        self.assertTrue(result.url.endswith('/api/create'),result.url)
        new_job=self.root/'jobs'/result.json()['job_id']
        self.assertNotEqual(new_job,self.job)
        request=pv.load_json(new_job/'request.json')
        self.assertEqual(request['script'],revised)
        self.assertEqual(request['article'],self.request['article'])
        self.assertEqual(request['links'],self.request['links'])
        self.assertEqual(request['files'],self.request['files'])
        self.assertFalse(request['auto_render'])
        self.assertEqual(before,{name:(self.job/name).read_bytes() for name in before})
        self.assertFalse((new_job/'analysis/studio_plan.json').exists())
        self.assertEqual(requests,[self.base+'/api/create'])
        self.http.manager.start.assert_called_once_with(new_job,'prepare','')
        self.assertEqual(self.errors,[])

    def test_rejected_replan_keeps_unsaved_script_and_original_job(self):
        revised='新的讲解稿已经没有原来的切入短语。'
        self.open_job();self.page.locator('[data-narration="0"]').fill(revised)
        before=(self.job/'analysis/studio_plan.json').read_bytes()
        (self.job/'assets/source.mp4').unlink()
        with self.page.expect_response(lambda response:response.url.endswith('/api/create')) as response:
            self.page.locator('#replan').click()
        self.assertEqual(response.value.status,400)
        self.assertIn('重新上传',response.value.json()['error'])
        self.assertEqual(self.page.locator('[data-narration="0"]').input_value(),revised)
        self.assertEqual(self.page.evaluate('()=>state.job.id'),self.job.name)
        self.assertTrue(self.page.evaluate('()=>state.dirty'))
        self.assertFalse(self.page.locator('#replan').is_disabled())
        self.assertEqual((self.job/'analysis/studio_plan.json').read_bytes(),before)
        self.http.manager.start.assert_not_called();self.assertEqual(self.errors,[])


if __name__=='__main__':unittest.main()
