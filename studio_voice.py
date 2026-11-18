"""Sentence-aware Edge narration with original word timings carried forward."""
import re


def delivery_segments(text):
    chunks=re.findall(r'[^。！？!?；;\n]+[。！？!?；;\n]*|[。！？!?；;\n]+',text)
    result=[]
    for chunk in chunks:
        if not chunk.strip():continue
        dense=bool(re.search(r'\d|[A-Z]{2,}|参数|损失|梯度|基线|成功率|百分之|相比|对照',chunk))
        question=chunk.rstrip().endswith(('？','?'))
        rate='-4%' if dense else '+2%' if question else '+6%'
        result.append({'text':chunk,'rate':rate,'pause_ms':120 if question else 80 if dense else 40,
                       'reason':'数字或术语' if dense else '问题' if question else '叙述'})
    return result


async def make_adaptive_voice(text,voice,cache,ffmpeg):
    from make_video import make_voice,RATE_HZ
    pieces=[];events=[];hits=[];timing=[];samples=0
    for segment in delivery_segments(text):
        pcm,words,hit=await make_voice(segment['text'],voice,segment['rate'],cache,ffmpeg)
        offset=round(samples/RATE_HZ*10_000_000)
        events.extend({**word,'offset':word['offset']+offset} if 'offset' in word else dict(word) for word in words)
        timing.append({**segment,'start':samples/RATE_HZ,'audio_seconds':len(pcm)/(2*RATE_HZ),'cached':hit})
        pause=b'\0'*round(segment['pause_ms']/1000*RATE_HZ)*2
        pieces.extend((pcm,pause));samples+=(len(pcm)+len(pause))//2;hits.append(hit)
    return b''.join(pieces),events,all(hits),timing
