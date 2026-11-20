"""Real local FFmpeg sampling reaches the vision boundary with bounded images."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from make_video import ffmpeg_path
from studio_media_index import build_media_index, validate_media_selection


class MediaIndexDecodeTests(unittest.TestCase):
    def test_actual_video_frames_reach_indexer_and_cached_choice_validates(self):
        with tempfile.TemporaryDirectory() as temporary:
            job=Path(temporary).resolve()
            video=job/'two_states.mp4'
            subprocess.run([str(ffmpeg_path()),'-v','error','-y',
                            '-f','lavfi','-i','color=red:s=320x180:r=10:d=1',
                            '-f','lavfi','-i','color=blue:s=320x180:r=10:d=1',
                            '-filter_complex','[0:v][1:v]concat=n=2:v=1:a=0[v]',
                            '-map','[v]','-c:v','libx264','-pix_fmt','yuv420p',str(video)],
                           check=True,timeout=30,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            source={'id':'clip','type':'video','file':str(video),'duration_seconds':2,
                    'sample_times':[0,.5,1,1.5]}
            seen=[]
            def vision(prompt,images,analysis,request,schema,suffix):
                seen.append(images)
                self.assertEqual(len(images),1)
                with Image.open(images[0]) as sheet:
                    first=sheet.convert('RGB').getpixel((240,120))
                    third=sheet.convert('RGB').getpixel((1200,120))
                self.assertGreater(first[0],220)
                self.assertLess(first[2],30)
                self.assertGreater(third[2],220)
                self.assertLess(third[0],30)
                return {'segments':[{'id':'clip_1','source_id':'clip','start':0,'end':2,
                         'description':'前两个采样帧为红色，后两个为蓝色。','visual_role':'comparison',
                         'observed_times':[0,.5,1,1.5],'limitations':['颜色变化之间未连续观察。']}],
                        'gaps':[]}
            report=build_media_index(job,{},[source],[],vision)
            self.assertEqual(report['status'],'completed')
            cached=build_media_index(job,{},[source],[],vision)
            self.assertEqual(len(seen),1)
            self.assertTrue(cached['sources'][0]['cache_hit'])
            shot={'kind':'video','source_id':'clip','in':0,'duration':2,
                  'media_segment_id':report['segments'][0]['id'],'selection_reason':'对照两个可见颜色状态。'}
            validate_media_selection(shot,cached,[source],job)


if __name__=='__main__':
    unittest.main()
