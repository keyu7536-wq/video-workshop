'use strict';

let mediaSelectionDraft=null,storylineEditorInitialized=false;

function storylineChanged(){
  state.dirty=true;
  renderPlanReview();updateControls();renderMeasuredSegments(false);
}

function renderStoryline(){
  const board=$('storyboard'),plan=state.job?.plan;
  let panel=$('storylinePanel');
  if(!board||!plan){panel?.remove();return;}
  const wasOpen=panel?.dataset.jobId===state.job.id&&panel.open;
  if(!panel){panel=document.createElement('details');panel.id='storylinePanel';panel.className='panel director-fields';board.before(panel);}
  panel.dataset.jobId=state.job.id;panel.open=Boolean(wasOpen);
  const story=plan.story,steps=new Map((story?.steps||[]).map(step=>[step.block_id,step]));
  const example=story?.example||{},selectedSources=new Set(example.source_ids||[]);
  const sourceOptions=(state.job.sources||[]).map(source=>({id:source.id,label:source.name||source.display_name||source.id}));
  for(const id of selectedSources)if(!sourceOptions.some(source=>source.id===id))sourceOptions.push({id,label:'未找到来源：'+id});
  panel.innerHTML=`<summary>讲述主线</summary>
    <label class="checkbox"><input type="checkbox" data-story-enabled ${story?'checked':''}> 为这条视频整理主线</label>
    <div data-story-fields ${story?'':'hidden'}>
      <p class="small muted">只编辑讲述安排，已有旁白保持原文。这些内容不显示在成片上。</p>
      <label>这条视频回答什么问题<input data-story-field="question" maxlength="500" value="${escapeHTML(story?.question||'')}"></label>
      <label>看完应该得到什么回答<textarea data-story-field="answer" rows="2" maxlength="500">${escapeHTML(story?.answer||'')}</textarea></label>
      <label>贯穿各段的对象或例子<textarea data-story-field="example" rows="2" maxlength="500">${escapeHTML(example.description||'')}</textarea></label>
      <label class="checkbox"><input type="checkbox" data-story-field="illustrative" ${example.illustrative?'checked':''}> 这个例子是为讲解设想的示意</label>
      <label>例子对应的资料<select data-story-field="source_ids" multiple size="${Math.min(5,Math.max(2,sourceOptions.length))}">${sourceOptions.map(source=>`<option value="${escapeHTML(source.id)}" ${selectedSources.has(source.id)?'selected':''}>${escapeHTML(source.label)}</option>`).join('')}</select></label>
      <p class="small muted">来自资料的例子需关联来源；示意例子应在旁白里自然说明，不能当作实验结果。</p>
      ${plan.blocks.map((block,index)=>{
        const step=steps.get(block.id),enabled=story?Boolean(step):true;
        return `<details class="director-fields" data-story-step="${index}"><summary>${index+1}. ${escapeHTML(block.chapter||'本段')}</summary>
          <label class="checkbox"><input type="checkbox" data-step-enabled ${enabled?'checked':''}> 将这段加入主线</label>
          <div data-step-fields ${enabled?'':'hidden'}>
            <label>这段增加了什么理解<textarea data-step-field="change" rows="2" maxlength="500">${escapeHTML(step?.change||'')}</textarea></label>
            <label>接着哪一段讲<select data-step-field="depends_on"><option value="">独立说明</option>${plan.blocks.slice(0,index).map((prior,position)=>`<option value="${escapeHTML(prior.id)}" ${step?.depends_on===prior.id?'selected':''}>${position+1}. ${escapeHTML(prior.chapter||'前一段')}</option>`).join('')}</select></label>
            <label>为什么能接到这里<textarea data-step-field="why" rows="2" maxlength="500">${escapeHTML(step?.why||'')}</textarea></label>
          </div></details>`;
      }).join('')}
    </div>`;
  const read=()=>({question:panel.querySelector('[data-story-field="question"]').value,
    answer:panel.querySelector('[data-story-field="answer"]').value,
    example:{description:panel.querySelector('[data-story-field="example"]').value,
      illustrative:panel.querySelector('[data-story-field="illustrative"]').checked,
      source_ids:[...panel.querySelector('[data-story-field="source_ids"]').selectedOptions].map(option=>option.value)},
    steps:[...panel.querySelectorAll('[data-story-step]')].filter(row=>row.querySelector('[data-step-enabled]').checked).map(row=>({
      block_id:plan.blocks[Number(row.dataset.storyStep)].id,
      ...Object.fromEntries([...row.querySelectorAll('[data-step-field]')].map(field=>[field.dataset.stepField,field.value]))}))});
  const commit=()=>{if(panel.querySelector('[data-story-enabled]').checked){plan.story=read();storylineChanged();}};
  panel.querySelector('[data-story-enabled]').onchange=event=>{
    panel.querySelector('[data-story-fields]').hidden=!event.target.checked;
    plan.story=event.target.checked?read():null;storylineChanged();
  };
  for(const field of panel.querySelectorAll('[data-story-field],[data-step-field]')){
    field.addEventListener(field.tagName==='SELECT'||field.type==='checkbox'?'change':'input',commit);
  }
  for(const row of panel.querySelectorAll('[data-story-step]'))row.querySelector('[data-step-enabled]').onchange=event=>{
    row.querySelector('[data-step-fields]').hidden=!event.target.checked;commit();
  };
}

function mediaCandidateWindow(segment){
  if(!Number.isFinite(segment.start)||!Number.isFinite(segment.end)||segment.end-segment.start<1)return null;
  const times=(segment.observed_times||[]).filter(time=>Number.isFinite(time)&&segment.start<=time&&time<segment.end);
  if(!times.length)return null;
  let start=segment.start,end=Math.min(segment.end,start+20);
  if(!times.some(time=>start<=time&&time<end)){
    start=Math.max(segment.start,Math.min(times[0],segment.end-1));end=Math.min(segment.end,start+20);
  }
  return {start,duration:end-start};
}

function renderMediaSelection(shot){
  const form=$('shotForm');if(!form)return;
  let panel=$('mediaSelectionPanel');
  if(!panel){panel=document.createElement('section');panel.id='mediaSelectionPanel';panel.className='director-fields';form.querySelector('.production-options')?.after(panel);if(!panel.parentElement)form.append(panel);}
  const sourceId=$('shotSource').value,source=(state.job?.sources||[]).find(item=>item.id===sourceId);
  panel.hidden=source?.type!=='video';
  mediaSelectionDraft=null;
  if(panel.hidden){panel.innerHTML='';return;}
  const manifest=state.job.media_index;
  const segments=(manifest?.segments||[]).filter(segment=>segment.source_id===sourceId);
  const selected=segments.find(segment=>segment.id===shot?.media_segment_id);
  if(selected)mediaSelectionDraft={id:selected.id,sourceId,start:Number($('shotIn').value),duration:Number($('shotDuration').value),reason:shot.selection_reason||''};
  const roles={demonstration:'动作示例',comparison:'对照画面',process:'过程状态',context:'场景背景'};
  const gaps=(manifest?.gaps||[]).filter(gap=>!gap.source_id||gap.source_id===sourceId);
  panel.innerHTML=`<h3>原片中的候选片段</h3><p class="small muted">根据采样帧整理，未核验完整连续动作。选择后仍需检查它是否对应这段旁白。</p>
    ${segments.length?segments.map((segment,index)=>`<details class="director-fields"><summary>${Number(segment.start).toFixed(2)}–${Number(segment.end).toFixed(2)} 秒 · ${escapeHTML(roles[segment.visual_role]||'画面候选')}</summary>
      <p>${escapeHTML(segment.description)}</p><p class="small muted">已观察：${(segment.observed_times||[]).map(time=>Number(time).toFixed(2)).join('、')} 秒</p>
      ${(segment.limitations||[]).map(limit=>`<p class="small muted">${escapeHTML(limit)}</p>`).join('')}
      <button type="button" class="secondary" data-media-candidate="${index}" ${mediaCandidateWindow(segment)?'':'disabled'}>使用这个片段</button></details>`).join(''):
      '<p class="small">暂无可用索引。可以播放原片后，手动设置起始时间和取片长度。</p>'}
    ${gaps.map(gap=>`<p class="small muted">${escapeHTML(gap.reason)}</p>`).join('')}
    <div data-media-binding ${mediaSelectionDraft?'':'hidden'}><label>选这段画面的理由<textarea id="mediaSelectionReason" rows="2" maxlength="320" placeholder="说明它对应哪句话，观众应看见什么">${escapeHTML(mediaSelectionDraft?.reason||'')}</textarea></label>
    <button type="button" class="secondary" id="clearMediaSelection">改为手动选片</button></div>
    <p id="mediaSelectionHint" class="small" role="status">${shot?.media_segment_id&&!selected?'原片段索引已不可用，请重新选择候选或手动选片。':''}</p>`;
  for(const button of panel.querySelectorAll('[data-media-candidate]'))button.onclick=()=>{
    const segment=segments[Number(button.dataset.mediaCandidate)],window=mediaCandidateWindow(segment);
    if(!window)return;
    for(const id of ['shotIn','shotDuration'])$(id).step='any';
    $('shotIn').value=window.start;$('shotDuration').value=window.duration;
    mediaSelectionDraft={id:segment.id,sourceId,start:window.start,duration:window.duration,reason:''};
    panel.querySelector('[data-media-binding]').hidden=false;$('mediaSelectionReason').value='';
    $('mediaSelectionHint').textContent='已设置取片范围。请填写画面与旁白的对应关系，保存前可预览原片。';
    if(typeof updateShotPreview==='function')updateShotPreview();
  };
  $('mediaSelectionReason').oninput=event=>{if(mediaSelectionDraft)mediaSelectionDraft.reason=event.target.value;};
  $('clearMediaSelection').onclick=()=>clearMediaSelection('已改为手动选片，保留当前起始时间和长度。');
}

function clearMediaSelection(message){
  mediaSelectionDraft=null;
  const panel=$('mediaSelectionPanel');
  if(panel){const binding=panel.querySelector('[data-media-binding]');if(binding)binding.hidden=true;}
  if($('mediaSelectionHint'))$('mediaSelectionHint').textContent=message||'';
}

function readMediaSelection(source,start,duration){
  if(!mediaSelectionDraft)return {media_segment_id:'',selection_reason:''};
  const sourceId=typeof source==='string'?source:source?.id;
  const selected=(state.job?.media_index?.segments||[]).find(segment=>segment.id===mediaSelectionDraft.id&&segment.source_id===sourceId);
  if(!selected||sourceId!==mediaSelectionDraft.sourceId||Number(start)!==mediaSelectionDraft.start||Number(duration)!==mediaSelectionDraft.duration){
    clearMediaSelection('来源或范围已改变，原片段关联已取消。');
    throw Error('来源或范围已改变，请重新选择片段或按当前范围手动保存。');
  }
  if(!Number.isFinite(Number(start))||!Number.isFinite(Number(duration))||Number(start)<selected.start||Number(start)+Number(duration)>selected.end+1e-6||!(selected.observed_times||[]).some(time=>Number(start)<=time&&time<Number(start)+Number(duration)))throw Error('所选范围需要位于候选内，并包含已观察的采样点。');
  const reason=mediaSelectionDraft.reason.trim();
  if(!reason)throw Error('请说明这段画面对应哪句话、观众应看见什么。');
  return {media_segment_id:selected.id,selection_reason:reason};
}

function initializeStorylineEditor(){
  if(storylineEditorInitialized)return;storylineEditorInitialized=true;
  $('shotSource').addEventListener('change',()=>renderMediaSelection(null));
  for(const id of ['shotIn','shotDuration'])for(const event of ['input','change'])$(id).addEventListener(event,()=>{
    if(mediaSelectionDraft)clearMediaSelection('已调整范围，原片段关联已取消。可以重新选候选，或按当前范围手动保存。');
  });
}
