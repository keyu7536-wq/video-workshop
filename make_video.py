#!/usr/bin/env python3
"""Render an audited narration/shot plan into a voiced, captioned video.

This program does not interpret arbitrary research papers or choose footage by
itself. Use studio_server.py to prepare an editable plan from input sources.
"""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time
import wave

ROOT = Path(__file__).resolve().parent
RATE_HZ = 24000


def log(message):
    print(message, flush=True)


def run(args, **kwargs):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace")[-3500:]
        raise RuntimeError(f"外部程序执行失败：{Path(str(args[0])).name}\n{detail}")
    return result.stdout


def ffmpeg_path():
    executable = shutil.which("ffmpeg")
    if not executable:
        try:
            import imageio_ffmpeg
            executable = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError as exc:
            raise RuntimeError("缺少依赖。请运行启动脚本，或 pip install -r requirements.txt。") from exc
    filters = run([executable, "-hide_banner", "-filters"]).decode("utf-8", "replace")
    if not re.search(r"\bass\s+", filters):
        raise RuntimeError("此 FFmpeg 不含 ass 字幕滤镜。请安装含 libass 的完整 FFmpeg。")
    return executable


def find_font(work, supplied=None):
    candidates = []
    if supplied:
        candidates.append((Path(supplied).expanduser(), "Noto Sans CJK SC"))
    candidates += [
        (Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc", "Microsoft YaHei"),
        (Path("/System/Library/Fonts/PingFang.ttc"), "PingFang SC"),
        (Path("/System/Library/Fonts/STHeiti Light.ttc"), "Heiti SC"),
        (Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"), "Noto Sans CJK SC"),
        (Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"), "Noto Sans CJK SC"),
        (Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"), "Noto Sans CJK SC"),
    ]
    for path, family in candidates:
        if path.is_file():
            fonts = work / "fonts"
            fonts.mkdir(exist_ok=True)
            target = fonts / ("chinese" + path.suffix)
            if not target.exists() or target.stat().st_size != path.stat().st_size:
                shutil.copy2(path, target)
            return family
    raise RuntimeError("没有找到中文字体。Windows 需微软雅黑、macOS 需苹方；Linux 可安装 fonts-noto-cjk，或用 --font 指定中文字体文件。")


def norm(text):
    return "".join(c.lower() for c in text if c.isalnum())


def words_with_positions(text, events):
    source_norm, source_map = [], []
    for index, char in enumerate(text):
        if char.isalnum():
            source_norm.append(char.lower())
            source_map.append(index)
    normalized = "".join(source_norm)
    position = 0
    result = []
    for event in events:
        if event.get("type") != "WordBoundary":
            continue
        token = norm(event.get("text", ""))
        if not token:
            continue
        location = normalized.find(token, position)
        if location < 0:
            raise RuntimeError("配音时间戳无法与文稿对齐；请保留缓存并核查此段。")
        first = source_map[location]
        last = source_map[location + len(token) - 1] + 1
        position = location + len(token)
        result.append({"start": event["offset"] / 10_000_000,
                       "end": (event["offset"] + event["duration"]) / 10_000_000,
                       "a": first, "b": last, "norm_start": location,
                       "norm_end": position})
    if not result:
        raise RuntimeError("TTS 未返回词级时间戳，已停止，避免输出假同步字幕。")
    for index, item in enumerate(result):
        item["b"] = result[index + 1]["a"] if index + 1 < len(result) else len(text)
    return result


def measured_cue_candidates(text, words, max_candidates=48, max_characters=24):
    """Bounded, unique original phrases starting at measured word boundaries.

    This is diagnostic evidence, not a semantic recommendation or a new timing
    estimate. A phrase can span several measured words to disambiguate repeats.
    Long blocks are sampled across their whole span, including the endpoints.
    """
    if max_candidates < 1 or max_characters < 1:
        return []
    normalized = norm(text)
    candidates = []
    for index, word in enumerate(words):
        for end_index in range(index,len(words)):
            end_word = words[end_index]
            phrase = text[word['a']:end_word['b']].strip().rstrip('，。！？；：、,.!?;: \t\r\n')
            if len(phrase) > max_characters:
                break
            target = norm(phrase)
            if len(target) < min(4, max_characters):
                continue
            first = normalized.find(target)
            if first >= 0 and normalized.find(target, first + 1) < 0:
                candidates.append({'cue': phrase, 'start': word['start']})
                break
    if len(candidates) <= max_candidates:
        return candidates
    if max_candidates == 1:
        return candidates[:1]
    return [candidates[round(index*(len(candidates)-1)/(max_candidates-1))]
            for index in range(max_candidates)]


def cue_time(cue, text, words):
    target = norm(text).find(norm(cue))
    if target < 0:
        raise RuntimeError(f"镜头提示词未在口播中找到：{cue}")
    for word in words:
        if word["norm_start"] <= target < word["norm_end"]:
            return word["start"]
    raise RuntimeError(f"无法定位口播提示词：{cue}")


def subtitle_phrases(text, words, base):
    # Prefer natural Chinese clauses, so a caption never begins with a tail
    # such as “运动，底层...” merely because a character budget was reached.
    clauses = []
    for match in re.finditer(r"[^，。！？：；]+[，。！？：；]*", text):
        start, end = match.span()
        # A long enumeration can be split at its natural list punctuation.
        if end - start > 28 and "、" in match.group():
            for part in re.finditer(r"[^、]+、?", match.group()):
                clauses.append([start + part.start(), start + part.end()])
        else:
            clauses.append([start, end])
    merged = []
    for start, end in clauses:
        if merged:
            prev_start, prev_end = merged[-1]
            previous = text[prev_start:prev_end].strip()
            current = text[start:end].strip()
            short = min(len(norm(previous)), len(norm(current))) <= 4
            if short and not re.search(r"[。！？]$", previous) and len(text[prev_start:end].strip()) <= 26:
                merged[-1][1] = end
                continue
        merged.append([start, end])
    result = []
    for start, end in merged:
        related = [word for word in words if word["a"] < end and word["b"] > start]
        if not related:
            continue
        if end - start <= 34:
            result.append((base + related[0]["start"], base + related[-1]["end"] + .12,
                           text[start:end].strip()))
            continue
        # Rare long clauses: fall back only at word boundaries, not mid-word.
        group = []
        for word in related:
            if group and len(text[group[0]["a"]:word["b"]].strip()) > 30:
                result.append((base + group[0]["start"], base + group[-1]["end"] + .12,
                               text[group[0]["a"]:group[-1]["b"]].strip()))
                group = []
            group.append(word)
        if group:
            result.append((base + group[0]["start"], base + group[-1]["end"] + .12,
                           text[group[0]["a"]:group[-1]["b"]].strip()))
    for i in range(len(result) - 1):
        st, en, phrase = result[i]
        result[i] = (st, min(en, result[i+1][0]), phrase)
    return result


def wrap_caption(phrase):
    if len(phrase) <= 18:
        return phrase
    middle = len(phrase) / 2
    # Prefer an existing clause/list break close to the midpoint; otherwise
    # balance the two lines, avoiding isolated one- or two-character tails.
    candidates = [i + 1 for i, char in enumerate(phrase) if char in "，、：； "
                  and 5 <= i + 1 <= 18 and 5 <= len(phrase) - i - 1 <= 18]
    split = min(candidates, key=lambda i: abs(i - middle)) if candidates else math.ceil(middle)
    return phrase[:split].rstrip() + "\n" + phrase[split:].lstrip()


async def make_voice(text, voice, rate, cache, ffmpeg):
    import edge_tts
    import edge_tts.communicate
    # Add normally trusted platform CAs. TLS certificate verification remains on.
    # No certificate data is distributed with this project.
    edge_tts.communicate._SSL_CTX.load_default_certs()
    fingerprint = hashlib.sha256(json.dumps(["edge-tts-7.3.1", text, voice, rate],
                                           ensure_ascii=False).encode()).hexdigest()[:24]
    mp3, meta = cache / f"{fingerprint}.mp3", cache / f"{fingerprint}.jsonl"
    hit = mp3.exists() and mp3.stat().st_size > 1000 and meta.exists() and meta.stat().st_size > 20
    if not hit:
        temp = mp3.with_suffix(".part.mp3")
        tempmeta = meta.with_suffix(".part.jsonl")
        communicator = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary",
                                            connect_timeout=15, receive_timeout=45)
        try:
            await asyncio.wait_for(communicator.save(str(temp), str(tempmeta)), timeout=90)
            if temp.stat().st_size < 1000:
                raise RuntimeError("返回的音频过短或为空。")
            temp.replace(mp3)
            tempmeta.replace(meta)
        except Exception as exc:
            for path in (temp, tempmeta):
                path.unlink(missing_ok=True)
            detail = str(exc).split("?")[0][:220]
            raise RuntimeError(f"在线中文配音失败（{type(exc).__name__}）：{detail}\n已有缓存会保留；检查网络后重跑即可。程序没有关闭 TLS 验证。") from exc
    events = [json.loads(line) for line in meta.read_text(encoding="utf-8").splitlines() if line.strip()]
    pcm = run([ffmpeg, "-v", "error", "-i", str(mp3), "-f", "s16le", "-ac", "1", "-ar", str(RATE_HZ), "pipe:1"])
    return pcm, events, hit


def choose_shots(block, duration, words, fps):
    shots = [dict(s) for s in block["shots"]]
    total_weight = sum(s["duration"] for s in shots)
    edges = [0.0]
    previous_weight = 0.0
    for index, shot in enumerate(shots[1:], 1):
        previous_weight += shots[index - 1]["duration"]
        edge = cue_time(shot["cue"], block["narration"], words) if shot.get("cue") else duration * previous_weight / total_weight
        if shot.get("min_start") is not None:
            edge = max(edge, shot["min_start"])
        edges.append(round(edge * fps) / fps)
    edges.append(duration)
    if any(b <= a for a, b in zip(edges, edges[1:])):
        raise RuntimeError(f"第 {block['id']} 段的镜头时间顺序无效，请检查提示词。")
    for shot, start, end in zip(shots, edges, edges[1:]):
        shot["local_start"], shot["local_end"] = start, end
        shot["render_duration"] = end - start
    return shots


def ts_ass(t):
    centis = round(t * 100)
    return f"{centis // 360000}:{centis // 6000 % 60:02}:{centis // 100 % 60:02}.{centis % 100:02}"


def ts_srt(t):
    millis = round(t * 1000)
    return f"{millis // 3600000:02}:{millis // 60000 % 60:02}:{millis // 1000 % 60:02},{millis % 1000:03}"


def clean_ass(text):
    return text.replace("\\", "\\\\").replace("{", "").replace("}", "").replace("\n", "\\N")


def write_subtitles(output, work, font, title, shots, phrases, total):
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 720
PlayResY: 1280
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Brand,{font},46,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,7,36,36,130,1
Style: Chapter,{font},31,&H00BAE553,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,7,36,36,220,1
Style: Label,{font},24,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,8,30,30,930,1
Style: Source,{font},17,&H00BDB8AE,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,8,25,25,981,1
Style: Voice,{font},38,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,42,42,151,1
Style: Foot,{font},16,&H008A8780,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,2,25,25,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    def add(st, en, style, text):
        events.append(f"Dialogue: 0,{ts_ass(st)},{ts_ass(en)},{style},,0,0,0,,{clean_ass(text)}")
    add(0, total, "Brand", title)
    add(0, total, "Foot", "AI 合成配音  ·  官方演示与论文图")
    for shot in shots:
        add(shot["start"], shot["end"], "Chapter", shot["chapter"])
        label = shot["label"]
        if len(label) > 27:
            label = label.replace("  跟踪器", "\n跟踪器")
        add(shot["start"], shot["end"], "Label", label)
        add(shot["start"], shot["end"], "Source", shot["source"])
    for start, end, phrase in phrases:
        wrapped = wrap_caption(phrase)
        add(start, end, "Voice", wrapped)
    (work / "captions.ass").write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    srt = "\n\n".join(f"{i}\n{ts_srt(start)} --> {ts_srt(end)}\n{phrase}"
                        for i, (start, end, phrase) in enumerate(phrases, 1)) + "\n"
    (output / "subtitles.srt").write_text(srt, encoding="utf-8")


def render_shot(index, shot, root, work, ffmpeg, fps):
    target = work / f"shot_{index:03}.mp4"
    source = (root / shot["file"]).resolve()
    duration = shot["render_duration"]
    frames = round(duration * fps)
    args = [ffmpeg, "-hide_banner", "-v", "error", "-y"]
    if shot["kind"] == "image":
        args += ["-loop", "1", "-framerate", str(fps)]
    else:
        args += ["-ss", str(shot.get("in", 0)), "-t", str(shot["duration"])]
    args += ["-i", str(source)]
    vf = "setpts=PTS-STARTPTS,scale=672:600:force_original_aspect_ratio=decrease:force_divisible_by=2,pad=720:1280:(ow-iw)/2:310+(600-ih)/2:color=0x05090f,setsar=1,fps=30"
    if shot["kind"] == "video":
        vf += f",tpad=stop_mode=clone:stop_duration={duration:.6f}"
    args += ["-vf", vf, "-frames:v", str(frames), "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-threads", "2", "-pix_fmt", "yuv420p", str(target)]
    run(args)
    log(f"  镜头 {index + 1:02} 已完成（{duration:.2f} 秒）")
    return target


async def build(args):
    started = time.time()
    project_path = Path(args.project).resolve()
    root = project_path.parent
    project_bytes = project_path.read_bytes()
    config = json.loads(project_bytes.decode("utf-8"))
    project_sha256 = hashlib.sha256(project_bytes).hexdigest()
    output_name = config.get("output_name", "video.mp4")
    if not isinstance(output_name, str) or Path(output_name).name != output_name or not output_name.lower().endswith(".mp4"):
        raise RuntimeError("output_name 必须是不含目录的 MP4 文件名。")
    output = (root / "output").resolve()
    work = output / "work"
    cache = root / "audio_cache"
    for directory in (output, work, cache):
        directory.mkdir(parents=True, exist_ok=True)
    recovery_path = output / 'timing_recovery.json'
    if not args.check:
        recovery_path.unlink(missing_ok=True)
    ffmpeg = ffmpeg_path()
    font = find_font(work, args.font)
    for block in config["blocks"]:
        for shot in block["shots"]:
            if shot['kind']!='diagram' and not (root / shot["file"]).is_file():
                raise RuntimeError(f"缺少素材：{shot['file']}。请完整解压素材包后运行。")
    fps = int(config.get("fps", 30))
    if fps != 30:
        raise RuntimeError("当前渲染器使用 30 fps，请保留项目的 fps=30。")
    voice, rate = args.voice or config["voice"], args.rate or config["rate"]
    studio = config.get('renderer') == 'studio'
    if studio:
        import studio_render
    width, height = (int(config.get('width', 1080)), int(config.get('height', 1920))) if studio else (720, 1280)
    if studio:
        from studio_display import validate_dimensions
        validate_dimensions(width,height)
    log(f"中文音色：{voice}；语速：{rate}；中文字体：{font}")
    if args.check:
        log("依赖、中文字体和所有素材检查通过。尚未生成配音或渲染。")
        return
    pieces, phrases, shots, reports = [], [], [], []
    timing_issues = []
    local_voice=None
    if voice.startswith('qwen:'):
        import studio_local_voice
        local_voice=studio_local_voice.prepare(project_path)
    cursor, hits = 0.0, 0
    log("[1/4] 生成中文配音，并读取真实词级时间戳")
    for number, block in enumerate(config["blocks"], 1):
        log(f"  第 {number}/{len(config['blocks'])} 段")
        delivery=[]
        if local_voice is not None:
            pcm, events, hit, delivery = studio_local_voice.load(local_voice[block['id']],ffmpeg)
        elif rate == 'auto':
            from studio_voice import make_adaptive_voice
            pcm, events, hit, delivery = await make_adaptive_voice(block["narration"], voice, cache, ffmpeg)
        else:
            pcm, events, hit = await make_voice(block["narration"], voice, rate, cache, ffmpeg)
        hits += int(hit)
        audio_duration = len(pcm) / (RATE_HZ * 2)
        minimum = block.get("min_duration", 0)
        duration = math.ceil(max(audio_duration + .25, minimum) * fps) / fps
        words = words_with_positions(block["narration"], events)
        if studio:
            try:
                selected = studio_render.choose_shots(block, duration, words, fps)
            except studio_render.NeedsMoreVisualsError as exc:
                timing_issues.extend({**issue,'block_start_seconds':cursor} for issue in exc.issues)
                selected = []
        else:
            selected = choose_shots(block, duration, words, fps)
        for shot in selected:
            shot["start"] = cursor + shot["local_start"]
            shot["end"] = cursor + shot["local_end"]
            if shot['kind']=='diagram':
                graph=dict(shot['diagram']);stages=[]
                for index,stage in enumerate(graph['stages']):
                    timestamp=0.0 if index==0 else cue_time(stage['cue'],block['narration'],words)-shot['local_start']
                    reason = ('diagram_cue_outside_shot' if not 0<=timestamp<shot['render_duration']
                              else 'diagram_cues_not_increasing' if stages and timestamp<=stages[-1]['start']
                              else None)
                    if reason:
                        timing_issues.append({'block_id':block['id'],'shot_index':shot['shot_index'],
                            'reason':reason,'time_basis':'block_local','block_start_seconds':cursor,
                            'stage_index':index,'cue':stage.get('cue',''),
                            'expected_cue_time':timestamp+shot['local_start'],
                            'stage_local_time':timestamp,'start':shot['local_start'],'end':shot['local_end'],
                            'available_cue_window':shot['render_duration'],
                            'missing_seconds':round(max(0,-timestamp,timestamp-shot['render_duration']+1/fps),6),
                            'file':shot.get('file','')})
                    stages.append({**stage,'start':timestamp})
                shot['diagram_timeline']=stages
        shots.extend(selected)
        phrases.extend(subtitle_phrases(block["narration"], words, cursor))
        sample_count = round(duration * RATE_HZ)
        pieces.append(pcm + b"\x00" * max(0, sample_count*2 - len(pcm)))
        reports.append({"id": block["id"], "start": cursor, "end": cursor + duration,
                        "audio_seconds": audio_duration, "cached": hit, "delivery": delivery,
                        "cue_time_basis": "block_local",
                        "cue_candidates": measured_cue_candidates(block['narration'],words),
                        "characters_per_second": round(len(norm(block['narration']))/max(.1,audio_duration),2)})
        cursor += duration
    if config.get("outro"):
        outro = dict(config["outro"])
        outro.update(start=cursor, end=cursor + outro["duration"], render_duration=outro["duration"])
        shots.append(outro)
        cursor += outro["duration"]
        pieces.append(b"\x00" * round(outro["duration"] * RATE_HZ) * 2)
    with wave.open(str(output / "narration.wav"), "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(RATE_HZ)
        for pcm in pieces:
            wav.writeframes(pcm)
    if studio:
        import studio_render
        try:
            shots, hold_replacements = studio_render.replace_video_holds(shots, root, work, fps, config.get('minimal_text',False))
            timing_adjustments = [shot['timing_adjustment'] for shot in shots if shot.get('timing_adjustment')]
        except studio_render.NeedsMoreVisualsError as exc:
            timing_issues.extend(exc.issues)
            timing_adjustments = exc.adjustments
        if timing_issues:
            failure = studio_render.NeedsMoreVisualsError(timing_issues,timing_adjustments)
            diagnostic = {**failure.diagnostic,'project_sha256':project_sha256,
                          'blocks':reports,'duration_seconds':cursor,'audio_cache_hits':hits}
            recovery_path.write_text(json.dumps(diagnostic,ensure_ascii=False,indent=2),encoding='utf-8')
            raise failure
        recovery_path.write_text(json.dumps({'status':'resolved','code':'insufficient_visual_duration',
            'project_sha256':project_sha256,'issues':[],'adjustments':timing_adjustments},
            ensure_ascii=False,indent=2),encoding='utf-8')
        # Do not spend time rendering any diagram until every block has passed
        # measured timing and source-coverage validation.
        for shot in shots:
            if shot['kind']=='diagram':
                from studio_diagrams import render_diagram
                graph={**shot['diagram'],'stages':shot['diagram_timeline']}
                target=(root/shot['file']).resolve();target.parent.mkdir(parents=True,exist_ok=True)
                render_diagram(graph,target,shot['render_duration'],width,height,fps,ffmpeg)
                shot.update(kind='video',duration=shot['render_duration'])
                shot['in']=0
        studio_render.write_subtitles(output, work, font, config['title'], shots, phrases, cursor, width, height, config)
    else:
        hold_replacements = 0
        timing_adjustments = []
        write_subtitles(output, work, font, config["title"], shots, phrases, cursor)
    if args.subtitles_only:
        old_path = output / "rendered_shots.json"
        if not (work / "picture.mp4").exists() or not old_path.exists() or json.loads(old_path.read_text(encoding="utf-8")) != shots:
            raise RuntimeError("字幕重绘需要与上次相同的镜头和时长；请去掉 --subtitles-only，执行完整渲染。")
        log("[2/4] 复用已完成的全部画面，仅重新合成字幕")
    else:
        log(f"[2/4] 自动裁片与排图，共 {len(shots)} 个镜头；成片 {cursor:.2f} 秒")
        with ThreadPoolExecutor(max_workers=2) as pool:
            if studio:
                futures = [pool.submit(studio_render.render_shot, i, shot, root, work, ffmpeg, fps, width, height, config) for i, shot in enumerate(shots)]
            else:
                futures = [pool.submit(render_shot, i, shot, root, work, ffmpeg, fps) for i, shot in enumerate(shots)]
            rendered = [future.result() for future in futures]
        (work / "concat.txt").write_text("".join(f"file '{p.name}'\n" for p in rendered), encoding="utf-8")
        run([ffmpeg, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", "concat.txt", "-c", "copy", "picture.mp4"], cwd=work)
    log("[3/4] 合成音频、字幕、章节标题和来源标注")
    destination = output / output_name
    run([ffmpeg, "-v", "error", "-y", "-i", "picture.mp4", "-i", str(output / "narration.wav"),
         "-vf", "ass=captions.ass:fontsdir=fonts", "-map", "0:v:0", "-map", "1:a:0",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-threads", "4", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-t", f"{cursor:.6f}",
         "-movflags", "+faststart", str(destination)], cwd=work)
    report = {"output": destination.name, "duration_seconds": cursor, "size_bytes": destination.stat().st_size,
              "resolution": f"{width}x{height}", "fps": fps, "voice": voice, "rate": rate,
              "video_holds_replaced": hold_replacements,
              "timing_adjustments": timing_adjustments,
              "narration_blocks": len(config["blocks"]), "audio_cache_hits": hits, "shots": len(shots),
              "subtitle_phrases": len(phrases), "elapsed_seconds": round(time.time()-started, 2),
              "subtitles_only_rerender": args.subtitles_only,
              "timing": ("Qwen forced alignment of actual audio" if local_voice is not None else "TTS WordBoundary timestamps") + ("; studio video plays at original speed; measured gaps use selected same-block static visuals." if studio else "; image/video duration follows recorded audio; legacy video may hold its last frame."),
              "scope": config.get("plan_origin", "Uses the supplied, previously reviewed script and source ranges. Does not automatically understand new papers or select new footage."),
              "blocks": reports}
    (output / "run_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "rendered_shots.json").write_text(json.dumps(shots, ensure_ascii=False, indent=2), encoding="utf-8")
    log("[4/4] 已完成：" + str(destination))
    log(f"  字幕：{output / 'subtitles.srt'}")
    log(f"  本次复用配音缓存：{hits}/{len(config['blocks'])}")


def main():
    parser = argparse.ArgumentParser(description="视频工坊：配音、裁片、插图、字幕与合成")
    parser.add_argument("--project", default=str(ROOT / "project.json"))
    parser.add_argument("--voice", help="覆盖项目中的 Edge TTS 音色")
    parser.add_argument("--rate", help="例如 --rate=-10%%")
    parser.add_argument("--font", help="中文字体文件路径，通常无需设置")
    parser.add_argument("--check", action="store_true", help="仅检查本地依赖、素材与中文字体")
    parser.add_argument("--subtitles-only", action="store_true", help="复用已有画面，仅重新合成字幕（镜头和时长需保持不变）")
    args = parser.parse_args()
    try:
        asyncio.run(build(args))
    except KeyboardInterrupt:
        log("已停止；配音缓存已保留。")
        sys.exit(130)
    except Exception as exc:
        log("错误：" + str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
