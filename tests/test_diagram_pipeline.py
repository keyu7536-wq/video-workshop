"""Integration checks for speech-timed diagrams, export, framing and review."""
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


def diagram():
    return {'title':'','note':'原理示意',
        'nodes':[{'id':'input','label':'输入','x':.2,'y':.4},
                 {'id':'model','label':'模型','x':.5,'y':.4},
                 {'id':'output','label':'结果','x':.8,'y':.4}],
        'edges':[{'id':'feed','source':'input','target':'model','label':'','via':[]},
                 {'id':'predict','source':'model','target':'output','label':'','via':[]}],
        'stages':[{'cue':'','active_nodes':['input'],'active_edges':[],'visible_nodes':['input']},
                  {'cue':'得到输出','active_nodes':['model','output'],'active_edges':['feed','predict'],
                   'visible_nodes':['input','model','output']}]}


def plan():
    return {'title':'输入与输出','summary':'演示输入怎样经过模型形成输出。','source_issues':[],'unused_sources':[],
        'blocks':[{'id':'b1','chapter':'模型','narration':'先看输入。再看模型。得到输出。','evidence':[],
            'shots':[{'kind':'image','source_id':'still','in':0,'duration':1.3,'bbox':[],'focus':[],
                      'keep_full':False,'label':'','card_text':'','cue':'','diagram':None},
                     {'kind':'diagram','source_id':'','in':0,'duration':2,'bbox':[],'focus':[],
                      'keep_full':False,'label':'','card_text':'','cue':'再看模型','diagram':diagram()}]}]}


def prepare_job(folder):
    job=Path(folder);(job/'assets').mkdir();(job/'analysis').mkdir();(job/'output').mkdir()
    still=job/'assets/still.png';Image.new('RGB',(640,360),'#10212a').save(still)
    source={'id':'still','type':'image','file':str(still),'name':'示意背景'}
    config=studio_engine.export(job,plan(),[source],{
        'format':'custom','width':640,'height':360,'media_fit':'cover',
        'minimal_text':True,'show_sources':False,'show_disclosure':False,'voice':'fixture','rate':'+0%'})
    return job,config


def voice():
    # Recorded word events are deliberately uneven, making proportional timing
    # or using the whole block's timestamp instead of the shot offset detectable.
    events=[{'type':'WordBoundary','text':text,'offset':round(start*10_000_000),'duration':8_000_000}
            for text,start in [('先看输入',0),('再看模型',1.3),('得到输出',2.4)]]
    return b'\0'*(int(3.5*make_video.RATE_HZ)*2),events,True


def args(job):
    return SimpleNamespace(project=str(job/'project.json'),voice=None,rate=None,font=None,
                           check=False,subtitles_only=False)


class DiagramPipelineTests(unittest.TestCase):
    def test_plan_requires_unique_ordered_stage_cues(self):
        sources=[{'id':'still','type':'image'}]
        valid=plan();studio_engine.validate(valid,sources)
        for cues in [('得到输出',''),('','未出现的话'),('','得到输出','再看模型')]:
            invalid=plan();stages=invalid['blocks'][0]['shots'][1]['diagram']['stages']
            template=copy.deepcopy(stages[-1])
            invalid['blocks'][0]['shots'][1]['diagram']['stages']=[dict(template,cue=cue) for cue in cues]
            with self.assertRaisesRegex(ValueError,'阶段|顺序'):studio_engine.validate(invalid,sources)

    def test_export_and_portable_bundle_keep_editable_graph(self):
        with tempfile.TemporaryDirectory() as folder:
            job,config=prepare_job(folder)
            exported=config['blocks'][0]['shots'][1]
            self.assertEqual(exported['kind'],'diagram')
            self.assertEqual(exported['diagram'],diagram())
            # Bundle export must retain cue-based editable sources, even when a
            # generated diagram MP4 has also been materialized in assets.
            Path(exported['file']).write_bytes(b'fixture')
            studio_delivery.export_formats(job,['bundle'])
            with zipfile.ZipFile(job/'output/project_bundle.zip') as archive:
                portable=json.loads(archive.read('project.json'))
                saved=portable['blocks'][0]['shots'][1]
                self.assertEqual(saved['diagram'],diagram())
                self.assertEqual(saved['file'],'assets/diagram_00_01.mp4')
                self.assertIn(saved['file'],archive.namelist())

    def test_actual_build_uses_measured_shot_local_timing_and_full_canvas(self):
        import imageio_ffmpeg
        with tempfile.TemporaryDirectory() as folder:
            job,config=prepare_job(folder)
            with patch('make_video.make_voice',new=AsyncMock(return_value=voice())),patch('make_video.log'):
                asyncio.run(make_video.build(args(job)))
            shots=pv.load_json(job/'output/rendered_shots.json');actual=shots[1]
            self.assertAlmostEqual(actual['start'],1.3)
            self.assertAlmostEqual(actual['diagram_timeline'][1]['start'],1.1)
            self.assertEqual(actual['kind'],'video')
            self.assertAlmostEqual(actual['duration'],actual['render_duration'])
            self.assertEqual(actual['diagram'],diagram())
            report=pv.load_json(job/'output/run_report.json')
            self.assertEqual(report['video_holds_replaced'],0)
            self.assertAlmostEqual(report['duration_seconds'],3.7666666666666666)
            # Decode the composed shot, not just its source graph. A second
            # editorial letterbox or cover crop would move this node centre.
            reader=imageio_ffmpeg.read_frames(str(job/'output/work/shot_001.mp4'))
            try:
                metadata=next(reader);first=Image.frombytes('RGB',metadata['size'],next(reader))
                encoded_count=1+sum(1 for _ in reader)
            finally:reader.close()
            self.assertEqual(metadata['size'],(640,360))
            self.assertEqual(encoded_count,round(actual['render_duration']*30))
            red,green,blue=first.getpixel((128,145))
            self.assertGreater(green,red+12)
            self.assertGreater(blue,red+12)
            make_video.run([make_video.ffmpeg_path(),'-v','error','-i',str(job/'output/video.mp4'),'-f','null','-'])

    def test_stage_cues_outside_actual_shot_window_stop_before_diagram_render(self):
        for cue,extra_shot in [('先看输入',False),('得到输出',True)]:
            with self.subTest(cue=cue,extra_shot=extra_shot),tempfile.TemporaryDirectory() as folder:
                job,config=prepare_job(folder);block=config['blocks'][0]
                block['shots'][1]['diagram']['stages'][1]['cue']=cue
                if extra_shot:
                    block['shots'][1]['duration']=1
                    tail=copy.deepcopy(block['shots'][0]);tail.update(cue='得到输出',duration=1.3)
                    block['shots'].append(tail)
                pv.save_json(job/'project.json',config)
                with patch('make_video.make_voice',new=AsyncMock(return_value=voice())), \
                     patch('studio_diagrams.render_diagram') as render,patch('make_video.log'):
                    with self.assertRaisesRegex(ValueError,'本镜头内'):asyncio.run(make_video.build(args(job)))
                    render.assert_not_called()

    def test_review_stage_frame_stays_inside_short_measured_interval(self):
        with tempfile.TemporaryDirectory() as folder:
            job=Path(folder);(job/'analysis').mkdir();(job/'output').mkdir()
            pv.save_json(job/'output/rendered_shots.json',[
                {'start':10,'end':12,'kind':'video','label':'model',
                 'diagram_timeline':[{'start':0,'cue':''},{'start':.1,'cue':'更新'}]}])
            with patch('make_video.run',side_effect=lambda command:Path(command[-1]).write_bytes(b'frame fixture')),patch('make_video.ffmpeg_path',return_value='ffmpeg'):
                frames=studio_delivery.review_frames(job)
            first=next(frame for frame in frames if frame['label']=='动态图阶段：开始')
            self.assertGreaterEqual(first['time'],10)
            self.assertLess(first['time'],10.1)
            second=next(frame for frame in frames if frame['label']=='动态图阶段：更新')
            self.assertGreaterEqual(second['time'],10.1)
            self.assertLess(second['time'],12)

    def test_review_budget_preserves_each_shot_before_extra_diagram_frames(self):
        with tempfile.TemporaryDirectory() as folder:
            job=Path(folder);(job/'analysis').mkdir();(job/'output').mkdir()
            shots=[{'start':i*2,'end':i*2+2,'kind':'video','label':f'shot {i}',
                    'diagram_timeline':[{'start':0,'cue':''},{'start':.8,'cue':'更新'}]} for i in range(24)]
            pv.save_json(job/'output/rendered_shots.json',shots)
            with patch('make_video.run',side_effect=lambda command:Path(command[-1]).write_bytes(b'frame fixture')),patch('make_video.ffmpeg_path',return_value='ffmpeg'):
                frames=studio_delivery.review_frames(job)
            self.assertEqual(len(frames),48)
            labels={frame['label'] for frame in frames}
            self.assertTrue({f'shot {i}' for i in range(24)}<=labels)


if __name__=='__main__':unittest.main()
