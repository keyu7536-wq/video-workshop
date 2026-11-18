"""Exercise the local video renderer offline using generated test patterns.

The audio is explicitly a sine-wave test tone, NOT narration or a voice sample.
No paper, author footage, account, model request or TTS network call is used.
"""
from __future__ import annotations
import argparse
import array
import asyncio
import hashlib
import json
import math
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import make_video as renderer

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,help='New or empty directory; defaults to an OS temporary directory')
    parser.add_argument('--font',help='Optional font file if the OS has no supported font')
    args=parser.parse_args()
    job=(args.output_dir or Path(tempfile.mkdtemp(prefix='video-workshop-smoke-'))).resolve()
    if job.exists() and any(job.iterdir()):raise ValueError('Choose a new or empty output directory')
    (job/'assets').mkdir(parents=True,exist_ok=True)
    ffmpeg=renderer.ffmpeg_path()
    renderer.run([ffmpeg,'-v','error','-y','-f','lavfi','-i','testsrc2=size=640x360:rate=30:duration=4',
                  '-c:v','libx264','-pix_fmt','yuv420p',str(job/'assets/generated-pattern.mp4')])
    text='Offline render test.'
    config={'schema_version':1,'renderer':'studio','title':'Offline render test - test tone only',
      'voice':'test-tone','rate':'+0%','width':640,'height':360,'fps':30,'output_name':'video.mp4',
      'layout_style':'immersive','media_fit':'contain','show_sources':False,'show_disclosure':False,
      'show_headings':False,'minimal_text':True,'outro':None,
      'plan_origin':'Synthetic offline renderer test. Audio is a test tone, not speech.',
      'blocks':[{'id':'test','chapter':'','narration':text,'shots':[{'kind':'video','file':'assets/generated-pattern.mp4','in':0,
          'duration':4,'label':'Generated test pattern','source':'Generated locally by FFmpeg','keep_full':False,'protect_content':False}]}]}
    project=job/'project.json';project.write_text(json.dumps(config,indent=2),encoding='utf-8')
    async def test_tone(*_):
        duration=3;rate=renderer.RATE_HZ
        pcm=array.array('h',(int(1800*math.sin(2*math.pi*440*i/rate)) for i in range(rate*duration)))
        if sys.byteorder!='little':pcm.byteswap()
        return pcm.tobytes(),[{'type':'WordBoundary','offset':0,'duration':duration*10_000_000,'text':text}],False
    original=renderer.make_voice
    try:
        renderer.make_voice=test_tone
        asyncio.run(renderer.build(SimpleNamespace(project=str(project),voice=None,rate=None,font=args.font,check=False,subtitles_only=False)))
    finally:renderer.make_voice=original
    video=job/'output/video.mp4'
    renderer.run([ffmpeg,'-v','error','-i',str(video),'-f','null','-'])
    import imageio_ffmpeg
    reader=imageio_ffmpeg.read_frames(str(video))
    try:metadata=next(reader)
    finally:reader.close()
    assert metadata['size']==(640,360) and 3.2<metadata['duration']<3.4
    result={'passed':True,'video':str(video),'duration_seconds':metadata['duration'],
        'resolution':list(metadata['size']),'sha256':hashlib.sha256(video.read_bytes()).hexdigest(),
        'audio':'synthetic test tone, not speech','network_calls':False}
    (job/'output/smoke_check.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))

if __name__=='__main__':main()
