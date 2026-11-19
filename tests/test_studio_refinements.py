import tempfile,unittest
from pathlib import Path
import paper_video as pv
from studio_revision import file_hash,render_settings_hash,review_stale
from studio_server import validate_settings

class RefinementTests(unittest.TestCase):
    def test_browser_failure_leaves_an_actionable_address(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from studio_server import open_workbench
        for outcome in (False,OSError('Browser unavailable')):
            with self.subTest(outcome=outcome),patch('studio_server.webbrowser.open',side_effect=outcome if isinstance(outcome,Exception) else None,return_value=outcome):
                output=io.StringIO()
                with redirect_stdout(output):self.assertFalse(open_workbench('http://127.0.0.1:8780'))
                self.assertIn('未能自动打开浏览器',output.getvalue())
                self.assertIn('http://127.0.0.1:8780',output.getvalue())

    def test_render_endpoint_persists_voice_and_display_together(self):
        import json,threading
        from urllib.request import Request,urlopen
        from urllib.error import HTTPError
        from unittest.mock import Mock,patch
        from studio_server import StudioHTTPServer,Handler,Manager
        with tempfile.TemporaryDirectory() as folder:
            job=Path(folder);(job/'analysis').mkdir()
            pv.save_json(job/'analysis/studio_plan.json',{'blocks':[]})
            pv.save_json(job/'job.json',{'engine':'studio','status':'prepared'})
            pv.save_json(job/'request.json',{'voice':'zh-CN-YunyangNeural','rate':'+0%'})
            server=StudioHTTPServer(('127.0.0.1',0),Handler)
            server.manager=Manager();server.manager.start=Mock()
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def post(data):
                request=Request(f'http://127.0.0.1:{server.server_port}/api/render',data=json.dumps(data).encode(),headers={'Content-Type':'application/json'})
                with urlopen(request) as response:return json.load(response)
            try:
                with patch('studio_server.job_path',return_value=job):
                    post({'job_id':'fixture','content_type':'general','speech':{'voice':'zh-CN-YunjianNeural','rate':'+50%'},'display':{'format':'landscape'}})
                    saved=pv.load_json(job/'request.json')
                    self.assertEqual((saved['voice'],saved['rate'],saved['format']),('zh-CN-YunjianNeural','+50%','landscape'))
                    self.assertEqual(saved['content_type'],'general')
                    server.manager.start.assert_called_once()
                    with self.assertRaises(HTTPError):post({'job_id':'fixture','speech':{'rate':'+999%'},'display':{'format':'portrait'}})
                    self.assertEqual(pv.load_json(job/'request.json'),saved)
            finally:server.shutdown();server.server_close();thread.join()

    def test_all_audition_voices_support_approved_rate(self):
        for voice in ('zh-CN-YunyangNeural','zh-CN-YunxiNeural','zh-CN-YunjianNeural','zh-TW-YunJheNeural','zh-CN-YunxiaNeural'):
            validate_settings({'voice':voice,'rate':'+50%'})

    def test_review_invalidates_changed_audio_display_project_and_video(self):
        with tempfile.TemporaryDirectory() as folder:
            job=Path(folder);(job/'output').mkdir()
            request={'voice':'zh-CN-YunyangNeural','rate':'+50%','media_fit':'contain'}
            pv.save_json(job/'request.json',request);pv.save_json(job/'project.json',{})
            video=job/'output/video.mp4';video.write_bytes(b'original')
            review={'plan_hash':'plan','settings_hash':render_settings_hash(request),'project_sha256':file_hash(job/'project.json'),'video_sha256':file_hash(video)}
            self.assertFalse(review_stale(job,review,'plan'))
            self.assertTrue(review_stale(job,review,'edited plan'))
            for key,value in [('rate','+10%'),('voice','zh-CN-YunxiNeural'),('media_fit','cover')]:
                pv.save_json(job/'request.json',request|{key:value})
                self.assertTrue(review_stale(job,review,'plan'))
            pv.save_json(job/'request.json',request)
            pv.save_json(job/'project.json',{'outro':None})
            self.assertTrue(review_stale(job,review,'plan'))
            pv.save_json(job/'project.json',{})
            video.write_bytes(b'changed video')
            self.assertTrue(review_stale(job,review,'plan'))

    def test_same_size_timestamp_changes_invalidate_actual_bytes_and_export_receipt(self):
        import hashlib,os
        from studio_engine import plan_hash
        from studio_revision import export_version_current
        for changed_file,replacement in [('output/video.mp4',b'other bytes'),('project.json',b'{"value":2}')]:
            with self.subTest(changed_file=changed_file),tempfile.TemporaryDirectory() as folder:
                job=Path(folder);(job/'output').mkdir();(job/'analysis').mkdir()
                pv.save_json(job/'request.json',{})
                plan={'blocks':[]};pv.save_json(job/'analysis/studio_plan.json',plan)
                (job/'project.json').write_bytes(b'{"value":1}')
                (job/'output/video.mp4').write_bytes(b'first bytes')
                review={'plan_hash':plan_hash(plan),'settings_hash':render_settings_hash({}),
                        'project_sha256':file_hash(job/'project.json'),
                        'video_sha256':file_hash(job/'output/video.mp4')}
                pv.save_json(job/'output/render_receipt.json',review)
                self.assertFalse(review_stale(job,review,plan_hash(plan)))
                self.assertTrue(export_version_current(job))
                target=job/changed_file;before=target.stat();old_hash=file_hash(target)
                target.write_bytes(replacement)
                os.utime(target,ns=(before.st_atime_ns,before.st_mtime_ns))
                after=target.stat()
                self.assertEqual((before.st_size,before.st_mtime_ns),(after.st_size,after.st_mtime_ns))
                self.assertNotEqual(hashlib.sha256(replacement).hexdigest(),old_hash)
                self.assertEqual(file_hash(target),hashlib.sha256(replacement).hexdigest())
                self.assertTrue(review_stale(job,review,plan_hash(plan)))
                self.assertFalse(export_version_current(job))

    def test_editorial_prefix_is_removed_from_saved_plan_overlay(self):
        from studio_render import write_subtitles
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            write_subtitles(root,root,'Arial','研究标题',[{'start':0,'end':2,'chapter':'先讲清：机器人到底在学什么'}],[],2,1920,1080,{'layout_style':'immersive','show_sources':False,'show_disclosure':False})
            ass=(root/'captions.ass').read_text(encoding='utf-8')
            self.assertNotIn('先讲清',ass);self.assertIn('机器人到底在学什么',ass)

    def test_new_studio_exports_have_no_synthetic_outro(self):
        from test_studio import plan_fixture
        from studio_engine import export
        with tempfile.TemporaryDirectory() as folder:
            job=Path(folder);(job/'assets').mkdir()
            config=export(job,plan_fixture(),[],{})
            self.assertIsNone(config['outro'])
            self.assertFalse((job/'assets/cover.png').exists())
