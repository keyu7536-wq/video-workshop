import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
import paper_video as pv
from make_video import ffmpeg_path,run
from studio_delivery import final_review,review_frames
from studio_engine import REVIEW_SCHEMA


class ReviewFrameCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.job=Path(self.temporary.name)
        (self.job/'analysis').mkdir();(self.job/'output').mkdir()

    def tearDown(self):self.temporary.cleanup()

    def shots(self,count):
        shots=[{'start':i*2,'end':i*2+2,'kind':'video','label':f'shot-{i}',
            'keep_full':True} for i in range(count)]
        pv.save_json(self.job/'output/rendered_shots.json',shots)
        return shots

    def fake_decode(self,command):
        Image.new('RGB',(16,16),'blue').save(command[-1])

    def test_real_first_and_last_encoded_frames_are_kept_even_beyond_planned_timeline(self):
        source=self.job/'source';source.mkdir()
        for index in range(61):
            color='red' if index==0 else 'lime' if index==60 else 'blue'
            Image.new('RGB',(96,64),color).save(source/f'{index:03}.png')
        ffmpeg=ffmpeg_path()
        run([ffmpeg,'-v','error','-y','-framerate','10','-i',str(source/'%03d.png'),
            '-c:v','libx264','-pix_fmt','yuv420p',str(self.job/'output/video.mp4')])
        # The last green frame occurs after the planned timeline: seeking the
        # timeline's end instead of the encoded tail would incorrectly see blue.
        pv.save_json(self.job/'output/rendered_shots.json',[
            {'start':0,'end':2,'label':'first shot'},{'start':2,'end':5,'label':'last planned shot'}])
        with patch('make_video.run',wraps=run) as decoder,patch('make_video.ffmpeg_path',return_value=ffmpeg):
            frames=review_frames(self.job)
        first=next(frame for frame in frames if frame['sample']=='first_encoded_frame')
        last=next(frame for frame in frames if frame['sample']=='last_encoded_frame')
        with Image.open(first['path']) as image:r,g,b=image.convert('RGB').getpixel((100,100))
        self.assertGreater(r,220);self.assertLess(g,35);self.assertLess(b,35)
        with Image.open(last['path']) as image:r,g,b=image.convert('RGB').getpixel((100,100))
        self.assertGreater(g,220);self.assertLess(r,35);self.assertLess(b,35)
        self.assertEqual(last['time'],5)
        self.assertEqual(last['time_basis'],'timeline_end_approximation')
        self.assertIn('未读取该帧PTS',last['time_note'])
        commands=[call.args[0] for call in decoder.call_args_list]
        tail=next(command for command in commands if '-sseof' in command)
        self.assertEqual(tail[tail.index('-sseof')+1],'-2')
        self.assertEqual(tail[tail.index('-vf')+1],'scale=960:-2,reverse')
        self.assertEqual(len(frames),4)

    def test_46_shots_keep_every_midpoint_and_both_endpoints_within_budget(self):
        self.shots(46)
        with patch('make_video.run',side_effect=self.fake_decode),patch('make_video.ffmpeg_path',return_value='ffmpeg'):
            frames=review_frames(self.job)
        self.assertEqual(len(frames),48)
        labels={frame['label'] for frame in frames}
        self.assertTrue({f'shot-{i}' for i in range(46)}<=labels)
        self.assertEqual({frame['sample'] for frame in frames},{'seek','first_encoded_frame','last_encoded_frame'})
        coverage=pv.load_json(self.job/'analysis/final_frames/coverage.json')
        self.assertTrue(coverage['complete'])
        self.assertEqual(coverage['covered_shot_indices'],list(range(46)))
        self.assertEqual(coverage['omitted_shot_indices'],[])

    def test_over_budget_is_explicitly_incomplete_even_when_model_approves(self):
        self.shots(60);(self.job/'output/video.mp4').write_bytes(b'video fixture')
        pv.save_json(self.job/'project.json',{'blocks':[]})
        pv.save_json(self.job/'output/run_report.json',{'duration_seconds':120})
        prompts=[]
        def reviewer(prompt,*args):
            prompts.append(prompt)
            return {'ready':True,'summary':'only supplied frames reviewed','issues':[]}
        with patch('make_video.run',side_effect=self.fake_decode),patch('make_video.ffmpeg_path',return_value='ffmpeg'):
            result,frames=final_review(self.job,{}, {'blocks':[]},[],reviewer,REVIEW_SCHEMA)
        self.assertEqual(len(frames),48)
        self.assertEqual(result['status'],'incomplete')
        self.assertFalse(result['ready']);self.assertFalse(result['review_complete'])
        coverage=result['sampling_coverage']
        self.assertFalse(coverage['complete']);self.assertEqual(coverage['total_shots'],60)
        self.assertEqual(len(coverage['covered_shot_indices']),46)
        self.assertEqual(len(coverage['omitted_shot_indices']),14)
        self.assertEqual(set(coverage['covered_shot_indices'])|set(coverage['omitted_shot_indices']),set(range(60)))
        self.assertIn(0,coverage['covered_shot_indices']);self.assertIn(59,coverage['covered_shot_indices'])
        self.assertTrue(all('omitted_shot_indices' in prompt for prompt in prompts))
        self.assertTrue(all(review['status']=='completed' for review in result['reviews']))

    def test_empty_decoder_output_cannot_reuse_previous_review_frame(self):
        self.shots(1);folder=self.job/'analysis/final_frames';folder.mkdir()
        stale=folder/'00.jpg';Image.new('RGB',(16,16),'green').save(stale)
        with patch('make_video.run'),patch('make_video.ffmpeg_path',return_value='ffmpeg'):
            with self.assertRaisesRegex(RuntimeError,'未解码出审核画面'):review_frames(self.job)
        self.assertFalse(stale.exists())


if __name__=='__main__':unittest.main()
