"""Explicit, speech-timed activity states without changing object identity."""
import copy
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from studio_motion import COLORS, MOTION_SCHEMA, render_frame, render_motion, validate_motion


def state_scene(state=None, measured=False, shape='circle'):
    pose = {'id': 'source', 'x': .5, 'y': .4, 'visible': True, 'label': None}
    if state is not None:
        pose['state'] = state
    stage = {'cue': '', 'poses': [pose], 'links': []}
    if measured:
        stage['start'] = 0
    return {'title': '处理状态', 'objects': [
        {'id': 'source', 'shape': shape, 'label': '同一份资料', 'color': 'blue'}],
        'stages': [stage]}


def state_timeline():
    result = state_scene('neutral', measured=True)
    for cue, start, state in [('等待处理', .7, 'idle'), ('开始处理', 1.4, 'active'),
                              ('完成处理', 2.1, 'done')]:
        stage = copy.deepcopy(result['stages'][0])
        stage.update(cue=cue, start=start)
        stage['poses'][0]['state'] = state
        result['stages'].append(stage)
    return result


class MotionStateTests(unittest.TestCase):
    def test_new_schema_requires_an_explicit_known_state(self):
        from jsonschema import Draft202012Validator
        pose_schema = MOTION_SCHEMA['properties']['stages']['items']['properties']['poses']['items']
        self.assertIn('state', pose_schema['required'])
        self.assertEqual(set(pose_schema['properties']['state']['enum']),
                         {'neutral', 'idle', 'active', 'done'})
        validator = Draft202012Validator(MOTION_SCHEMA)
        self.assertTrue(list(validator.iter_errors(state_scene())))
        for state in ('neutral', 'idle', 'active', 'done'):
            with self.subTest(state=state):
                self.assertEqual(list(validator.iter_errors(state_scene(state))), [])

    def test_legacy_defaults_are_deep_copied_without_guessing_label_meaning(self):
        source = state_scene()
        source['objects'][0]['label'] = '已完成'
        source['stages'][0]['poses'][0]['label'] = '正在处理'
        before = copy.deepcopy(source)
        result = validate_motion(source, check_layout=False)
        self.assertEqual(result['stages'][0]['poses'][0]['state'], 'neutral')
        self.assertEqual(source, before)
        result['stages'][0]['poses'][0]['state'] = 'done'
        self.assertEqual(source, before)

    def test_invalid_state_types_and_values_are_rejected_consistently(self):
        for state in ('', 'working', 'Active', None, True, 1, [], {}, float('nan')):
            spec = state_scene('neutral')
            spec['stages'][0]['poses'][0]['state'] = state
            with self.subTest(state=state), self.assertRaises(ValueError):
                validate_motion(spec, check_layout=False)

    def test_legacy_and_explicit_neutral_render_the_same_for_every_shape(self):
        for shape in ('circle', 'rect', 'label', 'document', 'photo'):
            with self.subTest(shape=shape):
                legacy = state_scene(measured=True, shape=shape)
                explicit = state_scene('neutral', measured=True, shape=shape)
                self.assertEqual(render_frame(legacy, .2, 1, 640, 360).tobytes(),
                                 render_frame(explicit, .2, 1, 640, 360).tobytes())

    def test_states_are_visible_without_relabeling_or_recoloring_identity(self):
        before = state_scene('neutral', measured=True)
        identity = tuple(int(COLORS['blue'][i:i+2], 16) for i in (1, 3, 5))
        pictures = []
        for state in ('neutral', 'idle', 'active', 'done'):
            spec = copy.deepcopy(before)
            spec['stages'][0]['poses'][0]['state'] = state
            image = render_frame(spec, .2, 1, 960, 540)
            # The center of the same circular object retains its identity color.
            self.assertEqual(image.getpixel((480, 216)), identity)
            self.assertEqual(spec['objects'], before['objects'])
            self.assertEqual(spec['stages'][0]['poses'][0]['label'], None)
            pictures.append(image.tobytes())
        self.assertEqual(len(set(pictures)), 4)

    def test_state_switches_at_the_measured_cue_with_no_position_change(self):
        spec = state_timeline()
        stable = state_scene('neutral', measured=True)
        self.assertEqual(render_frame(spec, .699, 2.8, 640, 360).tobytes(),
                         render_frame(stable, .699, 2.8, 640, 360).tobytes())
        for start in (.7, 1.4, 2.1):
            with self.subTest(start=start):
                previous = render_frame(spec, start-.001, 2.8, 640, 360)
                current = render_frame(spec, start, 2.8, 640, 360)
                self.assertNotEqual(previous.tobytes(), current.tobytes())
        without_times = copy.deepcopy(spec)
        for stage in without_times['stages']:
            stage.pop('start')
        with self.assertRaisesRegex(ValueError, 'measured'):
            render_frame(without_times, .7, 2.8, 640, 360)

    def test_activity_is_repeating_indeterminate_motion_not_a_completion_fraction(self):
        spec = state_scene('active', measured=True)
        first = render_frame(spec, 0, 3, 960, 540)
        turn = render_frame(spec, .45, 3, 960, 540)
        repeated = render_frame(spec, 1.8, 3, 960, 540)
        self.assertNotEqual(first.tobytes(), turn.tobytes())
        self.assertEqual(first.tobytes(), repeated.tobytes())

    def test_states_keep_caption_space_clear_in_both_orientations(self):
        spec = state_timeline()
        for width, height in ((960, 540), (540, 960)):
            for time in (0, .7, 1.5, 2.1, 2.79):
                with self.subTest(size=(width, height), time=time):
                    image = render_frame(spec, time, 2.8, width, height)
                    self.assertEqual(image.crop((0, round(height*.8), width, height)).getextrema(),
                                     ((11, 11), (16, 16), (24, 24)))

    def test_real_state_video_encodes_and_decodes_in_landscape_and_portrait(self):
        import imageio_ffmpeg
        from make_video import ffmpeg_path, run
        spec = state_timeline()
        with tempfile.TemporaryDirectory() as directory:
            for width, height in ((640, 360), (360, 640)):
                with self.subTest(size=(width, height)):
                    output = Path(directory)/f'states_{width}x{height}.mp4'
                    render_motion(spec, output, 2.8, width, height)
                    reader = imageio_ffmpeg.read_frames(str(output))
                    try:
                        metadata = next(reader)
                        frames = list(reader)
                    finally:
                        reader.close()
                    self.assertEqual(metadata['size'], (width, height))
                    self.assertEqual(len(frames), 84)
                    self.assertEqual(len({frames[index] for index in (3, 27, 51, 75)}), 4)
                    for index in (3, 27, 51, 75):
                        image = Image.frombytes('RGB', (width, height), frames[index])
                        extrema = image.crop((0, round(height*.8), width, height)).getextrema()
                        self.assertTrue(all(high-low <= 3 for low, high in extrema))
                    run([ffmpeg_path(), '-v', 'error', '-i', str(output), '-f', 'null', '-'])


if __name__ == '__main__':
    unittest.main()
