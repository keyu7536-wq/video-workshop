import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from studio_display import dimensions,validate_display
from studio_render import render_shot,write_subtitles,replace_video_holds,media_geometry,layout

class DisplayTests(unittest.TestCase):
    def test_paper_figures_keep_subtitle_safe_area(self):
        for w,h in [(1920,1080),(1080,1920),(1600,1000)]:
            x,y,mw,mh=media_geometry(w,h,{'layout_style':'immersive'},True)
            box=layout(w,h,'immersive')
            # Real subtitle clauses can wrap to two lines; paper labels must
            # remain above both lines, including their line spacing.
            self.assertLess(y+mh,h-box['voice_bottom']-2*box['voice_size']*1.2)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            write_subtitles(root,root,'Arial','覆盖标题',[{'start':0,'end':2,'chapter':'覆盖章节','protect_content':True}],[(0,2,'旁白')],2,1920,1080,{'layout_style':'immersive','show_sources':False,'show_disclosure':False})
            ass=(root/'captions.ass').read_text(encoding='utf-8')
            self.assertNotIn('覆盖标题',ass);self.assertNotIn('覆盖章节',ass)
    def test_custom_bounds_and_types(self):
        self.assertEqual(dimensions({'format':'square'}),(1080,1080))
        self.assertEqual(dimensions({'format':'custom','width':1600,'height':1000}),(1600,1000))
        for width,height in [(321,720),(True,720),('720',1280),(3840,3840),(320,1920),(0,720)]:
            with self.assertRaises(ValueError):validate_display({'format':'custom','width':width,'height':height})
        for key,value in [('media_fit','stretch'),('layout_style','missing'),('show_sources','false')]:
            with self.assertRaises(ValueError):validate_display({key:value})

    def test_cover_and_protected_figures(self):
        with tempfile.TemporaryDirectory() as folder,patch('make_video.run') as run:
            root=Path(folder);shot={'file':'clip.mp4','kind':'video','duration':1,'render_duration':1}
            render_shot(0,shot,root,root,'ffmpeg',30,1600,1000,{'layout_style':'immersive','media_fit':'cover'})
            command=run.call_args.args[0];vf=command[command.index('-vf')+1]
            self.assertIn('force_original_aspect_ratio=increase',vf);self.assertIn('crop=1600:1000',vf)
            render_shot(0,{**shot,'protect_content':True},root,root,'ffmpeg',30,1600,1000,{'layout_style':'immersive','media_fit':'cover'})
            command=run.call_args.args[0];vf=command[command.index('-vf')+1]
            self.assertIn('force_original_aspect_ratio=decrease',vf);self.assertNotIn('crop=',vf)

    def test_clean_screen_keeps_subtitles_and_no_page_notes(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            write_subtitles(root,root,'Arial','标题',[{'start':0,'end':2,'chapter':'方法','label':'内部提示','source':'论文第3页'}],[(0,2,'这是仿真结果')],2,1600,1000,{'layout_style':'immersive','show_sources':False,'show_disclosure':False})
            ass=(root/'captions.ass').read_text(encoding='utf-8')
            self.assertIn('这是仿真结果',ass)
            for word in ('论文第3页','内部提示','AI 辅助制作'):self.assertNotIn(word,ass)
            self.assertIn('这是仿真结果',(root/'subtitles.srt').read_text(encoding='utf-8'))

    def test_shortage_does_not_silently_create_text_page(self):
        with self.assertRaisesRegex(ValueError,'增加相关镜头'):
            replace_video_holds([{'kind':'video','render_duration':10,'duration':2}],Path('.'),Path('.'),30,True)

    def test_export_preserves_custom_settings_and_protects_cards(self):
        from studio_engine import export,validate_visual_policy
        from test_studio import plan_fixture
        settings={'format':'custom','width':1600,'height':1000,'layout_style':'immersive','media_fit':'cover','show_sources':False,'show_disclosure':False,'minimal_text':True}
        plan=plan_fixture()
        with tempfile.TemporaryDirectory() as folder:
            job=Path(folder);(job/'assets').mkdir()
            config=export(job,plan,[],settings)
            self.assertEqual((config['width'],config['height']),(1600,1000))
            self.assertEqual(config['media_fit'],'cover');self.assertFalse(config['show_sources'])
            self.assertTrue(config['blocks'][0]['shots'][0]['protect_content'])
        plan['blocks'][0]['shots'][0]['card_text']='长文字'*20
        with self.assertRaisesRegex(ValueError,'超过36字'):validate_visual_policy(plan,settings)
