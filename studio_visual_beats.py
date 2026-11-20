"""Cue-timed detail views that retain the complete source image as context.

Boxes refer to the *prepared shot image*, after its source crop/composition.
These are camera/detail views of an existing image, never generated evidence.
Planning uses cue strings; rendering accepts only measured local start times.
"""
from __future__ import annotations

from bisect import bisect_right
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile

from PIL import Image, ImageDraw, ImageFont, ImageOps

from studio_diagrams import _font_path


BEATS_SCHEMA = {
    'type': 'array', 'minItems': 0, 'maxItems': 6,
    'items': {'type': 'object', 'additionalProperties': False,
              'properties': {
                  'cue': {'type': 'string', 'maxLength': 80},
                  'bbox': {'type': 'array', 'minItems': 4, 'maxItems': 4,
                           'items': {'type': 'number', 'minimum': 0, 'maximum': 1}},
                  'label': {'type': 'string', 'maxLength': 24}},
              'required': ['cue', 'bbox', 'label']}}

BACKGROUND = '#0b1018'
ACCENT = '#91bfff'


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{name} must be a finite number')
    return float(value)


def _text(value, name, limit):
    if (not isinstance(value, str) or len(value) > limit
            or any(ord(c) < 32 for c in value)):
        raise ValueError(f'{name} must be text of at most {limit} characters')
    return value.strip()


def validate_beats(beats, narration=None):
    """Return a validated independent copy; [] disables detail views.

    When narration is supplied, noninitial cues must occur exactly once and in
    spoken order. No estimated start times are invented by this validator.
    """
    if not isinstance(beats, list) or len(beats) > 6:
        raise ValueError('Visual beats must be an array of at most six items')
    if narration is not None and not isinstance(narration, str):
        raise ValueError('Narration must be text')
    result = copy.deepcopy(beats)
    previous_position, previous_start = -1, -1.0
    seen = set()
    for index, beat in enumerate(result):
        if not isinstance(beat, dict):
            raise ValueError('Each visual beat must be an object')
        if set(beat) - {'cue', 'bbox', 'label', 'start'}:
            raise ValueError('Unknown visual beat field')
        beat['cue'] = _text(beat.get('cue'), 'Visual beat cue', 80)
        beat['label'] = _text(beat.get('label', ''), 'Visual beat label', 24)
        cue = beat['cue']
        if (index == 0 and cue) or (index > 0 and not cue):
            raise ValueError('The first visual beat cue must be empty; later cues cannot be empty')
        if index and cue in seen:
            raise ValueError('Visual beat cues must be unique')
        seen.add(cue)
        if index and narration is not None:
            if narration.count(cue) != 1:
                raise ValueError(f'Visual beat cue must occur exactly once in narration: {cue}')
            position = narration.index(cue)
            if position <= previous_position:
                raise ValueError('Visual beat cues must follow narration order')
            previous_position = position
        box = beat.get('bbox')
        if not isinstance(box, list) or len(box) != 4:
            raise ValueError('Visual beat bbox needs four normalized coordinates')
        box = [_number(value, 'Visual beat bbox coordinate') for value in box]
        if (not all(0 <= value <= 1 for value in box)
                or box[2] - box[0] < .02 or box[3] - box[1] < .02):
            raise ValueError('Visual beat bbox must be inside the image and at least 2% wide/high')
        beat['bbox'] = box
        if 'start' in beat:
            start = _number(beat['start'], 'Visual beat start')
            if start < 0 or (index == 0 and start != 0) or start <= previous_start:
                raise ValueError('Measured visual beat starts must begin at zero and increase strictly')
            previous_start = start
            beat['start'] = start
    return result


def _fit_box(image_size, bounds):
    left, top, right, bottom = bounds
    scale = min((right-left)/image_size[0], (bottom-top)/image_size[1])
    width, height = max(1, round(image_size[0]*scale)), max(1, round(image_size[1]*scale))
    x, y = round((left+right-width)/2), round((top+bottom-height)/2)
    return x, y, x+width, y+height


class _Frames:
    def __init__(self, source_image, beats, duration, width, height, font):
        self.duration = _number(duration, 'duration')
        if not 0 < self.duration <= 600:
            raise ValueError('Visual beat duration must be in (0, 600] seconds')
        if (any(type(n) is not int or n < 320 or n > 3840 or n % 2 for n in (width, height))
                or width*height > 8294400):
            raise ValueError('Visual beat dimensions must be even, 320–3840 pixels, and at most 4K')
        self.beats = validate_beats(beats)
        if not self.beats or any('start' not in beat for beat in self.beats):
            raise ValueError('Resolve every visual beat cue to a measured start before rendering')
        if any(beat['start'] >= self.duration for beat in self.beats):
            raise ValueError('Visual beat starts must be before the end of the shot')
        self.starts = [beat['start'] for beat in self.beats]
        self.width, self.height = width, height
        if isinstance(source_image, Image.Image):
            self.source = ImageOps.exif_transpose(source_image).convert('RGB')
        else:
            with Image.open(source_image) as source:
                self.source = ImageOps.exif_transpose(source).convert('RGB')
        if min(self.source.size) < 2:
            raise ValueError('Source image is too small for a detail view')
        self.font_path = _font_path(font)
        if width >= height:
            self.context_bounds = (.035*width, .24*height, .365*width, .72*height)
            self.detail_bounds = (.415*width, .24*height, .965*width, .76*height)
            self.label_bounds = (.415*width, .18*height, .965*width, .225*height)
        else:
            self.context_bounds = (.08*width, .16*height, .92*width, .34*height)
            self.detail_bounds = (.055*width, .42*height, .945*width, .77*height)
            self.label_bounds = (.055*width, .365*height, .945*width, .41*height)
        self.context_box = _fit_box(self.source.size, self.context_bounds)
        self.base = Image.new('RGB', (width, height), BACKGROUND)
        box = self.context_box
        self.base.paste(self.source.resize((box[2]-box[0], box[3]-box[1]), Image.Resampling.LANCZOS), box[:2])

    def _label(self, draw, text):
        if not text:
            return
        left, top, right, bottom = self.label_bounds
        size = max(12, round(min(self.width, self.height)*.036))
        while size >= 10:
            font = ImageFont.truetype(str(self.font_path), size)
            ink = draw.textbbox((0, 0), text, font=font)
            if ink[2]-ink[0] <= right-left and ink[3]-ink[1] <= bottom-top:
                draw.text((left-ink[0], top-ink[1]), text, fill='#e4edf9', font=font)
                return
            size -= 1
        raise ValueError('Visual beat label does not fit; shorten it for this canvas')

    def frame(self, time):
        index = max(0, bisect_right(self.starts, time)-1)
        beat = self.beats[index]
        box = beat['bbox']
        # Move the view window only. Pixel content is never warped or invented.
        if index:
            next_start = self.starts[index+1] if index+1 < len(self.starts) else self.duration
            transition = min(.3, (next_start-self.starts[index])/2)
            amount = min(1.0, max(0.0, (time-self.starts[index])/transition))
            amount = amount*amount*(3-2*amount)
            box = [a+(b-a)*amount for a, b in zip(self.beats[index-1]['bbox'], box)]
        width, height = self.source.size
        pixel_box = (math.floor(box[0]*width), math.floor(box[1]*height),
                     math.ceil(box[2]*width), math.ceil(box[3]*height))
        detail = self.source.crop(pixel_box)
        detail_box = _fit_box(detail.size, self.detail_bounds)
        image = self.base.copy()
        image.paste(detail.resize((detail_box[2]-detail_box[0], detail_box[3]-detail_box[1]),
                                  Image.Resampling.LANCZOS), detail_box[:2])
        draw = ImageDraw.Draw(image)
        stroke = max(2, round(min(self.width, self.height)*.003))
        cx0, cy0, cx1, cy1 = self.context_box
        # Boundaries use the actual integer source crop so locator and detail
        # show exactly the same area, even for small source images.
        locator = (cx0+pixel_box[0]/width*(cx1-cx0), cy0+pixel_box[1]/height*(cy1-cy0),
                   cx0+pixel_box[2]/width*(cx1-cx0), cy0+pixel_box[3]/height*(cy1-cy0))
        draw.rectangle(locator, outline=ACCENT, width=stroke)
        draw.rectangle(detail_box, outline=ACCENT, width=stroke)
        if self.width >= self.height:
            a = (locator[2], (locator[1]+locator[3])/2)
            b = (detail_box[0], (detail_box[1]+detail_box[3])/2)
        else:
            a = ((locator[0]+locator[2])/2, locator[3])
            b = ((detail_box[0]+detail_box[2])/2, detail_box[1])
        # A simple guide links the source location and detail; no moving markers.
        if self.width < self.height and beat['label']:
            # Do not run the location guide behind the portrait label.
            top, bottom = self.label_bounds[1], self.label_bounds[3]
            dy = b[1]-a[1]
            if dy > 0:
                x_at = lambda y: a[0]+(b[0]-a[0])*(y-a[1])/dy
                upper, lower = top-self.height*.008, bottom+self.height*.008
                if a[1] < upper:
                    draw.line((a, (x_at(upper), upper)), fill='#557493', width=max(1, stroke//2))
                if lower < b[1]:
                    draw.line(((x_at(lower), lower), b), fill='#557493', width=max(1, stroke//2))
        else:
            draw.line((a, b), fill='#557493', width=max(1, stroke//2))
        self._label(draw, beat['label'])
        return image


def render_frame(source_image, beats, time, duration, width=1920, height=1080, font=None):
    """Return an RGB preview at a measured local time, including final frame."""
    frames = _Frames(source_image, beats, duration, width, height, font)
    time = _number(time, 'time')
    if not 0 <= time <= frames.duration:
        raise ValueError('Preview time is outside the clip')
    return frames.frame(time)


def render_visual_beats(source_image, beats, output, duration, width=1920, height=1080,
                        fps=30, ffmpeg=None, font=None):
    """Atomically encode a silent MP4 after validating all measured timings."""
    if type(fps) is not int or not 1 <= fps <= 60:
        raise ValueError('fps must be an integer in 1–60')
    frames = _Frames(source_image, beats, duration, width, height, font)
    output = Path(output)
    if output.suffix.lower() != '.mp4':
        raise ValueError('Visual beat output must be an MP4 file')
    if not isinstance(source_image, Image.Image) and Path(source_image).resolve() == output.resolve():
        raise ValueError('Output cannot replace its source image')
    output.parent.mkdir(parents=True, exist_ok=True)
    count = max(1, round(duration*fps))
    if ffmpeg is None:
        from make_video import ffmpeg_path
        ffmpeg = ffmpeg_path()
    with tempfile.TemporaryDirectory(prefix='visual-beats-', dir=output.parent) as folder:
        temporary = Path(folder)/'detail.mp4'
        with tempfile.TemporaryFile() as error:
            process = subprocess.Popen([
                str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-y', '-f', 'rawvideo',
                '-pixel_format', 'rgb24', '-video_size', f'{width}x{height}', '-framerate', str(fps),
                '-i', 'pipe:0', '-an', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18',
                '-threads', '4', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(temporary)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=error,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                for index in range(count):
                    process.stdin.write(frames.frame(index/fps).tobytes())
                process.stdin.close()
                code = process.wait()
            except BaseException:
                if process.stdin and not process.stdin.closed:
                    try:
                        process.stdin.close()
                    except BrokenPipeError:
                        pass
                if process.poll() is None:
                    process.kill()
                process.wait()
                raise
            if code:
                error.seek(0)
                raise RuntimeError('Detail-view encoding failed: '+error.read().decode('utf-8', 'replace')[-2000:])
        os.replace(temporary, output)
    digest = hashlib.sha256(json.dumps(frames.beats, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {'file': str(output), 'duration_seconds': count/fps, 'frames': count, 'fps': fps,
            'resolution': [width, height], 'beats_sha256': digest,
            'provenance': 'detail views of the supplied image, with complete source context retained; no generated evidence'}
