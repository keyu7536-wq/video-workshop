"""Responsive editorial layouts, original figures and deliberate source changes."""
from pathlib import Path
import math
import re

def topic_label(text):
    """Drop editorial scaffolding from labels, never from narration/subtitles."""
    text=str(text).strip()
    scaffolding=r'(?:先讲清|核心想法|先看结论|关键结论|最后总结)'
    if re.fullmatch(scaffolding+r'[:：]?',text) or text=='这篇论文真正推进了什么':return ''
    return re.sub(r'^'+scaffolding+r'\s*[:：]\s*','',text)


def card_lines(text, draw, font, max_width):
    """Wrap by measured pixels; keep numeric quantities and Latin words intact."""
    lines=[]
    for paragraph in text.splitlines():
        tokens=re.findall(r'[A-Za-z0-9]+(?:[._:/-][A-Za-z0-9]+)*(?:%|×)?(?:小时|分钟|秒|次|段|倍|张|台)?|.',paragraph)
        line=''
        for token in tokens:
            if line and draw.textlength(line+token,font=font)>max_width:
                lines.append(line.rstrip());line=token.lstrip()
            else:line+=token
        lines.append(line.rstrip())
    return lines or ['']


def layout(width, height, layout_style='editorial'):
    portrait = height > width
    if layout_style=='immersive':
        unit=min(width,height)
        return {'margin':int(width*.04),'brand':int(height*.025),'chapter':int(height*.085),
                'media':(0,0,width,height),'label':int(height*.79),'source':int(height*.94),
                'voice_bottom':int(height*.09),'brand_size':int(unit*.040),'chapter_size':int(unit*.032),
                'voice_size':int(unit*.050),'small':int(unit*.022),'caption_chars':18 if portrait else 32}
    if portrait:
        return {'margin': int(width * .05), 'brand': int(height * .10), 'chapter': int(height * .175),
                'media': (int(width * .035), int(height * .27), int(width * .93), int(height * .40)),
                'label': int(height * .72), 'source': int(height * .77), 'voice_bottom': int(height * .12),
                'brand_size': int(width * .061), 'chapter_size': int(width * .042), 'voice_size': int(width * .051),
                'small': int(width * .026), 'caption_chars': 18}
    return {'margin': int(width * .035), 'brand': int(height * .045), 'chapter': int(height * .12),
            'media': (int(width * .035), int(height * .20), int(width * .93), int(height * .57)),
            'label': int(height * .79), 'source': int(height * .91), 'voice_bottom': int(height * .09),
            'brand_size': int(height * .046), 'chapter_size': int(height * .031), 'voice_size': int(height * .039),
            'small': int(height * .022), 'caption_chars': 32}


def card(path, title, text, source='', size=(1400, 900)):
    from PIL import Image, ImageDraw, ImageFont
    width, height = size
    image = Image.new('RGB', size, '#10232c')
    draw = ImageDraw.Draw(image)
    candidates = ['C:/Windows/Fonts/msyh.ttc', '/System/Library/Fonts/PingFang.ttc', '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc']
    font_file = next((p for p in candidates if Path(p).is_file()), None)
    if not font_file:
        raise RuntimeError('没有找到中文字体，请安装中文字体后重试。')
    title_font = ImageFont.truetype(font_file, int(width * .044))
    body_font = ImageFont.truetype(font_file, int(width * .034))
    note_font = ImageFont.truetype(font_file, int(width * .020))
    draw.rounded_rectangle((2, 2, width - 3, height - 3), radius=24, outline='#37545e', width=3)
    draw.rounded_rectangle((64, 65, 76, 118), radius=4, fill='#54d6bd')
    draw.text((105, 65), title[:24], font=title_font, fill='#65dfc8')
    # This is a labeled explanation card, never an invented experiment chart.
    for font_size in range(int(width*.034),25,-1):
        body_font=ImageFont.truetype(font_file,font_size)
        lines=card_lines(text,draw,body_font,width-144)
        if len(lines)*font_size*1.4<=height-310 and all(draw.textlength(line,font=body_font)<=width-144 for line in lines):break
    else:raise ValueError('讲解卡片文字过多，请缩短文字或拆成两个镜头。')
    y = 190
    spacing = min(82, (height - 310) / max(1, len(lines)))
    for line in lines:
        draw.text((72, y), line, font=body_font, fill='#f4f8f9')
        y += spacing
    if source:draw.text((72, height - 72), source[:54], font=note_font, fill='#9eb6bf')
    image.save(path)


def media_geometry(width,height,config,protect_content=False):
    if config.get('layout_style')=='immersive' and protect_content:
        # Paper axes, titles and legends must not sit under narration overlays.
        # Narration clauses can wrap to two lines. Reserve their actual style
        # size, line leading and outline rather than assuming one line.
        box=layout(width,height,'immersive')
        outline=max(2,round(min(width,height)*.004))
        safe_bottom=height-box['voice_bottom']-math.ceil(2*box['voice_size']*1.35)-2*outline-int(height*.02)
        top=int(height*.06)
        return (int(width*.025),top,int(width*.95),max(2,safe_bottom-top))
    return layout(width,height,config.get('layout_style','editorial'))['media']


def write_subtitles(output, work, font, title, shots, phrases, total, width, height, config=None):
    from make_video import ts_ass, ts_srt, clean_ass
    config=config or {}
    immersive=config.get('layout_style')=='immersive'
    box = layout(width, height,config.get('layout_style','editorial'))
    styles = []
    def style(name, size, color, align=7, top=0, bottom=0):
        border=3 if immersive and name=='Voice' else 1
        outline=max(2,round(min(width,height)*.004)) if border==3 else (2 if immersive else 1)
        outline_color='&H8010181C' if border==3 else '&H0010181C'
        styles.append(f'Style: {name},{font},{size},{color},&H00FFFFFF,{outline_color},&H8010181C,-1,0,0,0,100,100,0,0,{border},{outline},0,{align},{box["margin"]},{box["margin"]},{bottom or top},1')
    style('Brand', box['brand_size'], '&H00FFFFFF', top=box['brand'])
    style('Chapter', box['chapter_size'], '&H00BDE554', top=box['chapter'])
    style('IntroChapter', box['chapter_size'], '&H00BDE554', align=9, top=box['brand'])
    style('Label', int(box['small'] * 1.15), '&H00FFFFFF', align=8, top=box['label'])
    style('Source', max(16, int(box['small'] * .85)), '&H00C6B7AB', align=8, top=box['source'])
    style('Voice', box['voice_size'], '&H00FFFFFF', align=2, bottom=box['voice_bottom'])
    style('Foot', max(15, int(box['small'] * .72)), '&H009D948A', align=2, bottom=int(height * .026))
    header = f'''[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
''' + '\n'.join(styles) + '''\n[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    events = []
    def add(start, end, name, text):
        events.append(f'Dialogue: 0,{ts_ass(start)},{ts_ass(end)},{name},,0,0,0,,{clean_ass(text)}')
    if not immersive:add(0, total, 'Brand', title)
    if config.get('show_disclosure',True):add(0, total, 'Foot', 'AI 辅助制作 · 原始素材与来源已随项目保存')
    for index,shot in enumerate(shots):
        protected=immersive and shot.get('protect_content',False)
        if immersive and index==0 and not protected:add(shot['start'],min(shot['end'],shot['start']+8),'Brand',title)
        chapter=shot.get('chapter', '')
        # Existing saved plans may contain editorial prompts as chapter prefixes.
        chapter=topic_label(chapter)
        if not protected and not shot.get('diagram'):add(shot['start'], shot['end'], 'IntroChapter' if immersive and index==0 else 'Chapter', chapter)
        if not immersive:add(shot['start'], shot['end'], 'Label', shot.get('label', ''))
        if config.get('show_sources',True):add(shot['start'], shot['end'], 'Source', shot.get('source', '')[:70])
    for start, end, text in phrases:
        lines = caption_lines(text,box['caption_chars'])
        add(start, end, 'Voice', '\n'.join(lines))
    (work / 'captions.ass').write_text(header + '\n'.join(events), encoding='utf-8')
    (output / 'subtitles.srt').write_text('\n\n'.join(f'{i}\n{ts_srt(st)} --> {ts_srt(en)}\n{text}' for i, (st, en, text) in enumerate(phrases, 1)) + '\n', encoding='utf-8')


def caption_lines(text,limit=18):
    if len(text)<=limit:return [text]
    if limit==18:
        from make_video import wrap_caption
        return wrap_caption(text).split('\n')
    count=math.ceil(len(text)/limit);width=math.ceil(len(text)/count)
    lines=[]
    while len(text)>limit:
        cut=width
        while cut<len(text) and text[cut] in '，。！？：；、,.;!?':cut+=1
        lines.append(text[:cut]);text=text[cut:]
    if text:lines.append(text)
    return lines


def render_shot(index, shot, root, work, ffmpeg, fps, width, height, config=None):
    from make_video import run, log
    target = work / f'shot_{index:03}.mp4'
    source = (root / shot['file']).resolve()
    seconds = shot['render_duration']
    frames = round(seconds * fps)
    config=config or {}
    x, y, media_width, media_height = media_geometry(width,height,config,shot.get('protect_content',False))
    if shot.get('diagram'):x,y,media_width,media_height=0,0,width,height
    media_width-=media_width%2;media_height-=media_height%2
    cover=config.get('media_fit')=='cover' and not shot.get('protect_content',False) and not shot.get('diagram')
    args = [ffmpeg, '-hide_banner', '-v', 'error', '-y']
    if shot['kind'] == 'image':
        args += ['-loop', '1', '-framerate', str(fps)]
    else:
        args += ['-ss', str(shot.get('in', 0)), '-t', str(shot['duration'])]
    args += ['-i', str(source)]
    fit=(f'scale={media_width}:{media_height}:force_original_aspect_ratio=increase:force_divisible_by=2,crop={media_width}:{media_height}' if cover else f'scale={media_width}:{media_height}:force_original_aspect_ratio=decrease:force_divisible_by=2')
    vf = f'setpts=PTS-STARTPTS,{fit},pad={width}:{height}:{x}+({media_width}-iw)/2:{y}+({media_height}-ih)/2:color=0x080e15,setsar=1,fps={fps}'
    # Insufficient video spans are resolved before encoding; never freeze footage.
    if shot['kind'] == 'image' and shot.get('focus'):
        from PIL import Image
        with Image.open(source) as image: source_width,source_height=image.size
        scale=(max if cover else min)(media_width/source_width,media_height/source_height)
        iw,ih=source_width*scale,source_height*scale
        fx0,fy0,fx1,fy1=shot['focus']
        bx=x+(media_width-iw)/2+fx0*iw;by=y+(media_height-ih)/2+fy0*ih
        right=min(x+media_width,bx+(fx1-fx0)*iw);bottom=min(y+media_height,by+(fy1-fy0)*ih)
        bx=max(x,bx);by=max(y,by)
        if right>bx and bottom>by:vf+=f',drawbox=x={bx:.1f}:y={by:.1f}:w={right-bx:.1f}:h={bottom-by:.1f}:color=0x54d6bd:t={max(3,width//250)}'
    args += ['-vf', vf, '-frames:v', str(frames), '-an', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '19',
             '-threads', '2', '-pix_fmt', 'yuv420p', str(target)]
    run(args)
    log(f'  镜头 {index + 1:02} 已完成（{seconds:.2f} 秒）')
    return target


def choose_shots(block,duration,words,fps):
    from make_video import cue_time
    shots=[dict(shot) for shot in block['shots']]
    minimum=[shot['duration'] if shot['kind']=='video' and shot.get('keep_full') else min(1.2,shot['duration']) for shot in shots]
    total_weight=sum(shot['duration'] for shot in shots)
    edges=[0.0];weight=0.0
    for index,shot in enumerate(shots[1:],1):
        weight+=shots[index-1]['duration']
        desired=cue_time(shot['cue'],block['narration'],words) if shot.get('cue') else duration*weight/total_weight
        earliest=edges[-1]+minimum[index-1];latest=duration-sum(minimum[index:])
        # A spoken cue is a real boundary, not a hint that can move several
        # seconds earlier to squeeze in footage. Infeasible complete clips are
        # sent to the host for bounded replanning below.
        edge=desired if shot.get('cue') else max(earliest,min(desired,latest))
        edges.append(round(edge*fps)/fps)
    edges.append(duration)
    issues=[]
    for index,(shot,start,end) in enumerate(zip(shots,edges,edges[1:])):
        required=round(shot['duration']*fps)/fps
        if shot['kind']=='video' and shot.get('keep_full') and end-start+1e-7<required:
            expected=cue_time(shot['cue'],block['narration'],words) if shot.get('cue') else start
            issues.append({'block_id':block.get('id'),'shot_index':index,
                'reason':'complete_clip_exceeds_cue_window','time_basis':'block_local',
                'cue':shot.get('cue',''),'expected_cue_time':expected,
                'required_source_duration':shot['duration'],'available_cue_window':max(0,end-start),
                'required_seconds':required,'available_seconds':max(0,end-start),
                'missing_seconds':round(required-max(0,end-start),6),
                'start':start,'end':end,'file':shot.get('file','')})
    if issues:
        raise NeedsMoreVisualsError(issues)
    for index,(shot,start,end) in enumerate(zip(shots,edges,edges[1:])):
        if end<=start:raise RuntimeError('镜头时长不足，请调整完整演示时长。')
        shot.update(local_start=start,local_end=end,render_duration=end-start,
                    block_id=block.get('id'),shot_index=index)
    return shots


class NeedsMoreVisualsError(ValueError):
    """Narration is intact, but selected visuals cannot cover its measured span."""
    def __init__(self, issues, adjustments=None):
        self.issues = issues
        self.adjustments = adjustments or []
        self.diagnostic = {'status':'needs_more_visuals',
                           'code':'insufficient_visual_duration',
                           'issues':issues,'adjustments':self.adjustments}
        labels = ', '.join(str(i.get('block_id') or i.get('chapter') or '?') for i in issues)
        detail = ('；动态图阶段的旁白提示必须落在本镜头内并按顺序出现'
                  if any(i.get('reason','').startswith('diagram_cue') for i in issues) else '')
        super().__init__('需补镜头：演示素材或切点无法覆盖实际讲解，请增加相关镜头或重新分镜：'+labels+detail)


def replace_video_holds(shots, root, work, fps, minimal_text=False):
    """Cover excess speech with an already selected same-block static visual.

    Keep all original cue boundaries and complete source ranges. Generated
    diagrams have their own measured duration; they are never replayed or frozen
    as substitute footage. ``minimal_text`` remains accepted for caller
    compatibility; neither mode invents an explanation card.
    """
    result, adjustments, issues = [], [], []
    for global_index,shot in enumerate(shots):
        available = round(shot.get('duration',0) * fps) / fps
        if shot['kind'] != 'video' or shot['render_duration'] <= available + 1e-7:
            result.append(shot)
            continue
        start = shot.get('start',0.0)
        end = shot.get('end',start + shot['render_duration'])
        missing = shot['render_duration'] - available
        block_id = shot.get('block_id')
        candidates = [(index,other) for index,other in enumerate(shots)
                      if block_id is not None and other.get('block_id') == block_id
                      and other['kind'] == 'image' and other.get('file')
                      and (Path(root) / other['file']).is_file()]
        issue = {'block_id':block_id,'shot_index':shot.get('shot_index',global_index),
                 'chapter':shot.get('chapter',''),'file':shot.get('file',''),
                 'time_basis':'video',
                 'start':start,'end':end,'available_seconds':available,
                 'required_seconds':shot['render_duration'],'missing_seconds':round(missing,6),
                 'cue':shot.get('cue','')}
        if not candidates or available <= 0 or shot.get('diagram'):
            issue['reason'] = ('generated_diagram_duration_mismatch' if shot.get('diagram')
                               else 'no_selected_same_block_static_visual')
            issues.append(issue)
            result.append(shot)
            continue
        donor_index,donor = min(candidates,key=lambda item:(abs(item[0]-global_index),
                                                           item[0]>global_index,item[0]))
        boundary = start + available
        local_start = shot.get('local_start',0.0)
        first = dict(shot,render_duration=available,end=boundary,
                     local_end=local_start+available)
        adjustment = {**issue,'type':'same_block_static_continuation',
                      'continuation_start':boundary,'continuation_end':end,
                      'visual_shot_index':donor.get('shot_index',donor_index),
                      'visual_file':donor['file'],'visual_source':donor.get('source','')}
        rest = dict(donor,kind='image',start=boundary,end=end,
                    local_start=local_start+available,
                    local_end=shot.get('local_end',local_start+shot['render_duration']),
                    render_duration=missing,duration=missing,keep_full=False,
                    timing_adjustment=adjustment)
        rest.pop('cue',None)
        result.extend((first,rest));adjustments.append(adjustment)
    if issues:
        raise NeedsMoreVisualsError(issues,adjustments)
    return result, len(adjustments)
