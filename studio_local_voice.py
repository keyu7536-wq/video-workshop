"""Launch the isolated local speech environment and consume verified cached audio."""
from pathlib import Path
import json,os,subprocess
from studio_console import console_print
ROOT=Path(__file__).resolve().parent

def prepare(project):
    executable=Path(os.environ['VIDEO_WORKSHOP_VOICE_PYTHON']) if os.environ.get('VIDEO_WORKSHOP_VOICE_PYTHON') else ROOT/('.venv-voice/Scripts/python.exe' if os.name=='nt' else '.venv-voice/bin/python')
    if not executable.is_file():raise RuntimeError('尚未安装可选本地配音环境，请参考 docs/configuration.md；也可改选在线 Edge 音色。')
    command=[str(executable),'-u',str(ROOT/'local_voice_worker.py'),'--project',str(project)]
    process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',
        env={**os.environ,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8','HF_HUB_OFFLINE':'1'},
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    with (project.parent/'voice_worker.log').open('w',encoding='utf-8') as log:
        for line in process.stdout:
            log.write(line);log.flush();console_print(line,end='')
    if process.wait():raise RuntimeError('本地配音失败，详情见 '+str(project.parent/'voice_worker.log'))
    manifest=json.loads((project.parent/'audio_cache/qwen/manifest.json').read_text(encoding='utf-8'))
    return {item['id']:item for item in manifest['blocks']}

def load(item,ffmpeg):
    from make_video import run,RATE_HZ
    meta=json.loads(Path(item['alignment']).read_text(encoding='utf-8'))
    if not meta['speech_check']['passed']:raise RuntimeError('此音频没有通过内容核对')
    pcm=run([ffmpeg,'-v','error','-i',item['audio'],'-f','s16le','-ac','1','-ar',str(RATE_HZ),'pipe:1'])
    return pcm,meta['events'],item.get('cached',False),[{'provider':meta['provider'],'speaker':meta['speaker'],
        'alignment':meta['aligner'],'speech_check':meta['speech_check']}]
