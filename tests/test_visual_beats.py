import copy
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from studio_visual_beats import BEATS_SCHEMA, BACKGROUND, render_frame, render_visual_beats, validate_beats


def source():
    picture = Image.new('RGB', (800, 400), 'white')
    draw = ImageDraw.Draw(picture)
    draw.rectangle((40, 60, 310, 340), fill='#d43e42')
    draw.ellipse((470, 60, 750, 340), fill='#3154bc')
    draw.line((15, 380, 785, 380), fill='black', width=5)
    return picture


def beats(measured=False):
    items = [{'cue': '', 'bbox': [.025, .1, .425, .9], 'label': '第一次观察'},
             {'cue': '然后比较', 'bbox': [.575, .1, .975, .9], 'label': '比较另一侧'}]
    if measured:
        items[0]['start'], items[1]['start'] = 0, .6
    return items


class VisualBeatTests(unittest.TestCase):
    def test_optional_planning_schema_and_no_mutation(self):
        from jsonschema import Draft202012Validator
        original = beats()
        before = copy.deepcopy(original)
        self.assertEqual(validate_beats([], '稿件'), [])
        self.assertEqual(list(Draft202012Validator(BEATS_SCHEMA).iter_errors(original)), [])
        validated = validate_beats(original, '先观察左侧，然后比较右侧。')
        validated[0]['bbox'][0] = .1
        self.assertEqual(original, before)

    def test_rejects_invalid_bounds_and_values(self):
        for box in ([0, 0, 1.01, 1], [.5, .5, .4, .8], [0, 0, .01, .8],
                    [0, 0, float('nan'), 1], [0, 0, float('inf'), 1], [False, 0, 1, 1], [0, 0, 1]):
            with self.subTest(box=box):
                items = beats(); items[0]['bbox'] = box
                with self.assertRaises(ValueError):
                    validate_beats(items)
        for bad in (None, {}, [None], beats()*4):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_beats(bad)

    def test_cues_are_required_unique_and_ordered(self):
        for change in (lambda v: v[0].update(cue='开场'),
                       lambda v: v[1].update(cue=''),
                       lambda v: v[1].update(label='x'*25),
                       lambda v: v[1].update(cue='非法\n换行')):
            items = beats(); change(items)
            with self.assertRaises(ValueError):
                validate_beats(items)
        with self.assertRaisesRegex(ValueError, 'exactly once'):
            validate_beats(beats(), '没有对应短语')
        with self.assertRaisesRegex(ValueError, 'exactly once'):
            validate_beats(beats(), '然后比较，然后比较')
        items = beats()+[{'cue': '先观察', 'bbox': [0, 0, 1, 1], 'label': ''}]
        with self.assertRaisesRegex(ValueError, 'order'):
            validate_beats(items, '先观察，然后比较')
        items[2]['cue'] = '然后比较'
        with self.assertRaisesRegex(ValueError, 'unique'):
            validate_beats(items)

    def test_render_requires_measured_times_and_valid_canvas(self):
        with self.assertRaisesRegex(ValueError, 'measured'):
            render_frame(source(), beats(), 0, 1.2, 640, 360)
        for changed in ([{**beats(True)[0], 'start': .1}],
                        [beats(True)[0], {**beats(True)[1], 'start': 0}],
                        [beats(True)[0], {**beats(True)[1], 'start': 1.2}],
                        [beats(True)[0], {**beats(True)[1], 'start': float('nan')}]):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                render_frame(source(), changed, 0, 1.2, 640, 360)
        for size in ((639, 360), (200, 360), (3840, 3840)):
            with self.subTest(size=size), self.assertRaises(ValueError):
                render_frame(source(), beats(True), 0, 1.2, *size)
        for time in (-.1, 1.3, float('nan')):
            with self.subTest(time=time), self.assertRaises(ValueError):
                render_frame(source(), beats(True), time, 1.2, 640, 360)

    def test_landscape_and_portrait_keep_context_and_caption_area(self):
        from studio_visual_beats import _Frames
        original = source()
        before = original.tobytes()
        for width, height in ((960, 540), (540, 960), (640, 640)):
            frames = _Frames(original, beats(True), 1.2, width, height, None)
            first, last = frames.frame(.1), frames.frame(1.1)
            self.assertNotEqual(first.tobytes(), last.tobytes())
            self.assertEqual(first.crop((0, round(height*.8), width, height)).getextrema(),
                             ((11, 11), (16, 16), (24, 24)))
            # An unaffected corner of the original overview survives the zoom.
            left, top, right, bottom = frames.context_box
            point = (left+2, top+2)
            self.assertEqual(first.getpixel(point), (255, 255, 255))
            self.assertEqual(first.getpixel(point), last.getpixel(point))
        self.assertEqual(original.tobytes(), before)

    def test_real_encoded_output_has_timing_motion_and_full_decode(self):
        import imageio_ffmpeg
        from make_video import ffmpeg_path, run
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'details.mp4'
            picture = Path(directory)/'source.png'; source().save(picture)
            original = picture.read_bytes()
            result = render_visual_beats(picture, beats(True), output, 1.2, 640, 360)
            reader = imageio_ffmpeg.read_frames(str(output))
            try:
                metadata = next(reader); frames = list(reader)
            finally:
                reader.close()
            self.assertEqual(metadata['size'], (640, 360))
            self.assertEqual(len(frames), 36)
            self.assertEqual(result['duration_seconds'], 1.2)
            self.assertNotEqual(frames[3], frames[33])
            self.assertEqual(picture.read_bytes(), original)
            run([ffmpeg_path(), '-v', 'error', '-i', str(output), '-f', 'null', '-'])

    def test_failed_encoder_keeps_existing_output_and_removes_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); output = root/'existing.mp4'
            output.write_bytes(b'previous complete video')
            with self.assertRaises(OSError):
                render_visual_beats(source(), beats(True), output, 1.2, 640, 360,
                                    ffmpeg=root/'missing-encoder')
            self.assertEqual(output.read_bytes(), b'previous complete video')
            self.assertEqual([item.name for item in root.iterdir()], ['existing.mp4'])


if __name__ == '__main__':
    unittest.main()
