"""Loopback-only browser studio. No web build tooling or cloud hosting required."""
from __future__ import annotations
import argparse
from contextlib import ExitStack
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse
import uuid
import webbrowser
from urllib.request import urlopen

import paper_video as pv
import studio_engine as engine
from app_config import APP_ID,VERSION,PROTOCOL_VERSION,load_config
from studio_console import configure_console,console_print

ROOT=pv.ROOT
WEB=ROOT/'studio'
INBOX=ROOT/'tmp/studio_uploads'
ALLOWED={'.pdf','.txt','.md','.png','.jpg','.jpeg','.webp','.mp4','.mov','.mkv','.webm','.avi'}


def validate_settings(data):
    from studio_story import normalize_framework
    normalize_framework(data.get('story_framework'))
    if data.get('content_type','auto') not in ('auto','paper','general'):raise ValueError('不支持此内容类型')
    if data.get('voice','zh-CN-YunyangNeural') not in ('zh-CN-YunyangNeural','zh-CN-YunxiNeural','zh-CN-YunjianNeural','zh-TW-YunJheNeural','zh-CN-YunxiaNeural','zh-CN-XiaoxiaoNeural','qwen:Uncle_Fu','qwen:Dylan','qwen:Serena','qwen:Vivian'):raise ValueError('不支持此音色')
    if data.get('rate','auto') not in ('auto','-10%','-5%','+0%','+10%','+25%','+50%'):raise ValueError('不支持此语速设置')
    from studio_display import validate_display
    validate_display(data)
    formats=data.get('export_formats',[])
    if not isinstance(formats,list) or len(formats)>7 or any(f not in ('mp4','mov','webm','mp3','wav','vtt','bundle') for f in formats):raise ValueError('导出格式无效')
    for name in ('auto_render','web_research','deep_read','final_review','auto_repair'):
        if name in data and not isinstance(data[name],bool):raise ValueError('制作开关必须是布尔值')


def story_settings(request,data):
    """Validate all narrative choices before mutating any stored job."""
    from studio_story import normalize_framework
    changes={k:data[k] for k in ('content_type','story_framework') if k in data}
    validate_settings(changes)
    if 'story_framework' in changes:changes['story_framework']=normalize_framework(changes['story_framework'])
    return {**request,**changes}


def model_settings(request,data):
    """Persist the selected provider, never a credential from the browser."""
    changes={key:data[key] for key in ('backend','api_base','model') if key in data}
    if any(not isinstance(value,str) for value in changes.values()):raise ValueError('模型设置必须为文本。')
    merged={**request,**changes}
    if merged.get('backend','codex') not in ('codex','api'):raise ValueError('未知的模型来源。')
    if merged.get('backend')=='api':
        from model_api import APIConfig
        config=APIConfig(merged.get('api_base') or 'https://api.openai.com/v1',merged.get('model') or 'gpt-4.1','validation-only')
        config.endpoint()
    return merged


def editorial_settings(request,data,previous_plan):
    """A deliberate editor save updates requirements and protected narration together."""
    import hashlib
    result=dict(request)
    if 'brief' in data:
        brief=data['brief']
        if not isinstance(brief,str) or len(brief)>20000:
            raise ValueError('创作要求须为不超过20000字的文本。')
        if brief!=request.get('brief',''):
            result['brief']=brief
            result['_brief_revision']=hashlib.sha256(brief.encode('utf-8')).hexdigest()
    if request.get('script','').strip():
        old=[b['narration'] for b in previous_plan.get('blocks',[])]
        new=[b['narration'] for b in data['plan']['blocks']]
        if old!=new:result['script']='\n\n'.join(new)
    return result


class StudioHTTPServer(ThreadingHTTPServer):
    allow_reuse_address=False
    def server_bind(self):
        if os.name=='nt' and hasattr(socket,'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        super().server_bind()


def job_path(identifier):
    if not re.fullmatch(r'[A-Za-z0-9_-]+',identifier): raise ValueError('无效的任务编号。')
    path=(ROOT/'jobs'/identifier).resolve()
    if not path.is_relative_to((ROOT/'jobs').resolve()) or not path.is_dir(): raise ValueError('找不到该任务。')
    return path


def recover_interrupted_job(job):
    """Recover a proven dead run; never infer failure from an idle new server."""
    from studio_process import job_operation,process_alive
    state=pv.load_json(job/'job.json')
    if state.get('status')!='processing':return state
    try:
        with job_operation(job):
            state=pv.load_json(job/'job.json')
            if state.get('status')!='processing':return state
            metadata=pv.load_json(job/'analysis/worker.json')
            if not state.get('run_id') or metadata.get('run_id')!=state['run_id']:return state
            # A live owner may still be collecting an exited worker's result.
            # Access-denied, missing metadata and PID reuse all stay conservative.
            if process_alive(metadata.get('server_pid')) is not False:return state
            if process_alive(metadata.get('worker_pid')) is not False:return state
            recovered=dict(state,status='failed',
                error='此前工作台和制作进程均已退出，任务中断。已有素材和输出已保留，可继续恢复任务。',
                failed_action=state.get('stage') or state.get('last_action') or metadata.get('action'))
            pv.save_json(job/'job.json',recovered)
            state=recovered
    except (OSError,ValueError,AttributeError):
        # Missing/unreadable records or a concurrent transaction are not proof.
        pass
    return state


class Manager:
    def __init__(self):
        self.lock=threading.RLock(); self.process=None; self.worker=None; self.job=None; self.lines=[]; self.cancelled=False
    def start(self,job,action,key=''):
        from studio_process import console_python,redact,job_operation
        from datetime import datetime,timezone
        with self.lock,job_operation(job):
            if ((self.process and self.process.poll() is None) or
                    (self.worker and self.worker.is_alive())): raise ValueError('已有任务正在进行，请等待或先停止。')
            state=recover_interrupted_job(job)
            if state.get('status')=='processing':
                raise ValueError('此任务仍标记为制作中，可能由另一个工作台运行；请回到原工作台查看进度。尚未核实原制作进程已结束，为避免重复制作，本程序保留此状态。')
            self.job=job; self.lines=[]; self.cancelled=False
            run_id=uuid.uuid4().hex
            state.update(status='processing',error=None,failed_action=None,
                stage=None,last_action=action,run_id=run_id); pv.save_json(job/'job.json',state)
            env={**os.environ,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}
            if key: env['PAPER_VIDEO_API_KEY']=key
            secrets=[key,env.get('PAPER_VIDEO_API_KEY',''),env.get('OPENAI_API_KEY','')]
            log_path=job/'analysis/studio_run.log'; log=None
            try:
                log_path.parent.mkdir(parents=True,exist_ok=True); log=log_path.open('w',encoding='utf-8')
                self.process=subprocess.Popen([console_python(),'-u',str(ROOT/'studio_engine.py'),'--job',str(job),'--action',action],
                    cwd=ROOT,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                    text=True,encoding='utf-8',errors='replace',bufsize=1,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            except (OSError,ValueError,RuntimeError) as exc:
                message=redact('制作进程未能启动：'+str(exc),secrets)
                self.process=None; self.lines=[message]
                if log: log.write(message+'\n'); log.close()
                state.update(status='failed',error=message,failed_action=action); pv.save_json(job/'job.json',state)
                raise RuntimeError(message) from exc
            try:
                pv.save_json(job/'analysis/worker.json',{'run_id':run_id,'worker_pid':self.process.pid,
                    'server_pid':os.getpid(),'action':action,'started_at':datetime.now(timezone.utc).isoformat()})
            except OSError as exc:
                # The process is already running: do not claim that launch failed.
                warning=redact('制作进程已启动，但进程信息保存失败：'+str(exc),secrets)
                self.lines.append(warning);log.write(warning+'\n');log.flush()
            self.worker=threading.Thread(target=self._collect,
                args=(job,action,self.process,log,secrets,run_id),daemon=True)
            self.worker.start()
    def _collect(self,job,action,process,log,secrets,run_id):
        from studio_process import redact,failure_detail,job_operation
        from datetime import datetime,timezone
        tail=[]; read_error=None
        try:
            with log:
                for raw in process.stdout:
                    line=redact(raw,secrets)
                    with self.lock: self.lines=(self.lines+[line.rstrip()])[-1200:]
                    tail=(tail+[line])[-60:]; log.write(line); log.flush()
        except (OSError,ValueError) as exc:
            read_error=redact('制作日志读取失败：'+str(exc),secrets)
            tail.append(read_error)
        finally:
            process.stdout.close(); returncode=process.wait()
        with self.lock,job_operation(job,wait=True):
            state=pv.load_json(job/'job.json')
            if state.get('run_id')!=run_id:return
            metadata_path=job/'analysis/worker.json'
            try:
                metadata=pv.load_json(metadata_path)
                if metadata.get('run_id')==run_id:
                    metadata.update(exit_code=returncode,finished_at=datetime.now(timezone.utc).isoformat())
                    pv.save_json(metadata_path,metadata)
            except (OSError,ValueError,AttributeError):pass
            expected={'completed','review_needed'}
            if action in ('prepare','resume'): expected.add('prepared')
            if self.cancelled:
                state.update(status='cancelled',error='用户已停止任务。')
            elif returncode or read_error or state.get('status') not in expected:
                if state.get('status')!='failed' or not state.get('error'):
                    state['error']=failure_detail('制作进程',returncode,tail,'analysis/studio_run.log')
                state['status']='failed'
                if not state.get('failed_action'):state['failed_action']=state.get('stage') or action
            state['last_exit_code']=returncode
            pv.save_json(job/'job.json',state)
    def cancel(self):
        with self.lock: process=self.process
        if process and process.poll() is None:
            self.cancelled=True
            if os.name=='nt': subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            else: process.terminate()
    def snapshot(self):
        with self.lock:
            active=bool((self.process and self.process.poll() is None) or (self.worker and self.worker.is_alive()))
            return {'active':active,'job_id':self.job.name if self.job else None,'logs':list(self.lines)}


def snapshot(job):
    state=recover_interrupted_job(job); plan_path=job/'analysis/studio_plan.json'
    result={'id':job.name,'status':state['status'],'error':state.get('error'),'engine':state.get('engine','legacy')}
    if (job/'project.json').is_file():
        project=pv.load_json(job/'project.json');result['preview_shots']=[]
        for block in project.get('blocks',[]):
            for shot in block.get('shots',[]):
                target=(job/shot['file']).resolve()
                if target.is_relative_to(job.resolve()):
                    result['preview_shots'].append({k:shot[k] for k in ('kind','label','duration','in','protect_content') if k in shot}|{'path':target.relative_to(job.resolve()).as_posix()})
    if plan_path.is_file(): result['plan']=pv.load_json(plan_path)
    elif (job/'analysis/plan.json').is_file(): result['legacy_plan']=pv.load_json(job/'analysis/plan.json')
    for filename,key in [('request.json','request'),('review.json','review'),('output/run_report.json','report'),('publishing.json','publishing'),('research.json','research'),('output/final_review.json','final_review'),('output/exports.json','exports'),('analysis/research/manifest.json','research_manifest')]:
        if (job/filename).is_file(): result[key]=pv.load_json(job/filename)
    if (job/'analysis/sources.json').is_file():
        result['sources']=[{k:v for k,v in source.items() if k not in ('text','file','preview')} for source in pv.load_json(job/'analysis/sources.json')]
    if result.get('final_review') and result.get('plan'):
        from studio_revision import review_stale
        result['final_review']['stale']=review_stale(job,result['final_review'],engine.plan_hash(result['plan']))
    video=job/'output/video.mp4';poster=job/'output/poster.jpg'
    result['video_url']=f'/files/{job.name}/output/video.mp4?v={video.stat().st_mtime_ns}' if video.is_file() else None
    result['poster_url']=f'/files/{job.name}/output/poster.jpg?v={poster.stat().st_mtime_ns}' if poster.is_file() else None
    from studio_revision import export_version_current
    current_export=video.is_file() and export_version_current(job)
    can_retry_export=(state['status'] in ('failed','cancelled') and state.get('failed_action')=='export' and current_export)
    recovery=None
    unavailable_review=(result.get('final_review',{}).get('status')=='unavailable' or state.get('failed_action')=='review')
    receipt_valid=False;receipt=job/'output/render_receipt.json'
    if unavailable_review and receipt.is_file() and result.get('plan'):
        from studio_revision import review_stale
        try:receipt_valid=not review_stale(job,pv.load_json(receipt),engine.plan_hash(result['plan']))
        except (ValueError,OSError,KeyError):pass
    can_retry_review=(unavailable_review and video.is_file() and (job/'output/run_report.json').is_file()
        and receipt_valid)
    if state.get('engine')=='studio' and state['status'] in ('failed','cancelled','review_needed'):
        if can_retry_export:recovery={'action':'export','label':'重试导出'}
        elif can_retry_review:recovery={'action':'review','label':'重试成片复查'}
        elif unavailable_review and plan_path.is_file():recovery={'action':'render','label':'重新生成并复查'}
        elif state['status']=='review_needed':pass
        elif not plan_path.is_file() or state.get('failed_action') in ('prepare','resume'):
            recovery={'action':'resume','label':'继续生成文稿与分镜'}
        else:recovery={'action':'render','label':'重试生成视频'}
    result['recovery']=recovery
    result['can_export']=current_export and (state['status'] in ('completed','review_needed') or can_retry_export)
    # Use measured, rendered shot windows; the plan may differ after timing repair.
    result['rendered_segments']=[]
    timeline=job/'output/rendered_shots.json'
    if result['can_export'] and timeline.is_file():
        try:
            duration=float(result.get('report',{}).get('duration_seconds',0))
            for index,shot in enumerate(pv.load_json(timeline)):
                start,end=float(shot['start']),float(shot['end'])
                if not (math.isfinite(start) and math.isfinite(end) and 0<=start<end<=duration+.05):
                    raise ValueError('Invalid rendered shot window')
                result['rendered_segments'].append({'index':index,'start':start,'end':min(end,duration),
                    'label':str(shot.get('label') or f'镜头 {index+1}'),'kind':shot.get('kind',''),
                    'chapter':str(shot.get('chapter',''))})
        except (OSError,ValueError,TypeError,KeyError):
            result['rendered_segments']=[]
    return result


class Handler(BaseHTTPRequestHandler):
    server_version='VideoWorkshop/'+VERSION
    def log_message(self,*_): pass
    def json(self,value,status=200):
        raw=json.dumps(value,ensure_ascii=False).encode('utf-8')
        try:
            self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(raw))); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(raw)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):
            # Closing/reloading a browser cancels polling requests. The action
            # may already have succeeded; do not write another error response.
            self.close_connection=True
    def body(self):
        if 'application/json' not in self.headers.get('Content-Type',''): raise ValueError('请求需要JSON格式。')
        length=int(self.headers.get('Content-Length','0'))
        if not 0<length<=2*1024**2: raise ValueError('请求为空或内容过大。')
        value=json.loads(self.rfile.read(length))
        if not isinstance(value,dict): raise ValueError('请求需要JSON对象。')
        return value
    def local_request(self):
        expected=f'127.0.0.1:{self.server.server_port}'
        if self.headers.get('Host') not in (expected,f'localhost:{self.server.server_port}'): raise ValueError('仅允许本机访问。')
        origin=self.headers.get('Origin')
        if origin and origin not in ('http://'+expected,f'http://localhost:{self.server.server_port}'): raise ValueError('不接受其他网站的请求。')
    def do_HEAD(self): self.do_GET(head=True)
    def do_GET(self,head=False):
        try:
            self.local_request(); parsed=urlparse(self.path); query=parse_qs(parsed.query); path=unquote(parsed.path)
            if path=='/api/health': return self.json({'app':APP_ID,'version':PROTOCOL_VERSION,'release':VERSION})
            if path=='/api/app-config':
                config=load_config()
                validate_settings({'voice':config['default_voice'],'rate':config['default_rate']})
                return self.json(config)
            if path=='/api/status': return self.json(self.server.manager.snapshot())
            if path=='/api/jobs':
                items=[]
                for job in sorted((ROOT/'jobs').glob('*'),reverse=True):
                    if (job/'job.json').is_file():
                        try:
                            item=snapshot(job); items.append({'id':item['id'],'status':item['status'],'title':item.get('plan',item.get('legacy_plan',{})).get('title',job.name),'engine':item['engine']})
                        except (ValueError,OSError,KeyError): pass
                return self.json({'jobs':items[:40]})
            if path=='/api/job': return self.json(snapshot(job_path(query.get('id',[''])[0])))
            if path=='/api/source':
                job=job_path(query.get('job',[''])[0]); source_id=query.get('id',[''])[0]
                source=next((s for s in pv.load_json(job/'analysis/sources.json') if s['id']==source_id),None)
                if not source: raise ValueError('素材不存在。')
                if source['type']=='page' and not source.get('preview'):
                    import pymupdf
                    pdf=Path(source['file']).resolve()
                    if not pdf.is_relative_to(job.resolve()):raise ValueError('素材超出任务目录。')
                    preview_dir=job/'analysis/on_demand_previews';preview_dir.mkdir(exist_ok=True)
                    target=preview_dir/(str(source['page'])+'_'+str(abs(hash(str(pdf))))+'.png')
                    if not target.is_file():
                        with pymupdf.open(pdf) as document:document[source['page']-1].get_pixmap(matrix=pymupdf.Matrix(1.5,1.5),alpha=False).save(target)
                    return self.file(target,head)
                target=Path(source.get('preview') or source.get('file','')).resolve()
                if not target.is_relative_to(job.resolve()): raise ValueError('素材超出任务目录。')
                return self.file(target,head)
            if path.startswith('/files/'):
                parts=path.split('/',3)
                if len(parts)!=4: raise ValueError('文件路径无效。')
                job=job_path(parts[2]); target=(job/parts[3]).resolve()
                if not target.is_relative_to(job) or target.suffix.lower() not in ('.mp4','.jpg','.png','.srt','.txt','.json','.md','.vtt','.mov','.webm','.mp3','.wav','.zip'): raise ValueError('文件路径无效。')
                return self.file(target,head)
            target=WEB/('index.html' if path=='/' else path.lstrip('/'))
            target=target.resolve()
            if not target.is_relative_to(WEB.resolve()): raise ValueError('文件路径无效。')
            return self.file(target,head)
        except (ValueError,OSError,KeyError) as exc: self.json({'error':str(exc)},400)
    def do_POST(self):
        transaction=ExitStack()
        try:
            self.local_request(); path=urlparse(self.path).path
            if path=='/api/upload': return self.upload()
            data=self.body()
            if path in ('/api/plan','/api/resume','/api/render','/api/review','/api/export','/api/replan'):
                from studio_process import job_operation
                # Check, save and start form one transaction. A rejected request
                # must not overwrite the settings/plan of an accepted request.
                transaction.enter_context(self.server.manager.lock)
                transaction.enter_context(job_operation(job_path(data['job_id'])))
            elif path=='/api/create':
                transaction.enter_context(self.server.manager.lock)
                if self.server.manager.snapshot()['active']:
                    raise ValueError('已有任务正在进行，请等待或先停止。')
            if path=='/api/create':
                seconds=int(data.get('seconds',100))
                if not 30<=seconds<=600: raise ValueError('目标时长需在30–600秒之间。')
                if data.get('backend','codex') not in ('codex','api'): raise ValueError('未知的模型来源。')
                # Only accept attachment IDs generated by this server, never arbitrary file paths.
                files=[]
                reused=data.get('reuse_files',[])
                if not isinstance(reused,list) or any(type(index) is not int or index<0 for index in reused):
                    raise ValueError('复用附件编号无效。')
                if reused:
                    old_job=job_path(data.get('from_job',''))
                    original=pv.load_json(old_job/'request.json').get('files',[])
                    for index in dict.fromkeys(reused):
                        if index>=len(original):raise ValueError('复用附件编号无效。')
                        record=original[index]
                        if not Path(record['path']).is_file():raise ValueError(f'原附件已不存在，请重新上传：{record.get("name","附件")}')
                        files.append({'path':record['path'],'name':record.get('name',Path(record['path']).name)})
                for identifier in data.get('uploads',[]):
                    if not re.fullmatch(r'[a-f0-9]{32}\.[a-z0-9]+',identifier): raise ValueError('附件编号无效。')
                    target=(INBOX/identifier).resolve()
                    if not target.is_file() or not target.is_relative_to(INBOX.resolve()): raise ValueError('附件不存在，请重新添加。')
                    meta=pv.load_json(target.with_suffix(target.suffix+'.json'))
                    files.append({'path':str(target),'name':meta['name']})
                validate_settings(data)
                from studio_display import DISPLAY_KEYS
                request={k:data[k] for k in ('brief','script','article','links','seconds','voice','rate','backend','api_base','model','content_type','story_framework','auto_render','web_research','deep_read','final_review','auto_repair','export_formats',*DISPLAY_KEYS) if k in data}
                from studio_story import normalize_framework
                request['story_framework']=normalize_framework(request.get('story_framework'))
                request.setdefault('auto_render',False)
                request.setdefault('auto_repair',False)
                request['files']=files
                if not files and not request.get('article','').strip() and not request.get('script','').strip() and not request.get('links'): raise ValueError('请添加资料或粘贴文案、文章。')
                job=engine.new_job(request); self.server.manager.start(job,'prepare',data.get('key','').strip()); return self.json({'job_id':job.name})
            if path=='/api/plan':
                job=job_path(data['job_id']); self.ensure_idle(job)
                request=pv.load_json(job/'request.json') if (job/'request.json').is_file() else {}
                request=story_settings(request,data)
                sources=pv.load_json(job/'analysis/sources.json'); engine.validate(data['plan'],sources)
                previous=pv.load_json(job/'analysis/studio_plan.json')
                request=editorial_settings(request,data,previous)
                pv.save_json(job/'analysis/studio_plan.json',data['plan'])
                pv.save_json(job/'request.json',request)
                state=pv.load_json(job/'job.json'); state.update(status='prepared',error=None); pv.save_json(job/'job.json',state)
                return self.json({'saved':True,'script':request.get('script',''),'brief_revision':request.get('_brief_revision')})
            if path=='/api/resume':
                job=job_path(data['job_id']);self.ensure_idle(job,require_plan=False)
                state=pv.load_json(job/'job.json')
                if state.get('engine')!='studio':raise ValueError('此恢复入口仅用于视频工坊任务。')
                if state['status'] not in ('failed','cancelled','created'):raise ValueError('此任务无需恢复，请使用现有分镜继续制作。')
                request=model_settings(story_settings(pv.load_json(job/'request.json'),data),data)
                pv.save_json(job/'request.json',request)
                self.server.manager.start(job,'resume',data.get('key','').strip());return self.json({'job_id':job.name})
            if path=='/api/render':
                job=job_path(data['job_id']); self.ensure_idle(job)
                request=model_settings(story_settings(pv.load_json(job/'request.json'),data),data)
                choices={key:data[key] for key in ('final_review','auto_repair') if key in data}
                validate_settings(choices)
                request.update(choices)
                if 'display' in data:
                    from studio_display import DISPLAY_KEYS,validate_display
                    validate_display(data['display'])
                    request.update({k:v for k,v in data['display'].items() if k in DISPLAY_KEYS})
                if 'speech' in data:
                    validate_settings(data['speech'])
                    request.update({k:v for k,v in data['speech'].items() if k in ('voice','rate')})
                pv.save_json(job/'request.json',request)
                self.server.manager.start(job,'render',data.get('key','').strip()); return self.json({'job_id':job.name})
            if path=='/api/review':
                job=job_path(data['job_id']);self.ensure_idle(job)
                if (snapshot(job).get('recovery') or {}).get('action')!='review':
                    raise ValueError('此任务没有可单独重试的成片复查；内容或设置已改变时，请重新生成视频。')
                request=model_settings(pv.load_json(job/'request.json'),data)
                pv.save_json(job/'request.json',request)
                self.server.manager.start(job,'review',data.get('key','').strip());return self.json({'job_id':job.name})
            if path=='/api/export':
                job=job_path(data['job_id']);self.ensure_idle(job,require_plan=False);validate_settings(data)
                if not (job/'output/video.mp4').is_file():raise ValueError('请先生成视频')
                if not snapshot(job)['can_export']:raise ValueError('文稿已修改或视频生成未完成，请先重新生成视频')
                request=pv.load_json(job/'request.json');request['export_formats']=data.get('export_formats',['mp4'])
                pv.save_json(job/'request.json',request);self.server.manager.start(job,'export');return self.json({'job_id':job.name})
            if path=='/api/replan':
                job=job_path(data['job_id']); self.ensure_idle(job)
                plan=pv.load_json(job/'analysis/studio_plan.json'); request=model_settings(story_settings(pv.load_json(job/'request.json'),data),data)
                request['script']='\n\n'.join(block['narration'] for block in plan['blocks']); request['auto_render']=False
                pv.save_json(job/'request.json',request)
                self.server.manager.start(job,'prepare',data.get('key','').strip()); return self.json({'job_id':job.name})
            if path=='/api/cancel': self.server.manager.cancel(); return self.json({'stopped':True})
            if path=='/api/test-api':
                from model_api import test_connection
                return self.json({'message':test_connection(data,data.get('key','').strip())})
            raise ValueError('接口不存在。')
        except (ValueError,OSError,KeyError,RuntimeError) as exc: self.json({'error':str(exc)},400)
        finally:transaction.close()
    def ensure_idle(self,job,require_plan=True):
        if self.server.manager.snapshot()['active']: raise ValueError('制作中不能修改分镜，请先停止任务。')
        if recover_interrupted_job(job).get('status')=='processing':
            raise ValueError('此任务仍标记为制作中，可能由另一个工作台运行；请回到原工作台查看进度。尚未核实原制作进程已结束，为避免重复制作，本程序保留此状态。')
        if require_plan and not (job/'analysis/studio_plan.json').is_file(): raise ValueError('尚未生成可编辑的分镜，请先继续生成文稿与分镜。')
    def upload(self):
        length=int(self.headers.get('Content-Length','0')); name=unquote(self.headers.get('X-Filename',''))
        suffix=Path(name).suffix.lower()
        if suffix not in ALLOWED: raise ValueError('支持PDF、TXT、MD、图片及视频文件。')
        if not 0<length<=2*1024**3: raise ValueError('附件不能为空，单个文件最多2GB。')
        INBOX.mkdir(parents=True,exist_ok=True); identifier=uuid.uuid4().hex+suffix; target=INBOX/identifier
        remaining=length
        with target.open('wb') as stream:
            while remaining:
                chunk=self.rfile.read(min(1024**2,remaining))
                if not chunk: raise ValueError('文件传输中断。')
                stream.write(chunk); remaining-=len(chunk)
        pv.save_json(target.with_suffix(target.suffix+'.json'),{'name':Path(name).name,'size':length})
        self.json({'id':identifier,'name':Path(name).name,'size':length})
    def file(self,path,head=False):
        if not path.is_file(): raise ValueError('文件不存在或尚未生成。')
        size=path.stat().st_size; start,end=0,size-1; partial=False
        range_header=self.headers.get('Range','')
        if range_header:
            match=re.fullmatch(r'bytes=(\d*)-(\d*)',range_header)
            if not match: raise ValueError('不支持此视频范围。')
            left,right=match.groups()
            if left: start=int(left); end=min(int(right),end) if right else end
            elif right: start=max(0,size-int(right))
            if start>end or start>=size: self.send_error(416); return
            partial=True
        self.send_response(206 if partial else 200)
        self.send_header('Content-Type',mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
        self.send_header('Content-Length',str(end-start+1)); self.send_header('Accept-Ranges','bytes')
        self.send_header('Cache-Control','no-cache')
        if partial: self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        self.end_headers()
        if head:return
        try:
            with path.open('rb') as stream:
                stream.seek(start); remaining=end-start+1
                while remaining:
                    chunk=stream.read(min(256*1024,remaining))
                    if not chunk:break
                    self.wfile.write(chunk); remaining-=len(chunk)
        except (BrokenPipeError,ConnectionResetError):pass


def open_workbench(url):
    """Opening a browser is optional; keep the working server accessible."""
    try:
        if webbrowser.open(url):return True
    except Exception:
        pass
    console_print('服务可用，但未能自动打开浏览器。请在浏览器地址栏输入：'+url)
    return False


def compatible_server(existing):
    """A matching protocol alone does not mean backend fixes are loaded."""
    if existing.get('app')!=APP_ID:return False
    if existing.get('version')!=PROTOCOL_VERSION or existing.get('release')!=VERSION:
        raise RuntimeError(f'端口上的工作台版本为 {existing.get("release") or "未知旧版"}，当前程序为 {VERSION}。'
            '请先结束原任务，再关闭旧服务并重新启动，或用 --port 显式指定空闲端口。')
    return True


def main():
    configure_console()
    config=load_config(); validate_settings({'voice':config['default_voice'],'rate':config['default_rate']})
    parser=argparse.ArgumentParser(description=config['name']); parser.add_argument('--port',type=int,default=config['port']); parser.add_argument('--no-browser',action='store_true'); args=parser.parse_args()
    url=f'http://127.0.0.1:{args.port}'
    if args.port:
        try:
            with urlopen(url+'/api/health',timeout=2) as response:existing=json.load(response)
            if compatible_server(existing):
                console_print('工作台已经运行：'+url)
                if not args.no_browser:open_workbench(url)
                return
        except (OSError,ValueError):pass
    try:server=StudioHTTPServer(('127.0.0.1',args.port),Handler)
    except OSError:
        url=f'http://127.0.0.1:{args.port}'
        try:
            with urlopen(url+'/api/health',timeout=2) as response:existing=json.load(response)
        except Exception:raise RuntimeError('端口被其他程序占用，请用 --port 指定另一个端口。') from None
        if not compatible_server(existing):raise RuntimeError('端口被其他程序占用，请用 --port 指定另一个端口。')
        console_print('工作台已经运行：'+url)
        if not args.no_browser:open_workbench(url)
        return
    server.manager=Manager()
    url=f'http://127.0.0.1:{server.server_port}'
    console_print(config['name']+' 已启动：'+url)
    if not args.no_browser: threading.Timer(.5,lambda:open_workbench(url)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.manager.cancel(); server.server_close()

if __name__=='__main__':main()
