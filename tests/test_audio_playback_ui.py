"""Real browser/media fixtures for codec guidance without overriding user audio."""
import contextlib
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import paper_video as pv
import studio_engine as engine
import studio_server as server
from studio_revision import file_hash, render_settings_hash


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'Playwright is not installed')
class AudioPlaybackUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        from make_video import ffmpeg_path
        cls.resources=contextlib.ExitStack();cls.addClassCleanup(cls.resources.close)
        cls.root=Path(cls.resources.enter_context(tempfile.TemporaryDirectory()))
        (cls.root/'jobs').mkdir()
        cls.resources.enter_context(patch.object(server,'ROOT',cls.root))
        cls.media=cls.root/'audio.mp4';cls.silent=cls.root/'silent.mp4';cls.webm=cls.root/'audio.webm'
        common=[ffmpeg_path(),'-v','error','-y','-f','lavfi','-i','testsrc2=size=320x180:rate=30']
        subprocess.run(common+['-f','lavfi','-i','sine=frequency=440:sample_rate=48000',
            '-t','3','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-movflags','+faststart',str(cls.media)],
            check=True,capture_output=True)
        subprocess.run(common+['-t','3','-c:v','libx264','-pix_fmt','yuv420p','-an','-movflags','+faststart',str(cls.silent)],
            check=True,capture_output=True)
        subprocess.run(common+['-f','lavfi','-i','sine=frequency=440:sample_rate=48000',
            '-t','3','-c:v','libvpx-vp9','-deadline','realtime','-cpu-used','8','-c:a','libopus',str(cls.webm)],
            check=True,capture_output=True)
        cls.http=server.StudioHTTPServer(('127.0.0.1',0),server.Handler);cls.http.manager=server.Manager()
        cls.thread=threading.Thread(target=cls.http.serve_forever,daemon=True);cls.thread.start()
        cls.resources.callback(cls.thread.join);cls.resources.callback(cls.http.server_close)
        cls.resources.callback(cls.http.shutdown);cls.base=f'http://127.0.0.1:{cls.http.server_port}'
        cls.playwright=sync_playwright().start();cls.resources.callback(cls.playwright.stop)
        executable=Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
        try:cls.browser=cls.playwright.chromium.launch(headless=True,
                **({'executable_path':str(executable)} if executable.is_file() else {}))
        except Exception as exc:raise unittest.SkipTest(f'Headless browser unavailable: {exc}')
        cls.resources.callback(cls.browser.close)

    def setUp(self):
        self.job=self.root/'jobs'/self._testMethodName
        (self.job/'analysis').mkdir(parents=True);(self.job/'output').mkdir()
        self.request={'backend':'codex','format':'landscape'}
        self.plan={'title':'音频预览','summary':'测试','source_issues':[],'unused_sources':[],
            'blocks':[{'id':'b1','chapter':'','narration':'测试旁白。','evidence':[],
                'shots':[{'kind':'card','source_id':'','in':0,'duration':3,'bbox':[],
                    'label':'测试镜头','card_text':'测试','cue':''}]}]}
        engine.validate(self.plan,[])
        for name,data in [('request.json',self.request),('job.json',{'engine':'studio','status':'completed'}),
                          ('analysis/studio_plan.json',self.plan),('analysis/sources.json',[]),
                          ('project.json',{'blocks':[]}),('output/run_report.json',
                           {'duration_seconds':3,'resolution':'320x180','fps':30,'shots':1}),
                          ('output/rendered_shots.json',[{'start':0,'end':3,'label':'测试镜头','kind':'card'}]),
                          ('output/verification.json',{'decoded_video':True,'audio_non_silent':True})]:
            pv.save_json(self.job/name,data)
        self.install_video(self.media)
        self.page=self.browser.new_page(viewport={'width':1440,'height':1000});self.addCleanup(self.page.close)
        self.page.set_default_timeout(7000);self.errors=[]
        self.page.on('pageerror',lambda error:self.errors.append(str(error)))

    def install_video(self,path):
        shutil.copyfile(path,self.job/'output/video.mp4')
        pv.save_json(self.job/'output/render_receipt.json',{'plan_hash':engine.plan_hash(self.plan),
            'settings_hash':render_settings_hash(self.request),'video_sha256':file_hash(self.job/'output/video.mp4'),
            'project_sha256':file_hash(self.job/'project.json')})

    def unsupported_aac(self):
        self.page.add_init_script('''const original=HTMLMediaElement.prototype.canPlayType;
            HTMLMediaElement.prototype.canPlayType=function(type){
                return type.includes('mp4a.40.2')?'':original.call(this,type);
            };''')

    def open_job(self):
        with self.page.expect_response(lambda response:'/output/verification.json' in response.url) as verification:
            self.page.goto(self.base+'/?job='+self.job.name,wait_until='domcontentloaded')
        self.page.wait_for_function("document.querySelector('#videoPlayer').readyState>=1")

    def open_segment(self):
        self.page.locator('[data-segment="0"]').click()
        self.page.wait_for_function("document.querySelector('#segmentVideo').readyState>=1")

    def test_supported_aac_and_source_preview_do_not_show_compatibility_warning(self):
        self.open_job()
        supports_aac=self.page.locator('#videoPlayer').evaluate('(v)=>v.canPlayType(\'audio/mp4; codecs="mp4a.40.2"\')')
        if not supports_aac:
            self.assertTrue(self.page.locator('#videoPlayerAudioNotice').is_visible())
            self.assertIn('不支持 AAC',self.page.locator('#videoPlayerAudioNotice').inner_text())
            self.open_segment()
            self.assertTrue(self.page.locator('#segmentVideoAudioNotice').is_visible())
            self.assertEqual(self.errors,[])
            self.skipTest('Browser build has no AAC capability; compatibility guidance verified, AAC decoding check skipped')
        self.assertTrue(self.page.locator('#videoPlayerAudioNotice').is_hidden())
        self.assertTrue(self.page.locator('#videoPlayerAudioLevel').is_hidden())
        self.assertTrue(self.page.locator('#shotVideoPreview').evaluate('(v)=>v.muted'))
        self.open_segment()
        self.assertTrue(self.page.locator('#segmentVideoAudioNotice').is_hidden())
        self.assertEqual(self.page.locator('#segmentVideo').evaluate('(v)=>[v.muted,v.volume]'),[False,1])
        self.page.locator('#playFinalClip').click()
        self.page.wait_for_function("document.querySelector('#segmentVideo').currentTime>.2")
        decoded=self.page.locator('#segmentVideo').evaluate('(v)=>v.webkitAudioDecodedByteCount')
        if decoded is None:
            self.skipTest('Browser exposes no decoded audio counter; playback UI verified, AAC decoding check skipped')
        self.assertGreater(decoded,0)
        self.assertEqual(self.errors,[])

    def test_unsupported_aac_guidance_copies_current_task_for_both_players(self):
        self.unsupported_aac()
        self.page.add_init_script("Object.defineProperty(navigator,'clipboard',{value:{writeText:async(text)=>{window.copiedAddress=text;}}})")
        self.open_job()
        notice=self.page.locator('#videoPlayerAudioNotice')
        self.assertTrue(notice.is_visible());self.assertIn('如果这份 MP4 使用 AAC',notice.inner_text())
        expected=self.base+'/?job='+self.job.name
        self.assertEqual(self.page.locator('#videoPlayerAudioAddress').input_value(),expected)
        notice.locator('[data-copy-task]').click()
        self.page.wait_for_function('window.copiedAddress!==undefined')
        self.assertEqual(self.page.evaluate('window.copiedAddress'),expected)
        self.open_segment()
        self.assertTrue(self.page.locator('#segmentVideoAudioNotice').is_visible())
        self.assertEqual(self.page.locator('#segmentVideoAudioAddress').input_value(),expected)
        self.assertEqual(self.page.locator('#videoPlayer').evaluate('(v)=>[v.muted,v.volume]'),[False,1])
        self.assertEqual(self.errors,[])

    def test_browser_without_aac_skips_decoding_only_after_guidance_is_verified(self):
        self.unsupported_aac()
        with self.assertRaisesRegex(unittest.SkipTest,'compatibility guidance verified'):
            self.test_supported_aac_and_source_preview_do_not_show_compatibility_warning()

    def test_user_muting_and_zero_volume_are_visible_and_never_overridden(self):
        self.open_job()
        self.page.locator('#videoPlayer').evaluate('(v)=>{v.volume=.4;v.muted=true;}')
        self.page.wait_for_function("document.querySelector('#videoPlayerAudioLevel').textContent.includes('已静音')")
        self.assertEqual(self.page.locator('#videoPlayer').evaluate('(v)=>[v.muted,v.volume]'),[True,.4])
        self.page.locator('#videoPlayer').evaluate('(v)=>{v.muted=false;v.volume=0;}')
        self.page.wait_for_function("document.querySelector('#videoPlayerAudioLevel').textContent.includes('音量为 0')")
        self.open_segment()
        self.assertEqual(self.page.locator('#segmentVideo').evaluate('(v)=>[v.muted,v.volume]'),[False,1])
        self.page.locator('#segmentVideo').evaluate('(v)=>v.muted=true')
        self.page.wait_for_function("document.querySelector('#segmentVideoAudioLevel').textContent.includes('已静音')")
        self.assertEqual(self.page.locator('#videoPlayer').evaluate('(v)=>[v.muted,v.volume]'),[False,0])
        self.assertEqual(self.errors,[])

    def test_no_video_or_metadata_has_no_codec_warning(self):
        self.unsupported_aac()
        # A stalled video request has no metadata; a browser capability result
        # must not be presented as a diagnosis of this unloaded file.
        self.page.route('**/output/video.mp4?*',lambda route:route.abort())
        self.page.goto(self.base+'/?job='+self.job.name,wait_until='networkidle')
        self.assertTrue(self.page.locator('#videoPlayerAudioNotice').is_hidden())
        self.page.locator('#newProject').click()
        self.assertTrue(self.page.locator('#videoPlayerAudioNotice').is_hidden())
        self.assertTrue(self.page.locator('#segmentVideoAudioNotice').is_hidden())
        self.assertEqual(self.errors,[])

    def test_failed_audio_check_is_not_mislabeled_as_browser_codec_failure(self):
        self.install_video(self.silent);self.unsupported_aac()
        pv.save_json(self.job/'output/verification.json',{'decoded_video':True,'audio_non_silent':False})
        self.open_job();notice=self.page.locator('#videoPlayerAudioNotice')
        self.page.wait_for_function("playbackAudioContext.audioCheck===false")
        self.assertTrue(notice.is_visible());self.assertIn('任务记录中的音频检查未通过',notice.inner_text())
        self.assertNotIn('不支持 AAC',notice.inner_text())
        self.assertTrue(notice.locator('[data-audio-open]').is_hidden());self.assertEqual(self.errors,[])

    def test_unknown_verification_is_quiet_and_clipboard_denial_keeps_selectable_address(self):
        (self.job/'output/verification.json').unlink()
        self.unsupported_aac()
        self.page.add_init_script("Object.defineProperty(navigator,'clipboard',{value:{writeText:async()=>{throw Error('denied');}}})")
        self.open_job();notice=self.page.locator('#videoPlayerAudioNotice')
        self.assertNotIn('音频检查未通过',notice.inner_text());self.assertNotIn('丢失',notice.inner_text())
        notice.locator('[data-copy-task]').click()
        self.page.wait_for_function("document.querySelector('#videoPlayerAudioNotice').textContent.includes('地址已选中')")
        address=self.page.locator('#videoPlayerAudioAddress')
        self.assertEqual(address.evaluate('(v)=>v.selectionEnd-v.selectionStart'),len(address.input_value()))
        self.assertEqual(self.errors,[])

    def test_explicit_non_aac_codec_suppresses_aac_warning(self):
        self.unsupported_aac()
        report=pv.load_json(self.job/'output/run_report.json');report['audio_codec']='opus'
        pv.save_json(self.job/'output/run_report.json',report)
        self.open_job()
        self.assertTrue(self.page.locator('#videoPlayerAudioNotice').is_hidden());self.assertEqual(self.errors,[])

    def test_webm_opus_is_not_diagnosed_using_aac_capabilities(self):
        self.unsupported_aac()
        shutil.copyfile(self.webm,self.job/'output/preview.webm')
        snapshot=server.snapshot(self.job)
        snapshot['video_url']=f'/files/{self.job.name}/output/preview.webm'
        self.page.route('**/api/job?*',lambda route:route.fulfill(json=snapshot))
        self.open_job()
        self.assertTrue(self.page.locator('#videoPlayerAudioNotice').is_hidden())
        self.open_segment()
        self.assertTrue(self.page.locator('#segmentVideoAudioNotice').is_hidden());self.assertEqual(self.errors,[])

    def test_late_previous_job_check_cannot_change_current_job_or_segment_notice(self):
        self.unsupported_aac();held=[]
        self.page.route(f'**/files/{self.job.name}/output/verification.json*',lambda route:held.append(route))
        self.page.goto(self.base+'/?job='+self.job.name,wait_until='domcontentloaded')
        self.page.wait_for_function("document.querySelector('#videoPlayer').readyState>=1")
        self.assertEqual(len(held),1)
        other=self.root/'jobs'/'other_audio_job';shutil.copytree(self.job,other)
        self.page.evaluate('(id)=>loadJob(id)',other.name)
        self.page.wait_for_function("document.querySelector('#videoPlayer').readyState>=1 && document.querySelector('#videoPlayer').currentSrc.includes('other_audio_job')")
        held[0].fulfill(json={'audio_non_silent':False})
        self.page.wait_for_function("playbackAudioContext.jobId==='other_audio_job' && playbackAudioContext.audioCheck===true")
        self.open_segment()
        for prefix in ('videoPlayer','segmentVideo'):
            self.assertNotIn('检查未通过',self.page.locator('#'+prefix+'AudioNotice').inner_text())
            self.assertEqual(self.page.locator('#'+prefix+'AudioAddress').input_value(),self.base+'/?job='+other.name)
        self.assertEqual(self.errors,[])


if __name__=='__main__':unittest.main()
