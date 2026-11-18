"""Exercise recovery HTTP contracts without starting a model or renderer."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen

import paper_video as pv
import studio_server as server
import studio_engine as engine


class DisconnectedResponseTests(unittest.TestCase):
    def test_json_disconnect_is_normal_close_not_a_second_error_response(self):
        for error in (BrokenPipeError(),ConnectionResetError(),ConnectionAbortedError()):
            with self.subTest(error=type(error).__name__):
                handler=server.Handler.__new__(server.Handler)
                handler.send_response=Mock();handler.send_header=Mock();handler.end_headers=Mock()
                handler.wfile=Mock();handler.wfile.write.side_effect=error
                handler.json({'status':'saved'})
                self.assertTrue(handler.close_connection)
                handler.send_response.assert_called_once_with(200)

    def test_non_connection_write_error_is_not_hidden(self):
        handler=server.Handler.__new__(server.Handler)
        handler.send_response=Mock();handler.send_header=Mock();handler.end_headers=Mock()
        handler.wfile=Mock();handler.wfile.write.side_effect=OSError('unexpected I/O error')
        with self.assertRaises(OSError):handler.json({'status':'saved'})


def plan():
    return {'title':'恢复测试','summary':'测试','source_issues':[],'unused_sources':[],
        'blocks':[{'id':'b1','chapter':'测试','narration':'这段文稿需要保留。','evidence':[],
          'shots':[{'kind':'card','source_id':'','in':0,'duration':4,'bbox':[],'label':'测试',
                    'card_text':'测试','cue':''}]}]}


class RecoveryHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name).resolve();(self.root/'jobs').mkdir()
        root_patch=patch.object(server,'ROOT',self.root);root_patch.start();self.addCleanup(root_patch.stop)
        self.http=server.StudioHTTPServer(('127.0.0.1',0),server.Handler)
        self.http.manager=server.Manager();self.http.manager.start=Mock()
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.thread.join);self.addCleanup(self.http.server_close);self.addCleanup(self.http.shutdown)
        self.url=f'http://127.0.0.1:{self.http.server_port}'

    def job(self,name='fixture',with_plan=True,failed_action='render',status='failed',video=False):
        job=self.root/'jobs'/name;(job/'analysis').mkdir(parents=True);(job/'output').mkdir()
        pv.save_json(job/'job.json',{'engine':'studio','status':status,'error':'fixture failure','failed_action':failed_action})
        pv.save_json(job/'request.json',{'backend':'codex','format':'landscape','auto_render':False})
        pv.save_json(job/'analysis/sources.json',[]);pv.save_json(job/'analysis/images.json',[])
        if with_plan:pv.save_json(job/'analysis/studio_plan.json',plan())
        if video:(job/'output/video.mp4').write_bytes(b'previously-rendered-fixture')
        return job

    def post(self,endpoint,data):
        request=Request(self.url+endpoint,data=json.dumps(data).encode(),headers={'Content-Type':'application/json'})
        try:
            with urlopen(request) as response:return response.status,json.load(response)
        except HTTPError as response:return response.code,json.load(response)

    def test_failed_prepare_without_plan_can_resume_cached_sources(self):
        job=self.job(with_plan=False,failed_action='prepare')
        request={'job_id':job.name,'backend':'api','api_base':'https://model.example/v1',
                 'model':'vision-test','key':'session-only-secret'}
        status,result=self.post('/api/resume',request)
        self.assertEqual(status,200,result)
        self.http.manager.start.assert_called_once_with(job,'resume','session-only-secret')
        saved=pv.load_json(job/'request.json')
        self.assertEqual(saved['backend'],'api');self.assertEqual(saved['model'],'vision-test')
        self.assertNotIn('key',saved);self.assertNotIn('session-only-secret',(job/'request.json').read_text())
        self.assertEqual(server.snapshot(job)['recovery']['action'],'resume')

    def test_resume_invalid_provider_is_rejected_before_any_persistent_change(self):
        job=self.job(with_plan=False);before=(job/'request.json').read_bytes()
        for change in ({'backend':'unknown'},{'backend':'api','api_base':'http://remote.example/v1'},
                       {'backend':'api','api_base':'https://model.example/v1?key=secret'}):
            status,_=self.post('/api/resume',{'job_id':job.name,**change})
            self.assertEqual(status,400)
            self.assertEqual((job/'request.json').read_bytes(),before)
        self.http.manager.start.assert_not_called()

    def test_resume_cannot_compete_with_an_active_worker(self):
        job=self.job(with_plan=False)
        with patch.object(self.http.manager,'snapshot',return_value={'active':True}):
            status,_=self.post('/api/resume',{'job_id':job.name})
        self.assertEqual(status,400);self.http.manager.start.assert_not_called()

    def test_second_idle_server_cannot_edit_or_restart_processing_job(self):
        other=server.StudioHTTPServer(('127.0.0.1',0),server.Handler)
        other.manager=server.Manager();other.manager.start=Mock()
        thread=threading.Thread(target=other.serve_forever,daemon=True);thread.start()
        self.addCleanup(thread.join);self.addCleanup(other.server_close);self.addCleanup(other.shutdown)
        self.assertFalse(self.http.manager.snapshot()['active'])
        self.assertFalse(other.manager.snapshot()['active'])
        for index,endpoint in enumerate(('/api/plan','/api/render','/api/replan','/api/resume','/api/review','/api/export')):
            job=self.job('external_'+str(index),status='processing',video=True)
            before={name:(job/name).read_bytes() for name in ('job.json','request.json','analysis/studio_plan.json')}
            revised=plan();revised['blocks'][0]['narration']='第二个服务不能保存这段改动。'
            request=Request(f'http://127.0.0.1:{other.server_port}'+endpoint,
                data=json.dumps({'job_id':job.name,'plan':revised,'backend':'api',
                    'api_base':'https://model.example/v1','model':'different-model','export_formats':['mov']}).encode(),
                headers={'Content-Type':'application/json'})
            try:
                with urlopen(request) as response:status,result=response.status,json.load(response)
            except HTTPError as response:status,result=response.code,json.load(response)
            self.assertEqual(status,400,(endpoint,result))
            self.assertIn('制作',result['error'])
            self.assertEqual(before,{name:(job/name).read_bytes() for name in before},endpoint)
        other.manager.start.assert_not_called()

    def test_manager_cannot_reenter_job_already_marked_processing(self):
        job=self.job(status='processing')
        before=(job/'job.json').read_bytes();manager=server.Manager()
        with patch.object(server.subprocess,'Popen') as launch:
            with self.assertRaisesRegex(ValueError,'制作'):manager.start(job,'render')
        launch.assert_not_called()
        self.assertEqual((job/'job.json').read_bytes(),before)
        self.assertIsNone(manager.job);self.assertIsNone(manager.process)

    def test_manager_still_allows_recovery_statuses_to_start(self):
        for status in ('failed','cancelled','prepared'):
            job=self.job('recover_'+status,status=status)
            manager=server.Manager()
            with patch.object(server.subprocess,'Popen',side_effect=OSError('fixture launch failure')) as launch:
                with self.assertRaisesRegex(RuntimeError,'fixture launch failure'):manager.start(job,'render')
            launch.assert_called_once()
            saved=pv.load_json(job/'job.json')
            self.assertEqual(saved['status'],'failed');self.assertEqual(saved['last_action'],'render')

    def test_render_and_replan_use_new_provider_without_persisting_key(self):
        for index,endpoint in enumerate(('/api/render','/api/replan')):
            job=self.job('provider_'+str(index))
            status,result=self.post(endpoint,{'job_id':job.name,'backend':'api',
                'api_base':'https://model.example/v1','model':'new-vision','key':'transient-key'})
            self.assertEqual(status,200,result)
            saved=pv.load_json(job/'request.json')
            self.assertEqual((saved['backend'],saved['api_base'],saved['model']),
                ('api','https://model.example/v1','new-vision'))
            self.assertNotIn('transient-key',(job/'request.json').read_text(encoding='utf-8'))
            self.assertEqual(self.http.manager.start.call_args.args[-1],'transient-key')
            if endpoint=='/api/replan':self.assertEqual(saved['script'],'这段文稿需要保留。')

    def test_export_failure_retries_export_without_rendering_or_changing_video(self):
        job=self.job(failed_action='export',video=True);before=(job/'output/video.mp4').read_bytes()
        snapshot=server.snapshot(job)
        self.assertTrue(snapshot['can_export']);self.assertEqual(snapshot['recovery']['action'],'export')
        status,result=self.post('/api/export',{'job_id':job.name,'export_formats':['mov','wav']})
        self.assertEqual(status,200,result)
        self.http.manager.start.assert_called_once_with(job,'export')
        self.assertEqual((job/'output/video.mp4').read_bytes(),before)

    def test_unrelated_failure_or_edited_plan_cannot_export_old_video(self):
        for index,(status,stage) in enumerate([('failed','render'),('failed',None),('prepared','export')]):
            job=self.job('stale_'+str(index),status=status,failed_action=stage,video=True)
            self.assertFalse(server.snapshot(job)['can_export'])
            response,_=self.post('/api/export',{'job_id':job.name,'export_formats':['mov']})
            self.assertEqual(response,400)
        self.http.manager.start.assert_not_called()

    def unavailable_review(self,job):
        from studio_revision import file_hash,render_settings_hash
        pv.save_json(job/'project.json',{'fixture':'rendered project'})
        pv.save_json(job/'output/run_report.json',{'duration_seconds':5})
        pv.save_json(job/'output/final_review.json',{'status':'unavailable','ready':False,'review_complete':False,
            'summary':'review service timeout','unavailable_reason':'TimeoutError: review service timeout',
            'issues':[],'round':1,'coverage':'prepared frames, no successful review',
            'plan_hash':engine.plan_hash(plan()),'settings_hash':render_settings_hash(pv.load_json(job/'request.json')),
            'video_sha256':file_hash(job/'output/video.mp4'),'project_sha256':file_hash(job/'project.json')})
        pv.save_json(job/'output/render_receipt.json',{key:value for key,value in pv.load_json(job/'output/final_review.json').items()
            if key in ('plan_hash','settings_hash','video_sha256','project_sha256')})

    def test_unavailable_review_retries_only_review_and_preserves_render_settings(self):
        job=self.job(status='review_needed',failed_action='review',video=True);self.unavailable_review(job)
        before=(job/'output/video.mp4').read_bytes()
        self.assertEqual(server.snapshot(job)['recovery']['action'],'review')
        status,result=self.post('/api/review',{'job_id':job.name,'backend':'api','api_base':'https://model.example/v1',
            'model':'review-vision','key':'review-session-key','voice':'should-not-change','display':{'format':'portrait'}})
        self.assertEqual(status,200,result)
        self.http.manager.start.assert_called_once_with(job,'review','review-session-key')
        saved=pv.load_json(job/'request.json')
        self.assertEqual(saved['backend'],'api');self.assertEqual(saved['format'],'landscape')
        self.assertNotIn('voice',saved);self.assertNotIn('key',saved)
        self.assertEqual((job/'output/video.mp4').read_bytes(),before)

    def test_changed_plan_or_real_content_errors_cannot_use_service_retry(self):
        changed=self.job('changed',status='review_needed',failed_action='review',video=True)
        self.unavailable_review(changed)
        revised=plan();revised['blocks'][0]['narration']='文稿已经修改。'
        pv.save_json(changed/'analysis/studio_plan.json',revised)
        self.assertEqual(server.snapshot(changed)['recovery']['action'],'render')
        status,_=self.post('/api/review',{'job_id':changed.name});self.assertEqual(status,400)
        actual=self.job('content_error',status='review_needed',failed_action=None,video=True)
        self.unavailable_review(actual)
        review=pv.load_json(actual/'output/final_review.json');review.update(status='completed',review_complete=True,
            issues=[{'severity':'error','block_id':'b1','reason':'Actual unsupported statement'}])
        pv.save_json(actual/'output/final_review.json',review)
        self.assertIsNone(server.snapshot(actual)['recovery'])
        status,_=self.post('/api/review',{'job_id':actual.name});self.assertEqual(status,400)
        self.http.manager.start.assert_not_called()

    def test_missing_receipt_requests_render_instead_of_unverifiable_review(self):
        job=self.job(status='review_needed',failed_action='review',video=True)
        pv.save_json(job/'output/run_report.json',{'duration_seconds':4})
        self.assertEqual(server.snapshot(job)['recovery']['action'],'render')
        status,_=self.post('/api/review',{'job_id':job.name});self.assertEqual(status,400)
        self.http.manager.start.assert_not_called()


if __name__=='__main__':unittest.main()
