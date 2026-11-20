"""Stable-object motion, bounded layouts and measured-stage video encoding."""
import copy
from pathlib import Path
import tempfile
import unittest

from PIL import Image
from studio_motion import MOTION_SCHEMA, render_frame, render_motion, validate_motion


def pose(identifier, x, y, visible=True, label=None):
    return {'id': identifier, 'x': x, 'y': y, 'visible': visible, 'label': label}


def sequence(measured=False):
    result = {'title': '位置变化示意', 'objects': [
        {'id': 'token', 'shape': 'circle', 'label': '示意对象', 'color': 'blue'},
        {'id': 'a', 'shape': 'rect', 'label': '路线一', 'color': 'amber'},
        {'id': 'b', 'shape': 'rect', 'label': '路线二', 'color': 'purple'}],
        'stages': [
            {'cue': '', 'poses': [pose('token', .17, .46), pose('a', .85, .32), pose('b', .85, .65)], 'links': []},
            {'cue': '出现两个方向', 'poses': [pose('token', .42, .46), pose('a', .85, .32), pose('b', .85, .65)],
             'links': [{'source': 'token', 'target': 'a', 'label': ''}, {'source': 'token', 'target': 'b', 'label': ''}]},
            {'cue': '沿着其中一路', 'poses': [pose('token', .63, .32), pose('a', .85, .32), pose('b', .85, .65)],
             'links': [{'source': 'token', 'target': 'a', 'label': ''}]}]}
    if measured:
        for stage, start in zip(result['stages'], (0, .7, 1.4)):
            stage['start'] = start
    return result


def parallel(measured=False):
    result = {'title': '两条流程分别推进', 'objects': [
        {'id': 'a', 'shape': 'circle', 'label': '整理素材', 'color': 'blue'},
        {'id': 'b', 'shape': 'rect', 'label': '核对来源', 'color': 'amber'}],
        'stages': [
            {'cue': '', 'poses': [pose('a', .2, .33), pose('b', .2, .65)], 'links': []},
            {'cue': '各自推进', 'poses': [pose('a', .5, .33), pose('b', .4, .65)], 'links': []},
            {'cue': '继续处理', 'poses': [pose('a', .8, .33), pose('b', .7, .65)], 'links': []}]}
    if measured:
        for stage, start in zip(result['stages'], (0, .7, 1.4)):
            stage['start'] = start
    return result


class MotionTests(unittest.TestCase):
    def test_schema_validation_and_input_preservation(self):
        from jsonschema import Draft202012Validator
        spec = sequence(); before = copy.deepcopy(spec)
        validated = validate_motion(spec, '先看一个对象，出现两个方向，然后沿着其中一路移动。')
        self.assertEqual(list(Draft202012Validator(MOTION_SCHEMA).iter_errors(validated)), [])
        validated['objects'][0]['label'] = '独立副本'
        self.assertEqual(spec, before)

    def test_rejects_invalid_objects_positions_and_links(self):
        mutators = (
            lambda s: s['objects'][1].update(id='token'),
            lambda s: s['objects'][0].update(shape='path'),
            lambda s: s['objects'][0].update(color='#ff0000'),
            lambda s: s['objects'][0].update(color=[]),
            lambda s: s['stages'][0]['poses'][0].update(x=float('nan')),
            lambda s: s['stages'][0]['poses'][0].update(x=True),
            lambda s: s['stages'][0]['poses'][0].update(y=1.2),
            lambda s: s['stages'][0]['poses'][0].update(visible=1),
            lambda s: s['stages'][0]['poses'][0].update(id='absent'),
            lambda s: s['stages'][0]['poses'].pop(),
            lambda s: s['stages'][1]['links'][0].update(target='absent'),
            lambda s: s['stages'][1]['links'][0].update(target='token'),
            lambda s: s['stages'][1]['poses'][1].update(visible=False),
            lambda s: s['stages'][1]['links'].append(s['stages'][1]['links'][0].copy()),
        )
        for mutate in mutators:
            spec = sequence(); mutate(spec)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                validate_motion(spec)

    def test_cue_order_and_runtime_measurements(self):
        with self.assertRaisesRegex(ValueError, 'measured'):
            render_frame(sequence(), 0, 2.2)
        for mutate in (
            lambda s: s['stages'][0].update(cue='开场'),
            lambda s: s['stages'][1].update(cue=''),
            lambda s: s['stages'][2].update(cue='出现两个方向')):
            spec = sequence(); mutate(spec)
            with self.assertRaises(ValueError):
                validate_motion(spec)
        with self.assertRaisesRegex(ValueError, 'exactly once'):
            validate_motion(sequence(), '没有对应提示')
        with self.assertRaisesRegex(ValueError, 'order'):
            validate_motion(sequence(), '沿着其中一路，出现两个方向。')
        for start in (-1, 0, float('inf'), 2.2):
            spec = sequence(True); spec['stages'][1]['start'] = start
            with self.subTest(start=start), self.assertRaises(ValueError):
                render_frame(spec, 0, 2.2)

    def test_geometry_is_checked_on_target_canvas_not_forced_landscape(self):
        spec = parallel()
        spec['stages'][0]['poses'][0].update(y=.21)
        # This circle leaves enough room in portrait but would encroach on the
        # header in landscape. Structural validation must remain canvas-neutral.
        validate_motion(spec, check_layout=False)
        validate_motion(spec, width=None, height=None)
        validate_motion(spec, width=720, height=1280)
        with self.assertRaisesRegex(ValueError, 'fit'):
            validate_motion(spec, width=1280, height=720)

    def test_overlap_and_crossing_paths_are_rejected(self):
        spec = parallel()
        spec['stages'][0]['poses'][1].update(y=.33)
        with self.assertRaisesRegex(ValueError, 'overlap'):
            validate_motion(spec)
        spec = parallel()
        # Endpoints are separated, but two same-lane objects would collide while swapping.
        spec['stages'] = spec['stages'][:2]
        spec['stages'][0]['poses'] = [pose('a', .2, .45), pose('b', .8, .45)]
        spec['stages'][1]['poses'] = [pose('a', .8, .45), pose('b', .2, .45)]
        with self.assertRaisesRegex(ValueError, 'paths'):
            validate_motion(spec)

    def test_same_object_moves_through_intermediate_position(self):
        spec = parallel(True)
        first = render_frame(spec, .1, 2.2, 960, 540)
        middle = render_frame(spec, .875, 2.2, 960, 540)
        later = render_frame(spec, 1.3, 2.2, 960, 540)
        # Stage 1 lasts .7 sec, so its .35 sec transition is halfway at .875.
        y = round(.33*540)
        self.assertEqual(first.getpixel((192, y)), (142, 184, 255))
        self.assertEqual(middle.getpixel((336, y)), (142, 184, 255))
        self.assertEqual(later.getpixel((480, y)), (142, 184, 255))
        self.assertEqual(later.getpixel((192, y)), (11, 16, 24))

    def test_labels_can_change_and_objects_can_disappear(self):
        spec = parallel(True)
        spec['stages'][1]['poses'][0]['label'] = '已整理'
        spec['stages'][1]['poses'][1]['visible'] = False
        changed = render_frame(spec, 1.3, 2.2, 960, 540)
        original = render_frame(parallel(True), 1.3, 2.2, 960, 540)
        self.assertNotEqual(changed.tobytes(), original.tobytes())
        self.assertEqual(changed.getpixel((384, 351)), (11, 16, 24))

    def test_landscape_and_portrait_keep_caption_area_empty(self):
        for width, height in ((960, 540), (540, 960), (640, 640)):
            for time in (.1, .85, 1.5, 2.1):
                frame = render_frame(parallel(True), time, 2.2, width, height)
                self.assertEqual(frame.crop((0, round(height*.8), width, height)).getextrema(),
                                 ((11, 11), (16, 16), (24, 24)))

    def test_link_labels_do_not_enter_header_or_subtitle_areas(self):
        spec = {'title': '', 'objects': [
            {'id': 'a', 'shape': 'rect', 'label': '输入', 'color': 'blue'},
            {'id': 'b', 'shape': 'rect', 'label': '输出', 'color': 'amber'}],
            'stages': [{'cue': '', 'start': 0, 'poses': [pose('a', .2, .25), pose('b', .8, .25)],
                        'links': [{'source': 'a', 'target': 'b', 'label': '条件关系'}]}]}
        for width, height in ((960, 540), (540, 960)):
            frame = render_frame(spec, .1, 1, width, height)
            self.assertEqual(frame.crop((0, 0, width, int(height*.18))).getextrema(),
                             ((11, 11), (16, 16), (24, 24)))
            self.assertEqual(frame.crop((0, round(height*.8), width, height)).getextrema(),
                             ((11, 11), (16, 16), (24, 24)))
            blank = copy.deepcopy(spec); blank['stages'][0]['links'][0]['label'] = ''
            self.assertNotEqual(frame.tobytes(), render_frame(blank, .1, 1, width, height).tobytes())

    def test_invalid_preview_time_and_encoding_settings(self):
        for time in (-.1, 2.3, float('nan'), True):
            with self.subTest(time=time), self.assertRaises(ValueError):
                render_frame(parallel(True), time, 2.2, 640, 360)
        for width, height in ((639, 360), (640, 200), (3840, 3840)):
            with self.subTest(size=(width, height)), self.assertRaises(ValueError):
                render_frame(parallel(True), 0, 2.2, width, height)
        with tempfile.TemporaryDirectory() as directory:
            for fps in (0, 61, True, 29.5):
                with self.subTest(fps=fps), self.assertRaises(ValueError):
                    render_motion(parallel(True), Path(directory)/'invalid.mp4', 2.2, 640, 360, fps=fps)

    def test_actual_encoding_and_error_do_not_replace_previous_complete_video(self):
        import imageio_ffmpeg
        from make_video import ffmpeg_path, run
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); output = root/'motion.mp4'
            result = render_motion(parallel(True), output, 2.2, 640, 360)
            reader = imageio_ffmpeg.read_frames(str(output))
            try:
                metadata = next(reader); frames = list(reader)
            finally:
                reader.close()
            self.assertEqual(metadata['size'], (640, 360))
            self.assertEqual(len(frames), 66)
            self.assertEqual(result['duration_seconds'], 2.2)
            self.assertNotEqual(frames[2], frames[45])
            run([ffmpeg_path(), '-v', 'error', '-i', str(output), '-f', 'null', '-'])
            before = output.read_bytes()
            with self.assertRaises(OSError):
                render_motion(parallel(True), output, 2.2, 640, 360, ffmpeg=root/'missing')
            self.assertEqual(output.read_bytes(), before)
            self.assertEqual([p.name for p in root.iterdir()], ['motion.mp4'])

    def test_concrete_shapes_have_identifiable_details_with_labels_below(self):
        from studio_motion import _Frames, _bounds
        spec=parallel(True)
        spec['objects'][0].update(shape='document',label='论文资料')
        spec['objects'][1].update(shape='photo',label='图片素材')
        frames=_Frames(spec,2.2,1280,720,None)
        picture=frames.frame(.1)
        pose_map={item['id']:item for item in spec['stages'][0]['poses']}
        paper_box=_bounds(spec['objects'][0],pose_map['a'],1280,720,physical=True)
        # The cutaway paper corner stays background, unlike a rectangular block.
        self.assertEqual(picture.getpixel((round(paper_box[2]-2),round(paper_box[1]+2))),(11,16,24))
        # The photo's sun is a visible part of the icon, not text embedded inside.
        photo=pose_map['b'];sun=(round(photo['x']*1280+.057*720),round(photo['y']*720-.0525*720))
        self.assertEqual(picture.getpixel(sun),(230,191,114))
        changed=copy.deepcopy(spec)
        changed['objects'][0]['label']='换个名称'
        changed['objects'][1]['label']='另一个名称'
        changed_picture=render_frame(changed,.1,2.2,1280,720)
        for obj in spec['objects']:
            box=tuple(round(value) for value in _bounds(obj,pose_map[obj['id']],1280,720,physical=True))
            self.assertEqual(picture.crop(box).tobytes(),changed_picture.crop(box).tobytes())
        self.assertNotEqual(picture.tobytes(),changed_picture.tobytes())

    def test_enlarged_icons_preserve_lower_label_space_and_validate_the_new_top(self):
        from studio_motion import _bounds
        for shape,half_width in (('document',.0525),('photo',.0975)):
            obj={'id':'a','shape':shape,'label':'第一份资料的处理状态','color':'blue'}
            anchor=pose('a',.5,.65)
            physical=_bounds(obj,anchor,1280,720,physical=True)
            whole=_bounds(obj,anchor,1280,720)
            self.assertAlmostEqual(physical[2]-physical[0],half_width*2*720)
            self.assertAlmostEqual(physical[3]-physical[1],.135*720)
            self.assertAlmostEqual(whole[3],(.65+.123)*720)
            spec={'title':'','objects':[obj],'stages':[{'cue':'','poses':[anchor],'links':[],'start':0}]}
            for width,height in ((1280,720),(720,1280)):
                image=render_frame(spec,0,1,width,height)
                self.assertEqual(image.crop((0,round(height*.8),width,height)).getextrema(),
                                 ((11,11),(16,16),(24,24)))
            spec['stages'][0]['poses'][0]['y']=.25
            with self.assertRaisesRegex(ValueError,'fit'):
                validate_motion(spec,width=1280,height=720)
            validate_motion(spec,width=720,height=1280)

    def test_concrete_shapes_encode_in_both_orientations_and_keep_caption_space(self):
        import imageio_ffmpeg
        from make_video import ffmpeg_path,run
        spec=parallel(True);spec['stages']=spec['stages'][:2]
        spec['objects'][0].update(shape='document',label='论文')
        spec['objects'][1].update(shape='photo',label='图片')
        with tempfile.TemporaryDirectory() as directory:
            for width,height in ((640,360),(360,640)):
                output=Path(directory)/f'{width}.mp4'
                render_motion(spec,output,1.4,width,height)
                reader=imageio_ffmpeg.read_frames(str(output))
                try:
                    metadata=next(reader);frames=list(reader)
                finally:reader.close()
                self.assertEqual(metadata['size'],(width,height));self.assertEqual(len(frames),42)
                self.assertNotEqual(frames[0],frames[-1])
                decoded=Image.frombytes('RGB',(width,height),frames[-1])
                extrema=decoded.crop((0,round(height*.8),width,height)).getextrema()
                self.assertTrue(all(high-low<=2 for low,high in extrema))
                run([ffmpeg_path(),'-v','error','-i',str(output),'-f','null','-'])


if __name__ == '__main__':
    unittest.main()
