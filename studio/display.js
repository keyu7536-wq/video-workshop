'use strict';
let previewItems=[], previewMedia=null, previewToken=0, previewProtected=false;
function displaySettings(){return {format:$('format').value,width:Number($('customWidth').value),height:Number($('customHeight').value),layout_style:$('layoutStyle').value,media_fit:$('mediaFit').value,show_sources:$('showSources').checked,show_disclosure:$('showDisclosure').checked,minimal_text:$('minimalText').checked};}
function effectiveDisplaySettings(settings){
  const format=settings.format||'portrait';
  const [width,height]=({portrait:[1080,1920],landscape:[1920,1080],square:[1080,1080],reference:[720,1280]})[format]||[Number(settings.width),Number(settings.height)];
  return {format,width,height,layout_style:settings.layout_style||'editorial',media_fit:settings.media_fit||'contain',
    show_sources:settings.show_sources??true,show_disclosure:settings.show_disclosure??true,minimal_text:settings.minimal_text??false};
}
function displaySettingsChanged(){
  return Boolean(state.job?.plan&&state.job.request&&JSON.stringify(effectiveDisplaySettings(displaySettings()))!==JSON.stringify(effectiveDisplaySettings(state.job.request)));
}
function displayDimensions(){const s=displaySettings();return ({portrait:[1080,1920],landscape:[1920,1080],square:[1080,1080],reference:[720,1280]})[s.format]||[s.width,s.height];}
function validateDisplayForm(){const [w,h]=displayDimensions();if([w,h].some(n=>!Number.isInteger(n)||n<320||n>3840||n%2)||w*h>8294400||Math.max(w/h,h/w)>3)throw Error('宽高须为320～3840之间的偶数，总像素不超过4K，长宽比例不超过3:1。');}
async function requireDisplayServer(){const health=await api('/api/health');if(health.version<15)throw Error('界面与后台版本不一致，请用项目启动脚本打开新版服务。');}
function displayToForm(s){$('format').value=s.format||'portrait';$('customWidth').value=s.width||1920;$('customHeight').value=s.height||1080;$('layoutStyle').value=s.layout_style||'editorial';$('mediaFit').value=s.media_fit||'contain';$('showSources').checked=s.show_sources??true;$('showDisclosure').checked=s.show_disclosure??true;$('minimalText').checked=s.minimal_text??false;updateDisplayPreview();}
function previewBox(w,h){if($('layoutStyle').value==='immersive')return previewProtected?[Math.floor(w*.025),Math.floor(h*.06),Math.floor(w*.95),Math.floor(h*.78)]:[0,0,w,h];return h>w?[Math.floor(w*.035),Math.floor(h*.27),Math.floor(w*.93),Math.floor(h*.40)]:[Math.floor(w*.035),Math.floor(h*.20),Math.floor(w*.93),Math.floor(h*.57)];}
function refreshPreviewSources(){
  const selected=$('previewSource').value;
  previewItems=state.uploads.filter(f=>f.previewURL).map(f=>({id:f.previewURL,name:f.name,url:f.previewURL,video:f.previewType.startsWith('video/')}));
  if(state.job?.preview_shots){const planned=state.job.plan?.blocks.flatMap(b=>b.shots)||[];previewItems.push(...state.job.preview_shots.map((s,i)=>({id:'shot-'+i,name:s.label||'镜头 '+(i+1),url:`/files/${encodeURIComponent(state.job.id)}/${s.path.split('/').map(encodeURIComponent).join('/')}`,video:s.kind==='video',time:(s.in||0)+Math.min(1,s.duration/2),protected:s.protect_content??['page','card'].includes(planned[i]?.kind)})));}
  if(window.localPreviewItem)previewItems.push(window.localPreviewItem);
  $('previewSource').innerHTML='<option value="">16:9 构图示意（非素材）</option>'+previewItems.map(s=>`<option value="${escapeHTML(s.id)}">${escapeHTML(s.name)}</option>`).join('');
  $('previewSource').value=previewItems.some(s=>s.id===selected)?selected:(previewItems[0]?.id||'');loadDisplayMedia();
}
function loadDisplayMedia(){
  const token=++previewToken,item=previewItems.find(s=>s.id===$('previewSource').value);previewMedia=null;previewProtected=!!item?.protected;updateDisplayPreview();if(!item)return;
  const media=document.createElement(item.video?'video':'img');
  const accept=()=>{if(token===previewToken){previewMedia=media;updateDisplayPreview();}};
  media.onerror=()=>{if(token===previewToken){previewMedia=null;updateDisplayPreview();$('displaySummary').textContent='此素材暂不能预览，请选其他图片或视频。';}};
  if(item.video){media.muted=true;media.preload='auto';media.onloadedmetadata=()=>{media.currentTime=Math.min(item.time||.1,Math.max(0,media.duration-.05));};media.onseeked=accept;}else media.onload=accept;
  media.src=item.url;
}
function updateDisplayPreview(){
  $('customDimensions').hidden=$('format').value!=='custom';
  try{validateDisplayForm();}catch(error){$('displaySummary').textContent=error.message;return;}
  const [w,h]=displayDimensions(),canvas=$('layoutCanvas'),ctx=canvas.getContext('2d');canvas.width=w;canvas.height=h;
  ctx.fillStyle='#080e15';ctx.fillRect(0,0,w,h);
  let [x,y,mw,mh]=previewBox(w,h);mw-=mw%2;mh-=mh%2;
  const sw=previewMedia?(previewMedia.videoWidth||previewMedia.naturalWidth):1600,sh=previewMedia?(previewMedia.videoHeight||previewMedia.naturalHeight):900;
  const cover=$('mediaFit').value==='cover'&&!previewProtected,scale=(cover?Math.max:Math.min)(mw/sw,mh/sh),dw=sw*scale,dh=sh*scale,dx=x+(mw-dw)/2,dy=y+(mh-dh)/2;
  const original=document.createElement('canvas');original.width=sw;original.height=sh;const oc=original.getContext('2d');
  if(previewMedia)oc.drawImage(previewMedia,0,0,sw,sh);else{oc.fillStyle='#2a655c';oc.fillRect(0,0,sw,sh);oc.strokeStyle='#96c3ad';oc.lineWidth=6;for(let i=1;i<4;i++){oc.strokeRect(i*sw/8,i*sh/8,sw*(1-i/4),sh*(1-i/4));}oc.fillStyle='white';oc.font='54px sans-serif';oc.textAlign='center';oc.fillText('16:9 素材构图示意',sw/2,sh/2);}
  ctx.save();ctx.beginPath();ctx.rect(x,y,mw,mh);ctx.clip();ctx.drawImage(original,dx,dy,dw,dh);ctx.restore();
  const immersive=$('layoutStyle').value==='immersive',unit=immersive?Math.min(w,h):(h>w?w:h),margin=w*(immersive?.04:h>w?.05:.035);
  function textLine(text,top,size,color='white'){ctx.font=`600 ${Math.max(12,size)}px "Microsoft YaHei",sans-serif`;ctx.textAlign='left';ctx.textBaseline='top';ctx.fillStyle='#080e15aa';ctx.fillRect(margin-5,top-3,Math.min(w-2*margin+10,ctx.measureText(text).width+10),size*1.35);ctx.fillStyle=color;ctx.fillText(text,margin,top,w-2*margin);}
  if(!(immersive&&previewProtected)){textLine('视频标题（仅开场）',h*(immersive?.025:h>w?.10:.045),unit*(immersive?.040:h>w?.061:.046));
  textLine('当前章节',h*(immersive?.085:h>w?.175:.12),unit*(immersive?.032:h>w?.042:.031),'#9ed5ac');}
  textLine('字幕显示位置',h*(immersive?.85:h>w?.82:.85),unit*(immersive?.050:h>w?.051:.039));
  if($('showSources').checked)textLine('素材来源 · 页码',h*(immersive?.94:h>w?.77:.91),unit*.022);
  if($('showDisclosure').checked)textLine('AI 辅助制作',h*.965,unit*.018);
  const crop=$('cropCanvas'),cc=crop.getContext('2d');crop.width=600;crop.height=Math.max(1,Math.round(600*sh/sw));cc.drawImage(original,0,0,crop.width,crop.height);
  const rx=cover?(sw-mw/scale)/2:0,ry=cover?(sh-mh/scale)/2:0,rw=cover?mw/scale:sw,rh=cover?mh/scale:sh,k=crop.width/sw;
  if(cover){cc.fillStyle='#0009';cc.beginPath();cc.rect(0,0,crop.width,crop.height);cc.rect(rx*k,ry*k,rw*k,rh*k);cc.fill('evenodd');}
  cc.strokeStyle='#ffb24b';cc.lineWidth=4;cc.strokeRect(rx*k+2,ry*k+2,rw*k-4,rh*k-4);
  const percent=Math.round(rw*rh/(sw*sh)*100);
  $('displaySummary').textContent=`${w} × ${h} · ${previewProtected?'PDF 页面／卡片保护：完整显示':cover?`铺满裁切，保留原图约 ${percent}%`:'完整显示，不裁切'}。${!previewMedia?'当前是示意图，请选择素材查看真实构图。':''}`;
}
document.addEventListener('DOMContentLoaded',()=>{
  for(const id of ['format','customWidth','customHeight','layoutStyle','mediaFit','showSources','showDisclosure','minimalText'])$(id).addEventListener('input',()=>{
    updateDisplayPreview();
    if(state.job?.plan){renderPlanReview();updateControls();}
  });
  $('previewSource').onchange=loadDisplayMedia;$('pickPreview').onclick=()=>$('previewFile').click();
  $('previewFile').onchange=e=>{const file=e.target.files[0];if(!file)return;if(window.localPreviewItem)URL.revokeObjectURL(window.localPreviewItem.url);const url=URL.createObjectURL(file);window.localPreviewItem={id:url,name:file.name,url,video:file.type.startsWith('video/')};refreshPreviewSources();$('previewSource').value=url;loadDisplayMedia();};
  const show=()=>{tab('create');$('displayPreview').open=true;$('displayPreview').scrollIntoView({behavior:'smooth',block:'center'});};$('editDisplay').onclick=show;$('outputDisplay').onclick=show;
  $('regenerateDisplay').onclick=()=>$('renderButton').click();
});
