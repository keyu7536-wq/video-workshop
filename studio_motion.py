"""Small, speech-timed motion scenes for explanatory objects and relationships.

Objects retain stable IDs across stages. Their screen movement describes an
explanation, not a measured trajectory, physical simulation, or experimental
result. Planning contains cue strings; encoding requires measured local starts.
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

from PIL import Image, ImageDraw, ImageFont
from studio_diagrams import _font_path, _clip_to_box


COLORS = {'blue': '#8eb8ff', 'amber': '#e6bf72', 'coral': '#ef9b95',
          'purple': '#bea6ed', 'mint': '#87c9bb', 'gray': '#bcc6d4'}
BACKGROUND = '#0b1018'
STATES = ('neutral', 'idle', 'active', 'done')


def _object(properties):
    return {'type': 'object', 'additionalProperties': False, 'properties': properties,
            'required': list(properties)}


def _array(items, maximum, minimum=0):
    return {'type': 'array', 'items': items, 'minItems': minimum, 'maxItems': maximum}


_ID = {'type': 'string', 'minLength': 1, 'maxLength': 32}
_LABEL = {'type': 'string', 'maxLength': 18}
_POSE = _object({'id': _ID, 'x': {'type': 'number', 'minimum': 0, 'maximum': 1},
                 'y': {'type': 'number', 'minimum': 0, 'maximum': 1},
                 'visible': {'type': 'boolean'}, 'label': {'anyOf': [{'type': 'null'}, _LABEL]},
                 'state': {'type': 'string', 'enum': list(STATES)}})
_LINK = _object({'source': _ID, 'target': _ID, 'label': {'type': 'string', 'maxLength': 12}})
MOTION_SCHEMA = _object({
    'title': {'type': 'string', 'maxLength': 36},
    'objects': _array(_object({'id': _ID, 'shape': {'enum': ['circle', 'rect', 'label', 'document', 'photo'], 'type': 'string'},
                               'label': _LABEL, 'color': {'type': 'string', 'enum': list(COLORS)}}), 8, 1),
    'stages': _array(_object({'cue': {'type': 'string', 'maxLength': 80},
                              'poses': _array(_POSE, 8, 1), 'links': _array(_LINK, 12)}), 10, 1)})


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{name} must be a finite number')
    return float(value)


def _text(value, name, maximum):
    if (not isinstance(value, str) or len(value) > maximum
            or any(ord(character) < 32 for character in value)):
        raise ValueError(f'{name} must be text of at most {maximum} characters')
    return value.strip()


def _fields(value, allowed, name):
    if not isinstance(value, dict) or set(value)-set(allowed):
        raise ValueError(f'{name} has unknown fields or is not an object')


def _bounds(obj, pose, width, height, physical=False):
    unit = min(width, height)
    x, y = pose['x']*width, pose['y']*height
    if obj['shape'] == 'circle':
        radius = unit*.037
        if physical:
            return x-radius, y-radius, x+radius, y+radius
        # Reserve two label lines beneath the token, including for later labels.
        return x-unit*.14, y-radius, x+unit*.14, y+unit*.115
    if obj['shape'] in ('document', 'photo'):
        if physical:
            half_width = unit*(.0525 if obj['shape'] == 'document' else .0975)
            # Enlarge upward, preserving the existing label and subtitle space.
            return x-half_width, y-unit*.09, x+half_width, y+unit*.045
        return x-unit*.14, y-unit*.09, x+unit*.14, y+unit*.123
    half_width, half_height = unit*.14, unit*(.065 if obj['shape'] == 'rect' else .05)
    return x-half_width, y-half_height, x+half_width, y+half_height


def _overlap(a, b, margin=0):
    return (min(a[2], b[2])-max(a[0], b[0]) > -margin
            and min(a[3], b[3])-max(a[1], b[1]) > -margin)


def _motion_overlaps(a0, a1, b0, b1):
    """Exact swept AABB test for linear translations, excluding mere touching."""
    lower, upper = 0.0, 1.0
    for axis in (0, 1):
        relative = a0[axis]-b0[axis]
        velocity = (a1[axis]-a0[axis])-(b1[axis]-b0[axis])
        left = -(a0[axis+2]-a0[axis])
        right = b0[axis+2]-b0[axis]
        if not velocity:
            if not left < relative < right:
                return False
        else:
            first, last = sorted(((left-relative)/velocity, (right-relative)/velocity))
            lower, upper = max(lower, first), min(upper, last)
            if lower >= upper:
                return False
    return lower < upper


def validate_motion(spec, narration=None, width=1920, height=1080, check_layout=True):
    """Validate layout and cue order without inventing any audio timestamps."""
    check_layout = check_layout and width is not None and height is not None
    if check_layout and (any(type(n) is not int or n < 320 or n > 3840 or n % 2 for n in (width, height))
            or width*height > 8294400):
        raise ValueError('Motion dimensions must be even, 320–3840 pixels, and at most 4K')
    _fields(spec, ('title', 'objects', 'stages'), 'Motion scene')
    result = copy.deepcopy(spec)
    result['title'] = _text(result.get('title', ''), 'Motion title', 36)
    objects, stages = result.get('objects'), result.get('stages')
    if not isinstance(objects, list) or not 1 <= len(objects) <= 8:
        raise ValueError('Motion scene needs 1–8 objects')
    if not isinstance(stages, list) or not 1 <= len(stages) <= 10:
        raise ValueError('Motion scene needs 1–10 stages')
    if narration is not None and not isinstance(narration, str):
        raise ValueError('Motion narration must be text')
    lookup = {}
    for obj in objects:
        _fields(obj, ('id', 'shape', 'label', 'color'), 'Motion object')
        identifier = _text(obj.get('id'), 'Motion object id', 32)
        if not identifier or identifier in lookup:
            raise ValueError('Motion object IDs must be nonempty and unique')
        obj['id'] = identifier
        obj['label'] = _text(obj.get('label', ''), 'Motion object label', 18)
        if obj.get('shape') not in ('circle', 'rect', 'label', 'document', 'photo'):
            raise ValueError('Motion shape must be circle, rect, label, document or photo')
        if not isinstance(obj.get('color'), str) or obj['color'] not in COLORS:
            raise ValueError('Unknown motion palette color')
        lookup[identifier] = obj
    previous_position, previous_start, previous_poses = -1, -1.0, None
    seen_cues = set()
    for index, stage in enumerate(stages):
        _fields(stage, ('cue', 'poses', 'links', 'start'), 'Motion stage')
        stage['cue'] = _text(stage.get('cue'), 'Motion cue', 80)
        cue = stage['cue']
        if (index == 0 and cue) or (index > 0 and (not cue or cue in seen_cues)):
            raise ValueError('First motion cue must be empty; later cues must be nonempty and unique')
        seen_cues.add(cue)
        if index and narration is not None:
            if narration.count(cue) != 1:
                raise ValueError(f'Motion cue must occur exactly once in narration: {cue}')
            position = narration.index(cue)
            if position <= previous_position:
                raise ValueError('Motion cues must follow narration order')
            previous_position = position
        if 'start' in stage:
            start = _number(stage['start'], 'Motion start')
            if start < 0 or (index == 0 and start != 0) or start <= previous_start:
                raise ValueError('Measured motion starts must begin at zero and increase strictly')
            stage['start'], previous_start = start, start
        poses = stage.get('poses')
        if not isinstance(poses, list) or len(poses) != len(objects):
            raise ValueError('Each motion stage needs exactly one pose per object')
        pose_map = {}
        for pose in poses:
            _fields(pose, ('id', 'x', 'y', 'visible', 'label', 'state'), 'Motion pose')
            identifier = pose.get('id')
            if not isinstance(identifier, str) or identifier not in lookup or identifier in pose_map:
                raise ValueError('Motion poses contain duplicate or unknown object IDs')
            if type(pose.get('visible')) is not bool:
                raise ValueError('Motion visibility must be boolean')
            pose.setdefault('state', 'neutral')
            if not isinstance(pose['state'], str) or pose['state'] not in STATES:
                raise ValueError('Motion state must be neutral, idle, active or done')
            pose['x'], pose['y'] = _number(pose.get('x'), 'Motion x'), _number(pose.get('y'), 'Motion y')
            if not 0 <= pose['x'] <= 1 or not 0 <= pose['y'] <= 1:
                raise ValueError('Motion coordinates must be normalized to 0–1')
            if pose.get('label') is not None:
                pose['label'] = _text(pose['label'], 'Motion pose label', 18)
            else:
                pose['label'] = None
            if check_layout:
                box = _bounds(lookup[identifier], pose, width, height)
                if box[0] < width*.025 or box[2] > width*.975 or box[1] < height*.18 or box[3] > height*.78:
                    raise ValueError(f'Motion object {identifier!r} and its label must fit x=2.5–97.5%, y=18–78%')
            pose_map[identifier] = pose
        visible = [identifier for identifier, pose in pose_map.items() if pose['visible']]
        for i, left in enumerate(visible):
            for right in visible[i+1:]:
                if not check_layout:
                    continue
                a = _bounds(lookup[left], pose_map[left], width, height)
                b = _bounds(lookup[right], pose_map[right], width, height)
                if _overlap(a, b):
                    raise ValueError(f'Motion objects/labels {left!r} and {right!r} overlap; use separate lanes')
                if previous_poses and previous_poses[left]['visible'] and previous_poses[right]['visible']:
                    before_a = _bounds(lookup[left], previous_poses[left], width, height)
                    before_b = _bounds(lookup[right], previous_poses[right], width, height)
                    if _motion_overlaps(before_a, a, before_b, b):
                        raise ValueError(f'Motion paths {left!r} and {right!r} overlap; use separate stages or lanes')
        links = stage.get('links', [])
        if not isinstance(links, list) or len(links) > 12:
            raise ValueError('Motion stage supports at most 12 links')
        seen_links = set()
        for link in links:
            _fields(link, ('source', 'target', 'label'), 'Motion link')
            source, target = link.get('source'), link.get('target')
            if (not isinstance(source, str) or not isinstance(target, str)
                    or source not in visible or target not in visible or source == target):
                raise ValueError('Motion links require two distinct visible object IDs')
            if (source, target) in seen_links:
                raise ValueError('Duplicate motion link')
            seen_links.add((source, target))
            link['label'] = _text(link.get('label', ''), 'Motion link label', 12)
        stage['links'] = links
        previous_poses = pose_map
    return result


def _rgb(color):
    return tuple(int(color[i:i+2], 16) for i in (1, 3, 5))


class _Frames:
    def __init__(self, spec, duration, width, height, font):
        self.spec = validate_motion(spec, width=width, height=height)
        self.duration = _number(duration, 'Motion duration')
        if not 0 < self.duration <= 600:
            raise ValueError('Motion duration must be in (0, 600] seconds')
        if any('start' not in stage for stage in self.spec['stages']):
            raise ValueError('Resolve every motion cue to a measured start before rendering')
        self.starts = [stage['start'] for stage in self.spec['stages']]
        if self.starts[-1] >= self.duration:
            raise ValueError('Motion starts must be before the end of the shot')
        self.width, self.height, self.unit = width, height, min(width, height)
        self.font_path = _font_path(font)
        self.fonts = {}
        self.objects = {obj['id']: obj for obj in self.spec['objects']}

    def _font(self, size):
        size = max(10, round(size))
        if size not in self.fonts:
            self.fonts[size] = ImageFont.truetype(str(self.font_path), size)
        return self.fonts[size]

    def _text(self, draw, text, box, color, size=None, lines=2):
        if not text:
            return
        left, top, right, bottom = box
        for font_size in range(round(size or self.unit*.028), 9, -1):
            font = self._font(font_size)
            rows, row = [], ''
            for character in text:
                if row and draw.textlength(row+character, font=font) > right-left:
                    rows.append(row); row = ''
                row += character
            if row:
                rows.append(row)
            if len(rows) > lines:
                continue
            boxes = [draw.textbbox((0, 0), row, font=font) for row in rows]
            heights = [b[3]-b[1] for b in boxes]
            gap = max(2, round(font_size*.18))
            total = sum(heights)+gap*(len(rows)-1)
            if total > bottom-top:
                continue
            y = (top+bottom-total)/2
            for row, bounds, row_height in zip(rows, boxes, heights):
                x = (left+right-bounds[2]+bounds[0])/2
                draw.text((x-bounds[0], y-bounds[1]), row, font=font, fill=color)
                y += row_height+gap
            return
        raise ValueError('Motion label does not fit; shorten it for this canvas')

    def _endpoint(self, obj, pose, toward):
        box = _bounds(obj, pose, self.width, self.height, physical=True)
        if obj['shape'] != 'circle':
            return _clip_to_box(box, toward)
        center = (pose['x']*self.width, pose['y']*self.height)
        distance = math.dist(center, toward)
        radius = self.unit*.037
        return tuple(center[i]+(toward[i]-center[i])*radius/distance for i in (0, 1)) if distance else center

    def _state_mark(self, draw, obj, pose, elapsed):
        """Qualitative state only: activity is indeterminate, never a percentage."""
        state = pose['state']
        if state == 'neutral':
            return
        left, top, right, bottom = _bounds(obj, pose, self.width, self.height, physical=True)
        radius = self.unit*.012
        if obj['shape'] in ('rect', 'label'):
            x, y = right-self.unit*.022, (top+bottom)/2
        else:
            x, y = right+self.unit*.018, (top+bottom)/2
        stroke = max(2, round(self.unit*.003))
        color = '#e0e9f6'  # Object identity keeps its original palette color.
        # Clear links behind the small marker without covering the object itself.
        draw.ellipse((x-radius-2, y-radius-2, x+radius+2, y+radius+2), fill=BACKGROUND)
        if state == 'idle':
            for offset in (-.005, .005):
                draw.line((x+self.unit*offset, y-radius*.7, x+self.unit*offset, y+radius*.7),
                          fill=color, width=stroke)
        elif state == 'active':
            angle = (max(0, elapsed)/1.8*360) % 360
            draw.arc((x-radius, y-radius, x+radius, y+radius), angle, angle+110,
                     fill=color, width=stroke)
        else:
            draw.line([(x-radius*.85, y), (x-radius*.2, y+radius*.6),
                       (x+radius*.95, y-radius*.75)], fill=color, width=stroke, joint='curve')

    def _link_label_box(self, a, b, occupied):
        distance = max(1, math.dist(a, b))
        normal = (-(b[1]-a[1])/distance, (b[0]-a[0])/distance)
        for fraction in (.5, .3, .7):
            for offset in (-.055, .055, -.105, .105):
                center = (a[0]+(b[0]-a[0])*fraction+normal[0]*self.unit*offset,
                          a[1]+(b[1]-a[1])*fraction+normal[1]*self.unit*offset)
                box = (center[0]-self.unit*.13, center[1]-self.unit*.022,
                       center[0]+self.unit*.13, center[1]+self.unit*.022)
                if (box[0] >= self.width*.025 and box[2] <= self.width*.975
                        and box[1] >= self.height*.18 and box[3] <= self.height*.78
                        and not any(_overlap(box, other, margin=2) for other in occupied)):
                    return box
        raise ValueError('Motion link label has no clear space; shorten links or separate their objects')

    def frame(self, time):
        index = max(0, bisect_right(self.starts, time)-1)
        stage = self.spec['stages'][index]
        poses = {pose['id']: dict(pose) for pose in stage['poses']}
        amount = 1.0
        if index:
            end = self.starts[index+1] if index+1 < len(self.starts) else self.duration
            transition = min(.45, (end-self.starts[index])/2)
            amount = min(1.0, max(0.0, (time-self.starts[index])/transition))
            amount = amount*amount*(3-2*amount)
            previous = {pose['id']: pose for pose in self.spec['stages'][index-1]['poses']}
            for identifier, pose in poses.items():
                if pose['visible'] and previous[identifier]['visible']:
                    for axis in ('x', 'y'):
                        pose[axis] = previous[identifier][axis]+(pose[axis]-previous[identifier][axis])*amount
        image = Image.new('RGB', (self.width, self.height), BACKGROUND)
        draw = ImageDraw.Draw(image)
        self._text(draw, self.spec['title'], (.035*self.width, .055*self.height,
            .965*self.width, .13*self.height), '#e5ecf8', size=self.unit*.04, lines=1)
        occupied = [_bounds(self.objects[identifier], pose, self.width, self.height)
                    for identifier, pose in poses.items() if pose['visible']]
        for link in stage['links']:
            source, target = poses[link['source']], poses[link['target']]
            source_box = _bounds(self.objects[link['source']], source, self.width, self.height, physical=True)
            target_box = _bounds(self.objects[link['target']], target, self.width, self.height, physical=True)
            source_center = ((source_box[0]+source_box[2])/2, (source_box[1]+source_box[3])/2)
            target_center = ((target_box[0]+target_box[2])/2, (target_box[1]+target_box[3])/2)
            a = self._endpoint(self.objects[link['source']], source, target_center)
            b = self._endpoint(self.objects[link['target']], target, source_center)
            color = '#617992'
            draw.line((a, b), fill=color, width=max(2, round(self.unit*.003)))
            angle = math.atan2(b[1]-a[1], b[0]-a[0]); arrow = self.unit*.013
            draw.polygon([b, (b[0]-arrow*math.cos(angle-.45), b[1]-arrow*math.sin(angle-.45)),
                             (b[0]-arrow*math.cos(angle+.45), b[1]-arrow*math.sin(angle+.45))], fill=color)
            if link['label']:
                box = self._link_label_box(a, b, occupied)
                occupied.append(box)
                draw.rectangle(box, fill=BACKGROUND)
                self._text(draw, link['label'], box, '#b8c8db', size=self.unit*.022, lines=1)
        for identifier, pose in poses.items():
            if not pose['visible']:
                continue
            obj = self.objects[identifier]
            box = _bounds(obj, pose, self.width, self.height, physical=True)
            color = COLORS[obj['color']]
            label = obj['label'] if pose['label'] is None else pose['label']
            if obj['shape'] == 'circle':
                draw.ellipse(box, fill=color)
                label_box = _bounds(obj, pose, self.width, self.height)
                label_box = (label_box[0], box[3]+self.unit*.012, label_box[2], label_box[3])
                self._text(draw, label, label_box, '#e0e9f6')
            elif obj['shape'] in ('document', 'photo'):
                left, top, right, bottom = box
                stroke = max(2, round(self.unit*.0025))
                fill = tuple(round(channel*.16) for channel in _rgb(color))
                if obj['shape'] == 'document':
                    fold = self.unit*.0285
                    outline = [(left, top), (right-fold, top), (right, top+fold),
                               (right, bottom), (left, bottom), (left, top)]
                    draw.polygon(outline, fill=fill)
                    draw.line(outline, fill=color, width=stroke, joint='curve')
                    draw.polygon([(right-fold, top), (right-fold, top+fold), (right, top+fold)],
                                 fill=tuple(round(channel*.4) for channel in _rgb(color)))
                    draw.line([(right-fold, top), (right-fold, top+fold), (right, top+fold)],
                              fill=color, width=stroke)
                    for offset, length in ((.42, .62), (.60, .62), (.78, .43)):
                        y = top+(bottom-top)*offset
                        draw.line((left+(right-left)*.17, y, left+(right-left)*(.17+length), y),
                                  fill=color, width=max(1, stroke-1))
                else:
                    draw.rounded_rectangle(box, radius=max(2, round(self.unit*.012)),
                                           fill=fill, outline=color, width=stroke)
                    radius = self.unit*.012
                    sun = (right-self.unit*.0405, top+self.unit*.0375)
                    draw.ellipse((sun[0]-radius, sun[1]-radius, sun[0]+radius, sun[1]+radius), fill=color)
                    mountain = [(left+self.unit*.0165, bottom-self.unit*.0195),
                                (left+self.unit*.057, top+self.unit*.0525),
                                (left+self.unit*.096, top+self.unit*.09),
                                (left+self.unit*.1245, top+self.unit*.0675),
                                (right-self.unit*.015, bottom-self.unit*.0195)]
                    draw.polygon(mountain, fill=tuple(round(channel*.45) for channel in _rgb(color)))
                    draw.line(mountain, fill=color, width=max(1, stroke-1))
                label_box = _bounds(obj, pose, self.width, self.height)
                self._text(draw, label, (label_box[0], bottom+self.unit*.012,
                                       label_box[2], label_box[3]), '#e0e9f6')
            elif obj['shape'] == 'rect':
                fill = tuple(round(channel*.16) for channel in _rgb(color))
                draw.rounded_rectangle(box, radius=max(4, round(self.unit*.018)), fill=fill,
                                       outline=color, width=max(2, round(self.unit*.0025)))
                inset = self.unit*.012
                right = box[2]-(self.unit*.045 if pose['state'] != 'neutral' else inset)
                self._text(draw, label, (box[0]+inset, box[1]+inset, right, box[3]-inset), '#e0e9f6')
            else:
                label_box = (box[0], box[1], box[2]-self.unit*.045, box[3]) if pose['state'] != 'neutral' else box
                self._text(draw, label, label_box, color)
            self._state_mark(draw, obj, pose, time-self.starts[index])
        return image


def render_frame(spec, time, duration, width=1920, height=1080, font=None):
    frames = _Frames(spec, duration, width, height, font)
    time = _number(time, 'Motion preview time')
    if not 0 <= time <= frames.duration:
        raise ValueError('Motion preview time is outside the clip')
    return frames.frame(time)


def render_motion(spec, output, duration, width=1920, height=1080, fps=30, ffmpeg=None, font=None):
    """Encode a silent explanatory scene atomically from measured stages."""
    if type(fps) is not int or not 1 <= fps <= 60:
        raise ValueError('Motion fps must be an integer in 1–60')
    frames = _Frames(spec, duration, width, height, font)
    output = Path(output)
    if output.suffix.lower() != '.mp4':
        raise ValueError('Motion output must be an MP4 file')
    output.parent.mkdir(parents=True, exist_ok=True)
    count = max(1, round(duration*fps))
    if ffmpeg is None:
        from make_video import ffmpeg_path
        ffmpeg = ffmpeg_path()
    with tempfile.TemporaryDirectory(prefix='motion-', dir=output.parent) as folder:
        temporary = Path(folder)/'motion.mp4'
        with tempfile.TemporaryFile() as error:
            process = subprocess.Popen([str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-y',
                '-f', 'rawvideo', '-pixel_format', 'rgb24', '-video_size', f'{width}x{height}',
                '-framerate', str(fps), '-i', 'pipe:0', '-an', '-c:v', 'libx264', '-preset',
                'veryfast', '-crf', '18', '-threads', '4', '-pix_fmt', 'yuv420p',
                '-movflags', '+faststart', str(temporary)], stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=error, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                for index in range(count):
                    process.stdin.write(frames.frame(index/fps).tobytes())
                process.stdin.close(); code = process.wait()
            except BaseException:
                if process.stdin and not process.stdin.closed:
                    try:
                        process.stdin.close()
                    except BrokenPipeError:
                        pass
                if process.poll() is None:
                    process.kill()
                process.wait(); raise
            if code:
                error.seek(0)
                raise RuntimeError('Motion encoding failed: '+error.read().decode('utf-8', 'replace')[-2000:])
        os.replace(temporary, output)
    digest = hashlib.sha256(json.dumps(frames.spec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {'file': str(output), 'duration_seconds': count/fps, 'frames': count, 'fps': fps,
            'resolution': [width, height], 'motion_sha256': digest,
            'provenance': 'generated explanatory object motion; positions, paths and speed are illustrative, not measured physical trajectories, latency or experimental results'}
