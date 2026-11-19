'use strict';

function plannedShotTiming(shot){
  return shot.kind==='video'?`取片 ${shot.duration} 秒`:`停留权重 ${shot.duration}（实际时长配音后确定）`;
}

let playbackAudioContext=null,playbackAudioSequence=0;
const playbackAudioPanels=[];
function sameMediaURL(left,right){
  try{return new URL(left,location.href).href===new URL(right,location.href).href;}catch(_){return false;}
}
function playbackTaskURL(jobId){
  const url=new URL(location.pathname,location.origin);url.searchParams.set('job',jobId);return url.href;
}
function refreshPlaybackAudioHints(){
  for(const {video,panel,message,address,open,level} of playbackAudioPanels){
    const context=playbackAudioContext;
    const ready=context&&video.getAttribute('src')&&video.readyState>=1&&
      sameMediaURL(video.currentSrc,context.videoURL)&&sameMediaURL(video.getAttribute('src'),context.videoURL);
    panel.hidden=true;open.hidden=true;level.hidden=true;
    if(!ready)continue;
    if(video.muted||video.volume<1){
      level.textContent=video.muted?'此播放器已静音，可点击播放器的扬声器按钮开启声音。':
        video.volume===0?'此播放器音量为 0，请在播放器中调高音量。':`此播放器音量：${Math.round(video.volume*100)}%`;
      level.hidden=false;
    }
    if(context.audioCheck===false){
      message.textContent='任务记录中的音频检查未通过，请查看生成记录或重新生成。';panel.hidden=false;continue;
    }
    const codec=context.codec.toLowerCase();
    const aac=codec==='aac'||codec.startsWith('mp4a.40');
    const mp4=/\.(mp4|m4v|mov)$/i.test(new URL(context.videoURL,location.href).pathname);
    // An empty capability result is not evidence that a file lacks an audio
    // track. Nor does the MP4 extension prove that its audio codec is AAC.
    const capability=video.canPlayType('audio/mp4; codecs="mp4a.40.2"');
    if((aac||(!codec&&mp4))&&!capability){
      message.textContent=aac?'当前浏览器不支持这份成片使用的 AAC 音频，可能有画面却无声。请复制任务地址，在 Edge 或 Chrome 中打开。':
        '当前浏览器不支持 AAC 音频。如果这份 MP4 使用 AAC，可能有画面却无声。请复制任务地址，在 Edge 或 Chrome 中核对。';
      address.value=playbackTaskURL(context.jobId);open.hidden=false;panel.hidden=false;
    }
  }
}
function attachPlaybackAudioHint(video,anchor){
  const id=video.id+'Audio';
  anchor.insertAdjacentHTML('afterend',`<section id="${id}Notice" class="notice warning audio-compatibility" role="status" aria-live="polite" hidden>
    <p data-audio-message></p><div data-audio-open hidden><label for="${id}Address">在 Edge / Chrome 地址栏粘贴</label>
    <div class="audio-address"><input id="${id}Address" readonly aria-label="当前任务地址"><button type="button" class="secondary" data-copy-task>复制任务地址</button></div>
    <p class="small" data-copy-feedback></p></div></section><p id="${id}Level" class="audio-level small" aria-live="polite" hidden></p>`);
  const panel=$(id+'Notice'),address=$(id+'Address');
  playbackAudioPanels.push({video,panel,address,message:panel.querySelector('[data-audio-message]'),
    open:panel.querySelector('[data-audio-open]'),level:$(id+'Level')});
  panel.querySelector('[data-copy-task]').onclick=async()=>{
    const text=address.value,feedback=panel.querySelector('[data-copy-feedback]');
    try{await navigator.clipboard.writeText(text);feedback.textContent='已复制，请到 Edge 或 Chrome 地址栏粘贴。';}
    catch(_){address.focus();address.select();feedback.textContent='未能自动复制，地址已选中，请按 Ctrl+C 复制。';}
  };
  for(const event of ['loadedmetadata','loadeddata','volumechange','loadstart','emptied','error'])video.addEventListener(event,refreshPlaybackAudioHints);
}
function setPlaybackAudioJob(job){
  if(job?.id===playbackAudioContext?.jobId&&job?.video_url===playbackAudioContext?.videoURL){refreshPlaybackAudioHints();return;}
  const sequence=++playbackAudioSequence;
  playbackAudioContext=job?.video_url?{jobId:job.id,videoURL:job.video_url,audioCheck:null,
    codec:typeof job.report?.audio_codec==='string'?job.report.audio_codec:''}:null;
  for(const {panel,address} of playbackAudioPanels){address.value='';panel.querySelector('[data-copy-feedback]').textContent='';}
  refreshPlaybackAudioHints();
  if(!playbackAudioContext)return;
  const url=new URL(`/files/${encodeURIComponent(job.id)}/output/verification.json`,location.origin);
  url.search=new URL(job.video_url,location.href).search;
  fetch(url,{cache:'no-store'}).then(async response=>{const value=await response.json();return response.ok?value:null;}).then(check=>{
    if(sequence!==playbackAudioSequence||!playbackAudioContext)return;
    if(typeof check?.audio_non_silent==='boolean')playbackAudioContext.audioCheck=check.audio_non_silent;
    if(typeof check?.audio_codec==='string')playbackAudioContext.codec=check.audio_codec;
    refreshPlaybackAudioHints();
  }).catch(()=>{}); // Older jobs need not have verification metadata.
}

// Clip windows refer to existing media. No independently encoded shot is implied.
function attachClipPlayer(video,button,hint){
  let window=null,animation=0;
  const valid=()=>window&&Number.isFinite(video.duration)&&window.start<video.duration;
  const end=()=>Math.min(window.end,video.duration);
  const stopAtEnd=()=>{
    if(valid()&&video.currentTime>=end()-.015){video.pause();video.currentTime=end();}
    if(!video.paused)animation=requestAnimationFrame(stopAtEnd);
  };
  const ready=()=>{
    button.disabled=!valid();
    if(!valid()){hint.textContent=window?'所选起点超出原视频时长，请调整起始秒数。':'';return;}
    hint.textContent=`${window.label} · ${window.start.toFixed(2)}–${end().toFixed(2)} 秒${window.end>video.duration?'（已到原片结尾）':''}`;
    video.currentTime=window.start;
  };
  video.addEventListener('loadedmetadata',ready);
  video.addEventListener('play',()=>{
    if(!valid()){video.pause();return;}
    if(video.currentTime<window.start||video.currentTime>=end()-.04)video.currentTime=window.start;
    cancelAnimationFrame(animation);animation=requestAnimationFrame(stopAtEnd);
  });
  video.addEventListener('timeupdate',()=>{if(valid()&&!video.paused&&video.currentTime>=end())stopAtEnd();});
  video.addEventListener('seeking',()=>{
    if(!valid())return;
    if(video.currentTime<window.start-.02)video.currentTime=window.start;
    if(video.currentTime>end()+.02)video.currentTime=end();
  });
  video.addEventListener('pause',()=>cancelAnimationFrame(animation));
  video.addEventListener('error',()=>{button.disabled=true;hint.textContent='浏览器无法播放此素材编码，可下载原素材检查或换用 H.264 MP4。';});
  button.onclick=async()=>{
    if(!valid())return;video.currentTime=window.start;
    try{await video.play();}catch(error){hint.textContent='播放未开始：'+error.message;}
  };
  return {
    set(url,start,duration,label){
      video.pause();window={start:Number(start),end:Number(start)+Number(duration),label};
      button.disabled=true;
      if(!Number.isFinite(window.start)||window.start<0||!(window.end>window.start)){
        window=null;hint.textContent='请填写有效的起始秒数与取片秒数。';return;
      }
      if(video.getAttribute('src')!==url){video.src=url;hint.textContent='正在读取视频…';}
      else if(video.readyState>=1)ready();
    },
    stop(){video.pause();cancelAnimationFrame(animation);},
    clear(){this.stop();window=null;video.removeAttribute('src');video.load();button.disabled=true;}
  };
}

let sourceClip,finalClip;
let columnPreviewSequence=0;
function columnCropValue(){
  if(!$('columnCropEnabled').checked)return null;
  try{return JSON.parse($('columnCropSpec').value);}catch(_){throw Error('请填写有效的列区间 JSON，例如 y 和 columns 两个字段。');}
}
function updateColumnCropUI(){
  const source=(state.job?.sources||[]).find(item=>item.id===$('shotSource').value);
  const image=['page','image'].includes(source?.type),enabled=image&&$('columnCropEnabled').checked;
  $('columnCropControls').hidden=!image;$('columnCropFields').hidden=!enabled;
  $('shotFocus').disabled=enabled;$('cropBox').hidden=enabled;
  if(enabled)$('shotFocus').checked=false;
  if(!enabled){columnPreviewSequence++;$('columnCropPreview').hidden=true;$('columnCropHint').textContent='';}
}
async function updateColumnCropPreview(){
  const sequence=++columnPreviewSequence;
  if(!$('columnCropEnabled').checked)return null;
  $('columnCropPreview').hidden=true;$('columnCropHint').textContent='正在按原始比例生成预览…';
  try{
    const spec=columnCropValue();if(!spec)throw Error('请填写 y 和 columns 列区间，不能使用 null。');
    const query=new URLSearchParams({job:state.job.id,id:$('shotSource').value,column_crop:JSON.stringify(spec),display:JSON.stringify(displaySettings())});
    const result=await api('/api/column-preview?'+query);
    if(sequence!==columnPreviewSequence)return null;
    $('columnCropPreview').src=result.url;$('columnCropPreview').hidden=false;
    $('columnCropHint').textContent='同尺度拼接预览 · 成片会按当前画幅等比容纳此图，不含字幕。请与上方原图核对条件和数据。';
    return result.provenance.column_crop;
  }catch(error){
    if(sequence===columnPreviewSequence)$('columnCropHint').textContent=error.message;
    throw error;
  }
}
function initializeShotPreviews(){
  $('shotSource').closest('label').insertAdjacentHTML('afterend',`<section id="sourceClipPanel" class="clip-preview" hidden>
    <video id="shotVideoPreview" controls preload="metadata" playsinline muted></video>
    <p id="shotVideoHint" class="small"></p><button type="button" class="secondary" id="playSourceClip">播放所选原素材片段</button>
    <a id="downloadSourceClip" target="_blank" rel="noopener">打开原素材</a>
    <p class="small muted">原素材预览，不含成片旁白、字幕和裁切。修改起点或取片秒数后可重播。</p></section>
    <section id="generatedVisualPreview" class="generated-preview" hidden></section>`);
  $('videoPlayer').closest('.player-card').insertAdjacentHTML('beforeend',`<p id="outputDraftNotice" class="notice warning" hidden>文稿或分镜有未保存修改，当前播放的是之前的成片。保存并重新生成后，才会包含这些修改。</p><section id="renderedTimeline" hidden>
    <h3>逐镜头查看成片</h3><p class="small muted">点击镜头，播放成片中对应的画面、旁白和字幕。</p><div id="renderedSegments" class="segment-list"></div></section>`);
  document.body.insertAdjacentHTML('beforeend',`<dialog id="segmentDialog" class="shot-dialog">
    <div class="modal-heading"><h2 id="segmentTitle">成片片段</h2><button type="button" class="icon-button" id="closeSegment" aria-label="关闭">×</button></div>
    <video id="segmentVideo" controls preload="metadata" playsinline></video><p id="segmentHint" class="small"></p>
    <button type="button" class="secondary" id="playFinalClip">播放这段成片</button></dialog>`);
  $('retryJob').insertAdjacentHTML('afterend','<button class="secondary" id="editInputs" hidden>修改资料重新生成</button>');
  $('editInputs').onclick=()=>{tab('create');$('brief').focus();};
  sourceClip=attachClipPlayer($('shotVideoPreview'),$('playSourceClip'),$('shotVideoHint'));
  finalClip=attachClipPlayer($('segmentVideo'),$('playFinalClip'),$('segmentHint'));
  attachPlaybackAudioHint($('videoPlayer'),$('videoPlayer').closest('.player-stage'));
  attachPlaybackAudioHint($('segmentVideo'),$('segmentVideo'));
  $('closeSegment').onclick=()=>$('segmentDialog').close();
  $('shotDialog').addEventListener('close',()=>sourceClip.stop());
  $('segmentDialog').addEventListener('close',()=>finalClip.stop());
  for(const id of ['shotIn','shotDuration','shotCardText'])$(id).addEventListener('input',updateShotPreview);
  $('columnCropEnabled').onchange=()=>{updateColumnCropUI();if($('columnCropEnabled').checked)updateColumnCropPreview().catch(()=>{});};
  $('columnCropSpec').oninput=()=>{columnPreviewSequence++;$('columnCropPreview').hidden=true;$('columnCropHint').textContent='列区间已修改，点击更新预览；保存镜头时也会校验。';};
  $('refreshColumnPreview').onclick=()=>updateColumnCropPreview().catch(()=>{});
  $('shotDialog').addEventListener('close',()=>{columnPreviewSequence++;});
}

function updateShotPreview(){
  updateColumnCropUI();
  const source=(state.job?.sources||[]).find(item=>item.id===$('shotSource').value);
  const isVideo=source?.type==='video';$('sourceClipPanel').hidden=!isVideo;
  $('shotDurationLabel').textContent=isVideo?'取片秒数':'停留权重';
  $('shotDurationHint').textContent=isVideo?
    '从所选起点取用这么多秒的原视频；最终镜头还会结合旁白切入点安排。勾选完整播放时，会保留这段演示，必要时延长本段。':
    '这是同段画面分配的相对权重，不是固定秒数。实际时长配音后确定，并结合旁白切入点；生成后可在成片时间轴查看实测秒数。';
  if(isVideo){
    const url=sourceURL(source.id);$('downloadSourceClip').href=url;
    sourceClip.set(url,$('shotIn').value,$('shotDuration').value,'原素材片段');
  }else sourceClip.clear();
  const selected=$('shotSource').value,preview=$('generatedVisualPreview');
  preview.hidden=!['card','diagram'].includes(selected);
  if(selected==='card')preview.innerHTML=`<p class="small muted">卡片内容预览 · 最终布局随画幅生成</p><div class="card-content-preview">${escapeHTML($('shotCardText').value)}</div>`;
  if(selected==='diagram'){
    const [bi,si]=state.shot,graph=state.job.plan.blocks[bi].shots[si].diagram;
    if(!graph){preview.textContent='此镜头缺少动态图结构。';return;}
    const names=Object.fromEntries(graph.nodes.map(node=>[node.id,node.label]));
    preview.innerHTML='<p class="small muted">动态图结构预览 · 实际动画在配音后生成，按以下旁白提示推进。</p>'+
      '<div class="diagram-node-list">'+graph.nodes.map(node=>`<span>${escapeHTML(node.label)}</span>`).join('')+'</div>'+
      graph.edges.map(edge=>`<p>${escapeHTML(names[edge.source])} → ${escapeHTML(names[edge.target])}${edge.label?' · '+escapeHTML(edge.label):''}</p>`).join('')+
      '<ol>'+graph.stages.map(stage=>`<li>${escapeHTML(stage.cue||'本镜头开始')}：${(stage.active_nodes||[]).map(id=>escapeHTML(names[id])).join('、')||'连接过程'}</li>`).join('')+'</ol>';
  }
}

function renderMeasuredSegments(available){
  $('outputDraftNotice').hidden=!(state.dirty&&state.job?.video_url);
  const segments=available&&!state.dirty?(state.job?.rendered_segments||[]):[];
  $('renderedTimeline').hidden=!segments.length;
  $('renderedSegments').innerHTML=segments.map((segment,index)=>`<button class="secondary" data-segment="${index}">${index+1}. ${escapeHTML(segment.label)}<small>${segment.start.toFixed(2)}–${segment.end.toFixed(2)} 秒</small></button>`).join('');
  $('renderedSegments').querySelectorAll('[data-segment]').forEach(button=>button.onclick=()=>{
    const segment=segments[Number(button.dataset.segment)];$('videoPlayer').pause();
    $('segmentTitle').textContent=segment.label;
    finalClip.set(state.job.video_url,segment.start,segment.end-segment.start,'成片时间段');
    $('segmentDialog').showModal();
  });
}
