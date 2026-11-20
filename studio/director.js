'use strict';

const visualStrategies={context:'交代背景',demonstration:'展示动作',comparison:'对照差异',process:'解释过程',timeline:'时间顺序',data:'解读数据'};
let visualBeatDraft=[],visualBeatCrop=[],visualBeatPreviewSequence=0;
function invalidateVisualBeatPreview(){
  visualBeatPreviewSequence++;$('visualBeatPreview').hidden=true;
  $('visualBeatPreview').removeAttribute('src');$('visualBeatHint').textContent='';
}

function renderDirectorFields(){
  for(const card of $('storyboard').querySelectorAll('[data-block-index]')){
    const bi=Number(card.dataset.blockIndex),block=state.job.plan.blocks[bi],value=block.explanation;
    const fields=document.createElement('details');fields.className='director-fields';
    fields.innerHTML=`<summary>讲解安排</summary><label class="checkbox"><input type="checkbox" data-director-enabled ${value?'checked':''}> 为这段设置讲解安排</label><div data-director-fields ${value?'':'hidden'}>
      <label>这段回答什么问题<input data-director="question" maxlength="240" value="${escapeHTML(value?.question||'')}"></label>
      <label>观众看完应该明白什么<textarea data-director="takeaway" maxlength="320" rows="2">${escapeHTML(value?.takeaway||'')}</textarea></label>
      <label>画面用途<select data-director="visual_strategy">${Object.entries(visualStrategies).map(([key,label])=>`<option value="${key}" ${value?.visual_strategy===key?'selected':''}>${label}</option>`).join('')}</select></label>
      <label>与前后镜头怎样衔接<input data-director="continuity" maxlength="240" value="${escapeHTML(value?.continuity||'')}"></label>
      <p class="small muted">这些内容用于安排和复核镜头，不显示在成片上。</p></div>`;
    const changed=()=>{state.dirty=true;renderPlanReview();updateControls();renderMeasuredSegments(false);};
    const read=()=>Object.fromEntries([...fields.querySelectorAll('[data-director]')].map(input=>[input.dataset.director,input.value]));
    fields.querySelector('[data-director-enabled]').onchange=event=>{
      fields.querySelector('[data-director-fields]').hidden=!event.target.checked;
      block.explanation=event.target.checked?read():null;changed();
    };
    for(const input of fields.querySelectorAll('[data-director]'))input.oninput=()=>{block.explanation=read();changed();};
    card.querySelector('.evidence-note').after(fields);
  }
}

function visualQualityHTML(){
  const report=state.job?.visual_quality;
  if(!report||state.dirty)return '';
  const issues=report.issues||[];
  if(!issues.length)return '';
  return `<details class="notice warning"><summary>画面安排 · ${issues.length} 条建议</summary><p class="small">根据素材和镜头时间整理，供选片参考；不代表已完成内容或听音审核。</p><ul class="review-issues">${issues.map(issue=>`<li>${escapeHTML(issue.reason)}</li>`).join('')}</ul></details>`;
}

function prepareVisualBeatEditor(shot){
  visualBeatDraft=JSON.parse(JSON.stringify(shot.visual_beats||[]));
  visualBeatCrop=shot.bbox?.length===4?[...shot.bbox]:[0,0,1,1];
  $('visualBeatsEnabled').checked=visualBeatDraft.length>0;
  invalidateVisualBeatPreview();
  renderVisualBeatRows();
}

function updateVisualBeatSource(reset=false){
  const source=(state.job?.sources||[]).find(item=>item.id===$('shotSource').value);
  $('visualBeatControls').hidden=!['page','image'].includes(source?.type);
  if(reset){visualBeatDraft=[];visualBeatCrop=[0,0,1,1];$('visualBeatsEnabled').checked=false;renderVisualBeatRows();}
}

function renderVisualBeatRows(){
  invalidateVisualBeatPreview();
  const enabled=$('visualBeatsEnabled').checked;
  $('visualBeatFields').hidden=!enabled;
  $('visualBeatRows').innerHTML=visualBeatDraft.map((beat,index)=>`<div class="visual-beat-row">
    <label>${index?'读到哪里放大':'第一个区域'}<input data-beat-cue="${index}" maxlength="80" value="${escapeHTML(beat.cue)}" ${index?'placeholder="填写本段中唯一出现的短语"':'disabled placeholder="镜头开始时显示"'}></label>
    <label>画面短标签<input data-beat-label="${index}" maxlength="24" value="${escapeHTML(beat.label)}"></label>
    <p class="small muted">区域：${beat.bbox.map(v=>Number(v).toFixed(3)).join('，')}</p>
    <div class="beat-actions"><button type="button" class="secondary" data-beat-box="${index}">使用上方框选范围</button><button type="button" class="secondary" data-beat-preview="${index}">预览</button><button type="button" class="secondary" data-beat-remove="${index}">删除</button></div></div>`).join('');
  for(const input of $('visualBeatRows').querySelectorAll('[data-beat-cue]'))input.oninput=()=>{visualBeatDraft[Number(input.dataset.beatCue)].cue=input.value;invalidateVisualBeatPreview();};
  for(const input of $('visualBeatRows').querySelectorAll('[data-beat-label]'))input.oninput=()=>{visualBeatDraft[Number(input.dataset.beatLabel)].label=input.value;invalidateVisualBeatPreview();};
  for(const button of $('visualBeatRows').querySelectorAll('[data-beat-box]'))button.onclick=()=>{visualBeatDraft[Number(button.dataset.beatBox)].bbox=[...state.crop];renderVisualBeatRows();};
  for(const button of $('visualBeatRows').querySelectorAll('[data-beat-remove]'))button.onclick=()=>{visualBeatDraft.splice(Number(button.dataset.beatRemove),1);if(visualBeatDraft.length)visualBeatDraft[0].cue='';renderVisualBeatRows();};
  for(const button of $('visualBeatRows').querySelectorAll('[data-beat-preview]'))button.onclick=()=>previewVisualBeat(Number(button.dataset.beatPreview));
  $('addVisualBeat').disabled=visualBeatDraft.length>=6;
}

function readVisualBeats(){
  if(!$('visualBeatsEnabled').checked||$('visualBeatControls').hidden)return [];
  if($('shotFocus').checked||$('columnCropEnabled').checked)throw Error('逐步放大不能同时使用高亮或列拼接，请只保留一种方式。');
  if(!visualBeatDraft.length)throw Error('请至少添加一个放大区域，或关闭逐步放大。');
  const narration=state.job.plan.blocks[state.shot[0]].narration;
  let previous=-1;
  return visualBeatDraft.map((beat,index)=>{
    const cue=beat.cue.trim(),box=beat.bbox;
    if(index){const position=narration.indexOf(cue);if(!cue||position<=previous||narration.indexOf(cue,position+1)!==-1)throw Error('放大切点须在本段文稿中唯一出现，并按讲述顺序排列。');previous=position;}
    if(box.length!==4||box.some(v=>!Number.isFinite(v))||box[2]-box[0]<.02||box[3]-box[1]<.02)throw Error('请框选有效的放大区域。');
    if(box[0]<visualBeatCrop[0]||box[1]<visualBeatCrop[1]||box[2]>visualBeatCrop[2]||box[3]>visualBeatCrop[3])throw Error('放大区域须位于启用时的原图裁切范围内。');
    return {cue:index?cue:'',bbox:[...box],label:beat.label.trim()};
  });
}

async function previewVisualBeat(index){
  const sequence=++visualBeatPreviewSequence;
  try{
    const beats=readVisualBeats();
    const query=new URLSearchParams({job:state.job.id,id:$('shotSource').value,bbox:JSON.stringify(visualBeatCrop),beats:JSON.stringify(beats),index:String(index),display:JSON.stringify(displaySettings())});
    const result=await api('/api/visual-preview?'+query);
    if(sequence!==visualBeatPreviewSequence)return;
    $('visualBeatPreview').src=result.url;$('visualBeatPreview').hidden=false;
    $('visualBeatHint').textContent='当前区域的画面预览，字幕和切换时间在配音后确定。';
  }catch(error){if(sequence!==visualBeatPreviewSequence)return;$('visualBeatHint').textContent=error.message;$('visualBeatPreview').hidden=true;}
}

function initializeDirectorEditor(){
  $('visualBeatsEnabled').onchange=event=>{
    if(event.target.checked){
      if($('columnCropEnabled').checked){event.target.checked=false;toast('请先关闭列拼接。');return;}
      if(!visualBeatDraft.length){
        visualBeatCrop=$('shotFocus').checked?[...state.originalBBox]:[...state.crop];
        if(visualBeatCrop.length!==4)visualBeatCrop=[0,0,1,1];
        visualBeatDraft=[{cue:'',bbox:[...visualBeatCrop],label:''}];
      }
      $('shotFocus').checked=false;
    }else{
      state.crop=[...visualBeatCrop];drawCrop();
    }
    renderVisualBeatRows();
  };
  $('addVisualBeat').onclick=()=>{if(visualBeatDraft.length<6){visualBeatDraft.push({cue:'',bbox:[...state.crop],label:''});renderVisualBeatRows();}};
}
