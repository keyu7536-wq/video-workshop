from pathlib import Path
import json,hashlib,threading,time,re
from urllib.request import Request,urlopen
from concurrent.futures import ThreadPoolExecutor,as_completed
ROOT=Path(__file__).resolve().parents[1]/'.tts_models'
CHUNK=8*1024**2
def ranged(url,target,size,expected):
    if target.is_file():return
    target.parent.mkdir(parents=True,exist_ok=True)
    temp=target.with_suffix(target.suffix+'.ranged');state=target.with_suffix(target.suffix+'.ranges.json')
    done=set(json.loads(state.read_text())) if state.exists() else set()
    if not temp.exists():
        with temp.open('wb') as out:out.truncate(size)
        old=target.with_suffix(target.suffix+'.part')
        if old.is_file():
            count=old.stat().st_size//CHUNK
            with old.open('rb') as src,temp.open('r+b') as out:
                for n in range(count):out.write(src.read(CHUNK));done.add(n)
    lock=threading.Lock();total=(size+CHUNK-1)//CHUNK
    def part(n):
        start=n*CHUNK;end=min(size,start+CHUNK)-1
        for attempt in range(4):
            try:
                separator='&' if '?' in url else '?'
                req=Request(url+separator+'part='+str(n),headers={'Range':f'bytes={start}-{end}'})
                with urlopen(req,timeout=50) as response:
                    data=response.read(end-start+2)
                    if response.status!=206 or response.headers.get('Content-Range')!=f'bytes {start}-{end}/{size}' or len(data)!=end-start+1:
                        raise RuntimeError('range response mismatch')
                with lock:
                    with temp.open('r+b') as out:out.seek(start);out.write(data)
                    done.add(n);state.write_text(json.dumps(sorted(done)))
                    if len(done)%16==0 or len(done)==total:print(f'{target.parent.name}/{target.name}: {len(done)}/{total} chunks',flush=True)
                return
            except Exception:
                if attempt==3:raise
                time.sleep(1+attempt)
    print(f'Fetching {target.name}: {size/1e9:.2f} GB, {len(done)} cached chunks',flush=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        for f in as_completed([pool.submit(part,n) for n in range(total) if n not in done]):f.result()
    with temp.open('rb') as src:actual=hashlib.file_digest(src,'sha256').hexdigest()
    if expected and actual!=expected:raise ValueError('SHA256 mismatch for '+str(target))
    temp.replace(target);print('Verified '+str(target),flush=True)
def main():
    for pinned in (Path(__file__).parent/'voice-manifests').glob('*.json'):
        target=ROOT/pinned.stem/'download_manifest.json'
        target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists():target.write_bytes(pinned.read_bytes())
    for manifest in ROOT.glob('*/download_manifest.json'):
        info=json.loads(manifest.read_text(encoding='utf-8'));repo=info['repo']
        for file in info['files']:
            name=file['rfilename'];target=manifest.parent/name
            if target.exists() or name=='.gitattributes':continue
            url=('https://modelscope.cn/models/'+repo+'/resolve/master/' if repo.startswith('Qwen/') else 'https://huggingface.co/'+repo+'/resolve/'+info['revision']+'/')+name
            if file.get('lfs'):ranged(url,target,file['lfs']['size'],file['lfs']['sha256'])
            else:
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(urlopen(url,timeout=50).read())
if __name__=='__main__':main()
