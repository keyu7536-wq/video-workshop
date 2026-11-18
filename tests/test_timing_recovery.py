"""Measured narration outlasting a source clip must not freeze or rewrite it."""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import wave

from PIL import Image
import imageio_ffmpeg

import make_video as mv
import studio_render as sr


def measured_voice():
    events = [{'type':'WordBoundary','text':word,'offset':int(start*10_000_000),
               'duration':5_000_000} for word,start in [('Input',0),('Output',2.2)]]
    return b'\0' * (int(3.5*mv.RATE_HZ)*2), events, True


def config(shots):
    return {'renderer':'studio','title':'Timing test','voice':'test','rate':'+0%',
            'width':640,'height':360,'fps':30,'minimal_text':True,
            'show_sources':False,'show_disclosure':False,
            'blocks':[{'id':'b1','narration':'Input. Output.','shots':shots}]}


def arguments(root):
    return SimpleNamespace(project=str(root/'project.json'),voice=None,rate=None,
                           font=None,check=False,subtitles_only=False)


class TimingRecoveryTests(unittest.TestCase):
    def test_measured_cue_candidates_are_unique_bounded_original_phrases(self):
        text='准备观察。打开门。打开门。最后确认结果。'
        events=[{'type':'WordBoundary','text':char,'offset':int((.13+index*.31)*10_000_000),
                 'duration':1_000_000} for index,char in enumerate(text) if char.isalnum()]
        words=mv.words_with_positions(text,events)
        cues=mv.measured_cue_candidates(text,words)
        self.assertTrue(cues)
        for item in cues:
            self.assertIn(item['cue'],text)
            self.assertLessEqual(len(item['cue']),24)
            target=mv.norm(item['cue']);first=mv.norm(text).find(target)
            self.assertEqual(mv.norm(text).find(target,first+1),-1)
            self.assertEqual(mv.cue_time(item['cue'],text,words),item['start'])
            self.assertIn(item['start'],[word['start'] for word in words])
        text=' '.join(f'item{index:03d}' for index in range(150))
        events=[{'type':'WordBoundary','text':token,'offset':index*3_700_000,
                 'duration':1_000_000} for index,token in enumerate(text.split())]
        words=mv.words_with_positions(text,events)
        cues=mv.measured_cue_candidates(text,words)
        self.assertLessEqual(len(cues),48)
        self.assertEqual(cues[0]['start'],words[0]['start'])
        self.assertEqual(cues[-1]['start'],words[-1]['start'])

    def still(self, root, **values):
        path=root/'figure.png';Image.new('RGB',(320,180),'#1a8866').save(path)
        return {'kind':'image','file':str(path),'duration':3,'block_id':'b',
                'source':'actual source figure','protect_content':True,
                'focus':[.1,.1,.9,.9],**values}

    def test_short_video_then_selected_figure_preserves_original_cue(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            shots=[{'kind':'video','file':'source.mp4','duration':5,'render_duration':5.8,
                    'start':0,'end':5.8,'local_start':0,'local_end':5.8,
                    'block_id':'b','shot_index':0,'in':7,'keep_full':True},
                   self.still(root,start=5.8,end=12,local_start=5.8,local_end=12,
                              render_duration=6.2,shot_index=1,cue='paper identity')]
            original=json.loads(json.dumps(shots))
            result,count=sr.replace_video_holds(shots,root,root,30,True)
            self.assertEqual(count,1)
            self.assertEqual([(s['start'],s['end']) for s in result],[(0,5),(5,5.8),(5.8,12)])
            self.assertEqual(result[0]['in'],7)
            self.assertEqual(result[0]['duration'],5)
            self.assertEqual(result[2]['cue'],'paper identity')
            self.assertEqual(result[1]['source'],'actual source figure')
            self.assertEqual(result[1]['focus'],shots[1]['focus'])
            self.assertEqual(shots,original)

    def test_selected_figure_then_short_video_reuses_earlier_figure(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            shots=[self.still(root,start=0,end=2,render_duration=2,shot_index=0),
                   {'kind':'video','file':'source.mp4','duration':3,'render_duration':6,
                    'start':2,'end':8,'local_start':2,'local_end':8,
                    'block_id':'b','shot_index':1,'cue':'demonstration','keep_full':True}]
            result,count=sr.replace_video_holds(shots,root,root,30,False)
            self.assertEqual(count,1)
            self.assertEqual([(s['start'],s['end']) for s in result],[(0,2),(2,5),(5,8)])
            self.assertEqual(result[1]['cue'],'demonstration')
            self.assertEqual(result[-1]['file'],shots[0]['file'])

    def test_all_video_shortage_is_structured_and_other_blocks_are_not_borrowed(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            shots=[{'kind':'video','file':'source.mp4','duration':2,'render_duration':5,
                    'start':0,'end':5,'block_id':'b','shot_index':0},
                   self.still(root,block_id='unrelated',start=5,end=8,render_duration=3)]
            for minimal in (True,False):
                with self.assertRaises(sr.NeedsMoreVisualsError) as caught:
                    sr.replace_video_holds(shots,root,root,30,minimal)
                issue=caught.exception.issues[0]
                self.assertEqual(issue['block_id'],'b')
                self.assertEqual(issue['shot_index'],0)
                self.assertEqual(issue['missing_seconds'],3)
                self.assertEqual(caught.exception.diagnostic['status'],'needs_more_visuals')
            self.assertFalse(list(root.glob('continuation*')))

    def test_build_writes_hash_bound_diagnostic_before_stopping(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'source.mp4').write_bytes(b'not reached by encoder')
            project=config([{'kind':'video','file':'source.mp4','duration':1}])
            (root/'project.json').write_text(json.dumps(project),encoding='utf-8')
            with patch('make_video.make_voice',new=AsyncMock(return_value=measured_voice())),patch('make_video.log'):
                with self.assertRaises(sr.NeedsMoreVisualsError):
                    asyncio.run(mv.build(arguments(root)))
            report=json.loads((root/'output/timing_recovery.json').read_text(encoding='utf-8'))
            self.assertEqual(report['status'],'needs_more_visuals')
            self.assertEqual(report['code'],'insufficient_visual_duration')
            self.assertEqual(report['project_sha256'],hashlib.sha256((root/'project.json').read_bytes()).hexdigest())
            self.assertEqual(report['issues'][0]['block_id'],'b1')
            self.assertTrue((root/'output/narration.wav').is_file())
            self.assertFalse((root/'output/video.mp4').exists())

    def test_complete_clip_cannot_move_an_explicit_speech_cue(self):
        block={'id':'training','narration':'Input. Output.','shots':[
            {'kind':'image','file':'figure.png','duration':8},
            {'kind':'video','file':'source.mp4','duration':12,'keep_full':True,'cue':'Output'}]}
        events=measured_voice()[1]
        events[1]['offset']=100_000_000
        words=mv.words_with_positions(block['narration'],events)
        with self.assertRaises(sr.NeedsMoreVisualsError) as caught:
            sr.choose_shots(block,18+2/30,words,30)
        issue=caught.exception.issues[0]
        self.assertEqual(issue['reason'],'complete_clip_exceeds_cue_window')
        self.assertEqual(issue['expected_cue_time'],10)
        self.assertEqual(issue['required_source_duration'],12)
        self.assertAlmostEqual(issue['available_cue_window'],8+2/30)
        self.assertEqual(issue['shot_index'],1)

    def test_build_writes_cue_conflict_diagnostic_not_just_hold_shortages(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'source.mp4').write_bytes(b'not reached by encoder')
            still=self.still(root)
            project=config([still,{'kind':'video','file':'source.mp4','duration':3,
                                   'keep_full':True,'cue':'Output'}])
            (root/'project.json').write_text(json.dumps(project),encoding='utf-8')
            (root/'output').mkdir()
            (root/'output/timing_recovery.json').write_text('{"status":"stale"}',encoding='utf-8')
            with patch('make_video.make_voice',new=AsyncMock(return_value=measured_voice())),patch('make_video.log'):
                with self.assertRaises(sr.NeedsMoreVisualsError):
                    asyncio.run(mv.build(arguments(root)))
            report=json.loads((root/'output/timing_recovery.json').read_text(encoding='utf-8'))
            self.assertEqual(report['status'],'needs_more_visuals')
            self.assertEqual(report['issues'][0]['reason'],'complete_clip_exceeds_cue_window')
            self.assertEqual(report['issues'][0]['expected_cue_time'],2.2)
            self.assertEqual(report['project_sha256'],hashlib.sha256((root/'project.json').read_bytes()).hexdigest())
            block=report['blocks'][0]
            self.assertEqual(block['cue_time_basis'],'block_local')
            self.assertEqual(next(item['start'] for item in block['cue_candidates'] if item['cue']=='Output'),2.2)

    def test_actual_report_candidates_allow_earlier_cue_without_changing_full_clip_or_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'source.mp4').write_bytes(b'encoder must not run')
            still=self.still(root)
            text='先介绍研究。随后展示运动。接着看比较。'
            events=[{'type':'WordBoundary','text':token,'offset':int(start*10_000_000),
                     'duration':2_000_000} for token,start in
                    [('先介绍研究',0),('随后',1),('展示运动',2.2),('接着看比较',5)]]
            pcm=b'\x09\x01'*int(8*mv.RATE_HZ)
            video={'kind':'video','file':'source.mp4','duration':4,'in':7,
                   'keep_full':True,'cue':'展示运动'}
            block={'id':'motion','narration':text,'shots':[still,video,{**still,'cue':'接着看比较'}]}
            project={**config([]),'blocks':[block]}
            (root/'project.json').write_text(json.dumps(project),encoding='utf-8')
            with patch('make_video.make_voice',new=AsyncMock(return_value=(pcm,events,True))),patch('make_video.log'):
                with self.assertRaises(sr.NeedsMoreVisualsError):asyncio.run(mv.build(arguments(root)))
            diagnostic=json.loads((root/'output/timing_recovery.json').read_text('utf-8'))
            report=diagnostic['blocks'][0]
            candidate=next(c for c in report['cue_candidates'] if c['start']==1)
            repaired={**block,'shots':[still,{**video,'cue':candidate['cue']},block['shots'][2]]}
            shots=sr.choose_shots(repaired,report['end']-report['start'],mv.words_with_positions(text,events),30)
            self.assertEqual(shots[1]['local_start'],1)
            self.assertEqual(shots[2]['local_start'],5)
            self.assertEqual((shots[1]['in'],shots[1]['duration'],shots[1]['keep_full']),(7,4,True))
            self.assertEqual(repaired['narration'],text)
            with wave.open(str(root/'output/narration.wav'),'rb') as audio:
                self.assertEqual(audio.readframes(len(pcm)//2),pcm)

    def test_preflight_collects_all_blocks_and_diagram_boundaries_before_rendering(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'source.mp4').write_bytes(b'encoder must not run')
            still=self.still(root)
            graph={'nodes':[{'id':'a','label':'Input','x':.3,'y':.4}], 'edges':[],
                   'stages':[{'cue':'','active_nodes':['a']},{'cue':'Output','active_nodes':['a']}]}
            diagram={'kind':'diagram','file':'diagram.mp4','duration':1,'diagram':graph}
            video={'kind':'video','file':'source.mp4','duration':3,'keep_full':True}
            blocks=[
                {'id':'valid_diagram','narration':'Input. Output.','shots':[diagram]},
                {'id':'opening','narration':'Input. Output.',
                 'shots':[video,{**still,'cue':'Output'}]},
                {'id':'training','narration':'Input. Output.',
                 'shots':[still,{**video,'cue':'Output'}]},
                {'id':'uncovered','narration':'Input. Output.',
                 'shots':[{**video,'duration':1,'keep_full':False}]},
                {'id':'bad_diagram','narration':'Input. Output.',
                 'shots':[still,{**diagram,'cue':'Output','diagram':{**graph,
                    'stages':[{'cue':'','active_nodes':['a']},{'cue':'Input','active_nodes':['a']}]}}]},
            ]
            project={**config([]),'blocks':blocks}
            (root/'project.json').write_text(json.dumps(project),encoding='utf-8')
            with patch('make_video.make_voice',new=AsyncMock(return_value=measured_voice())) as voice, \
                 patch('studio_diagrams.render_diagram') as render_diagram, \
                 patch('studio_render.render_shot') as render_shot,patch('make_video.log'):
                with self.assertRaises(sr.NeedsMoreVisualsError):
                    asyncio.run(mv.build(arguments(root)))
                self.assertEqual(voice.await_count,5)
                render_diagram.assert_not_called();render_shot.assert_not_called()
            report=json.loads((root/'output/timing_recovery.json').read_text(encoding='utf-8'))
            self.assertEqual(len(report['blocks']),5)
            self.assertEqual(len(report['issues']),4)
            issues={issue['block_id']:issue for issue in report['issues']}
            for name in ('opening','training'):
                self.assertEqual(issues[name]['reason'],'complete_clip_exceeds_cue_window')
            self.assertEqual(issues['uncovered']['reason'],'no_selected_same_block_static_visual')
            self.assertEqual(issues['bad_diagram']['reason'],'diagram_cue_outside_shot')
            self.assertEqual(issues['bad_diagram']['expected_cue_time'],0)
            self.assertLess(issues['bad_diagram']['stage_local_time'],0)
            with wave.open(str(root/'output/narration.wav'),'rb') as audio:
                self.assertEqual(audio.getnframes(),5*round((113/30)*mv.RATE_HZ))
            self.assertFalse((root/'output/video.mp4').exists())

    def test_encoded_short_clip_uses_still_for_gap_and_keeps_full_audio_timeline(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            mv.run([mv.ffmpeg_path(),'-v','error','-y','-f','lavfi','-i',
                    'testsrc2=size=320x180:rate=30','-t','1','-c:v','libx264',
                    '-pix_fmt','yuv420p',str(root/'source.mp4')])
            still=self.still(root,cue='Output')
            project=config([{'kind':'video','file':'source.mp4','duration':1,'keep_full':True},still])
            (root/'project.json').write_text(json.dumps(project),encoding='utf-8')
            with patch('make_video.make_voice',new=AsyncMock(return_value=measured_voice())),patch('make_video.log'):
                asyncio.run(mv.build(arguments(root)))
            report=json.loads((root/'output/timing_recovery.json').read_text(encoding='utf-8'))
            shots=json.loads((root/'output/rendered_shots.json').read_text(encoding='utf-8'))
            self.assertEqual(report['status'],'resolved')
            self.assertEqual(len(report['adjustments']),1)
            self.assertEqual(shots[0]['render_duration'],1)
            self.assertAlmostEqual(shots[2]['start'],2.2)
            reader=imageio_ffmpeg.read_frames(str(root/'output/video.mp4'))
            try:
                next(reader); frame_count=sum(1 for _ in reader)
            finally:reader.close()
            self.assertEqual(frame_count,113)

    def test_diagram_is_generated_for_actual_audio_span_without_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            graph={'nodes':[{'id':'a','label':'Input','x':.25,'y':.4},
                            {'id':'b','label':'Output','x':.75,'y':.4}],
                   'edges':[{'id':'e','source':'a','target':'b'}],
                   'stages':[{'cue':'','active_nodes':['a']},
                             {'cue':'Output','active_nodes':['b'],'active_edges':['e']}]}
            project=config([{'kind':'diagram','file':'diagram.mp4','duration':1,'diagram':graph}])
            (root/'project.json').write_text(json.dumps(project),encoding='utf-8')
            with patch('make_video.make_voice',new=AsyncMock(return_value=measured_voice())),patch('make_video.log'):
                asyncio.run(mv.build(arguments(root)))
            shots=json.loads((root/'output/rendered_shots.json').read_text(encoding='utf-8'))
            report=json.loads((root/'output/timing_recovery.json').read_text(encoding='utf-8'))
            self.assertAlmostEqual(shots[0]['duration'],113/30)
            self.assertAlmostEqual(shots[0]['diagram_timeline'][1]['start'],2.2)
            self.assertEqual(report['adjustments'],[])
            self.assertEqual(report['status'],'resolved')
            mv.run([mv.ffmpeg_path(),'-v','error','-i',str(root/'output/video.mp4'),'-f','null','-'])


if __name__=='__main__':
    unittest.main()
