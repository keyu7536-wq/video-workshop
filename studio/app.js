'use strict';
const $ = id => document.getElementById(id);
const escapeHTML = text => String(text ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const state = {uploads:[], reuseFiles:[], mode:'auto', job:null, active:false, dirty:false, shot:null, crop:null, polling:false};
let config = {backend:'codex', api_base:'https://api.openai.com/v1', model:'gpt-4.1', rate:'+0%', key:''};
try { const saved=JSON.parse(localStorage.getItem('video-workshop-settings') || '{}'); for (const key of ['backend','api_base','model',...(saved.workflow_version===1?['rate']:[])]) if(saved[key]) config[key]=saved[key]; } catch (_) {}

async function api(path, options={}) {
  const response=await fetch(path,{...options,headers:{...(options.body?{'Content-Type':'application/json'}:{}),...options.headers}});
  const data=await response.json(); if(!response.ok) throw Error(data.error || '请求失败'); return data;
}
function toast(message) { $('toast').textContent=message; $('toast').hidden=false; clearTimeout(toast.timer); toast.timer=setTimeout(()=>$('toast').hidden=true,6000); }
function tab(name) { document.querySelectorAll('.view').forEach(view=>view.hidden=view.id!==`view-${name}`);$('quickCreate').hidden=name!=='create';document.querySelectorAll('[data-tab]').forEach(button=>{button.classList.toggle('active',button.dataset.tab===name);button.setAttribute('aria-selected',String(button.dataset.tab===name));}); }
document.querySelectorAll('[data-tab]').forEach(button=>button.addEventListener('click',()=>tab(button.dataset.tab)));
document.querySelectorAll('[data-close]').forEach(button=>button.addEventListener('click',()=>$(button.dataset.close).close()));
document.querySelectorAll('[data-mode]').forEach(button=>button.addEventListener('click',()=>{
  state.mode=button.dataset.mode; document.querySelectorAll('[data-mode]').forEach(b=>b.classList.toggle('selected',b===button));
  $('autoText').hidden=state.mode!=='auto';$('scriptText').hidden=state.mode!=='script';
  updateStoryHint();
}));

function updateStoryHint(){
  const order={auto:'按题材、受众和时长安排，兼顾可读性、解释深度与节奏。',problem_solution:'参考常见方法、问题、改进、具体做法和效果这些要点，按资料重点取舍、合并或调整顺序。',source_order:'沿着原文的主要内容顺序展开，根据受众与时长调整讲解深度。'}[$('storyFramework').value];
  const scope=state.job?.plan?'讲述结构用于新写稿；当前任务已有文稿，保存、重新配镜头和重新生成均保留原文。需要按新结构重写时，请新建任务并选择根据资料写稿。':state.mode==='script'?'口播按原文保留，框架仅供镜头组织参考，不会改写或重排已有文案。':$('article').value.trim()?'文章作为参考资料生成新文稿；需要逐字保留时，请选择“使用我的文案”。':'根据资料生成文稿，不编造资料没有支持的环节。';
  $('storyFrameworkHint').textContent=order+' '+scope;
}
$('storyFramework').onchange=()=>{if(state.job?.plan)state.dirty=true;updateStoryHint();};
$('article').addEventListener('input',updateStoryHint);

function settingsToForm() { $('backend').value=config.backend;$('apiBase').value=config.api_base;$('model').value=config.model;$('apiKey').value=config.key;$('rate').value=config.rate;$('apiFields').hidden=config.backend!=='api'; }
function settingsFromForm() { return {backend:$('backend').value,api_base:$('apiBase').value.trim(),model:$('model').value.trim(),key:$('apiKey').value.trim(),rate:$('rate').value}; }
function backendLabel() { $('backendLabel').textContent=config.backend==='api'?`API · ${config.model}`:'Codex CLI'; }
function modelSettings(){return {backend:config.backend,api_base:config.api_base,model:config.model,key:config.key};}
$('openSettings').onclick=()=>{settingsToForm();$('settingsDialog').showModal();};
$('backend').onchange=()=>{$('apiFields').hidden=$('backend').value!=='api';};
$('settingsForm').onsubmit=event=>{event.preventDefault();config=settingsFromForm(); const {key,...publicSettings}=config;localStorage.setItem('video-workshop-settings',JSON.stringify({...publicSettings,workflow_version:1}));backendLabel();$('settingsDialog').close();toast('设置已更新，后续制作会自动调用所选 AI。');};
$('testApi').onclick=async()=>{const button=$('testApi');button.disabled=true;$('apiTestMessage').textContent='正在测试图片输入与 JSON 输出…';try{const result=await api('/api/test-api',{method:'POST',body:JSON.stringify(settingsFromForm())});$('apiTestMessage').textContent=result.message;}catch(error){$('apiTestMessage').textContent=error.message;}finally{button.disabled=false;}};
backendLabel();

function renderUploads() {
  refreshPreviewSources();
  const retained=state.reuseFiles.map(index=>{const file=state.job.request.files[index];return `<div class="file-chip"><span>${escapeHTML(file.name)}</span><small>复用原附件</small><button type="button" data-unreuse="${index}" aria-label="不复用${escapeHTML(file.name)}">×</button></div>`;}).join('');
  $('attachments').innerHTML=retained+state.uploads.map((file,index)=>`<div class="file-chip"><span>${escapeHTML(file.name)}</span><small>${file.loading?'上传中…':(file.size/1024/1024).toFixed(1)+' MB'}</small><button type="button" data-remove="${index}" aria-label="移除${escapeHTML(file.name)}">×</button></div>`).join('');
  $('attachments').querySelectorAll('[data-remove]').forEach(button=>button.onclick=()=>{state.uploads.splice(Number(button.dataset.remove),1);renderUploads();});
  $('attachments').querySelectorAll('[data-unreuse]').forEach(button=>button.onclick=()=>{state.reuseFiles=state.reuseFiles.filter(index=>index!==Number(button.dataset.unreuse));renderUploads();});
}
async function addFiles(files) {
  for (const file of files) {
    const record={name:file.name,size:file.size,loading:true,previewURL:/^(image|video)\//.test(file.type)?URL.createObjectURL(file):null,previewType:file.type};state.uploads.push(record);renderUploads();
    try { const response=await fetch('/api/upload',{method:'POST',headers:{'X-Filename':encodeURIComponent(file.name),'Content-Type':'application/octet-stream'},body:file});const data=await response.json();if(!response.ok)throw Error(data.error);Object.assign(record,data,{loading:false}); }
    catch(error){state.uploads=state.uploads.filter(item=>item!==record);toast(error.message);}
    renderUploads();
  }
}
$('dropzone').onclick=()=>$('fileInput').click();$('dropzone').onkeydown=event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();$('fileInput').click();}};
$('fileInput').onchange=event=>{addFiles(event.target.files);event.target.value='';};
$('dropzone').ondragover=event=>{event.preventDefault();$('dropzone').classList.add('drag');};$('dropzone').ondragleave=()=>$('dropzone').classList.remove('drag');$('dropzone').ondrop=event=>{event.preventDefault();$('dropzone').classList.remove('drag');addFiles(event.dataTransfer.files);};

async function refreshJobs() { try { const result=await api('/api/jobs');$('jobList').innerHTML=result.jobs.length?result.jobs.map(job=>`<button class="job-button ${state.job?.id===job.id?'selected':''}" data-job="${escapeHTML(job.id)}">${escapeHTML(job.title)}<small>${({completed:'已生成',review_needed:'待复核草稿',prepared:'分镜已准备',failed:'需要处理',processing:'制作中',created:'新任务',cancelled:'已停止'})[job.status] || job.status}</small></button>`).join(''):'<p class="small muted">你的创作会显示在这里。</p>';$('jobList').querySelectorAll('[data-job]').forEach(button=>button.onclick=()=>loadJob(button.dataset.job)); } catch(error){toast(error.message);} }
$('refreshJobs').onclick=refreshJobs;
$('newProject').onclick=()=>{if(state.active)return toast('请等待当前制作完成，或先停止任务。');state.job=null;state.dirty=false;restoreInputs();$('contentType').value='auto';$('storyFramework').value='auto';updateStoryHint();$('voice').value=appDefaults.default_voice;const saved=JSON.parse(localStorage.getItem('video-workshop-settings')||'{}');config.rate=saved.rate||appDefaults.default_rate;settingsToForm();displayToForm({layout_style:'immersive',show_sources:false,show_disclosure:false,minimal_text:true});renderUploads();$('projectName').textContent='新的视频';$('taskStatus').hidden=true;$('researchPanel').innerHTML='';$('reviewBanner').innerHTML='';$('storyboard').innerHTML='';$('finalReviewPanel').innerHTML='';$('downloadLinks').innerHTML='';$('videoPlayer').pause();$('videoPlayer').removeAttribute('src');$('videoPlayer').hidden=true;$('videoEmpty').hidden=false;setPlaybackAudioJob(null);renderMeasuredSegments(false);updateControls();tab('create');refreshJobs();};

function updateControls() { const editable=Boolean(state.job?.plan),recovery=state.job?.recovery;$('retryJob').hidden=!recovery;$('retryJob').disabled=state.active;$('retryJob').textContent=recovery?.label||'继续制作';$('retryReview').hidden=recovery?.action!=='review';$('retryReview').disabled=state.active;$('regenerateDisplay').hidden=!editable;$('regenerateDisplay').disabled=state.active;$('createButton').disabled=state.active;$('quickCreate').disabled=state.active;$('savePlan').disabled=!editable||state.active;$('renderButton').disabled=!editable||state.active;$('replan').disabled=!editable||state.active;$('editInputs').hidden=!state.job;$('editInputs').disabled=state.active;$('exportButton').disabled=state.dirty||state.active||!(state.job?.can_export??['completed','review_needed'].includes(state.job?.status)); }
$('quickCreate').onclick=()=>$('createButton').click();
$('createButton').onclick=async()=>{
  if(state.uploads.some(file=>file.loading))return toast('请等待附件上传完成。');
  try{validateDisplayForm();await requireDisplayServer();}catch(error){return toast(error.message);}
  const body={...config,content_type:$('contentType').value,story_framework:$('storyFramework').value,brief:$('brief').value.trim(),article:$('article').value.trim(),script:state.mode==='script'?$('script').value.trim():'',
    uploads:state.uploads.map(file=>file.id),from_job:state.job?.id,reuse_files:state.reuseFiles,links:$('links').value.split(/\n/).map(value=>value.trim()).filter(Boolean),seconds:Number($('seconds').value),
    ...displaySettings(),voice:$('voice').value,auto_render:$('autoRender').checked,web_research:$('webResearch').checked,deep_read:$('deepRead').checked,final_review:$('finalReview').checked,auto_repair:$('autoRepair').checked,export_formats:['mp4','wav','vtt']};
  if(!body.uploads.length&&!body.reuse_files.length&&!body.links.length&&!body.article&&!body.script)return toast('请先粘贴文案、文章或添加资料。');
  if(state.mode==='script'&&!body.script)return toast('请填写你希望保留的文案。');
  try{state.active=true;updateControls();const result=await api('/api/create',{method:'POST',body:JSON.stringify(body)});state.job={id:result.job_id};state.reuseFiles=[];renderUploads();$('taskStatus').hidden=false;toast('任务已开始，AI 正在读取资料。');await poll();}catch(error){state.active=false;updateControls();toast(error.message);}
};

async function loadJob(id) {
  if(state.dirty&&!confirm('当前文稿有未保存修改，是否放弃并打开另一个任务？'))return;
  try{state.job=await api('/api/job?id='+encodeURIComponent(id));state.dirty=false;restoreInputs(state.job.request||{});$('contentType').value=state.job.request?.content_type||'auto';$('storyFramework').value=state.job.request?.story_framework||'auto';updateStoryHint();displayToForm(state.job.request||{});if(state.job.request?.voice)$('voice').value=state.job.request.voice;if(state.job.request?.rate)config.rate=state.job.request.rate;const savedModel=state.job.request||{};if(savedModel.api_base&&savedModel.api_base!==config.api_base)config.key='';for(const key of ['backend','api_base','model'])if(savedModel[key])config[key]=savedModel[key];settingsToForm();backendLabel();refreshPreviewSources();$('projectName').textContent=state.job.plan?.title||state.job.legacy_plan?.title||'视频任务';renderEditor();renderOutput();updateControls();refreshJobs();tab(['completed','review_needed'].includes(state.job.status)?'output':'editor');}catch(error){toast(error.message);}
}
function sourceURL(sourceId) { return `/api/source?job=${encodeURIComponent(state.job.id)}&id=${encodeURIComponent(sourceId)}`; }
function renderEditor() {
  const job=state.job, plan=job?.plan;
  $('editorBriefWrap').hidden=!plan;$('editorBrief').value=job?.request?.brief||'';
  const failure=job?.status==='failed'?(job.error||'本次生成未完成，请查看制作日志。'):'';
  const failureHTML=failure?`<div class="notice error" role="alert"><strong>本次生成未完成</strong><div style="margin-top:8px;white-space:pre-wrap;overflow-wrap:anywhere">${escapeHTML(failure)}</div></div>`:'';
  $('reviewBanner').innerHTML=failureHTML;
  if(!plan){$('editorSummary').textContent=failure?'文稿与分镜尚未生成，请根据下方具体错误处理。':job?.legacy_plan?'这是旧版任务，可在“预览与导出”播放；新建任务后支持可视化编辑。':'先添加资料，AI 会生成文稿与分镜。';$('storyboard').innerHTML='';return;}
  const characters=plan.blocks.reduce((n,block)=>n+block.narration.length,0);
  $('editorSummary').textContent=`${plan.blocks.length} 段讲解 · ${characters} 字 · 可修改文案、换镜头、拖动裁切图表`;
  const issues=[...(plan.source_issues||[]),...(job.review?.issues||[]).map(issue=>`${issue.severity==='error'?'需要处理':'提示'}：${issue.reason}`)];
  if(job.review?.ready===false&&!job.review.issues?.length)issues.push('需要处理：'+(job.review.summary||'内容复核未通过，未返回具体问题，请查看制作日志。'));
  const reviewHTML=issues.length?`<div class="notice ${job.review?.ready===false?'error':'warning'}"><details ${job.review?.ready===false?'open':''}><summary>已完成 AI 内容复核 · ${issues.length} 条${job.review?.ready===false?'需要处理的问题':'制作提示'}，点击查看</summary><div style="margin-top:9px">${issues.map(escapeHTML).join('<br>')}</div></details></div>`:job.review?.ready===true?'<div class="notice">资料与文稿已完成自动复核。你仍可在下方修改，导出前会再次检查更改。</div>':'<div class="notice">文稿与分镜已生成，尚无内容复核结果。</div>';
  $('reviewBanner').innerHTML=failureHTML+reviewHTML;
  $('storyboard').innerHTML=plan.blocks.map((block,bi)=>`<article class="story-card"><div class="block-index">${String(bi+1).padStart(2,'0')}</div><div><div class="block-heading"><input aria-label="第${bi+1}段章节" data-chapter="${bi}" maxlength="20" value="${escapeHTML(block.chapter)}"></div><textarea aria-label="第${bi+1}段文稿" data-narration="${bi}" rows="4">${escapeHTML(block.narration)}</textarea><div class="evidence-note">${block.evidence.length?`${block.evidence.length} 条原文依据 · ${block.evidence.map(item=>escapeHTML(item.quote.slice(0,38))+'…').join(' / ')}`:'文案／画面讲解，暂无逐句文字依据'}</div></div><div class="shot-list">${block.shots.map((shot,si)=>`<button class="shot-mini" data-shot="${bi},${si}">${['page','image'].includes(shot.kind)?`<img src="${sourceURL(shot.source_id)}" alt="素材预览" loading="lazy">`:'<span style="font-size:23px;padding:0 9px">'+(shot.kind==='video'?'▷':'▤')+'</span>'}<span>${escapeHTML(shot.label||'讲解画面')}<small>${({page:'PDF 原图',image:'提供的图片',video:'原视频',card:'讲解卡片',diagram:'旁白驱动动态图'})[shot.kind]} · ${escapeHTML(plannedShotTiming(shot))}${shot.cue?' · 读到“'+escapeHTML(shot.cue)+'”切入':''}</small></span></button>`).join('')}</div></article>`).join('');
  $('storyboard').querySelectorAll('[data-narration]').forEach(input=>input.oninput=()=>{plan.blocks[Number(input.dataset.narration)].narration=input.value;state.dirty=true;updateControls();renderMeasuredSegments(false);});
  $('storyboard').querySelectorAll('[data-chapter]').forEach(input=>input.oninput=()=>{plan.blocks[Number(input.dataset.chapter)].chapter=input.value;state.dirty=true;updateControls();renderMeasuredSegments(false);});
  $('storyboard').querySelectorAll('[data-shot]').forEach(button=>button.onclick=()=>openShot(...button.dataset.shot.split(',').map(Number)));
}
async function savePlan() { if(!state.job?.plan)throw Error('请先生成文稿与分镜。');await requireDisplayServer();const saved=await api('/api/plan',{method:'POST',body:JSON.stringify({job_id:state.job.id,plan:state.job.plan,brief:$('editorBrief').value,content_type:$('contentType').value,story_framework:$('storyFramework').value})});state.job.request={...(state.job.request||{}),brief:$('editorBrief').value,script:saved.script??state.job.request?.script??'',_brief_revision:saved.brief_revision,content_type:$('contentType').value,story_framework:$('storyFramework').value};$('brief').value=$('editorBrief').value;state.job.status='prepared';state.job.error=null;state.job.recovery=null;state.job.can_export=false;if(state.job.final_review)state.job.final_review.stale=true;state.dirty=false;renderEditor();renderOutput();updateControls(); }
$('savePlan').onclick=async()=>{try{await savePlan();toast('文稿与镜头修改已保存。');}catch(error){toast(error.message);}};
$('renderButton').onclick=async()=>{try{validateDisplayForm();await requireDisplayServer();await savePlan();await api('/api/render',{method:'POST',body:JSON.stringify({job_id:state.job.id,...modelSettings(),content_type:$('contentType').value,story_framework:$('storyFramework').value,display:displaySettings(),final_review:$('finalReview').checked,auto_repair:$('autoRepair').checked,speech:{voice:$('voice').value,rate:config.rate}})});state.active=true;state.job.status='processing';renderOutput();updateControls();$('taskStatus').hidden=false;tab('output');poll();}catch(error){toast(error.message);}};
$('replan').textContent='按文稿新建分镜';
$('reviewBanner').insertAdjacentHTML('beforebegin','<details id="editorBriefWrap" class="panel" hidden><summary>修改创作要求</summary><label for="editorBrief">受众、重点和画面要求</label><textarea id="editorBrief" rows="4" maxlength="20000"></textarea><p class="small muted">保存后按新要求重新复核；已生成的视频保留，重新生成后才能对应本次修改。</p></details>');
$('editorBrief').oninput=()=>{if(state.job){state.job.request={...(state.job.request||{}),brief:$('editorBrief').value};state.dirty=true;updateControls();renderMeasuredSegments(false);}};
$('replan').setAttribute('aria-describedby','replanHint');
$('reviewBanner').insertAdjacentHTML('beforebegin','<p id="replanHint" class="small muted" style="margin-bottom:16px">删改文稿后，可用“按文稿新建分镜”重新安排镜头：逐字保留当前文稿，复用本任务原始资料，另建任务。原任务与已有成片保留，新分镜确认后再制作。</p>');
$('replan').onclick=async()=>{
  if(state.active)return;
  try{
    if(!state.job?.plan)throw Error('请先生成可编辑的文稿。');
    const script=state.job.plan.blocks.map(block=>block.narration).join('\n\n');
    if(!script.trim())throw Error('请先填写要保留的文稿。');
    // New narration may invalidate old shot cues. Rebuild only from text and
    // original inputs; never save those invalid shots over the original plan.
    const {files=[],...original}=state.job.request||{};
    const body={...original,...modelSettings(),script,uploads:[],from_job:state.job.id,
      reuse_files:files.map((_,index)=>index),auto_render:false,
      content_type:$('contentType').value,story_framework:$('storyFramework').value};
    state.active=true;updateControls();await requireDisplayServer();
    const result=await api('/api/create',{method:'POST',body:JSON.stringify(body)});
    state.job={id:result.job_id};state.dirty=false;state.uploads=[];state.reuseFiles=[];
    renderUploads();renderEditor();renderOutput();updateControls();
    $('projectName').textContent='改稿新任务';$('taskStatus').hidden=false;tab('editor');
    toast('已按当前文稿新建分镜任务，文稿逐字保留；原任务和成片不变。');
    await refreshJobs();await poll();
  }catch(error){state.active=false;updateControls();toast(error.message);}
};
$('retryJob').onclick=async()=>{try{const action=state.job?.recovery?.action;if(action==='render')return await $('renderButton').onclick();if(action==='export')return await $('exportButton').onclick();if(!['resume','review'].includes(action))throw Error('当前任务没有可恢复的制作步骤。');if(state.dirty){if(action==='review')throw Error('文稿有未保存修改，请先保存并重新生成视频。');return await $('replan').onclick();}await requireDisplayServer();await api('/api/'+action,{method:'POST',body:JSON.stringify({job_id:state.job.id,...modelSettings(),...(action==='resume'?{content_type:$('contentType').value,story_framework:$('storyFramework').value}:{})})});state.active=true;state.job.status='processing';state.job.error=null;state.job.recovery=null;renderEditor();updateControls();$('taskStatus').hidden=false;toast(action==='review'?'正在重新复查已有成片，不重新生成配音或画面。':'已继续原任务，复用已读取的资料。');poll();}catch(error){toast(error.message);}};
$('retryReview').onclick=()=>$('retryJob').onclick();

function renderOutput() {
  const job=state.job;if(!job)return;
  $('outputTitle').textContent=job.plan?.title||job.legacy_plan?.title||job.publishing?.title||'成片预览';
  const report=job.report;
  $('outputMeta').textContent=report?`${report.duration_seconds.toFixed(1)} 秒 · ${report.resolution} · ${report.fps} fps · ${report.shots} 个镜头`:'分镜确认后，自动配音并合成视频。';
  const available=(job.can_export??['completed','review_needed'].includes(job.status))&&job.video_url;
  renderMeasuredSegments(available);$('videoTag').textContent=available?(job.status==='failed'?'成片已生成 · 导出未完成':job.status==='review_needed'?'待复核草稿':'已生成'):'待生成';$('videoPlayer').hidden=!available;$('videoEmpty').hidden=Boolean(available);
  if(available){if($('videoPlayer').getAttribute('src')!==job.video_url)$('videoPlayer').src=job.video_url;if(job.poster_url)$('videoPlayer').poster=job.poster_url;}
  else {$('videoPlayer').pause();$('videoPlayer').removeAttribute('src');}
  setPlaybackAudioJob(available?job:null);
  const prefix=`/files/${encodeURIComponent(job.id)}/`;
  renderResearch();renderFinalReview();$('exportButton').disabled=!available||state.active;$('exportButton').textContent=job.recovery?.action==='export'?'重试导出所选格式':'导出所选格式';
  $('downloadLinks').innerHTML=(available?`<a href="${prefix}output/video.mp4" download>↓ 下载 MP4 成片</a><a href="${prefix}output/subtitles.srt" download>↓ 下载 SRT 字幕</a>`:'')+(job.plan||job.legacy_plan?`<a href="${prefix}narration.txt" download>↓ 下载中文讲解稿</a><a href="${prefix}evidence.json" download>↓ 查看资料依据与裁切记录</a>`:'<span class="muted">等待生成文件…</span>');
  if(available&&job.exports?.files){$('downloadLinks').innerHTML=job.exports.files.map(file=>`<a href="${prefix}${escapeHTML(file.path)}" download>↓ ${escapeHTML(file.name)}</a>`).join('')+`<a href="${prefix}narration.txt" download>↓ 中文讲解稿</a><a href="${prefix}evidence.json" download>↓ 资料依据</a>`;}
  if(job.research)$('downloadLinks').innerHTML+=`<a href="${prefix}research.md" download>↓ 研究笔记与来源</a>`;
  $('publishTitle').value=job.publishing?.title||job.plan?.title||'';$('publishDescription').value=job.publishing?.description||job.plan?.summary||'';
}
$('copyPublishing').onclick=async()=>{try{await navigator.clipboard.writeText($('publishTitle').value+'\n\n'+$('publishDescription').value);toast('标题与简介已复制。');}catch(_){toast('请在输入框中选择文字并复制。');}};

function openShot(bi,si) {
  state.shot=[bi,si];const shot=state.job.plan.blocks[bi].shots[si];state.originalBBox=[...shot.bbox];state.crop=shot.focus?.length===4?[...shot.focus]:shot.bbox.length===4?[...shot.bbox]:[0,0,1,1];
  $('shotFocus').checked=Boolean(shot.focus?.length);$('shotKeepFull').checked=Boolean(shot.keep_full);
  $('shotSource').innerHTML='<option value="card">讲解卡片</option>'+(shot.kind==='diagram'?'<option value="diagram">原理动态图 · 按旁白推进</option>':'')+(state.job.sources||[]).filter(source=>['page','image','video'].includes(source.type)).map(source=>`<option value="${escapeHTML(source.id)}">${escapeHTML(source.name)}${source.page?' · 第'+source.page+'页':''}</option>`).join('');
  $('shotSource').value=['card','diagram'].includes(shot.kind)?shot.kind:shot.source_id;$('shotIn').value=shot.in;$('shotDuration').value=shot.duration;$('shotLabel').value=shot.label;$('shotCue').value=shot.cue;$('shotCardText').value=shot.card_text;
  updateShotSource();$('shotDialog').showModal();
}
function updateShotSource(){const source=(state.job.sources||[]).find(source=>source.id===$('shotSource').value);const image=['page','image'].includes(source?.type);$('cropWrap').hidden=!image;$('cropHint').hidden=!image;$('focusControl').hidden=!image;$('keepControl').hidden=source?.type!=='video';$('cropHint').textContent='拖选裁切范围；勾选高亮则只加框，保留上下文。';$('cardTextLabel').hidden=$('shotSource').value!=='card';$('shotIn').disabled=source?.type!=='video';if(image){$('cropImage').src=sourceURL(source.id);$('cropImage').onload=drawCrop;}updateShotPreview();}
$('shotSource').onchange=()=>{state.crop=[0,0,1,1];updateShotSource();};
function drawCrop(){const [x0,y0,x1,y1]=state.crop;$('cropBox').style.cssText=`left:${x0*100}%;top:${y0*100}%;width:${(x1-x0)*100}%;height:${(y1-y0)*100}%;`;}
let cropStart=null;
function cropPoint(event){const box=$('cropImage').getBoundingClientRect();return [Math.max(0,Math.min(1,(event.clientX-box.left)/box.width)),Math.max(0,Math.min(1,(event.clientY-box.top)/box.height))];}
$('cropWrap').onpointerdown=event=>{event.preventDefault();cropStart=cropPoint(event);$('cropWrap').setPointerCapture(event.pointerId);};
$('cropWrap').onpointermove=event=>{if(!cropStart)return;const point=cropPoint(event);state.crop=[Math.min(cropStart[0],point[0]),Math.min(cropStart[1],point[1]),Math.max(cropStart[0],point[0]),Math.max(cropStart[1],point[1])];drawCrop();};
$('cropWrap').onpointerup=()=>{cropStart=null;};
$('shotForm').onsubmit=event=>{event.preventDefault();const [bi,si]=state.shot,shot=state.job.plan.blocks[bi].shots[si],source=(state.job.sources||[]).find(source=>source.id===$('shotSource').value),focus=$('shotFocus').checked&&['page','image'].includes(source?.type);Object.assign(shot,{kind:$('shotSource').value==='diagram'?'diagram':source?.type||'card',source_id:source?.id||'',in:Number($('shotIn').value)||0,duration:Number($('shotDuration').value),bbox:['page','image'].includes(source?.type)?(focus?(state.originalBBox.length===4?state.originalBBox:source.type==='page'?[0,0,1,1]:[]):[...state.crop]):[],focus:focus?[...state.crop]:[],keep_full:source?.type==='video'&&$('shotKeepFull').checked,label:$('shotLabel').value,card_text:$('shotCardText').value,cue:$('shotCue').value});state.dirty=true;$('shotDialog').close();renderEditor();updateControls();renderMeasuredSegments(false);};

let previousActive=false;
async function poll(){if(state.polling)return;state.polling=true;try{const wasActive=state.active||previousActive;const result=await api('/api/status');state.active=result.active;updateControls();
  if(result.job_id&&(result.active||wasActive)){$('taskStatus').hidden=false;$('taskLogs').textContent=result.logs.join('\n');const stage=[...result.logs].reverse().find(line=>/^【(资料读取|文稿与分镜|成片与导出)】/.test(line));const match=stage?.match(/^【([^】]+)】\s*(.*)/);$('taskTitle').textContent=result.active?(match?match[1]+' · '+match[2]:'正在准备任务…'):'本次处理结束';$('progressFill').style.width=(result.active?({资料读取:20,文稿与分镜:50,成片与导出:85}[match?.[1]]||8):100)+'%';$('taskSpinner').hidden=!result.active;$('cancelTask').hidden=!result.active;
    // Global worker activity must not replace the editor's selected job. Its
    // plan, source URLs and visible controls all belong to the viewed job.
    if(wasActive&&!result.active){if(state.job?.id===result.job_id){await loadJob(result.job_id);if(state.job?.id===result.job_id)toast(state.job.status==='completed'?'视频已生成。':state.job.status==='prepared'?'文稿与分镜已准备。':state.job.error||'任务已停止。');}await refreshJobs();}
  }previousActive=result.active;
}catch(error){console.error(error);}finally{state.polling=false;}}
$('cancelTask').onclick=async()=>{try{await api('/api/cancel',{method:'POST',body:'{}'});toast('已停止制作，资料和配音缓存保留。');}catch(error){toast(error.message);}};


function renderResearch(){
  const job=state.job,d=job?.research;if(!d){$('researchPanel').innerHTML='';return;}
  const labels={problem:'问题',prior_work:'已有方法',mechanism:'核心机制',results:'实验依据',limits:'局限',implementation:'项目实现'};
  const coverage=job.research_manifest?.media_coverage;
  const coverageHTML=coverage?`<p><b>素材盘点：</b>发现 ${coverage.discovered} 个链接，下载／抽帧 ${coverage.sampled} 项，另有 ${coverage.not_reviewed} 项未逐项查看（包含重复画幅、编码、封面或预算限制）。<a href="/files/${encodeURIComponent(job.id)}/${escapeHTML(coverage.inventory||'analysis/research/media_inventory.json')}" target="_blank">查看逐项记录</a></p>`:'';
  $('researchPanel').innerHTML=`<details class="panel research-notes"><summary>研究笔记 · ${escapeHTML(d.title)} · ${d.claims.length} 条有原文依据的解读</summary><p>${escapeHTML(d.project_identity)}</p>${coverageHTML}<p><b>故事切入：</b>${escapeHTML(d.story_angle)}</p>${d.claims.map(c=>`<p><b>${labels[c.topic]}：</b>${escapeHTML(c.explanation)}<small>依据：${escapeHTML(c.quote)}</small></p>`).join('')}<p><b>资料缺口：</b>${escapeHTML([...d.missing_information,...(job.research_manifest?.gaps||[])].join('；')||'未报告')}</p><a href="/files/${encodeURIComponent(job.id)}/research.md" download>下载完整笔记</a></details>`;
}
function renderFinalReview(){
  const r=state.job?.final_review;
  if(r?.stale){$('finalReviewPanel').innerHTML='<p class="notice warning">文稿、配音、画面设置或视频版本与复核记录不一致；请重新生成并复查。</p>';return;}
  if(r?.status==='unavailable'){$('finalReviewPanel').innerHTML=`<div class="notice error"><strong>成片已保留，内容复查未完成</strong><p>${escapeHTML(r.unavailable_reason||r.summary||'审核服务暂不可用。')}</p>${(r.issues||[]).map(issue=>`<p>${issue.severity==='error'?'需要处理':'提示'}：${escapeHTML(issue.reason)}</p>`).join('')}<p>${state.job.recovery?.action==='review'?'可以重试复查，无需重新生成配音和画面。':'成片的生成记录缺失或内容已经改变，请重新生成视频后复查。'}</p></div>`;return;}
  $('finalReviewPanel').innerHTML=r?`<details class="notice ${r.ready?'':'error'}" ${r.ready?'':'open'}><summary>${r.ready?'已完成两项成片复查':'成片仍有待处理问题'} · 第 ${r.round} 轮</summary><p>${escapeHTML(r.coverage)}</p>${r.issues.map(i=>`<p>${i.severity==='error'?'需要处理':'提示'}：${escapeHTML(i.reason)}</p>`).join('')}</details>`:'<p class="small muted">此任务尚无成片内容复查记录。</p>';
}
$('exportButton').onclick=async()=>{try{const selected=$('exportFormat').value;const formats=[...new Set([...(state.job.request?.export_formats||['mp4']),selected])];await api('/api/export',{method:'POST',body:JSON.stringify({job_id:state.job.id,export_formats:formats})});state.active=true;updateControls();$('taskStatus').hidden=false;toast('正在导出，完成后下载链接会自动更新。');poll();}catch(error){toast(error.message);}};
function restoreInputs(request={}){
  for(const id of ['brief','article','script'])$(id).value=request[id]||'';
  $('links').value=(request.links||[]).join('\n');
  if(request.seconds){
    if(![...$('seconds').options].some(option=>Number(option.value)===request.seconds))$('seconds').add(new Option(`约 ${request.seconds} 秒`,request.seconds));
    $('seconds').value=request.seconds;
  }
  state.mode=request.script?'script':'auto';
  document.querySelectorAll('[data-mode]').forEach(button=>button.classList.toggle('selected',button.dataset.mode===state.mode));
  $('autoText').hidden=state.mode!=='auto';$('scriptText').hidden=state.mode!=='script';
  for(const [id,key,fallback] of [['autoRender','auto_render',false],['autoRepair','auto_repair',false],['deepRead','deep_read',true],['webResearch','web_research',true],['finalReview','final_review',true]])$(id).checked=request[key]??fallback;
  state.uploads=[];state.reuseFiles=(request.files||[]).map((_,index)=>index);renderUploads();
  $('newTaskHint').hidden=!state.job;
  $('createButton').textContent=state.job?'用修改后的资料新建任务 ↗':'生成文稿与分镜 ↗';
  $('quickCreate').textContent=state.job?'用这些资料新建任务 ↗':'生成文稿与分镜 ↗';
}
$('paperScreeningBrief').onclick=()=>{
  const template='用途：论文初筛，帮助我判断是否精读。讲清必要背景、研究问题、方法怎样起作用，以及结果的适用范围和局限；按内容安排顺序，不套固定章节，不用无依据的评分或“必读”结论。';
  if(!$('brief').value.includes(template))$('brief').value=[$('brief').value.trim(),template].filter(Boolean).join('\n');
  $('brief').focus();
};
let appDefaults={name:'视频工坊',default_voice:'zh-CN-YunyangNeural',default_rate:'+0%'};
async function initialize(){
  try{
    const provided=await api('/api/app-config');appDefaults={...appDefaults,...provided};
    const name=String(appDefaults.name||'视频工坊');document.title=name+' · 视频创作工作台';$('appName').textContent=name;$('appBrand').setAttribute('aria-label',name+' 首页');
    if([...$('voice').options].some(option=>option.value===appDefaults.default_voice))$('voice').value=appDefaults.default_voice;
    const saved=JSON.parse(localStorage.getItem('video-workshop-settings')||'{}');
    if(!saved.rate&&[...$('rate').options].some(option=>option.value===appDefaults.default_rate))config.rate=appDefaults.default_rate;
  }catch(error){console.warn('Using built-in application defaults:',error.message);}
  settingsToForm();backendLabel();refreshPreviewSources();updateDisplayPreview();updateStoryHint();
  await refreshJobs();const initialJob=new URLSearchParams(location.search).get('job');if(initialJob)await loadJob(initialJob);
  await poll();setInterval(poll,1600);
}
initializeShotPreviews();
initialize();
