(function () {
  'use strict';
  const palette = {blue:'#8eb8ff',amber:'#e6bf72',coral:'#ef9b95',purple:'#bea6ed',mint:'#87c9bb',gray:'#bcc6d4'};
  const colorNames = {blue:'蓝',amber:'金',coral:'珊瑚',purple:'紫',mint:'青',gray:'灰'};
  const shapeNames = {document:'纸张',photo:'图片',circle:'圆形',rect:'方框',label:'文字'};
  const stateNames = {neutral:'无标记',idle:'等待',active:'处理中',done:'完成'};
  const ns = 'http://www.w3.org/2000/svg';
  let draft = null, selectedStage = 0, panel = null;
  const byId = id => document.getElementById(id);
  const clone = value => JSON.parse(JSON.stringify(value));
  const sourceIsMotion = () => byId('shotSource')?.value === 'motion';

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function button(text, action, id) {
    const node = element('button', text, 'secondary'); node.type = 'button';
    if (id) node.id = id;
    node.onclick = action; return node;
  }
  function field(text, input) {
    const label = element('label', text); label.append(input); return label;
  }
  function input(value, label, change, maximum) {
    const node = element('input'); node.value = value ?? ''; node.setAttribute('aria-label', label);
    if (maximum) node.maxLength = maximum;
    node.oninput = () => { change(node.value); preview(); };
    return node;
  }
  function select(options, value, label, change) {
    const node = element('select'); node.setAttribute('aria-label', label);
    Object.entries(options).forEach(([key, text]) => {
      const option = element('option', text); option.value = key; node.append(option);
    });
    node.value = value;
    node.onchange = () => { change(node.value); preview(); };
    return node;
  }
  function defaultScene() {
    return {title:'',objects:[{id:'object_1',shape:'document',label:'资料',color:'blue'}],
      stages:[{cue:'',poses:[{id:'object_1',x:.25,y:.45,visible:true,label:null,state:'neutral'}],links:[]}]};
  }
  function canvasSize() {
    if (typeof displayDimensions === 'function') {
      const size = displayDimensions();
      if (Array.isArray(size) && size.every(n => Number.isFinite(n) && n > 0)) return size;
    }
    return [1920,1080];
  }
  function narration() {
    if (typeof state !== 'undefined' && state.shot && state.job?.plan?.blocks) {
      return state.job.plan.blocks[state.shot[0]]?.narration ?? null;
    }
    return null;
  }
  function notice(message) {
    const node = byId('motionEditorNotice');
    if (node) { node.textContent = message; node.hidden = !message; }
  }
  function attempt(action) {
    try { action(); notice(''); } catch (error) { notice(error.message); }
  }

  function initializeMotionEditor() {
    if (panel) return;
    const source = byId('shotSource');
    if (!source || !byId('shotForm')) return;
    panel = element('section'); panel.id = 'motionEditor'; panel.hidden = true;
    const style = element('style');
    style.textContent = `#motionEditor{margin:16px 0;padding:16px;border:1px solid var(--border,#344154);border-radius:12px;min-width:0}#motionEditor .motion-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}#motionEditor label{min-width:0}#motionEditor input,#motionEditor select{width:100%;min-width:0}#motionEditor .motion-row{border-top:1px solid var(--border,#344154);padding:12px 0}#motionEditor .motion-actions{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}#motionEditor .motion-pose{display:grid;grid-template-columns:minmax(70px,1fr) repeat(2,minmax(60px,.7fr)) minmax(80px,1.3fr);gap:8px;align-items:end}#motionEditor .motion-check{display:flex;align-items:center;gap:5px}#motionEditor .motion-check input{width:auto}#motionPreview{display:block;width:100%;height:auto;max-height:320px;background:#0b1018;border:1px solid #273346;border-radius:8px}#motionEditorNotice{color:var(--warning,#e6bf72)}@media(max-width:600px){#motionEditor .motion-grid{grid-template-columns:1fr}#motionEditor .motion-pose{grid-template-columns:repeat(2,minmax(0,1fr))}#motionEditor{padding:10px}}`;
    panel.append(style, element('h3','连续图解'));
    const hint = element('p','每个阶段对应一句旁白；同一对象会从上一阶段的位置移动过来。','small muted');
    panel.append(hint);
    const controls = element('div'); controls.id = 'motionEditorControls'; panel.append(controls);
    const message = element('p','','small'); message.id = 'motionEditorNotice'; message.hidden = true;
    message.setAttribute('role','status'); panel.append(message);
    const svg = document.createElementNS(ns,'svg'); svg.id = 'motionPreview';
    svg.setAttribute('role','img'); svg.setAttribute('aria-label','当前阶段静态布局预览'); panel.append(svg);
    panel.append(element('p','静态布局预览；成片会按实际旁白时间推进。','small muted'));
    source.closest('label').insertAdjacentElement('afterend',panel);
    source.addEventListener('change',() => {
      panel.hidden = !sourceIsMotion();
      if (!panel.hidden) { if (!draft) draft = defaultScene(); render(); }
    });
  }

  function openMotionEditor(shot) {
    initializeMotionEditor();
    if (!panel) return;
    draft = shot?.kind === 'motion' && shot.motion ? clone(shot.motion) : defaultScene();
    // Runtime timestamps must never turn a draft preview into purported speech timing.
    draft.stages.forEach(stage => {
      delete stage.start;
      stage.poses.forEach(pose => { if (pose.state === undefined) pose.state = 'neutral'; });
    });
    selectedStage = 0; panel.hidden = !sourceIsMotion(); notice(''); render();
  }

  function render() {
    if (!panel || !draft) return;
    const controls = byId('motionEditorControls'); controls.replaceChildren();
    controls.append(field('画面标题',input(draft.title,'图解标题',value => {draft.title=value;},36)));
    const objects = element('details'); objects.open = true;
    objects.append(element('summary','对象与颜色'));
    draft.objects.forEach((object,index) => {
      const row = element('div',undefined,'motion-row'); row.dataset.motionObject = object.id;
      const grid = element('div',undefined,'motion-grid');
      grid.append(field('名称',input(object.label,`对象${index+1}名称`,value => {object.label=value;},18)),
        field('形状',select(shapeNames,object.shape,`对象${index+1}形状`,value => {object.shape=value;})),
        field('颜色',select(colorNames,object.color,`对象${index+1}颜色`,value => {object.color=value;})));
      row.append(grid);
      const remove = button('删除对象',() => attempt(() => {
        if (draft.objects.length <= 1) throw Error('至少保留一个对象。');
        draft.objects = draft.objects.filter(item => item.id !== object.id);
        draft.stages.forEach(stage => {
          stage.poses = stage.poses.filter(pose => pose.id !== object.id);
          stage.links = stage.links.filter(link => link.source !== object.id && link.target !== object.id);
        }); render();
      })); remove.disabled = draft.objects.length <= 1; row.append(remove); objects.append(row);
    });
    const addObject = button('添加对象',() => attempt(() => {
      if (draft.objects.length >= 8) throw Error('最多添加8个对象。');
      let index = 1; while (draft.objects.some(object => object.id === 'object_'+index)) index++;
      const id = 'object_'+index;
      draft.objects.push({id,shape:'document',label:'新资料',color:'blue'});
      draft.stages.forEach(stage => stage.poses.push({id,x:.5,y:.5,visible:false,label:null,state:'neutral'}));
      render(); notice('新对象暂未显示，设置位置后勾选显示。');
    }),'motionAddObject'); addObject.disabled = draft.objects.length >= 8;
    objects.append(addObject); controls.append(objects);
    const stages = element('div',undefined,'motion-actions');
    const selector = select(Object.fromEntries(draft.stages.map((stage,index) => [String(index),'阶段 '+(index+1)])),
      String(selectedStage),'图解阶段',value => { selectedStage=Number(value); render(); });
    selector.id = 'motionStageSelect'; stages.append(selector);
    const addStage = button('添加阶段',() => attempt(() => {
      if (draft.stages.length >= 10) throw Error('最多添加10个阶段。');
      const stage = clone(draft.stages[selectedStage]); stage.cue=''; delete stage.start;
      draft.stages.splice(selectedStage+1,0,stage); selectedStage++; render();
    }),'motionAddStage'); addStage.disabled = draft.stages.length >= 10;
    const removeStage = button('删除阶段',() => attempt(() => {
      if (draft.stages.length <= 1) throw Error('至少保留一个阶段。');
      draft.stages.splice(selectedStage,1); draft.stages[0].cue='';
      selectedStage=Math.min(selectedStage,draft.stages.length-1); render();
    }),'motionDeleteStage'); removeStage.disabled = draft.stages.length <= 1;
    stages.append(addStage,removeStage); controls.append(stages);
    const stage = draft.stages[selectedStage];
    const cue = input(stage.cue,'阶段开始旁白',value => {stage.cue=value;},80); cue.id='motionStageCue';
    cue.disabled = selectedStage === 0; cue.placeholder=selectedStage===0?'镜头开始':'粘贴本段中只出现一次的短语';
    controls.append(field('读到这里进入本阶段',cue));
    stage.poses.forEach(pose => {
      const object = draft.objects.find(item => item.id === pose.id);
      if (!object) return;
      const row = element('div',undefined,'motion-row motion-pose'); row.dataset.motionPose = pose.id;
      const visible = element('input'); visible.type='checkbox'; visible.checked=pose.visible;
      visible.setAttribute('aria-label',`${object.label||pose.id}显示`);
      visible.onchange=()=>{pose.visible=visible.checked; preview();};
      const shown = field(object.label||pose.id,visible); shown.className='motion-check'; row.append(shown);
      ['x','y'].forEach((axis,index) => {
        const coordinate = input(Number.isFinite(pose[axis])?Number((pose[axis]*100).toFixed(2)):'',
          `${object.label||pose.id}${axis==='x'?'横向':'纵向'}位置`, value => {pose[axis]=value.trim()===''?NaN:Number(value)/100;});
        coordinate.type='number'; coordinate.min='0'; coordinate.max='100'; coordinate.step='.1';
        coordinate.dataset.motionAxis=axis; row.append(field(index?'纵向 %':'横向 %',coordinate));
      });
      const override = input(pose.label??'',`${object.label||pose.id}本阶段名称`,value => {pose.label=value.trim()?value:null;},18);
      override.placeholder='沿用对象名称'; row.append(field('本阶段名称',override));
      row.append(field('状态',select(stateNames,pose.state??'neutral',`${object.label||pose.id}本阶段状态`,value=>{pose.state=value;})));
      controls.append(row);
    });
    const linkTitle = element('h4','连接关系'); controls.append(linkTitle);
    const names = Object.fromEntries(draft.objects.map(object => [object.id,object.label||object.id]));
    stage.links.forEach((link,index) => {
      const row = element('div',undefined,'motion-row'); const grid=element('div',undefined,'motion-grid');
      grid.append(field('从',select(names,link.source,`连接${index+1}起点`,value => {link.source=value;})),
        field('到',select(names,link.target,`连接${index+1}终点`,value => {link.target=value;})),
        field('说明',input(link.label,`连接${index+1}说明`,value => {link.label=value;},12)));
      row.append(grid,button('删除连接',()=>{stage.links.splice(index,1);render();})); controls.append(row);
    });
    const addLink=button('添加连接',()=>attempt(()=>{
      const visible=stage.poses.filter(pose=>pose.visible);
      if(visible.length<2)throw Error('先显示至少两个对象，再添加连接。');
      if(stage.links.length>=12)throw Error('每个阶段最多12条连接。');
      stage.links.push({source:visible[0].id,target:visible[1].id,label:''});render();
    }),'motionAddLink'); addLink.disabled=stage.links.length>=12;controls.append(addLink);
    preview();
  }

  function svgElement(tag,attrs,text) {
    const node=document.createElementNS(ns,tag);
    Object.entries(attrs||{}).forEach(([key,value])=>node.setAttribute(key,String(value)));
    if(text!==undefined)node.textContent=text;
    return node;
  }
  function tint(color,scale) {
    return 'rgb('+[1,3,5].map(index=>Math.round(parseInt(color.slice(index,index+2),16)*scale)).join(',')+')';
  }
  function physicalBox(object,pose,width,height,unit) {
    const concrete=['document','photo'].includes(object.shape),x=pose.x*width,y=pose.y*height;
    const halfWidth=unit*(object.shape==='document'?.0525:object.shape==='photo'?.0975:object.shape==='circle'?.037:.14);
    const halfHeight=unit*(concrete?.0675:object.shape==='rect'?.065:object.shape==='circle'?.037:.05);
    const centerY=y-(concrete?unit*.0225:0);
    return [x-halfWidth,centerY-halfHeight,x+halfWidth,centerY+halfHeight];
  }
  function objectCenter(object,pose,width,height,unit) {
    const box=physicalBox(object,pose,width,height,unit);return [(box[0]+box[2])/2,(box[1]+box[3])/2];
  }
  function endpoint(object,pose,toward,width,height,unit) {
    const box=physicalBox(object,pose,width,height,unit),x=(box[0]+box[2])/2,y=(box[1]+box[3])/2,dx=toward[0]-x,dy=toward[1]-y;
    if(!dx&&!dy)return [x,y];
    if(object.shape==='circle'){const scale=unit*.037/Math.hypot(dx,dy);return [x+dx*scale,y+dy*scale];}
    const halfWidth=(box[2]-box[0])/2,halfHeight=(box[3]-box[1])/2;
    const scale=Math.min(dx?halfWidth/Math.abs(dx):Infinity,dy?halfHeight/Math.abs(dy):Infinity);
    return [x+dx*scale,y+dy*scale];
  }
  function stateMark(svg,object,pose,width,height,unit) {
    const state=pose.state??'neutral';if(state==='neutral'||!stateNames[state])return;
    const box=physicalBox(object,pose,width,height,unit),inside=['rect','label'].includes(object.shape);
    const x=box[2]+unit*(inside?-.022:.018),y=(box[1]+box[3])/2,radius=unit*.012;
    const group=svgElement('g',{'data-motion-state':state,fill:'none',stroke:'#e0e9f6','stroke-width':Math.max(2,unit*.003),'stroke-linejoin':'round'});
    group.append(svgElement('circle',{cx:x,cy:y,r:radius+2,fill:'#0b1018',stroke:'none'}));
    if(state==='idle')[-.005,.005].forEach(offset=>group.append(svgElement('line',{x1:x+unit*offset,y1:y-radius*.7,x2:x+unit*offset,y2:y+radius*.7})));
    if(state==='active'){
      const angle=110*Math.PI/180;
      group.append(svgElement('path',{d:`M${x+radius},${y} A${radius},${radius} 0 0 1 ${x+radius*Math.cos(angle)},${y+radius*Math.sin(angle)}`}));
    }
    if(state==='done')group.append(svgElement('polyline',{points:`${x-radius*.85},${y} ${x-radius*.2},${y+radius*.6} ${x+radius*.95},${y-radius*.75}`}));
    svg.append(group);
  }
  function preview() {
    if (!panel || !draft) return;
    const svg=byId('motionPreview');svg.replaceChildren();
    const [width,height]=canvasSize(),unit=Math.min(width,height),stage=draft.stages[selectedStage];
    svg.setAttribute('viewBox',`0 0 ${width} ${height}`);
    svg.setAttribute('width',width);svg.setAttribute('height',height);
    svg.append(svgElement('rect',{width,height,fill:'#0b1018'}));
    svg.append(svgElement('line',{x1:0,y1:height*.8,x2:width,y2:height*.8,stroke:'#273346','stroke-dasharray':'8 8'}));
    svg.append(svgElement('text',{x:width/2,y:height*.09,fill:'#e5ecf8','font-size':unit*.04,'text-anchor':'middle'},draft.title));
    const defs=svgElement('defs');const marker=svgElement('marker',{id:'motionArrow',markerWidth:8,markerHeight:8,refX:7,refY:4,orient:'auto',markerUnits:'strokeWidth'});
    marker.append(svgElement('path',{d:'M0,0 L8,4 L0,8 Z',fill:'#617992'}));defs.append(marker);svg.append(defs);
    const visible=Object.fromEntries(stage.poses.filter(pose=>pose.visible&&Number.isFinite(pose.x)&&Number.isFinite(pose.y)).map(pose=>[pose.id,pose]));
    stage.links.forEach(link=>{
      const a=visible[link.source],b=visible[link.target];if(!a||!b||link.source===link.target)return;
      const source=draft.objects.find(item=>item.id===link.source),target=draft.objects.find(item=>item.id===link.target);
      const from=endpoint(source,a,objectCenter(target,b,width,height,unit),width,height,unit);
      const to=endpoint(target,b,objectCenter(source,a,width,height,unit),width,height,unit);
      svg.append(svgElement('line',{x1:from[0],y1:from[1],x2:to[0],y2:to[1],stroke:'#617992','stroke-width':unit*.003,'marker-end':'url(#motionArrow)'}));
      if(link.label)svg.append(svgElement('text',{x:(a.x+b.x)*width/2,y:(a.y+b.y)*height/2-unit*.035,fill:'#bcc6d4','font-size':unit*.022,'text-anchor':'middle'},link.label));
    });
    draft.objects.forEach(object=>{
      const pose=visible[object.id];if(!pose)return;
      const x=pose.x*width,y=pose.y*height,color=palette[object.color]||palette.gray;
      if(object.shape==='circle')svg.append(svgElement('circle',{cx:x,cy:y,r:unit*.037,fill:color}));
      if(object.shape==='rect')svg.append(svgElement('rect',{x:x-unit*.14,y:y-unit*.065,width:unit*.28,height:unit*.13,rx:unit*.018,fill:'#142035',stroke:color,'stroke-width':unit*.0025}));
      if(object.shape==='document'){
        const left=x-unit*.0525,right=x+unit*.0525,top=y-unit*.09,bottom=y+unit*.045,fold=unit*.0285;
        const group=svgElement('g',{'data-motion-shape':'document'});
        group.append(svgElement('polygon',{points:`${left},${top} ${right-fold},${top} ${right},${top+fold} ${right},${bottom} ${left},${bottom}`,fill:tint(color,.16),stroke:color,'stroke-width':unit*.0025}));
        group.append(svgElement('polygon',{points:`${right-fold},${top} ${right-fold},${top+fold} ${right},${top+fold}`,fill:tint(color,.4),stroke:color,'stroke-width':unit*.0025}));
        [[.42,.62],[.60,.62],[.78,.43]].forEach(([offset,length])=>group.append(svgElement('line',{x1:left+(right-left)*.17,y1:top+(bottom-top)*offset,x2:left+(right-left)*(.17+length),y2:top+(bottom-top)*offset,stroke:color,'stroke-width':unit*.0015})));
        svg.append(group);
      }
      if(object.shape==='photo'){
        const left=x-unit*.0975,right=x+unit*.0975,top=y-unit*.09,bottom=y+unit*.045;
        const group=svgElement('g',{'data-motion-shape':'photo'});
        group.append(svgElement('rect',{x:left,y:top,width:right-left,height:bottom-top,rx:unit*.012,fill:tint(color,.16),stroke:color,'stroke-width':unit*.0025}));
        group.append(svgElement('circle',{cx:right-unit*.0405,cy:top+unit*.0375,r:unit*.012,fill:color}));
        group.append(svgElement('polygon',{points:`${left+unit*.0165},${bottom-unit*.0195} ${left+unit*.057},${top+unit*.0525} ${left+unit*.096},${top+unit*.09} ${left+unit*.1245},${top+unit*.0675} ${right-unit*.015},${bottom-unit*.0195}`,fill:tint(color,.45),stroke:color,'stroke-width':unit*.0015}));
        svg.append(group);
      }
      const label=pose.label===null?object.label:pose.label;
      const labelY=['document','photo'].includes(object.shape)?y+unit*.09:object.shape==='circle'?y+unit*.08:y;
      const inset=['rect','label'].includes(object.shape)&&pose.state&&pose.state!=='neutral';
      const textX=x-(inset?unit*(object.shape==='rect'?.0165:.0225):0);
      const text=svgElement('text',{x:textX,y:labelY,fill:'#e0e9f6','font-size':unit*(inset?.024:.028),'text-anchor':'middle','dominant-baseline':'middle'});
      const rows=String(label??'').match(/.{1,9}/gu)||[''];
      rows.slice(0,2).forEach((line,index)=>text.append(svgElement('tspan',{x:textX,dy:index===0?(rows.length>1?-unit*.015:0):unit*.032},line)));
      svg.append(text);
      stateMark(svg,object,pose,width,height,unit);
    });
  }

  function readMotionEditor() {
    if (!sourceIsMotion()) return null;
    if (!draft) throw Error('请先设置连续图解。');
    const result=clone(draft);
    const clean=(value,limit,name)=>{
      if(typeof value!=='string'||value.length>limit||/[\x00-\x1f]/.test(value))throw Error(`${name}格式不正确，最多${limit}字。`);
      return value.trim();
    };
    result.title=clean(result.title,36,'标题');
    if(!Array.isArray(result.objects)||result.objects.length<1||result.objects.length>8)throw Error('需要1～8个对象。');
    const ids=new Set();
    result.objects.forEach(object=>{
      if(!object.id||ids.has(object.id))throw Error('对象编号重复或缺失。');ids.add(object.id);
      object.label=clean(object.label,18,'对象名称');
      if(!shapeNames[object.shape]||!palette[object.color])throw Error('请选择有效的形状和颜色。');
    });
    if(!Array.isArray(result.stages)||result.stages.length<1||result.stages.length>10)throw Error('需要1～10个阶段。');
    const spoken=narration();let previous=-1;const cues=new Set();
    result.stages.forEach((stage,index)=>{
      stage.cue=clean(stage.cue,80,'旁白切点');delete stage.start;
      if(index===0&&stage.cue)throw Error('首阶段从镜头开始，切点应留空。');
      if(index>0){
        if(!stage.cue||cues.has(stage.cue))throw Error('后续阶段需要不同的旁白短语。');
        if(spoken!==null){const position=spoken.indexOf(stage.cue);if(position<0||spoken.indexOf(stage.cue,position+1)>=0)throw Error('阶段切点须在本段旁白中只出现一次。');if(position<=previous)throw Error('阶段切点须按旁白顺序排列。');previous=position;}
      }
      cues.add(stage.cue);
      if(!Array.isArray(stage.poses)||stage.poses.length!==ids.size)throw Error('每个阶段都需要各对象的位置。');
      const positions=new Map();
      stage.poses.forEach(pose=>{
        if(!ids.has(pose.id)||positions.has(pose.id))throw Error('阶段中的对象编号不正确。');
        if(typeof pose.visible!=='boolean'||[pose.x,pose.y].some(n=>typeof n!=='number'||!Number.isFinite(n)||n<0||n>1))throw Error('对象位置须为0～100之间的数值。');
        if(pose.state===undefined)pose.state='neutral';
        if(typeof pose.state!=='string'||!Object.hasOwn(stateNames,pose.state))throw Error('请选择有效的阶段状态。');
        pose.label=pose.label===null?null:clean(pose.label,18,'阶段名称');positions.set(pose.id,pose);
      });
      if(!Array.isArray(stage.links)||stage.links.length>12)throw Error('每阶段最多12条连接。');
      const links=new Set();stage.links.forEach(link=>{
        const key=link.source+'\0'+link.target;
        if(link.source===link.target||!positions.get(link.source)?.visible||!positions.get(link.target)?.visible||links.has(key))throw Error('连接须指向两个不同且已显示的对象，不能重复。');
        links.add(key);link.label=clean(link.label,12,'连接说明');
      });
    });
    return result;
  }

  window.initializeMotionEditor=initializeMotionEditor;
  window.openMotionEditor=openMotionEditor;
  window.readMotionEditor=readMotionEditor;
}());
