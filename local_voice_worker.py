"""Isolated CUDA speech worker: Qwen TTS, forced alignment, independent ASR check."""
from __future__ import annotations
import argparse,gc,hashlib,json,re,sys,time
from difflib import SequenceMatcher
from pathlib import Path

ROOT=Path(__file__).resolve().parent
MODELS=ROOT/'.tts_models'
STYLE='平实、清晰的普通话男声，中等语速，连贯讲述技术内容，音高起伏小，句尾自然收稳，数字与术语清楚。'
SPEAKERS={'Uncle_Fu','Dylan','Serena','Vivian'}

def save(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')

def fingerprint(text,speaker,style):
    revision=json.loads((MODELS/'qwen-custom/download_manifest.json').read_text(encoding='utf-8'))['revision']
    return hashlib.sha256(json.dumps(['qwen-voice-v1',revision,text,speaker,style],ensure_ascii=False).encode()).hexdigest()[:24]

def normalized_speech(text):
    from opencc import OpenCC
    import cn2an
    text=OpenCC('t2s').convert(text).lower().replace('半秒','零点五秒')
    # ASR often inserts the optional 一 in 这一批/这一类; it is not a reported quantity.
    text=re.sub(r'([这那])一([批类种项段个])',r'\1\2',text)
    try:text=cn2an.transform(text,'cn2an')
    except ValueError:pass
    return ''.join(c for c in text if c.isalnum())

def compact_speech(value):
    from opencc import OpenCC
    return ''.join(c for c in OpenCC('t2s').convert(value).lower() if c.isalnum())

def compare_speech(expected,actual):
    # ASR may omit the punctuation between "百分之八十五；六种".
    # Compare identical Chinese wording first, before numeric parsing can merge it.
    if compact_speech(expected)==compact_speech(actual):
        return {'similarity':1.0,'numbers_equal':True,'passed':True,'expected':expected,'recognized':actual,
                'method':'independent Whisper small transcription; identical wording ignoring punctuation; no reference prompt'}
    left,right=normalized_speech(expected),normalized_speech(actual)
    score=SequenceMatcher(None,left,right,autojunk=False).ratio()
    numbers_equal=re.findall(r'\d+',left)==re.findall(r'\d+',right)
    return {'similarity':round(score,4),'numbers_equal':numbers_equal,'passed':score>=.93 and numbers_equal,
            'expected':expected,'recognized':actual,'method':'independent Whisper small transcription; no reference prompt'}

def clear_gpu():
    import torch
    gc.collect();torch.cuda.empty_cache()

def prepare_windows_tokenizer():
    """DyNet's Windows model reader cannot open a Unicode package path."""
    import os,importlib.util,importlib.metadata,shutil
    if os.name!='nt':return
    spec=importlib.util.find_spec('nagisa')
    source=Path(spec.origin).parent
    if str(source).isascii():return
    version=importlib.metadata.version('nagisa')
    target=Path(os.environ['LOCALAPPDATA'])/'VideoWorkshop'/'voice-compat'/version/'nagisa'
    if not str(target).isascii():raise RuntimeError('Nagisa requires an ASCII runtime path on Windows.')
    if not (target/'.voice-ready').exists():
        shutil.copytree(source,target,dirs_exist_ok=True)
        (target/'.voice-ready').write_text(version)
    sys.path.insert(0,str(target.parent))

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--project',required=True);args=parser.parse_args()
    project=Path(args.project).resolve();config=json.loads(project.read_text(encoding='utf-8'))
    speaker=config['voice'].split(':',1)[1]
    if speaker not in SPEAKERS:raise ValueError('不支持的本地音色')
    style=config.get('voice_style') or STYLE
    if speaker in ('Serena','Vivian'):style=style.replace('男声','女声')
    folder=project.parent/'audio_cache/qwen';folder.mkdir(parents=True,exist_ok=True)
    items=[]
    for block in config['blocks']:
        key=fingerprint(block['narration'],speaker,style)
        items.append({'id':block['id'],'key':key,'text':block['narration'],'audio':str(folder/(key+'.wav')),
            'alignment':str(folder/(key+'.json')),'cached':(folder/(key+'.wav')).is_file() and (folder/(key+'.json')).is_file()})
        generation=folder/(key+'.generation.json')
        if generation.exists():items[-1]['actual_style']=json.loads(generation.read_text(encoding='utf-8'))['style']
    pending=[item for item in items if not Path(item['alignment']).is_file() or not Path(item['audio']).is_file()]
    if not pending:
        print('本地语音与对齐缓存全部可复用。',flush=True);save(folder/'manifest.json',{'speaker':speaker,'style':style,'blocks':items});return
    import torch,soundfile as sf,numpy as np
    from qwen_tts import Qwen3TTSModel
    if not torch.cuda.is_available():raise RuntimeError('高品质本地配音需要可用的 CUDA 显卡；请安装本地语音运行环境。')
    torch.set_num_threads(6)
    to_generate=[item for item in pending if not Path(item['audio']).is_file()]
    if to_generate:
        print('加载 Qwen3-TTS 1.7B，使用本机显卡…',flush=True)
        model=Qwen3TTSModel.from_pretrained(str(MODELS/'qwen-custom'),device_map='cuda:0',dtype=torch.bfloat16,attn_implementation='sdpa')
        for i,item in enumerate(to_generate):
            started=time.time();torch.manual_seed(42+i)
            print(f'生成自然配音 {i+1}/{len(to_generate)}：{item["text"][:22]}',flush=True)
            wavs,sr=model.generate_custom_voice(text=item['text'],language='Chinese',speaker=speaker,instruct=style,
                non_streaming_mode=True,max_new_tokens=min(2400,max(350,len(item['text'])*10)),
                temperature=.75,top_p=.9,repetition_penalty=1.05)
            audio=np.asarray(wavs[0],dtype=np.float32)
            if not np.isfinite(audio).all() or np.max(np.abs(audio))<.001:raise RuntimeError('本地语音为空或数值异常')
            duration=len(audio)/sr
            if not 1<duration<min(180,max(15,len(item['text'])*.8)):raise RuntimeError('本地语音时长异常，可能重复或截断')
            sf.write(item['audio'],audio,sr,subtype='PCM_16')
            save(folder/(item['key']+'.generation.json'),{'style':style,'seed':42+i,'retry':0})
            print(f'  {duration:.2f}秒音频，用时{time.time()-started:.1f}秒',flush=True)
        del model;clear_gpu()
    print('用独立语音识别核对旁白与数字…',flush=True)
    from faster_whisper import WhisperModel
    asr=WhisperModel(str(MODELS/'whisper-small'),device='cpu',compute_type='int8',cpu_threads=6)
    checks=[]
    for item in pending:
        # Supply PCM directly: recent PyAV removed metadata_errors from av.open.
        from scipy.signal import resample_poly
        samples,sample_rate=sf.read(item['audio'],dtype='float32')
        if samples.ndim>1:samples=samples.mean(axis=1)
        if sample_rate!=16000:
            from math import gcd
            divisor=gcd(sample_rate,16000)
            samples=resample_poly(samples,16000//divisor,sample_rate//divisor)
        segments,info=asr.transcribe(samples,language='zh',beam_size=5,condition_on_previous_text=False)
        recognized=''.join(segment.text for segment in segments)
        result=compare_speech(item['text'],recognized);item['speech_check']=result;checks.append({'id':item['id'],**result})
        print(f'  {item["id"]} 文字一致度 {result["similarity"]:.1%}，数字一致 {result["numbers_equal"]}',flush=True)
    save(folder/'speech_check.json',{'checks':checks,'auditory_review':False})
    failed=[c for c in checks if not c['passed']]
    if failed:
        print('重录未通过核对的段落：数字和术语放慢、清晰咬字…',flush=True)
        retry_style=style+' 放慢一点讲述，数字、量词和技术名词咬字清楚。数字后留极短停顿，句间自然停顿。'
        model=Qwen3TTSModel.from_pretrained(str(MODELS/'qwen-custom'),device_map='cuda:0',dtype=torch.bfloat16,attn_implementation='sdpa')
        for attempt in range(2):
            failed_ids={c['id'] for c in checks if not c['passed']}
            if not failed_ids:break
            for i,item in enumerate(pending):
                if item['id'] not in failed_ids:continue
                torch.manual_seed(123+attempt*31+i)
                wavs,sr=model.generate_custom_voice(text=item['text'],language='Chinese',speaker=speaker,instruct=retry_style,
                    non_streaming_mode=True,max_new_tokens=min(2400,max(350,len(item['text'])*10)),temperature=.65,top_p=.85,repetition_penalty=1.05)
                samples=np.asarray(wavs[0],dtype=np.float32)
                if not np.isfinite(samples).all() or np.max(np.abs(samples))<.001 or not 1<len(samples)/sr<180:
                    raise RuntimeError('重录音频异常')
                sf.write(item['audio'],samples,sr,subtype='PCM_16')
                item['actual_style']=retry_style
                save(folder/(item['key']+'.generation.json'),{'style':retry_style,'seed':123+attempt*31+i,'retry':attempt+1})
                from math import gcd
                divisor=gcd(sr,16000)
                samples=resample_poly(samples,16000//divisor,sr//divisor)
                segments,_=asr.transcribe(samples,language='zh',beam_size=5,condition_on_previous_text=False)
                result=compare_speech(item['text'],''.join(s.text for s in segments))
                item['speech_check']=result
                checks=[{'id':item['id'],**result,'regeneration_attempt':attempt+1} if c['id']==item['id'] else c for c in checks]
                save(folder/'speech_check.json',{'checks':checks,'auditory_review':False})
                print(f'  {item["id"]} 重录核对：{result["similarity"]:.1%}，数字一致 {result["numbers_equal"]}',flush=True)
        del model;clear_gpu()
        failed=[c for c in checks if not c['passed']]
    del asr;gc.collect()
    if failed:raise RuntimeError('配音内容核对未通过：'+','.join(c['id'] for c in failed)+'。详见 audio_cache/qwen/speech_check.json')
    print('从实际声音计算字词时间，重新对齐字幕…',flush=True)
    prepare_windows_tokenizer()
    from qwen_asr import Qwen3ForcedAligner
    aligner=Qwen3ForcedAligner.from_pretrained(str(MODELS/'qwen-aligner'),device_map='cuda:0',dtype=torch.bfloat16,attn_implementation='sdpa')
    for item in pending:
        results=aligner.align(audio=item['audio'],text=item['text'],language='Chinese')
        events=[];last=0.;duration=sf.info(item['audio']).duration
        for word in results[0]:
            start=float(word.start_time);end=float(word.end_time)
            if not 0<=start<=end<=duration+.15 or start<last-.05:raise RuntimeError('本地配音对齐时间异常')
            last=start
            events.append({'type':'WordBoundary','text':word.text,'offset':round(start*1e7),'duration':round((end-start)*1e7)})
        # Validate textual coverage before accepting the cache.
        if compact_speech(''.join(e['text'] for e in events))!=compact_speech(item['text']):
            raise RuntimeError('对齐器未覆盖全部旁白文字')
        save(Path(item['alignment']),{'events':events,'provider':'Qwen3-TTS-1.7B-CustomVoice','speaker':speaker,
            'style':item.get('actual_style',style),'aligner':'Qwen3-ForcedAligner-0.6B','speech_check':item['speech_check'],'duration':duration})
    del aligner;clear_gpu()
    save(folder/'manifest.json',{'speaker':speaker,'style':style,'blocks':items})
    print('本地配音、内容核对和字幕对齐已完成。',flush=True)

if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    main()
