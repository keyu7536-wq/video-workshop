"""Measured video shortages require an explicit new plan in workbench projects."""
import asyncio
import copy
import hashlib
import json
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import wave

from PIL import Image

import make_video as mv
import paper_video as pv
import studio_engine as engine
import studio_render as sr


class VideoContinuityTests(unittest.TestCase):
    def fixture(self, root, image_first=True):
        image = root/'paper-title.png'; Image.new('RGB', (320, 180), 'white').save(image)
        still = {'kind': 'image', 'file': str(image), 'duration': 2.2, 'render_duration': 2.2,
                 'start': 0, 'end': 2.2, 'local_start': 0, 'local_end': 2.2,
                 'block_id': 'identity', 'shot_index': 0, 'source': '论文标题页'}
        video = {'kind': 'video', 'file': 'author.mp4', 'duration': 1,
                 'render_duration': 7/3, 'start': 2.2, 'end': 2.2+7/3,
                 'local_start': 2.2, 'local_end': 2.2+7/3, 'in': 0,
                 'block_id': 'identity', 'shot_index': 1, 'cue': '看这台四足机器人',
                 'keep_full': True, 'summary': '看这台四足机器人，它正在学习向前走。'}
        return [still, video] if image_first else [video, still]

    def test_strict_does_not_cover_object_reference_with_earlier_paper_title(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); shots = self.fixture(root); before = copy.deepcopy(shots)
            with self.assertRaises(sr.NeedsMoreVisualsError) as caught:
                sr.replace_video_holds(shots, root, root, 30, True, strict_video_continuity=True)
            self.assertEqual(shots, before)
            issue = caught.exception.issues[0]
            self.assertEqual(issue['block_id'], 'identity'); self.assertEqual(issue['shot_index'], 1)
            self.assertEqual(issue['cue'], '看这台四足机器人')
            self.assertEqual(issue['reason'], 'selected_video_shorter_than_narration_window')
            self.assertAlmostEqual(issue['missing_seconds'], 4/3, places=6)
            self.assertEqual(issue['available_seconds'], 1)
            self.assertAlmostEqual(issue['required_seconds'], 7/3)
            self.assertEqual(caught.exception.adjustments, [])

    def test_later_same_block_picture_does_not_authorize_implicit_continuation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); shots = self.fixture(root, image_first=False)
            with self.assertRaises(sr.NeedsMoreVisualsError):
                sr.replace_video_holds(shots, root, root, 30, strict_video_continuity=True)

    def test_legacy_default_and_explicit_false_keep_previous_still_continuation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); shots = self.fixture(root)
            implicit, count = sr.replace_video_holds(shots, root, root, 30)
            explicit, explicit_count = sr.replace_video_holds(shots, root, root, 30,
                                                             strict_video_continuity=False)
            self.assertEqual((count, explicit_count), (1, 1)); self.assertEqual(implicit, explicit)
            self.assertEqual(implicit[-1]['file'], shots[0]['file'])
            self.assertEqual(implicit[-1]['timing_adjustment']['type'], 'same_block_static_continuation')

    def test_explicit_planned_static_and_fully_covered_video_are_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); shots = self.fixture(root)
            shots[1].update(duration=3, keep_full=False)
            before = copy.deepcopy(shots)
            result, count = sr.replace_video_holds(shots, root, root, 30, strict_video_continuity=True)
            self.assertEqual(count, 0); self.assertEqual(result, before); self.assertEqual(shots, before)

    def test_all_shortage_locations_are_reported_without_partial_donor_adjustments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); shots = self.fixture(root)
            shots.append(dict(shots[1], block_id='method', shot_index=0, start=5, end=5+7/3))
            with self.assertRaises(sr.NeedsMoreVisualsError) as caught:
                sr.replace_video_holds(shots, root, root, 30, strict_video_continuity=True)
            self.assertEqual([issue['block_id'] for issue in caught.exception.issues], ['identity', 'method'])
            self.assertEqual(caught.exception.adjustments, [])

    def test_generated_diagram_shortage_keeps_specific_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); shots = self.fixture(root)
            shots[1]['diagram'] = {'stages': []}
            with self.assertRaises(sr.NeedsMoreVisualsError) as caught:
                sr.replace_video_holds(shots, root, root, 30, strict_video_continuity=True)
            self.assertEqual(caught.exception.issues[0]['reason'], 'generated_diagram_duration_mismatch')

    def test_build_stops_before_encoding_and_writes_bound_diagnostic_even_with_a_paper_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root/'paper-title.png'; Image.new('RGB', (320, 180), 'white').save(image)
            (root/'author.mp4').write_bytes(b'encoding must not be reached')
            text = '论文介绍。看这台四足机器人。'
            project = {'renderer': 'studio', 'title': '物体指认', 'voice': 'fixture', 'rate': '+0%',
                       'width': 640, 'height': 360, 'fps': 30, 'minimal_text': True,
                       'strict_video_continuity': True, 'show_sources': False, 'show_disclosure': False,
                       'blocks': [{'id': 'identity', 'narration': text, 'shots': [
                           {'kind': 'image', 'file': 'paper-title.png', 'duration': 2},
                           {'kind': 'video', 'file': 'author.mp4', 'in': 0, 'duration': 1,
                            'keep_full': True, 'cue': '看这台四足机器人'}]}]}
            (root/'project.json').write_text(json.dumps(project, ensure_ascii=False), encoding='utf-8')
            words = [{'type': 'WordBoundary', 'text': phrase, 'offset': offset, 'duration': 8_000_000}
                     for phrase, offset in [('论文介绍', 0), ('看这台四足机器人', 22_000_000)]]
            voice = (b'\0' * (int(3.5*mv.RATE_HZ)*2), words, True)
            args = SimpleNamespace(project=str(root/'project.json'), voice=None, rate=None,
                                   font=None, check=False, subtitles_only=False)
            with patch('make_video.make_voice', new=AsyncMock(return_value=voice)), patch('make_video.log'), \
                 patch('studio_render.render_shot', side_effect=AssertionError('must stop before encoding')) as renderer:
                with self.assertRaises(sr.NeedsMoreVisualsError):
                    asyncio.run(mv.build(args))
            renderer.assert_not_called()
            diagnostic = json.loads((root/'output/timing_recovery.json').read_text(encoding='utf-8'))
            self.assertEqual(diagnostic['status'], 'needs_more_visuals')
            self.assertEqual(diagnostic['project_sha256'], hashlib.sha256((root/'project.json').read_bytes()).hexdigest())
            self.assertEqual(diagnostic['issues'][0]['reason'], 'selected_video_shorter_than_narration_window')
            self.assertAlmostEqual(diagnostic['issues'][0]['missing_seconds'], .3)
            self.assertEqual(diagnostic['issues'][0]['cue'], '看这台四足机器人')
            self.assertEqual(diagnostic['adjustments'], [])
            self.assertTrue((root/'output/narration.wav').is_file())
            self.assertFalse((root/'output/video.mp4').exists())


class VideoTailPaddingTests(unittest.TestCase):
    def exported_job(self, root, video_seconds=10, real=False):
        for directory in ('assets', 'analysis', 'output'):
            (root/directory).mkdir()
        source = root/'assets/demo.mp4'
        if real:
            mv.run([mv.ffmpeg_path(), '-v', 'error', '-y', '-f', 'lavfi', '-i',
                    'color=c=green:size=320x180:rate=30', '-t', str(video_seconds),
                    '-an', '-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p', str(source)], timeout=30)
        else:
            source.write_bytes(b'encoder mocked for measured timing tests')
        plan = {'title': '真实取段', 'summary': '', 'source_issues': [], 'unused_sources': [],
                'blocks': [{'id': 'motion', 'chapter': '观察动作', 'narration': '观察这个真实动作。',
                            'evidence': [], 'shots': [{'kind': 'video', 'source_id': 'demo',
                            'in': 0, 'duration': video_seconds, 'bbox': [], 'focus': [],
                            'keep_full': True, 'label': '', 'card_text': '', 'cue': ''}]}]}
        sources = [{'id': 'demo', 'type': 'video', 'file': str(source), 'name': '实际演示',
                    'duration_seconds': video_seconds, 'sample_times': [0]}]
        request = {'format': 'custom', 'width': 640, 'height': 360, 'voice': 'fixture', 'rate': '+0%',
                   'layout_style': 'immersive', 'media_fit': 'contain',
                   'show_sources': False, 'show_disclosure': False, 'minimal_text': True}
        engine.validate(plan, sources)
        config = engine.export(root, plan, sources, request)
        return config

    def voice(self, seconds):
        # Non-zero samples all the way to the end detect accidental truncation.
        pcm = b'\x31\x02' * round(seconds*mv.RATE_HZ)
        events = [{'type': 'WordBoundary', 'text': '观察这个真实动作', 'offset': 0,
                   'duration': round(seconds*10_000_000)}]
        return pcm, events, False

    def build(self, root, voice_seconds, mock_encoder=True):
        from contextlib import ExitStack
        voice = self.voice(voice_seconds)
        args = SimpleNamespace(project=str(root/'project.json'), voice=None, rate=None,
                               font=None, check=False, subtitles_only=False)
        with ExitStack() as stack:
            stack.enter_context(patch('make_video.make_voice', new=AsyncMock(return_value=voice)))
            stack.enter_context(patch('make_video.log'))
            if mock_encoder:
                stack.enter_context(patch('make_video.ffmpeg_path', return_value='ffmpeg'))
                stack.enter_context(patch('make_video.find_font', return_value='Fixture font'))
                def rendered(index, shot, job, work, *args):
                    target = work/f'shot_{index:03}.mp4'; target.write_bytes(b'mocked shot'); return target
                stack.enter_context(patch('studio_render.render_shot', side_effect=rendered))
                def run(command, **kwargs):
                    target = Path(command[-1]); target = target if target.is_absolute() else Path(kwargs['cwd'])/target
                    target.write_bytes(b'mocked container')
                    return b''
                stack.enter_context(patch('make_video.run', side_effect=run))
            asyncio.run(mv.build(args))
        return voice[0]

    def test_exported_ten_second_complete_video_with_five_second_voice_has_no_false_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); config = self.exported_job(root)
            self.assertEqual(config['blocks'][0]['min_duration'], 10)
            self.assertTrue(config['strict_video_continuity'])
            pcm = self.build(root, 5)
            report = pv.load_json(root/'output/run_report.json')
            self.assertEqual(report['duration_seconds'], 10)
            self.assertEqual(report['video_holds_replaced'], 0)
            with wave.open(str(root/'output/narration.wav')) as wav:
                self.assertEqual(wav.getnframes(), 10*mv.RATE_HZ)
                self.assertEqual(wav.readframes(5*mv.RATE_HZ), pcm)

    def test_voice_exactly_matching_complete_video_does_not_add_quarter_second(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.exported_job(root)
            pcm = self.build(root, 10)
            self.assertEqual(pv.load_json(root/'output/run_report.json')['duration_seconds'], 10)
            with wave.open(str(root/'output/narration.wav')) as wav:
                self.assertEqual(wav.getnframes(), 10*mv.RATE_HZ)
                self.assertEqual(wav.readframes(wav.getnframes()), pcm)

    def test_voice_longer_than_complete_video_reports_real_one_second_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.exported_job(root)
            with self.assertRaises(sr.NeedsMoreVisualsError):
                self.build(root, 11)
            diagnostic = pv.load_json(root/'output/timing_recovery.json')
            self.assertEqual(diagnostic['issues'][0]['missing_seconds'], 1)
            self.assertEqual(diagnostic['duration_seconds'], 11)
            with wave.open(str(root/'output/narration.wav')) as wav:
                self.assertEqual(wav.getnframes(), 11*mv.RATE_HZ)
                self.assertEqual(wav.readframes(wav.getnframes()), self.voice(11)[0])

    def test_real_short_video_keeps_full_clip_and_all_speech_samples(self):
        import imageio_ffmpeg
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.exported_job(root, video_seconds=2, real=True)
            pcm = self.build(root, 1, mock_encoder=False)
            report = pv.load_json(root/'output/run_report.json')
            self.assertEqual(report['duration_seconds'], 2)
            reader = imageio_ffmpeg.read_frames(str(root/'output/video.mp4'))
            try:
                meta = next(reader); frame_count = sum(1 for _ in reader)
            finally:
                reader.close()
            self.assertEqual(meta['size'], (640, 360)); self.assertEqual(frame_count, 60)
            with wave.open(str(root/'output/narration.wav')) as wav:
                self.assertEqual(wav.getnframes(), 2*mv.RATE_HZ)
                self.assertEqual(wav.readframes(mv.RATE_HZ), pcm)
                self.assertEqual(wav.readframes(mv.RATE_HZ), b'\0'*(mv.RATE_HZ*2))

    def test_strict_image_tail_and_legacy_project_keep_optional_padding(self):
        for strict, last_kind in ((True, 'image'), (False, 'video')):
            with self.subTest(strict=strict, last_kind=last_kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); config = self.exported_job(root)
                config['strict_video_continuity'] = strict
                if last_kind == 'image':
                    picture = root/'assets/still.png'; Image.new('RGB', (320, 180), 'white').save(picture)
                    config['blocks'][0]['shots'][0].update(kind='image', file=str(picture), keep_full=False)
                else:
                    config['blocks'][0]['shots'][0]['keep_full'] = False
                config['blocks'][0]['min_duration'] = 0
                pv.save_json(root/'project.json', config)
                self.build(root, 5)
                self.assertEqual(pv.load_json(root/'output/run_report.json')['duration_seconds'], math.ceil(5.25*30)/30)


if __name__ == '__main__':
    unittest.main()
