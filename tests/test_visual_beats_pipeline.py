"""Source coordinates, measured speech and delivery of editable detail views."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

from PIL import Image, ImageDraw

import make_video
import paper_video as pv
import studio_delivery
import studio_engine
import studio_render


def plan():
    still = {'kind': 'image', 'source_id': 'still', 'in': 0, 'duration': 1.3,
             'bbox': [], 'focus': [], 'keep_full': False, 'label': '',
             'card_text': '', 'cue': '', 'diagram': None}
    detail = dict(still, duration=2, cue='再看左边', bbox=[.1, .1, .9, .9],
                  visual_beats=[{'cue': '', 'bbox': [.1, .1, .45, .9], 'label': '左侧'},
                                {'cue': '然后看右边', 'bbox': [.55, .1, .9, .9], 'label': '右侧'}])
    return {'title': '观察与比较', 'summary': '以同一张图比较两侧。', 'source_issues': [],
            'unused_sources': [], 'blocks': [{'id': 'b1', 'chapter': '比较',
                'narration': '先看整体。再看左边。然后看右边。', 'evidence': [],
                'shots': [still, detail]}]}


def prepare_job(directory):
    job = Path(directory)
    for name in ('assets', 'analysis', 'output'):
        (job/name).mkdir()
    image = Image.new('RGB', (800, 400), '#c52c39')
    ImageDraw.Draw(image).rectangle((400, 0, 800, 400), fill='#315ac4')
    source = job/'assets/still.png'; image.save(source)
    sources = [{'id': 'still', 'type': 'image', 'file': str(source), 'name': '两侧示意'}]
    request = {'format': 'custom', 'width': 640, 'height': 360, 'media_fit': 'cover',
               'minimal_text': True, 'show_sources': False, 'show_disclosure': False,
               'voice': 'fixture', 'rate': '+0%'}
    return job, sources, request


def voice():
    # Deliberately uneven timestamps detect proportional estimates and missing
    # subtraction of the shot's 1.3-second local offset.
    events = [{'type': 'WordBoundary', 'text': text, 'offset': round(start*10_000_000),
               'duration': 8_000_000} for text, start in
              [('先看整体', 0), ('再看左边', 1.3), ('然后看右边', 2.4)]]
    return b'\0'*(int(3.5*make_video.RATE_HZ)*2), events, True


def args(job):
    return SimpleNamespace(project=str(job/'project.json'), voice=None, rate=None,
                           font=None, check=False, subtitles_only=False)


class VisualBeatPipelineTests(unittest.TestCase):
    def test_plan_validates_beat_region_and_own_shot_cue_scope(self):
        sources = [{'id': 'still', 'type': 'image'}]
        studio_engine.validate(plan(), sources)
        for mutate in (
            lambda p: p['blocks'][0]['shots'][1]['visual_beats'][0].update(bbox=[0, 0, .4, .8]),
            lambda p: p['blocks'][0]['shots'][1]['visual_beats'][1].update(cue='先看整体'),
            lambda p: p['blocks'][0]['shots'][1].update(focus=[.2, .2, .4, .4])):
            invalid = plan(); mutate(invalid)
            with self.assertRaises(ValueError):
                studio_engine.validate(invalid, sources)

    def test_export_maps_original_region_to_prepared_image(self):
        with tempfile.TemporaryDirectory() as directory:
            job, sources, request = prepare_job(directory)
            original = plan(); before = copy.deepcopy(original)
            exported = studio_engine.export(job, original, sources, request)
            shot = exported['blocks'][0]['shots'][1]
            self.assertEqual(original, before)
            with Image.open(shot['file']) as image:
                self.assertEqual(image.size, (640, 320))
            self.assertEqual(shot['kind'], 'image')
            self.assertTrue(shot['protect_content'])
            for value, expected in zip(shot['visual_beats'][0]['bbox'], [0, 0, .4375, 1]):
                self.assertAlmostEqual(value, expected)
            for value, expected in zip(shot['visual_beats'][1]['bbox'], [.5625, 0, 1, 1]):
                self.assertAlmostEqual(value, expected)

    def test_pdf_mapping_uses_actual_expanded_clip(self):
        import pymupdf
        with tempfile.TemporaryDirectory() as directory:
            job, sources, request = prepare_job(directory)
            pdf = job/'assets/paper.pdf'
            document = pymupdf.open(); page = document.new_page(width=1000, height=800)
            page.draw_rect(pymupdf.Rect(250, 200, 500, 400), color=(1, 0, 0), fill=(1, 0, 0))
            document.save(pdf); document.close()
            sources.append({'id': 'page', 'type': 'page', 'file': str(pdf), 'name': '原图', 'page': 1})
            original = plan(); detail = original['blocks'][0]['shots'][1]
            detail.update(kind='page', source_id='page', bbox=[.2, .2, .8, .8],
                          visual_beats=[{'cue': '', 'bbox': [.25, .25, .5, .5], 'label': '图中区域'}])
            with patch('studio_crop.panel_clip', return_value=pymupdf.Rect(100, 100, 900, 700)):
                exported = studio_engine.export(job, original, sources, request)
            mapped = exported['blocks'][0]['shots'][1]['visual_beats'][0]['bbox']
            for value, expected in zip(mapped, [.1875, 1/6, .5, .5]):
                self.assertAlmostEqual(value, expected)

    def test_real_build_measured_timing_full_canvas_and_portable_sources(self):
        import imageio_ffmpeg
        with tempfile.TemporaryDirectory() as directory:
            job, sources, request = prepare_job(directory)
            config = studio_engine.export(job, plan(), sources, request)
            with patch('make_video.make_voice', new=AsyncMock(return_value=voice())), patch('make_video.log'):
                asyncio.run(make_video.build(args(job)))
            shots = pv.load_json(job/'output/rendered_shots.json'); actual = shots[1]
            self.assertAlmostEqual(actual['start'], 1.3)
            self.assertAlmostEqual(actual['visual_timeline'][1]['start'], 1.1)
            self.assertEqual(actual['kind'], 'video')
            self.assertEqual(actual['duration'], actual['render_duration'])
            self.assertNotIn('start', config['blocks'][0]['shots'][1]['visual_beats'][1])
            reader = imageio_ffmpeg.read_frames(str(job/'output/work/shot_001.mp4'))
            try:
                metadata = next(reader); encoded = list(reader)
            finally:
                reader.close()
            self.assertEqual(metadata['size'], (640, 360))
            self.assertEqual(len(encoded), round(actual['render_duration']*30))
            first = Image.frombytes('RGB', (640, 360), encoded[0])
            last = Image.frombytes('RGB', (640, 360), encoded[-1])
            red, green, blue = first.getpixel((442, 180)); self.assertGreater(red, blue+90)
            red, green, blue = last.getpixel((442, 180)); self.assertGreater(blue, red+90)
            make_video.run([make_video.ffmpeg_path(), '-v', 'error', '-i', str(job/'output/video.mp4'), '-f', 'null', '-'])
            # Delivery retains the source PNG and cue-driven project, not only
            # a flattened temporary MP4 from the renderer.
            studio_delivery.export_formats(job, ['bundle'])
            with zipfile.ZipFile(job/'output/project_bundle.zip') as archive:
                portable = json.loads(archive.read('project.json'))
                saved = portable['blocks'][0]['shots'][1]
                self.assertEqual(saved['kind'], 'image')
                self.assertTrue(saved['file'].endswith('.png'))
                self.assertIn(saved['file'], archive.namelist())
                self.assertEqual(saved['visual_beats'], config['blocks'][0]['shots'][1]['visual_beats'])

    def test_outside_measured_window_stops_before_detail_encoding(self):
        for cue, extra_shot in [('先看整体', False), ('然后看右边', True)]:
            with self.subTest(cue=cue), tempfile.TemporaryDirectory() as directory:
                job, sources, request = prepare_job(directory)
                config = studio_engine.export(job, plan(), sources, request)
                block = config['blocks'][0]
                block['shots'][1]['visual_beats'][1]['cue'] = cue
                if extra_shot:
                    tail = copy.deepcopy(block['shots'][0]); tail.update(cue='然后看右边', duration=1.3)
                    block['shots'].append(tail)
                pv.save_json(job/'project.json', config)
                with patch('make_video.make_voice', new=AsyncMock(return_value=voice())), \
                     patch('studio_visual_beats.render_visual_beats') as render, patch('make_video.log'):
                    with self.assertRaises(studio_render.NeedsMoreVisualsError) as caught:
                        asyncio.run(make_video.build(args(job)))
                    render.assert_not_called()
                issues = caught.exception.issues
                self.assertEqual(issues[0]['reason'], 'visual_cue_outside_shot')
                self.assertLess(issues[0]['stage_local_time'], 0) if not extra_shot else self.assertGreaterEqual(
                    issues[0]['stage_local_time'], issues[0]['available_cue_window'])

    def test_old_plan_keeps_static_source_without_beat_defaults_in_saved_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            job, sources, request = prepare_job(directory)
            original = plan()
            original['blocks'][0]['shots'][1].pop('visual_beats')
            before = copy.deepcopy(original)
            studio_engine.validate(original, sources)
            exported = studio_engine.export(job, original, sources, request)
            self.assertEqual(original, before)
            self.assertTrue(all('visual_beats' not in shot for shot in exported['blocks'][0]['shots']))
            self.assertTrue(all(shot['kind'] == 'image' for shot in exported['blocks'][0]['shots']))


if __name__ == '__main__':
    unittest.main()
