"""Headless checks for persistent job errors using isolated jobs and HTTP ports."""
import contextlib
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


if __name__=='__main__':unittest.main()
