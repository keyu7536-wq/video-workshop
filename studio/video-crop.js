'use strict';
let videoCropSequence=0;
function initializeVideoCropEditor(){
  $('cropHint').insertAdjacentHTML('afterend','<section id="videoCropPanel" hidden><button type="button" class="secondary" id="refreshVideoCrop">查看裁后画面</button><p id="videoCropStatus" class="small muted"></p><img id="videoCropImage" alt="所选视频区域的构图预览" hidden style="display:block;max-width:100%;max-height:420px;object-fit:contain"></section>');
  $('refreshVideoCrop').onclick=()=>updateVideoCropPreview();
  $('videoCropPanel').insertAdjacentHTML('afterbegin','<label class="checkbox"><input type="checkbox" id="videoSubtitleSafe">为原片文字留出字幕位置</label>');
  $('videoSubtitleSafe').onchange=()=>{invalidateVideoCropPreview();updateVideoCropPreview();};
  for(const id of ['shotIn','shotDuration'])$(id).addEventListener('input',invalidateVideoCropPreview);
  $('shotDialog').addEventListener('close',()=>{videoCropSequence++;});
}
function invalidateVideoCropPreview(){
  videoCropSequence++;
  if(!$('videoCropPanel')?.hidden){$('videoCropImage').hidden=true;$('videoCropStatus').textContent='范围已修改，点击查看裁后画面。';}
}
function showVideoCropControls(isVideo){
  videoCropSequence++;$('videoCropPanel').hidden=!isVideo;$('videoCropImage').hidden=true;
  if(isVideo){$('cropImage').removeAttribute('src');updateVideoCropPreview();}
}
async function updateVideoCropPreview(){
  if($('videoCropPanel').hidden)return;
  const sequence=++videoCropSequence,source=$('shotSource').value;
  $('videoCropStatus').textContent='正在读取所选范围的实际帧…';$('videoCropImage').hidden=true;
  try{
    const query=new URLSearchParams({job:state.job.id,id:source,in:$('shotIn').value,duration:$('shotDuration').value,
      bbox:JSON.stringify(state.crop||[]),display:JSON.stringify(displaySettings()),subtitle_safe:JSON.stringify($('videoSubtitleSafe').checked)});
    const result=await api('/api/video-crop-preview?'+query);
    if(sequence!==videoCropSequence||source!==$('shotSource').value)return;
    const frame=result.frames[0],prefix='/files/'+encodeURIComponent(state.job.id)+'/';
    const path=value=>prefix+value.split('/').map(encodeURIComponent).join('/');
    $('cropImage').onload=drawCrop;$('cropImage').src=path(frame.original);
    $('videoCropImage').src=path(frame.preview);$('videoCropImage').hidden=false;
    $('videoCropStatus').textContent=`原片 ${frame.time.toFixed(2)} 秒的裁后构图 · 静态抽帧，不含字幕。`+
      (result.metadata.additional_center_crop?'当前“铺满”设置还会裁掉边缘，可改为“完整显示”保留所选区域。':'');
  }catch(error){if(sequence===videoCropSequence)$('videoCropStatus').textContent=error.message;}
}
