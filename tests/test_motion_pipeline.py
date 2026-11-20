"""Measured motion scenes through export, encoding, review and portable delivery."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

from PIL import Image
import make_video
import paper_video as pv
import studio_delivery
import studio_engine
import studio_render
from tests.test_motion import parallel


def plan():
    static = {'kind':'image','source_id':'still','in':0,'duration':1.1,'bbox':[],
              'focus':[],'keep_full':False,'label':'','card_text':'','cue':'','diagram':None}
    motion = dict(static,kind='motion',source_id='',duration=3.5,cue='进入流程',motion=parallel())
    return {'title':'并行过程','summary':'用示意对象说明两条流程的推进。','source_issues':[],
            'unused_sources':[],'blocks':[{'id':'b1','chapter':'','narration':'先看整体。进入流程。各自推进。继续处理。',
                'evidence':[],'shots':[static,motion]}]}


def prepare(directory):
    job=Path(directory)
    for name in ('assets','analysis','output'):(job/name).mkdir()
    source=job/'assets/still.png';Image.new('RGB',(640,360),'#172233').save(source)
    sources=[{'id':'still','type':'image','file':str(source),'name':'布局背景'}]
    request={'format':'custom','width':540,'height':960,'media_fit':'cover',
             'minimal_text':True,'show_sources':False,'show_disclosure':False,
             'voice':'fixture','rate':'+0%'}
    document=plan();studio_engine.validate(document,sources)
    config=studio_engine.export(job,document,sources,request)
    return job,document,sources,request,config


def voice(missing=False):
    events=[{'type':'WordBoundary','text':text,'offset':round(start*10_000_000),'duration':5_000_000}
            for text,start in [('先看整体',0),('进入流程',1.1),('各自推进',2.3),('继续处理',3.7)]
            if not(missing and text=='各自推进')]
    return b'\0'*(int(4.6*make_video.RATE_HZ)*2),events,True


def args(job):
    return SimpleNamespace(project=str(job/'project.json'),voice=None,rate=None,
                           font=None,check=False,subtitles_only=False)


class MotionPipelineTests(unittest.TestCase):
    def test_malformed_stage_containers_reach_value_error_correction_path(self):
        for field in ('stages','poses'):
            document=plan();spec=document['blocks'][0]['shots'][1]['motion']
            if field=='stages':spec['stages']=None
            else:spec['stages'][0]['poses']=None
            with self.subTest(field=field),self.assertRaises(ValueError):
                studio_engine.validate(document,[{'id':'still','type':'image'}])

    def test_actual_portrait_build_uses_local_speech_timing_and_preserves_editable_bundle(self):
        import imageio_ffmpeg
        with tempfile.TemporaryDirectory() as directory:
            job,document,sources,request,config=prepare(directory)
            with patch('make_video.make_voice',new=AsyncMock(return_value=voice())),patch('make_video.log'):
                asyncio.run(make_video.build(args(job)))
            shots=pv.load_json(job/'output/rendered_shots.json');actual=shots[1]
            self.assertAlmostEqual(actual['start'],1.1)
            self.assertAlmostEqual(actual['motion_timeline'][1]['start'],1.2)
            self.assertAlmostEqual(actual['motion_timeline'][2]['start'],2.6)
            self.assertEqual(actual['kind'],'video')
            self.assertEqual(actual['motion'],parallel())
            self.assertEqual(actual['duration'],actual['render_duration'])
            reader=imageio_ffmpeg.read_frames(str(job/'output/work/shot_001.mp4'))
            try:
                metadata=next(reader);encoded=list(reader)
            finally:reader.close()
            self.assertEqual(metadata['size'],(540,960))
            self.assertEqual(len(encoded),round(actual['render_duration']*30))
            # These are composed frames, catching a second letterbox/cover pass.
            first=Image.frombytes('RGB',(540,960),encoded[0])
            middle=Image.frombytes('RGB',(540,960),encoded[54])
            last=Image.frombytes('RGB',(540,960),encoded[-1])
            for picture,x in ((first,108),(middle,270),(last,432)):
                red,green,blue=picture.getpixel((x,317))
                self.assertGreater(blue,red+75);self.assertGreater(green,red+20)
            self.assertLess(max(last.getpixel((108,317))),40)
            make_video.run([make_video.ffmpeg_path(),'-v','error','-i',str(job/'output/video.mp4'),'-f','null','-'])
            frames=studio_delivery.review_frames(job)
            for index,stage in enumerate(actual['motion_timeline']):
                sample=next(item for item in frames if item['label']=='画面阶段：'+(stage['cue'] or '开始'))
                start=actual['start']+stage['start']
                end=(actual['start']+actual['motion_timeline'][index+1]['start']
                     if index+1<len(actual['motion_timeline']) else actual['end'])
                self.assertGreaterEqual(sample['time'],start)
                self.assertLess(sample['time'],end)
                self.assertTrue(Path(sample['path']).is_file())
            studio_delivery.export_formats(job,['bundle'])
            with zipfile.ZipFile(job/'output/project_bundle.zip') as archive:
                portable=json.loads(archive.read('project.json'))
                saved=portable['blocks'][0]['shots'][1]
                self.assertEqual(saved['kind'],'motion')
                self.assertEqual(saved['motion'],parallel())
                self.assertIn(saved['file'],archive.namelist())
                self.assertTrue(saved['file'].startswith('assets/motion_'))
                self.assertFalse(any('start' in stage for stage in saved['motion']['stages']))

    def test_structural_validation_defers_canvas_layout_to_visual_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            job,document,sources,request,config=prepare(directory)
            document['blocks'][0]['shots'][1]['motion']['stages'][0]['poses'][0]['y']=.21
            studio_engine.validate(document,sources)
            studio_engine.validate_visual_policy(document,request)
            with self.assertRaisesRegex(ValueError,'fit'):
                studio_engine.validate_visual_policy(document,dict(request,width=960,height=540))

    def test_plan_rejects_foreign_shot_cues_and_source_claims(self):
        for change in (lambda shot:shot['motion']['stages'][1].update(cue='先看整体'),
                       lambda shot:shot.update(source_id='still'),
                       lambda shot:shot.update(bbox=[0,0,1,1]),
                       lambda shot:shot.update(keep_full=True)):
            document=plan();change(document['blocks'][0]['shots'][1])
            with self.assertRaises(ValueError):
                studio_engine.validate(document,[{'id':'still','type':'image'}])

    def test_measured_outside_window_stops_before_motion_render(self):
        with tempfile.TemporaryDirectory() as directory:
            job,document,sources,request,config=prepare(directory)
            config['blocks'][0]['shots'][1]['motion']['stages'][1]['cue']='先看整体'
            pv.save_json(job/'project.json',config)
            with patch('make_video.make_voice',new=AsyncMock(return_value=voice())), \
                 patch('studio_motion.render_motion') as render,patch('make_video.log'):
                with self.assertRaises(studio_render.NeedsMoreVisualsError) as caught:
                    asyncio.run(make_video.build(args(job)))
                render.assert_not_called()
            self.assertEqual(caught.exception.issues[0]['reason'],'motion_cue_outside_shot')
            self.assertLess(caught.exception.issues[0]['stage_local_time'],0)
            self.assertEqual(pv.load_json(job/'output/timing_recovery.json')['status'],'needs_more_visuals')

    def test_missing_cue_or_tts_boundary_is_reported_without_guessed_timestamp(self):
        for missing_text in (True,False):
            with self.subTest(missing_text=missing_text),tempfile.TemporaryDirectory() as directory:
                job,document,sources,request,config=prepare(directory)
                if missing_text:
                    config['blocks'][0]['shots'][1]['motion']['stages'][1]['cue']='不存在的切点'
                    pv.save_json(job/'project.json',config)
                with patch('make_video.make_voice',new=AsyncMock(return_value=voice(missing=not missing_text))), \
                     patch('studio_motion.render_motion') as render,patch('make_video.log'):
                    with self.assertRaises(studio_render.NeedsMoreVisualsError) as caught:
                        asyncio.run(make_video.build(args(job)))
                    render.assert_not_called()
                issue=next(item for item in caught.exception.issues if item['reason']=='motion_cue_unresolved')
                self.assertEqual(issue['stage_index'],1)
                self.assertEqual(issue['cue'],'不存在的切点' if missing_text else '各自推进')
                self.assertNotIn('stage_local_time',issue)
                self.assertEqual(pv.load_json(job/'output/timing_recovery.json')['status'],'needs_more_visuals')

    def test_legacy_image_plan_keeps_existing_behavior(self):
        document=plan();document['blocks'][0]['shots']=document['blocks'][0]['shots'][:1]
        before=copy.deepcopy(document)
        studio_engine.validate(document,[{'id':'still','type':'image'}])
        self.assertEqual(document,before)
        self.assertNotIn('motion',document['blocks'][0]['shots'][0])


if __name__=='__main__':unittest.main()
