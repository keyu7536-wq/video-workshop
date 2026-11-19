"""Headless checks for persistent job errors using isolated jobs and HTTP ports."""
import contextlib
import copy
import importlib.util
import tempfile
import threading
from pathlib import Path
import unittest
from unittest.mock import patch,Mock

import paper_video as pv
import studio_server as server


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'Playwright is not installed')
class JobErrorUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.resources=contextlib.ExitStack()
        cls.addClassCleanup(cls.resources.close)
        cls.root=Path(cls.resources.enter_context(tempfile.TemporaryDirectory())).resolve()
        (cls.root/'jobs').mkdir()
        cls.resources.enter_context(patch.object(server,'ROOT',cls.root))
        cls.http=server.StudioHTTPServer(('127.0.0.1',0),server.Handler)
        cls.http.manager=server.Manager()
        cls.thread=threading.Thread(target=cls.http.serve_forever,daemon=True);cls.thread.start()
        cls.resources.callback(cls.thread.join)
        cls.resources.callback(cls.http.server_close)
        cls.resources.callback(cls.http.shutdown)
        cls.base=f'http://127.0.0.1:{cls.http.server_port}'
        plan={'title':'测试文稿','summary':'测试','source_issues':[],'unused_sources':[],
              'blocks':[{'id':'b1','chapter':'过程','narration':'这是测试文稿。','evidence':[],
                         'shots':[{'kind':'card','source_id':'','in':0,'duration':4,'bbox':[],
                                   'label':'过程','card_text':'测试','cue':''}]}]}
        cases=[('missing_plan',None,'首镜头cue必须为空。',None),
               ('render_failed',plan,'配音服务连接失败：voice.example 返回 HTTP 503。',{'ready':True,'issues':[]}),
               ('review_summary',plan,'文稿与分镜未通过内容复核：关键结果缺少原文依据。',
                {'ready':False,'issues':[],'summary':'关键结果缺少原文依据。'}),
               ('export_failed',plan,'MOV 导出失败，原始成片已保留。',{'ready':True,'issues':[]}),
               ('review_unavailable',plan,None,{'ready':True,'issues':[]}),
               ('prepared',plan,None,{'ready':True,'issues':[]})]
        cls.initial_jobs={}
        for name,document,error,review in cases:
            job=cls.root/'jobs'/name;(job/'analysis').mkdir(parents=True)
            if document:server.engine.validate(document,[])
            if review and document:review.update(_plan_hash=server.engine.plan_hash(document),_content_type='auto',_story_framework='auto')
            state={'engine':'studio','status':'failed' if error else 'prepared','error':error,
                   'failed_action':'export' if name=='export_failed' else None}
            if name=='review_unavailable':state['status']='review_needed'
            request={'format':'landscape','backend':'codex'}
            cls.initial_jobs[name]=(state,request,document)
            pv.save_json(job/'job.json',state);pv.save_json(job/'request.json',request)
            pv.save_json(job/'analysis/sources.json',[])
            if document:pv.save_json(job/'analysis/studio_plan.json',document)
            if review:pv.save_json(job/'review.json',review)
            if name in ('export_failed','review_unavailable'):
                (job/'output').mkdir();(job/'output/video.mp4').write_bytes(b'rendered fixture')
            if name=='review_unavailable':
                from studio_revision import file_hash,render_settings_hash
                pv.save_json(job/'project.json',{'fixture':'rendered project'})
                pv.save_json(job/'output/run_report.json',{'duration_seconds':4,'resolution':'640x360','fps':30,'shots':1})
                pv.save_json(job/'output/final_review.json',{'status':'unavailable','ready':False,'review_complete':False,
                    'unavailable_reason':'TimeoutError: independent review timed out','summary':'成片复查服务超时',
                    'issues':[],'round':1,'coverage':'review unavailable','plan_hash':server.engine.plan_hash(document),
                    'settings_hash':render_settings_hash(request),'video_sha256':file_hash(job/'output/video.mp4'),
                    'project_sha256':file_hash(job/'project.json')})
                pv.save_json(job/'output/render_receipt.json',{key:value for key,value in pv.load_json(job/'output/final_review.json').items()
                    if key in ('plan_hash','settings_hash','video_sha256','project_sha256')})
        for name,ready in [('plan_blocked',False),('plan_contradictory',True),('plan_multi_shot',False),('plan_display_blocked',False),('plan_review_service_failed',False)]:
            job=cls.root/'jobs'/name;(job/'analysis').mkdir(parents=True)
            document=copy.deepcopy(plan)
            document['source_issues']=['作者没有提供演示视频，采用论文图。']
            document['blocks'].append({'id':'failure','chapter':'靠近后的碰撞','narration':'论文报告这一回合发生碰撞。',
                'evidence':[],'shots':[{'kind':'image','source_id':'figure','in':0,'duration':4,
                    'bbox':[0,0,1,1],'focus':[.1,.1,.8,.45],'keep_full':False,'label':'轨迹对比',
                    'card_text':'','cue':''}]})
            if name=='plan_multi_shot':
                second=copy.deepcopy(document['blocks'][1]['shots'][0]);second['cue']='发生碰撞'
                document['blocks'][1]['shots'].append(second)
            state={'engine':'studio','status':'review_needed','stage':'plan_review','error':None}
            if name=='plan_review_service_failed':state.update(status='failed',failed_action='render',error='model unavailable: HTTP 503')
            request={'format':'landscape','backend':'codex','auto_repair':False,'final_review':True}
            if name=='plan_display_blocked':request.update(format='portrait',width=1920,height=1080)
            review={'ready':ready,'summary':'第二段需要修改裁图。','issues':[
                {'severity':'error','block_id':'failure','reason':'裁图混入另一回合，请单独裁出碰撞案例。'},
                {'severity':'warning','block_id':'b1','reason':'成片时确认图表文字可读。'}]}
            if name=='plan_display_blocked':review['issues'][0]['reason']='竖屏中图表过小，无法辨认旁白所述的差异。'
            from PIL import Image
            figure=job/'figure.png';Image.new('RGB',(600,400),'white').save(figure)
            sources=[{'id':'figure','type':'image','name':'实验轨迹图','file':str(figure)}]
            server.engine.validate(document,sources);review.update(_plan_hash=server.engine.plan_hash(document),_content_type='auto',_story_framework='auto')
            from studio_display import effective_display_settings
            review['_effective_display_settings']=effective_display_settings(request)
            pv.save_json(job/'analysis/sources.json',sources)
            pv.save_json(job/'job.json',state);pv.save_json(job/'request.json',request)
            pv.save_json(job/'analysis/studio_plan.json',document);pv.save_json(job/'review.json',review)
            cls.initial_jobs[name]=(state,request,document)
        cls.playwright=sync_playwright().start();cls.resources.callback(cls.playwright.stop)
        executable=Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
        try:
            cls.browser=cls.playwright.chromium.launch(headless=True,
                **({'executable_path':str(executable)} if executable.is_file() else {}))
        except Exception as exc:raise unittest.SkipTest(f'Headless browser unavailable: {exc}')
        cls.resources.callback(cls.browser.close)

    def setUp(self):
        for name,(state,request,plan) in self.initial_jobs.items():
            job=self.root/'jobs'/name;pv.save_json(job/'job.json',state);pv.save_json(job/'request.json',request)
            if plan:pv.save_json(job/'analysis/studio_plan.json',plan)
        self.page=self.browser.new_page(viewport={'width':1440,'height':1000})
        self.addCleanup(self.page.close)
        self.errors=[];self.page.on('pageerror',lambda error:self.errors.append(str(error)))
        self.page.goto(self.base,wait_until='networkidle')

    def open_job(self,name):
        self.page.locator(f'[data-job="{name}"]').click()
        self.page.wait_for_selector(f'[data-job="{name}"].selected')
        self.page.wait_for_selector('#view-editor:not([hidden])')

    def test_failed_job_without_plan_keeps_the_actual_error_visible_after_reopen(self):
        self.open_job('missing_plan')
        banner=self.page.locator('#reviewBanner')
        self.assertIn('首镜头cue必须为空。',banner.inner_text())
        self.assertTrue(banner.is_visible())
        self.page.reload(wait_until='networkidle');self.open_job('missing_plan')
        self.assertIn('首镜头cue必须为空。',self.page.locator('#reviewBanner').inner_text())
        self.assertEqual(self.errors,[])

    def test_failed_job_with_saved_plan_keeps_non_review_error_visible(self):
        self.open_job('render_failed')
        self.assertIn('HTTP 503',self.page.locator('#reviewBanner').inner_text())
        self.assertEqual(self.page.locator('[data-narration]').count(),1)
        self.assertEqual(self.errors,[])

    def test_blocked_review_summary_is_shown_without_claiming_review_passed(self):
        self.open_job('review_summary')
        text=self.page.locator('#reviewBanner').inner_text()
        self.assertIn('关键结果缺少原文依据。',text)
        self.assertNotIn('资料与文稿已完成自动复核',text)

    def test_plan_review_opens_editor_and_separates_blockers_from_notes(self):
        self.open_job('plan_blocked')
        self.assertIn('分镜待修改',self.page.locator('[data-job="plan_blocked"] small').inner_text())
        banner=self.page.locator('#reviewBanner')
        self.assertIn('1 项需要修改',banner.inner_text())
        self.assertIn('1 条非阻断提示',banner.inner_text())
        self.assertIn('1 条来源说明',banner.inner_text())
        self.assertIn('第 2 段 · 靠近后的碰撞',banner.inner_text())
        self.assertFalse(self.page.locator('#retryJob').is_visible())
        self.assertFalse(self.page.locator('#autoRepair').is_checked())
        self.assertTrue(self.page.locator('#repairPlanButton').is_visible())
        self.assertTrue(self.page.locator('#renderButton').is_disabled())
        self.assertFalse(self.page.locator('#exportButton').is_enabled())
        self.assertEqual(self.errors,[])

    def test_blocker_opens_its_exact_single_shot_without_changing_plan(self):
        self.open_job('plan_blocked')
        before=self.page.evaluate('JSON.stringify(state.job.plan)')
        self.page.locator('[data-review-shot="1,0"]').click()
        self.assertTrue(self.page.locator('#shotDialog').is_visible())
        self.assertEqual(self.page.locator('#shotSource').input_value(),'figure')
        self.assertTrue(self.page.locator('#shotFocus').is_checked())
        self.assertIn('取消勾选',self.page.locator('#cropHint').inner_text())
        self.assertEqual(self.page.evaluate('JSON.stringify(state.job.plan)'),before)

    def test_error_overrides_inconsistent_ready_true_flag(self):
        self.open_job('plan_contradictory')
        self.assertTrue(self.page.locator('#reviewBanner .plan-review-blocked').is_visible())
        self.assertIn('1 项需要修改',self.page.locator('#reviewBanner').inner_text())
        self.assertTrue(self.page.locator('#repairPlanButton').is_visible())
        self.assertTrue(self.page.locator('#renderButton').is_disabled())

    def test_multi_shot_issue_locates_block_without_guessing_which_shot(self):
        self.open_job('plan_multi_shot')
        self.assertEqual(self.page.locator('[data-review-shot="1,0"]').count(),0)
        self.page.locator('[data-review-block="1"]').click()
        self.assertEqual(self.page.evaluate('document.activeElement.dataset.narration'),'1')
        self.assertFalse(self.page.locator('#shotDialog').is_visible())
        self.assertEqual(self.page.locator('[data-block-index="1"] [data-shot]').count(),2)

    def test_explicit_auto_repair_uses_existing_render_api_and_quality_review(self):
        self.open_job('plan_blocked')
        self.assertIn('最多自动修正一次',self.page.locator('#reviewBanner').inner_text())
        before=pv.load_json(self.root/'jobs/plan_blocked/analysis/studio_plan.json')
        with patch.object(self.http.manager,'start') as start:
            with self.page.expect_response(lambda response:response.url.endswith('/api/render') and response.request.method=='POST') as response:
                self.page.locator('#repairPlanButton').click()
            self.assertEqual(response.value.status,200)
            start.assert_called_once_with(self.root/'jobs/plan_blocked','render','')
        request=pv.load_json(self.root/'jobs/plan_blocked/request.json')
        self.assertIs(request['auto_repair'],True)
        self.assertIs(request['final_review'],True)
        self.assertEqual(pv.load_json(self.root/'jobs/plan_blocked/analysis/studio_plan.json'),before)
        self.assertEqual(self.errors,[])

    def test_manual_shot_edit_saves_crop_and_enables_review_without_auto_repair(self):
        self.open_job('plan_blocked')
        self.page.locator('[data-review-shot="1,0"]').click()
        self.page.locator('#shotFocus').uncheck()
        self.page.wait_for_function('document.querySelector("#cropImage").naturalWidth > 0')
        box=self.page.locator('#cropImage').bounding_box()
        self.page.mouse.move(box['x']+box['width']*.1,box['y']+box['height']*.1)
        self.page.mouse.down();self.page.mouse.move(box['x']+box['width']*.85,box['y']+box['height']*.48);self.page.mouse.up()
        self.page.locator('#shotForm button[type="submit"]').click()
        self.assertIn('修改尚未复核',self.page.locator('#reviewBanner').inner_text())
        self.assertFalse(self.page.locator('#repairPlanButton').is_visible())
        self.assertFalse(self.page.locator('#renderButton').is_disabled())
        self.page.locator('#savePlan').click()
        self.page.wait_for_function('!state.dirty')
        self.assertIn('修改尚未复核',self.page.locator('#reviewBanner').inner_text())
        self.page.reload(wait_until='networkidle');self.open_job('plan_blocked')
        self.assertIn('修改尚未复核',self.page.locator('#reviewBanner').inner_text())
        self.assertFalse(self.page.locator('#repairPlanButton').is_visible())
        self.assertFalse(self.page.locator('#renderButton').is_disabled())
        with patch.object(self.http.manager,'start') as start:
            with self.page.expect_response(lambda response:response.url.endswith('/api/render') and response.request.method=='POST') as response:
                self.page.locator('#renderButton').click()
            self.assertEqual(response.value.status,200)
            start.assert_called_once_with(self.root/'jobs/plan_blocked','render','')
        shot=pv.load_json(self.root/'jobs/plan_blocked/analysis/studio_plan.json')['blocks'][1]['shots'][0]
        self.assertEqual(shot['focus'],[])
        self.assertAlmostEqual(shot['bbox'][1],.1,places=2)
        self.assertAlmostEqual(shot['bbox'][3],.48,places=2)
        self.assertFalse(pv.load_json(self.root/'jobs/plan_blocked/request.json')['auto_repair'])
        self.assertEqual(self.errors,[])

    def test_saving_unchanged_blocked_plan_does_not_enable_blind_render_retry(self):
        self.open_job('plan_blocked')
        with self.page.expect_response(lambda response:response.url.endswith('/api/plan')):
            self.page.locator('#savePlan').click()
        self.assertTrue(self.page.locator('#renderButton').is_disabled())
        self.assertFalse(self.page.locator('#retryJob').is_visible())
        self.assertTrue(self.page.locator('#repairPlanButton').is_visible())
        self.assertFalse(pv.load_json(self.root/'jobs/plan_blocked/request.json')['auto_repair'])

    def test_changed_requirements_reopen_as_pending_review_without_modifying_narration(self):
        self.open_job('plan_blocked')
        original=self.page.locator('[data-narration="1"]').input_value()
        self.page.locator('#editorBriefWrap summary').click()
        self.page.locator('#editorBrief').fill('这一段说明论文报告的失败案例，避免把轨迹当接触视频。')
        self.assertIn('修改尚未复核',self.page.locator('#reviewBanner').inner_text())
        with self.page.expect_response(lambda response:response.url.endswith('/api/plan')):
            self.page.locator('#savePlan').click()
        self.page.reload(wait_until='networkidle');self.open_job('plan_blocked')
        self.assertIn('修改尚未复核',self.page.locator('#reviewBanner').inner_text())
        self.assertFalse(self.page.locator('#repairPlanButton').is_visible())
        self.assertFalse(self.page.locator('#renderButton').is_disabled())
        self.assertEqual(self.page.locator('[data-narration="1"]').input_value(),original)

    def test_new_display_can_be_reviewed_but_plan_save_does_not_save_display(self):
        self.open_job('plan_display_blocked')
        job=self.root/'jobs/plan_display_blocked'
        before=pv.load_json(job/'analysis/studio_plan.json')
        self.page.locator('#editDisplay').click();self.page.locator('#format').select_option('landscape')
        self.assertFalse(self.page.locator('#regenerateDisplay').is_disabled())
        self.page.locator('[data-tab="editor"]').click()
        self.assertIn('画面设置待复核',self.page.locator('#reviewBanner').inner_text())
        self.assertFalse(self.page.locator('#repairPlanButton').is_visible())
        self.assertFalse(self.page.locator('#renderButton').is_disabled())
        self.assertFalse(self.page.evaluate('state.dirty'))
        with self.page.expect_response(lambda response:response.url.endswith('/api/plan')):
            self.page.locator('#savePlan').click()
        self.assertEqual(pv.load_json(job/'request.json')['format'],'portrait')
        self.assertEqual(self.page.evaluate('state.job.request.format'),'portrait')
        self.assertIn('画面设置待复核',self.page.locator('#reviewBanner').inner_text())
        self.assertIn('生成时才提交',self.page.locator('#reviewBanner').inner_text())
        self.page.locator('#editDisplay').click()
        with patch.object(self.http.manager,'start') as start:
            with self.page.expect_response(lambda response:response.url.endswith('/api/render') and response.request.method=='POST') as response:
                self.page.locator('#regenerateDisplay').click()
            self.assertEqual(response.value.status,200)
            body=response.value.request.post_data_json
            self.assertEqual(body['display']['format'],'landscape')
            self.assertIs(body['auto_repair'],False)
            start.assert_called_once_with(job,'render','')
        self.assertEqual(pv.load_json(job/'request.json')['format'],'landscape')
        self.assertIs(pv.load_json(job/'request.json')['auto_repair'],False)
        self.assertEqual(pv.load_json(job/'analysis/studio_plan.json'),before)
        self.assertEqual(self.errors,[])

    def test_restored_preset_ignores_dormant_custom_dimensions_and_restores_blocker(self):
        self.open_job('plan_display_blocked')
        self.page.locator('#editDisplay').click()
        self.page.locator('#format').select_option('landscape')
        self.assertFalse(self.page.locator('#regenerateDisplay').is_disabled())
        self.page.locator('#format').select_option('portrait')
        self.assertTrue(self.page.locator('#regenerateDisplay').is_disabled())
        self.page.locator('#format').select_option('custom')
        self.page.locator('#customWidth').fill('2048');self.page.locator('#customHeight').fill('1024')
        self.page.locator('#format').select_option('portrait')
        self.assertTrue(self.page.locator('#regenerateDisplay').is_disabled())
        self.page.locator('[data-tab="editor"]').click()
        self.assertTrue(self.page.locator('#renderButton').is_disabled())
        self.assertTrue(self.page.locator('#repairPlanButton').is_visible())
        self.assertNotIn('画面设置待复核',self.page.locator('#reviewBanner').inner_text())
        self.assertFalse(self.page.evaluate('state.dirty'))
        self.assertEqual(self.page.evaluate('state.job.request.width'),1920)
        self.assertEqual(self.errors,[])

    def test_review_service_failure_is_not_hidden_by_older_blocked_review(self):
        self.open_job('plan_review_service_failed')
        status={'active':True,'job_id':'plan_review_service_failed','logs':['【文稿与分镜】重新复核']}
        self.page.route('**/api/status',lambda route:route.fulfill(json=status));self.page.evaluate('poll()')
        status['active']=False;self.page.evaluate('poll()')
        self.assertIn('model unavailable: HTTP 503',self.page.locator('#toast').inner_text())
        banner=self.page.locator('#reviewBanner')
        self.assertIn('model unavailable: HTTP 503',banner.inner_text())
        self.assertIn('上次复核',banner.inner_text())
        self.assertFalse(self.page.locator('#repairPlanButton').is_visible())
        self.assertTrue(self.page.locator('#retryJob').is_visible())
        self.assertFalse(self.page.locator('#retryJob').is_disabled())
        self.assertFalse(self.page.locator('#renderButton').is_disabled())
        job=self.root/'jobs/plan_review_service_failed'
        original_review=(job/'review.json').read_bytes()
        with patch.object(self.http.manager,'start') as start:
            with self.page.expect_response(lambda response:response.url.endswith('/api/render') and response.request.method=='POST') as response:
                self.page.locator('#retryJob').click()
            self.assertEqual(response.value.status,200)
            start.assert_called_once_with(job,'render','')
        self.assertEqual((job/'review.json').read_bytes(),original_review)
        self.assertIs(pv.load_json(job/'request.json')['auto_repair'],False)
        self.assertIs(pv.load_json(job/'request.json')['final_review'],True)
        self.assertEqual(self.errors,[])

    def test_switching_jobs_clears_stale_error_and_stale_storyboard(self):
        self.open_job('render_failed');self.open_job('prepared')
        self.assertNotIn('HTTP 503',self.page.locator('#reviewBanner').inner_text())
        self.open_job('missing_plan')
        self.assertEqual(self.page.locator('[data-narration]').count(),0)
        self.assertIn('首镜头cue必须为空。',self.page.locator('#reviewBanner').inner_text())
        self.assertEqual(self.errors,[])

    def test_successful_save_clears_failure_without_losing_narration_edits(self):
        self.open_job('render_failed')
        revised='这段新文稿已在编辑器修改。'
        self.page.locator('[data-narration="0"]').fill(revised)
        self.page.locator('#savePlan').click()
        self.page.wait_for_function("document.querySelector('#toast').textContent.includes('已保存')")
        self.assertNotIn('HTTP 503',self.page.locator('#reviewBanner').inner_text())
        self.assertEqual(self.page.locator('[data-narration="0"]').input_value(),revised)
        saved=self.page.request.get(self.base+'/api/job?id=render_failed').json()
        self.assertEqual(saved['status'],'prepared');self.assertIsNone(saved['error'])
        self.assertEqual(saved['plan']['blocks'][0]['narration'],revised)

    def test_rejected_save_keeps_error_and_unsaved_narration(self):
        self.open_job('render_failed')
        revised='保存被拒绝时，文字仍留在这里。'
        self.page.locator('[data-narration="0"]').fill(revised)
        self.page.route('**/api/plan',lambda route:route.fulfill(status=400,
            content_type='application/json',body='{"error":"fixture save rejected"}'))
        self.page.locator('#savePlan').click()
        self.page.wait_for_function("document.querySelector('#toast').textContent.includes('fixture save rejected')")
        self.assertIn('HTTP 503',self.page.locator('#reviewBanner').inner_text())
        self.assertEqual(self.page.locator('[data-narration="0"]').input_value(),revised)

    def test_no_plan_retry_uses_selected_provider_and_keeps_key_only_in_session(self):
        self.open_job('missing_plan')
        self.assertTrue(self.page.locator('#retryJob').is_visible())
        self.assertFalse(self.page.locator('#retryJob').is_disabled())
        self.page.locator('#openSettings').click();self.page.locator('#backend').select_option('api')
        self.page.locator('#apiBase').fill('https://model.example/v1')
        self.page.locator('#model').fill('new-vision');self.page.locator('#apiKey').fill('ephemeral-test-key')
        self.page.locator('#settingsForm button[type="submit"]').click()
        with patch.object(self.http.manager,'start') as start:
            self.page.locator('#retryJob').click()
            self.page.wait_for_function("document.querySelector('#toast').textContent.includes('已继续原任务')")
            start.assert_called_once_with(self.root/'jobs/missing_plan','resume','ephemeral-test-key')
        saved=pv.load_json(self.root/'jobs/missing_plan/request.json')
        self.assertEqual((saved['backend'],saved['model']),('api','new-vision'))
        self.assertNotIn('ephemeral-test-key',str(saved))
        stored=self.page.evaluate('JSON.stringify(localStorage)')
        self.assertNotIn('ephemeral-test-key',stored)
        self.assertEqual(self.errors,[])

    def test_export_failure_has_a_direct_export_retry(self):
        self.open_job('export_failed')
        self.assertEqual(self.page.locator('#retryJob').inner_text(),'重试导出')
        self.assertFalse(self.page.locator('#exportButton').is_disabled())
        with patch.object(self.http.manager,'start') as start:
            self.page.locator('#retryJob').click()
            self.page.wait_for_function("document.querySelector('#toast').textContent.includes('正在导出')")
            start.assert_called_once_with(self.root/'jobs/export_failed','export')
        self.assertEqual((self.root/'jobs/export_failed/output/video.mp4').read_bytes(),b'rendered fixture')
        self.assertEqual(self.errors,[])

    def test_review_unavailable_has_a_visible_output_retry_without_render(self):
        self.page.locator('[data-job="review_unavailable"]').click()
        self.page.wait_for_selector('#view-output:not([hidden])')
        self.assertIn('TimeoutError',self.page.locator('#finalReviewPanel').inner_text())
        self.assertTrue(self.page.locator('#retryReview').is_visible())
        self.assertFalse(self.page.locator('#retryReview').is_disabled())
        with patch.object(self.http.manager,'start') as start:
            self.page.locator('#retryReview').click()
            self.page.wait_for_function("document.querySelector('#toast').textContent.includes('正在重新复查已有成片')")
            start.assert_called_once_with(self.root/'jobs/review_unavailable','review','')
        self.assertEqual((self.root/'jobs/review_unavailable/output/video.mp4').read_bytes(),b'rendered fixture')
        self.assertEqual(self.errors,[])

    def test_worker_failure_before_first_active_poll_is_still_loaded(self):
        self.open_job('missing_plan')
        def fail_immediately(job,action,key=''):
            self.http.manager.job=job
            pv.save_json(job/'job.json',{'engine':'studio','status':'failed','failed_action':action,
                'error':'fixture worker exited before the first status poll'})
        with patch.object(self.http.manager,'start',side_effect=fail_immediately):
            self.page.locator('#retryJob').click()
            self.page.wait_for_function("document.querySelector('#reviewBanner').textContent.includes('fixture worker exited before')",timeout=5000)
        self.assertEqual(self.errors,[])

    def test_background_job_never_replaces_selected_editor_or_its_unsaved_draft(self):
        self.open_job('prepared')
        revised='任务 A 的未保存文稿仍应留在当前编辑器。'
        self.page.locator('[data-narration="0"]').fill(revised)
        status={'active':True,'job_id':'render_failed','logs':['【成片与导出】后台任务 B']}
        self.page.route('**/api/status',lambda route:route.fulfill(json=status))
        self.page.evaluate('poll()')
        self.assertEqual(self.page.evaluate('state.job.id'),'prepared')
        self.assertTrue(self.page.evaluate('Boolean(state.job.plan)'))
        self.assertTrue(self.page.locator('#savePlan').is_disabled())
        self.page.locator('[data-shot="0,0"]').click()
        self.assertTrue(self.page.locator('#shotDialog').is_visible())
        self.assertEqual(self.page.locator('#shotCardText').input_value(),'测试')
        self.page.keyboard.press('Escape')
        status['active']=False
        self.page.evaluate('poll()')
        self.assertEqual(self.page.evaluate('state.job.id'),'prepared')
        self.assertEqual(self.page.locator('[data-narration="0"]').input_value(),revised)
        self.assertTrue(self.page.evaluate('state.dirty'))
        self.assertFalse(self.page.locator('#savePlan').is_disabled())
        self.assertNotIn('HTTP 503',self.page.locator('#reviewBanner').inner_text())
        self.assertEqual(self.errors,[])

    def test_background_completion_preserves_completed_job_preview(self):
        self.page.locator('[data-job="review_unavailable"]').click()
        self.page.wait_for_selector('#view-output:not([hidden])')
        before=self.page.locator('#videoPlayer').get_attribute('src')
        status={'active':True,'job_id':'render_failed','logs':['【成片与导出】后台任务 B']}
        self.page.route('**/api/status',lambda route:route.fulfill(json=status))
        self.page.evaluate('poll()')
        self.assertEqual(self.page.evaluate('state.job.id'),'review_unavailable')
        self.assertEqual(self.page.locator('#videoPlayer').get_attribute('src'),before)
        status['active']=False
        self.page.evaluate('poll()')
        self.assertEqual(self.page.evaluate('state.job.id'),'review_unavailable')
        self.assertTrue(self.page.locator('#view-output').is_visible())
        self.assertEqual(self.page.locator('#videoPlayer').get_attribute('src'),before)
        self.assertIn('TimeoutError',self.page.locator('#finalReviewPanel').inner_text())
        self.assertEqual(self.errors,[])

    def test_user_started_current_job_still_refreshes_on_completion(self):
        self.open_job('render_failed')
        status={'active':False,'job_id':None,'logs':[]}
        self.page.route('**/api/status',lambda route:route.fulfill(json=status))
        def start(job,action,key=''):
            status.update(active=True,job_id=job.name,logs=['【成片与导出】当前任务'])
            pv.save_json(job/'job.json',{'engine':'studio','status':'processing'})
        with patch.object(self.http.manager,'start',side_effect=start) as worker:
            self.page.locator('#renderButton').click()
            self.page.wait_for_function('state.active && !state.polling')
            worker.assert_called_once()
        job=self.root/'jobs/render_failed'
        completed=pv.load_json(job/'analysis/studio_plan.json');completed['title']='当前任务已完成'
        pv.save_json(job/'analysis/studio_plan.json',completed)
        pv.save_json(job/'job.json',{'engine':'studio','status':'completed'})
        status['active']=False
        self.page.evaluate('poll()')
        self.assertEqual(self.page.evaluate('state.job.id'),'render_failed')
        self.assertEqual(self.page.locator('#projectName').inner_text(),'当前任务已完成')
        self.assertTrue(self.page.locator('#view-output').is_visible())
        self.assertIn('视频已生成',self.page.locator('#toast').inner_text())
        self.assertEqual(self.errors,[])

    def test_worker_plan_checkpoint_returns_to_editor_with_actionable_message(self):
        self.open_job('render_failed')
        status={'active':True,'job_id':'render_failed','logs':['【文稿与分镜】复核']}
        self.page.route('**/api/status',lambda route:route.fulfill(json=status));self.page.evaluate('poll()')
        job=self.root/'jobs/render_failed';plan=pv.load_json(job/'analysis/studio_plan.json')
        pv.save_json(job/'job.json',{'engine':'studio','status':'review_needed','stage':'plan_review','error':None})
        pv.save_json(job/'review.json',{'ready':False,'summary':'请修改镜头','_plan_hash':server.engine.plan_hash(plan),
            '_content_type':'auto','_story_framework':'auto','issues':[{'severity':'error','block_id':'b1','reason':'所选画面对应其他案例。'}]})
        status['active']=False;self.page.evaluate('poll()')
        self.assertTrue(self.page.locator('#view-editor').is_visible())
        self.assertIn('分镜待修改',self.page.locator('#toast').inner_text())
        self.assertNotIn('任务已停止',self.page.locator('#toast').inner_text())
        self.assertTrue(self.page.locator('#repairPlanButton').is_visible())
        self.assertTrue(self.page.locator('#renderButton').is_disabled())
        self.assertEqual(self.errors,[])


if __name__=='__main__':unittest.main()
