"""Render cue-timed explanatory graphs, independent of subject or model provider.

Coordinates are node centres in normalized canvas space. Edges use stable IDs,
source/target node IDs, and optional normalized ``via`` waypoints for feedback
paths. Stages reveal nodes and highlight paths at measured local speech times.
Moving markers depict information flow only, not measured physical trajectories.
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

BACKGROUND = '#080e15'
ACCENT = '#65dfc8'


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{name} must be a finite number')
    return float(value)


def _text(value, name, maximum):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 and c != '\n' for c in value):
        raise ValueError(f'{name} must be text of at most {maximum} characters')
    return value.strip()


def _box(node, width, height):
    cx, cy = node['x'] * width, node['y'] * height
    half_w, half_h = width * .105, height * .08
    return (cx-half_w, cy-half_h, cx+half_w, cy+half_h)


def validate_graph(spec, duration, width=1920, height=1080):
    """Return a normalized copy, never mutate the caller's graph.

    Cue-only stages are accepted for planning validation but remain unresolved
    in the returned copy: ``render_diagram`` requires explicit measured starts.
    """
    duration = _number(duration, 'duration')
    if not 0 < duration <= 600:
        raise ValueError('Diagram duration must be in (0, 600] seconds')
    if any(type(n) is not int or n < 320 or n > 3840 or n % 2 for n in (width, height)) or width*height > 8294400:
        raise ValueError('Diagram dimensions must be even, 320–3840 pixels, and at most 4K')
    if not isinstance(spec, dict):
        raise ValueError('Diagram must be an object')
    result = copy.deepcopy(spec)
    result['title'] = _text(spec.get('title', ''), 'title', 48)
    result['note'] = _text(spec.get('note', '流程示意'), 'note', 24)
    nodes, edges, stages = result.get('nodes'), result.get('edges', []), result.get('stages')
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 12:
        raise ValueError('Diagram needs 1–12 nodes')
    if not isinstance(edges, list) or len(edges) > 32:
        raise ValueError('Diagram supports at most 32 edges')
    if not isinstance(stages, list) or not 1 <= len(stages) <= 24:
        raise ValueError('Diagram needs 1–24 stages')
    node_ids, edge_ids = set(), set()
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError('Each node must be an object')
        identifier = _text(node.get('id'), 'node id', 48)
        if not identifier or identifier in node_ids:
            raise ValueError('Node IDs must be nonempty and unique')
        node['id'] = identifier
        node_ids.add(identifier)
        node['label'] = _text(node.get('label'), 'node label', 32)
        node['detail'] = _text(node.get('detail', ''), 'node detail', 26)
        if not node['label']:
            raise ValueError('Node labels cannot be empty')
        node['x'], node['y'] = _number(node.get('x'), 'node x'), _number(node.get('y'), 'node y')
        x0,y0,x1,y1 = _box(node,width,height)
        if x0 < width*.025 or x1 > width*.975 or y0 < height*.18 or y1 > height*.78:
            raise ValueError('Node boxes must stay within x=2.5–97.5%, y=18–78%; use centres x=.13–.87, y=.26–.70')
    for edge in edges:
        if not isinstance(edge, dict):
            raise ValueError('Each edge must be an object')
        identifier = _text(edge.get('id'), 'edge id', 48)
        if not identifier or identifier in edge_ids:
            raise ValueError('Edge IDs must be nonempty and unique')
        edge['id'] = identifier
        edge_ids.add(identifier)
        if not isinstance(edge.get('source'),str) or not isinstance(edge.get('target'),str) or edge['source'] not in node_ids or edge['target'] not in node_ids:
            raise ValueError('Edge source and target must name existing nodes')
        edge['label'] = _text(edge.get('label', ''), 'edge label', 24)
        via = edge.get('via', [])
        if not isinstance(via, list) or len(via) > 8:
            raise ValueError(f'Edge {identifier!r} via supports at most eight waypoints')
        for point_index,point in enumerate(via):
            location=f'Edge {identifier!r} via[{point_index}]'
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError(f'{location}={point!r} must be an [x, y] pair')
            x,y = _number(point[0], location+' x'), _number(point[1], location+' y')
            if not .025 <= x <= .975 or not .18 <= y <= .78:
                raise ValueError(f'{location}={point!r} is outside the diagram safe area; '
                                 'allowed x=[0.025, 0.975], y=[0.18, 0.78], inclusive')
        if edge['source'] == edge['target'] and len(via) < 2:
            raise ValueError('Self-loop edges need at least two via waypoints')
    previous = -1.0
    for i,stage in enumerate(stages):
        if not isinstance(stage, dict):
            raise ValueError('Each stage must be an object')
        if 'start' not in stage and not isinstance(stage.get('cue'), str):
            raise ValueError('A stage needs measured start or a narration cue')
        start = _number(stage.get('start', duration*i/len(stages)), 'stage start')
        if start < 0 or start >= duration or start <= previous or (i == 0 and start != 0):
            raise ValueError('Stage starts must begin at zero and increase strictly before duration')
        previous = start
        if 'start' in stage:stage['start']=start
        for field, valid in [('active_nodes',node_ids),('visible_nodes',node_ids),('active_edges',edge_ids)]:
            values = stage.get(field, [n['id'] for n in nodes] if field == 'visible_nodes' else [])
            if not isinstance(values, list) or any(not isinstance(v,str) for v in values) or len(values) != len(set(values)) or any(v not in valid for v in values):
                raise ValueError(f'{field} contains duplicate or unknown IDs')
            stage[field] = values
        if not set(stage['active_nodes']) <= set(stage['visible_nodes']):
            raise ValueError('An active node must also be visible')
        for edge in edges:
            if edge['id'] in stage['active_edges'] and not {edge['source'],edge['target']} <= set(stage['visible_nodes']):
                raise ValueError('Active edges require both endpoint nodes to be visible')
        visible=[n for n in nodes if n['id'] in stage['visible_nodes']]
        for j,left in enumerate(visible):
            a=_box(left,width,height)
            for right in visible[j+1:]:
                b=_box(right,width,height)
                if min(a[2],b[2]) > max(a[0],b[0]) and min(a[3],b[3]) > max(a[1],b[1]):
                    raise ValueError('Visible diagram nodes overlap; adjust their centres')
    result['edges'] = edges
    return result


def _font_path(supplied=None):
    candidates = [Path(supplied)] if supplied else []
    candidates += [Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts/msyh.ttc',
        Path('/System/Library/Fonts/PingFang.ttc'),
        Path('/System/Library/Fonts/STHeiti Light.ttc'),
        Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'),
        Path('/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc'),
        Path('/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf')]
    for path in candidates:
        if path.is_file():return path
    raise ValueError('No font found. Pass a Chinese font path or install Noto CJK.')


def _clip_to_box(box, point):
    cx,cy=(box[0]+box[2])/2,(box[1]+box[3])/2
    dx,dy=point[0]-cx,point[1]-cy
    if not dx and not dy:return cx,cy
    scale=min((box[2]-box[0])/2/abs(dx) if dx else math.inf,
              (box[3]-box[1])/2/abs(dy) if dy else math.inf)
    return cx+dx*scale,cy+dy*scale


def _segment_crosses_box(start,end,box):
    """Whether the open segment enters a box, excluding a tangent touch."""
    lower,upper=0.0,1.0
    for axis in (0,1):
        delta=end[axis]-start[axis]
        if not delta:
            if not box[axis]<start[axis]<box[axis+2]:return False
            continue
        first,last=sorted(((box[axis]-start[axis])/delta,(box[axis+2]-start[axis])/delta))
        lower,upper=max(lower,first),min(upper,last)
        if lower>=upper:return False
    return lower<upper


def _point_on_path(points, amount):
    segments=[math.dist(a,b) for a,b in zip(points,points[1:])]
    distance=sum(segments)*max(0,min(1,amount))
    for a,b,length in zip(points,points[1:],segments):
        if length and distance <= length:
            f=distance/length
            return a[0]+f*(b[0]-a[0]),a[1]+f*(b[1]-a[1])
        distance-=length
    return points[-1]


class _Frames:
    def __init__(self,spec,duration,width,height,font):
        self.spec=validate_graph(spec,duration,width,height)
        self.duration,self.width,self.height=duration,width,height
        self.unit=min(width,height)
        self.font_path=_font_path(font)
        self.boxes={n['id']:_box(n,width,height) for n in self.spec['nodes']}
        self.paths={}
        pairs={(edge['source'],edge['target']) for edge in self.spec['edges']}
        for edge in self.spec['edges']:
            source,target=self.boxes[edge['source']],self.boxes[edge['target']]
            via=[(p[0]*width,p[1]*height) for p in edge.get('via',[])]
            # Leftward direction alone does not mean feedback: C -> B -> A is
            # a serial chain. Route only around an opposing edge or a node
            # blocking the direct path, preserving explicit author waypoints.
            if not via and source[0]>target[0] and abs(source[1]-target[1])<height*.025:
                begin=((source[0]+source[2])/2,(source[1]+source[3])/2)
                end=((target[0]+target[2])/2,(target[1]+target[3])/2)
                reverse=(edge['target'],edge['source']) in pairs
                blocked=any(_segment_crosses_box(begin,end,box) for identifier,box in self.boxes.items()
                            if identifier not in (edge['source'],edge['target']))
                if reverse or blocked:
                    top=min(source[1],target[1]);bottom=max(source[3],target[3])
                    lane=bottom+height*.065 if bottom+height*.065<=height*.76 else top-height*.065
                    if lane<height*.18:raise ValueError('Return edge needs explicit via waypoints in a free lane')
                    via=[(begin[0],lane),(end[0],lane)]
            centres=[((source[0]+source[2])/2,(source[1]+source[3])/2),
                *via,
                ((target[0]+target[2])/2,(target[1]+target[3])/2)]
            centres[0]=_clip_to_box(source,centres[1])
            centres[-1]=_clip_to_box(target,centres[-2])
            self.paths[edge['id']]=centres
        # Place labels once against all boxes, so later node reveals cannot
        # cover words or cause labels to jump between stages.
        draw=ImageDraw.Draw(Image.new('RGB',(1,1)))
        self.edge_labels={}
        for edge in self.spec['edges']:
            if edge['label']:self.edge_labels[edge['id']]=self.label_layout(edge,draw)
        self.starts=[s['start'] for s in self.spec['stages']]
        self.backgrounds=[self.background(stage) for stage in self.spec['stages']]

    def font(self,size):
        return ImageFont.truetype(str(self.font_path),max(12,int(size)))

    def label_layout(self,edge,draw):
        middle=_point_on_path(self.paths[edge['id']],.5)
        font=self.font(self.unit*.026)
        tw=draw.textlength(edge['label'],font=font)
        half=tw/2+self.unit*.009;label_height=self.unit*.036
        cx=max(self.width*.025+half,min(middle[0],self.width*.975-half))
        candidates=[middle[1]-self.unit*.048]
        relevant=[box for box in self.boxes.values() if box[0]<cx+half and box[2]>cx-half]
        if relevant:
            candidates.extend([min(box[1] for box in relevant)-label_height-self.unit*.012,
                               max(box[3] for box in relevant)+self.unit*.012])
        def overlaps(a,b):return min(a[2],b[2])>max(a[0],b[0]) and min(a[3],b[3])>max(a[1],b[1])
        for top in candidates:
            box=(cx-half,top,cx+half,top+label_height)
            if box[0]<self.width*.025 or box[2]>self.width*.975 or top<self.height*.18 or box[3]>self.height*.78:continue
            if any(overlaps(box,other) for other in [*self.boxes.values(),*[entry[0] for entry in self.edge_labels.values()]]):continue
            return box,(cx,top+self.unit*.006)
        raise ValueError(f'Edge label {edge["id"]!r} has no free space; shorten it, spread nodes, or route via a free lane')

    def background(self,stage):
        width,height=self.width,self.height
        image=Image.new('RGB',(width,height),BACKGROUND)
        draw=ImageDraw.Draw(image)
        # Header stays below the application's initial brand overlay.
        if self.spec['title']:
            font=self.font(self.unit*.038)
            if draw.textlength(self.spec['title'],font=font)>width*.76:
                raise ValueError('Diagram title is too long for the selected canvas')
            draw.text((width*.05,height*.12),self.spec['title'],fill='#edf5f4',font=font,anchor='lt')
        if self.spec['note']:
            draw.text((width*.95,height*.12),self.spec['note'],fill='#8ca8ac',font=self.font(self.unit*.023),anchor='rt')
        visible=set(stage['visible_nodes'])
        for edge in self.spec['edges']:
            if not {edge['source'],edge['target']} <= visible:continue
            points=self.paths[edge['id']]
            active=edge['id'] in stage['active_edges']
            color=ACCENT if active else '#34515a'
            thickness=max(2,round(self.unit*(.004 if active else .0025)))
            draw.line(points,fill=color,width=thickness,joint='curve')
            tip=points[-1];prev=points[-2]
            angle=math.atan2(tip[1]-prev[1],tip[0]-prev[0]);length=self.unit*.017
            draw.polygon([tip,(tip[0]-length*math.cos(angle-.45),tip[1]-length*math.sin(angle-.45)),
                (tip[0]-length*math.cos(angle+.45),tip[1]-length*math.sin(angle+.45))],fill=color)
            if edge['label']:
                font=self.font(self.unit*.026)
                # Labels sit above their path and use a solid backing for contrast.
                box,position=self.edge_labels[edge['id']]
                draw.rounded_rectangle(box,radius=5,fill=BACKGROUND)
                draw.text(position,edge['label'],fill=color,font=font,anchor='mt')
        from studio_render import card_lines
        for node in self.spec['nodes']:
            if node['id'] not in visible:continue
            box=self.boxes[node['id']];active=node['id'] in stage['active_nodes']
            border=ACCENT if active else '#34515a'
            draw.rounded_rectangle(box,radius=self.unit*.02,fill='#143139' if active else '#10212a',
                outline=border,width=max(2,round(self.unit*(.0035 if active else .002))))
            available=box[2]-box[0]-self.unit*.042
            maximum=box[3]-box[1]-self.unit*.033
            detailfont=self.font(self.unit*.024)
            details=card_lines(node['detail'],draw,detailfont,available) if node['detail'] else []
            for size in range(round(self.unit*.041),max(13,round(self.unit*.025))-1,-1):
                font=self.font(size);lines=card_lines(node['label'],draw,font,available)
                total=len(lines)*size*1.25+(len(details)*self.unit*.031+self.unit*.012 if details else 0)
                if len(lines)<=3 and total<=maximum:break
            else:raise ValueError('Node text does not fit; shorten its label/detail')
            y=(box[1]+box[3]-total)/2
            for line in lines:
                draw.text(((box[0]+box[2])/2,y),line,fill='#f5faf9' if active else '#c3d2d5',font=font,anchor='mt')
                y+=size*1.25
            if details:y+=self.unit*.012
            for line in details:
                draw.text(((box[0]+box[2])/2,y),line,fill='#9bb6bb',font=detailfont,anchor='mt');y+=self.unit*.031
        return image

    def frame(self,time):
        index=max(0,min(len(self.starts)-1,bisect_right(self.starts,time)-1))
        local=max(0,time-self.starts[index])
        image=self.backgrounds[index].copy()
        if index and local<.18:
            image=Image.blend(self.backgrounds[index-1],image,local/.18)
        draw=ImageDraw.Draw(image)
        for edge_id in self.spec['stages'][index]['active_edges']:
            point=_point_on_path(self.paths[edge_id],(local/1.65)%1)
            radius=max(3,self.unit*.006)
            for factor,color in [(2.3,'#245c5b'),(1.25,ACCENT),(.50,'#effffa')]:
                r=radius*factor
                draw.ellipse((point[0]-r,point[1]-r,point[0]+r,point[1]+r),fill=color)
        return image


def render_frame(spec,time,duration,width=1920,height=1080,font=None):
    """Preview a graph at a known local time; return a PIL RGB image."""
    _require_measured_stages(spec)
    time=_number(time,'time')
    if not 0 <= time <= duration:raise ValueError('Preview time is outside the clip')
    return _Frames(spec,duration,width,height,font).frame(time)


def _require_measured_stages(spec):
    if not isinstance(spec,dict) or not isinstance(spec.get('stages'),list) or any('start' not in s for s in spec['stages']):
        raise ValueError('Resolve every narration cue to a measured stage start before rendering')


def render_diagram(spec,output,duration,width=1920,height=1080,fps=30,ffmpeg=None,font=None):
    """Write a silent MP4 atomically; timestamps must come from actual audio.

    The result gives encoded duration (frame-aligned), graph fingerprint, and
    provenance. It is not an assertion of scientific or editorial correctness.
    """
    _require_measured_stages(spec)
    if type(fps) is not int or not 1 <= fps <= 60:raise ValueError('fps must be an integer in 1–60')
    frames=_Frames(spec,duration,width,height,font)
    count=max(1,round(duration*fps))
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True)
    if output.suffix.lower()!='.mp4':raise ValueError('Diagram output must be an MP4 file')
    if ffmpeg is None:
        from make_video import ffmpeg_path
        ffmpeg=ffmpeg_path()
    with tempfile.TemporaryDirectory(prefix='diagram-',dir=output.parent) as folder:
        temporary=Path(folder)/'diagram.mp4'
        with tempfile.TemporaryFile() as error:
            process=subprocess.Popen([str(ffmpeg),'-hide_banner','-loglevel','error','-y',
                '-f','rawvideo','-pixel_format','rgb24','-video_size',f'{width}x{height}',
                '-framerate',str(fps),'-i','pipe:0','-an','-c:v','libx264','-preset','veryfast',
                '-crf','18','-threads','4','-pix_fmt','yuv420p','-movflags','+faststart',str(temporary)],
                stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=error,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                for index in range(count):process.stdin.write(frames.frame(index/fps).tobytes())
                process.stdin.close();code=process.wait()
            except BaseException:
                if process.stdin and not process.stdin.closed:
                    try:process.stdin.close()
                    except BrokenPipeError:pass
                if process.poll() is None:process.kill()
                process.wait();raise
            if code:
                error.seek(0)
                raise RuntimeError('Diagram encoding failed: '+error.read().decode('utf-8','replace')[-2000:])
        os.replace(temporary,output)
    digest=hashlib.sha256(json.dumps(frames.spec,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return {'file':str(output),'duration_seconds':count/fps,'frames':count,'fps':fps,
            'resolution':[width,height],'graph_sha256':digest,
            'provenance':'generated explanatory diagram; animated markers indicate information flow, not measured physical motion'}
