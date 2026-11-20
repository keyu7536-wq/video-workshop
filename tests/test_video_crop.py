"""Real decoded crop geometry and bounded local preview sampling."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

import make_video
from studio_video_crop import validate_video_crop, video_crop_filter, preview_video_crop


class VideoCropValidationTests(unittest.TestCase):
    def test_empty_and_valid_region_are_copied(self):
        self.assertEqual(validate_video_crop(None), [])
        self.assertEqual(validate_video_crop([]), [])
        region = [.25, 0, .5, 1]
        self.assertEqual(validate_video_crop(region), region)
        self.assertIsNot(validate_video_crop(region), region)
        self.assertEqual(video_crop_filter([]), '')

    def test_rejects_nonfinite_reversed_too_small_or_untyped_values(self):
        for value in ('0,0,1,1', [0, 0, 1], [False, 0, 1, 1],
                      [0, 0, float('nan'), 1], [0, 0, float('inf'), 1],
                      [-.1, 0, 1, 1], [0, 0, 1.1, 1], [.5, 0, .4, 1],
                      [0, 0, .079, 1], [0, 0, 1, .049]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_video_crop(value)


class VideoCropPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.job = Path(cls.tmp.name)
        (cls.job/'assets').mkdir()
        image = Image.new('RGB', (640, 360), '#d02020')
        draw = ImageDraw.Draw(image)
        for left, color in ((160, '#20b040'), (320, '#2040d0'), (480, '#e0c020')):
            draw.rectangle((left, 0, left+159, 359), fill=color)
        cls.still = cls.job/'assets/four-panels.png'; image.save(cls.still)
        cls.video = cls.job/'assets/four-panels.mp4'
        cls.ffmpeg = make_video.ffmpeg_path()
        make_video.run([cls.ffmpeg, '-v', 'error', '-y', '-loop', '1', '-i', str(cls.still),
                        '-t', '4', '-r', '10', '-an', '-c:v', 'libx264', '-threads', '1',
                        '-pix_fmt', 'yuv420p', str(cls.video)], timeout=30)
        cls.source = {'id': 'author-video', 'type': 'video', 'file': str(cls.video),
                      'duration_seconds': 4, 'sample_times': [0, .5, 1, 1.5, 2, 2.5, 3, 3.5]}
        cls.request = {'format': 'custom', 'width': 640, 'height': 360,
                       'layout_style': 'immersive', 'media_fit': 'contain'}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_decoded_selected_panel_and_contain_geometry_match(self):
        source, request = copy.deepcopy(self.source), copy.deepcopy(self.request)
        result = preview_video_crop(self.job, source, .5, 1, [.25, 0, .5, 1], request)
        json.dumps(result)
        self.assertEqual(source, self.source); self.assertEqual(request, self.request)
        meta = result['metadata']
        self.assertEqual(meta['sampled_times'], [.5, 1])
        self.assertEqual(meta['crop_pixels'], [160, 0, 160, 360])
        self.assertEqual(meta['canvas'], [640, 360])
        self.assertEqual(meta['inspection'], 'sampled_frames_only')
        self.assertFalse(meta['continuous_motion_reviewed'])
        frame = result['frames'][0]
        with Image.open(self.job/frame['original']) as original:
            self.assertEqual(original.size, (640, 360))
            self.assertGreater(original.getpixel((80, 180))[0], 150)
            self.assertGreater(original.getpixel((400, 180))[2], 150)
        with Image.open(self.job/frame['preview']) as preview:
            self.assertEqual(preview.size, (640, 360))
            green = preview.getpixel((320, 180))
            self.assertGreater(green[1], 140); self.assertLess(green[0], 70)
            # 160x360 selected content stays 160x360; it is not stretched.
            self.assertEqual(preview.getpixel((230, 180)), (8, 14, 21))
            self.assertEqual(preview.getpixel((410, 180)), (8, 14, 21))

    def test_subtitle_safe_reserves_clear_space_and_changes_preview_identity(self):
        raw=preview_video_crop(self.job,self.source,0,1,[0,0,1,1],self.request)
        safe=preview_video_crop(self.job,self.source,0,1,[0,0,1,1],self.request,True)
        self.assertTrue(safe['metadata']['protect_content'])
        self.assertNotEqual(raw['frames'][0]['preview'],safe['frames'][0]['preview'])
        self.assertLess(safe['metadata']['media_box'][1]+safe['metadata']['media_box'][3],360*.86)
        with Image.open(self.job/safe['frames'][0]['preview']) as picture:
            self.assertEqual(picture.getpixel((320,330)),(8,14,21))
        with self.assertRaises(ValueError):preview_video_crop(self.job,self.source,0,1,[],self.request,'yes')

    def test_sample_limit_selects_existing_times_inside_window(self):
        result = preview_video_crop(self.job, self.source, 0, 4, [], self.request)
        times = result['metadata']['sampled_times']
        self.assertEqual(len(times), 3)
        self.assertEqual(times[0], 0); self.assertEqual(times[-1], 3.5)
        self.assertTrue(all(t in self.source['sample_times'] for t in times))
        self.assertTrue(all(0 <= t < 4 for t in times))

    def test_no_sample_in_selected_window_is_not_a_synthetic_preview(self):
        with self.assertRaisesRegex(ValueError, '抽帧'):
            preview_video_crop(self.job, self.source, .1, .1, [], self.request)

    def test_source_metadata_cannot_authorize_frames_past_actual_video(self):
        source = dict(self.source, duration_seconds=10, sample_times=[8])
        with self.assertRaisesRegex(ValueError, '实际|范围'):
            preview_video_crop(self.job, source, 8, 1, [], self.request)

    def test_non_video_outside_and_missing_local_sources_are_rejected(self):
        for source in (dict(self.source, type='image'),
                       dict(self.source, file=str(self.job.parent/'outside.mp4')),
                       dict(self.source, file='assets/missing.mp4')):
            with self.subTest(source=source), self.assertRaises(ValueError):
                preview_video_crop(self.job, source, 0, 1, [], self.request)

    def test_malformed_time_or_missing_intake_samples_are_rejected(self):
        for start, duration in ((False, 1), (0, 0), (-1, 1), (0, float('inf'))):
            with self.subTest(start=start, duration=duration), self.assertRaises(ValueError):
                preview_video_crop(self.job, self.source, start, duration, [], self.request)
        for times in ([], None, [False], [float('nan')]):
            with self.subTest(times=times), self.assertRaises(ValueError):
                preview_video_crop(self.job, dict(self.source, sample_times=times), 0, 1, [], self.request)

    def test_even_crop_filter_and_preview_crop_pixels_agree(self):
        region = [.251, .003, .504, .997]
        target = self.job/'filter-crop.png'
        make_video.run([self.ffmpeg, '-v', 'error', '-y', '-i', str(self.video), '-frames:v', '1',
                        '-vf', video_crop_filter(region), str(target)], timeout=30)
        result = preview_video_crop(self.job, self.source, 0, .4, region, self.request)
        pixels = result['metadata']['crop_pixels']
        self.assertTrue(all(value % 2 == 0 for value in pixels))
        with Image.open(target) as cropped:
            self.assertEqual(list(cropped.size), pixels[2:])
            self.assertGreater(cropped.getpixel((20, 20))[1], 140)

    def test_cover_is_explicit_and_can_change_the_visible_region(self):
        result = preview_video_crop(self.job, self.source, 0, .4, [.25, 0, .5, 1],
                                    dict(self.request, media_fit='cover'))
        self.assertEqual(result['metadata']['fit'], 'cover')
        self.assertTrue(result['metadata']['additional_center_crop'])
        with Image.open(self.job/result['frames'][0]['preview']) as preview:
            self.assertGreater(preview.getpixel((5, 180))[1], 140)

    def test_preview_failure_does_not_return_or_reuse_old_outputs(self):
        with patch('studio_video_crop._extract', side_effect=RuntimeError('decode failed')):
            with self.assertRaisesRegex(RuntimeError, 'decode failed'):
                preview_video_crop(self.job, self.source, 0, .4, [], self.request)

    def test_real_renderer_has_the_same_selected_region_and_fit(self):
        from studio_render import render_shot
        from PIL import ImageChops, ImageStat
        crop = [.25, 0, .5, 1]
        result = preview_video_crop(self.job, self.source, .5, 1, crop, self.request)
        work = self.job/'renderer_check'; work.mkdir(exist_ok=True)
        shot = {'kind': 'video', 'file': self.source['file'], 'in': .5, 'duration': 1,
                'render_duration': .5, 'video_crop': crop}
        with patch('make_video.log'):
            render_shot(0, shot, self.job, work, self.ffmpeg, 10, 640, 360, self.request)
        encoded = work/'encoded.png'
        make_video.run([self.ffmpeg, '-v', 'error', '-y', '-i', str(work/'shot_000.mp4'),
                        '-frames:v', '1', str(encoded)], timeout=30)
        with Image.open(encoded) as actual, Image.open(self.job/result['frames'][0]['preview']) as preview:
            error = ImageStat.Stat(ImageChops.difference(actual.convert('RGB'), preview.convert('RGB')))
            self.assertLess(max(error.mean), 4)
            self.assertGreater(actual.getpixel((320, 180))[1], 140)
            self.assertLess(max(actual.getpixel((230, 180))), 30)

    def test_relative_source_and_new_bytes_produce_separate_provenance(self):
        import shutil
        relative = dict(self.source, file='assets/four-panels.mp4')
        first = preview_video_crop(self.job, relative, 0, .4, [], self.request)
        alternate = self.job/'assets/alternate.mp4'
        shutil.copyfile(self.video, alternate)
        # Appended container padding leaves the decoded stream intact but is a
        # different source artifact; byte identity must not reuse its proof.
        with alternate.open('ab') as stream:
            stream.write(b'\0'*16)
        second = preview_video_crop(self.job, dict(relative, file=str(alternate)), 0, .4, [], self.request)
        self.assertNotEqual(first['metadata']['source_sha256'], second['metadata']['source_sha256'])
        self.assertNotEqual(first['frames'][0]['preview'], second['frames'][0]['preview'])


if __name__ == '__main__':
    unittest.main()
