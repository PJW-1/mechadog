import {mapPoint, worldPoint, mapImage} from './map-frame.js';
export {mapPoint, worldPoint} from './map-frame.js';
// 구역 순서의 정본은 서버 zones.ids. 초안은 로봇별로 분리하고 명령 API와 구분한다.
const clone=value=>JSON.parse(JSON.stringify(value));
const blankZone=id=>({id,name:id,x:null,y:null,yaw_deg:null,aim_deg:null,hazard:false,helmet:true,vest:true,note:''});
const NS='http://www.w3.org/2000/svg';
export class PlanningPanel {
 constructor(panel){this.p=panel;this.sessions=new Map();this.current=null;this.root=null}
 context(){const s=this.p.store;return {key:s.demo?'offline':(s.link?.baseUrl??'offline')+':'+s.selected,link:s.demo?null:s.link}}
 open(){
  const {key,link}=this.context();
  if(!this.sessions.has(key)){
   let draft=null,revision=null,invalidNumbers={};
   try{const saved=JSON.parse(this.p.document.defaultView.localStorage.getItem('mechadog.plan.v1.'+key));draft=saved?.draft;revision=saved?.revision;invalidNumbers=saved?.invalidNumbers||{}}catch{}
   if(!draft?.zones?.length||draft.zones.length>16||!draft.zones.every(z=>typeof z.id==='string'))draft=null;
   this.sessions.set(key,{key,link,revision,invalidNumbers,draft:draft||{zones:[blankZone('A')],random_after_first_cycle:false},selected:draft?.zones[0]?.id||'A',dirty:!!draft,loading:false,busy:false,snapshot:null,route:null,error:'',notice:'',loaded:false});
  }
  this.current=this.sessions.get(key);this.current.link=link;
  this.root=this.p.el('section',{class:'plan-workspace op-section','aria-label':'구역과 순찰 계획'});this.p.container.append(this.root);
  this.draw();if(link?.get&&!this.current.loaded&&!this.current.loading)this.load(this.current);
 }
 refresh(){if(this.context().key!==this.current?.key)this.p.render('zones')}
 visible(session){return this.current===session&&this.p.view==='zones'&&this.root?.isConnected}
 async load(session=this.current,replace=false){
  session.loading=true;session.error='';if(this.visible(session))this.draw();
  try{
   session.mapMeta=null;session.mapError='';
   const [data,meta]=await Promise.all([session.link.get('/api/planning'),session.link.get('/api/map/meta').catch(error=>{session.mapError=error.message;return null})]);session.snapshot=data;session.mapMeta=meta;session.loaded=true;
   if(replace||!session.dirty){session.draft=clone(data.saved);session.revision=data.revision;session.selected=session.draft.zones[0].id;session.dirty=false;session.invalidNumbers={};session.route=null;this.forgetDraft(session)}
   session.revision??=data.revision;
   session.notice=session.dirty?'이 브라우저에 작성 중인 계획이 있습니다. 저장본과 비교한 뒤 저장하세요.':'';
  }catch(error){session.error='설정 서버를 읽지 못했습니다. '+error.message;session.loaded=true}
  finally{session.loading=false;if(this.visible(session))this.draw()}
 }
 remember(session){
  try{this.p.document.defaultView.localStorage.setItem('mechadog.plan.v1.'+session.key,JSON.stringify({draft:session.draft,revision:session.revision,invalidNumbers:session.invalidNumbers}));session.local=true}catch{session.local=false}
 }
 forgetDraft(session){try{this.p.document.defaultView.localStorage.removeItem('mechadog.plan.v1.'+session.key)}catch{}}
 changed(geometry=true){const s=this.current;s.dirty=true;if(geometry)s.route=null;s.notice='';this.remember(s);this.updateStatus();this.drawList();if(geometry)this.drawMap();this.drawRoute()}
 updateStatus(){
  const s=this.current;
  if(!this.status)return;
  this.status.textContent=s.loading?'설정 불러오는 중':s.busy?'처리 중':Object.keys(s.invalidNumbers).length?'좌표 확인 필요 · 허용 범위 안의 숫자를 입력하세요':s.dirty?'작성 중 · '+(s.local===false?'이번 창에만 보관':'브라우저에 보관'):s.snapshot?.pending_restart?'저장됨 · 재시작 후 적용':s.snapshot?.active?'운용 설정과 일치':s.snapshot?'저장본 · 로봇 서버 시작 시 적용':'서버 미연결 · 초안 작성';
  this.status.dataset.pending=String(s.dirty||!!s.snapshot?.pending_restart);
  if(this.saveButton)this.saveButton.disabled=!s.snapshot||!s.dirty||s.busy||s.loading||Object.keys(s.invalidNumbers).length>0;
  if(this.previewButton)this.previewButton.disabled=!s.snapshot?.map.available||s.busy||s.loading||Object.keys(s.invalidNumbers).length>0;
 }
 draw(){
  const p=this.p,s=this.current;this.root.replaceChildren();
  this.status=p.el('span',{class:'plan-state',role:'status','aria-live':'polite'});
  this.saveButton=p.button('계획 저장',()=>this.save(),{class:'op-button primary','data-plan-save':''});
  const reload=p.button('저장본 다시 불러오기',()=>{
   if(s.dirty){p.confirmDevice({title:'작성 중인 계획을 바꿀까요?',body:'이 브라우저의 구역 초안을 버리고 서버 저장본을 불러옵니다.',confirm:'저장본 불러오기',action:()=>this.load(s,true)})}
   else return this.load(s,true);
  },{disabled:!s.link?.get||s.busy||s.loading});
  this.root.append(p.el('header',{class:'plan-header'},p.el('div',{},p.el('h2',{},'현장의 순서를 정하세요'),p.el('p',{},s.snapshot?'대상 '+s.snapshot.device+' · 구역 위치와 점검 항목을 한곳에서 관리합니다.':'지도를 연결하기 전에도 구역 이름과 점검 항목부터 준비할 수 있습니다.')),p.el('div',{class:'plan-actions'},this.status,reload,this.saveButton)));
  if(s.error)this.root.append(p.note(s.error,'warning'));
  if(s.notice)this.root.append(p.note(s.notice));
  this.list=p.el('div',{class:'plan-stops'});
  const random=p.el('input',{type:'checkbox',checked:s.draft.random_after_first_cycle,onchange:e=>{s.draft.random_after_first_cycle=e.target.checked;this.changed()}});
  const itinerary=p.el('aside',{class:'plan-itinerary'},p.el('div',{class:'plan-section-heading'},p.el('h3',{},'순찰 순서'),p.el('span',{},'첫 순회')),this.list,p.button('구역 추가',()=>this.add(),{'data-plan-add':'',disabled:s.busy||s.loading||s.draft.zones.length>=16}),p.el('label',{class:'op-check plan-random'},random,'첫 순회 후 순서 섞기'),p.note('목록 순서로 방문한 뒤 첫 구역으로 돌아옵니다.'));
  this.map=p.el('div',{class:'plan-map'});this.route=p.el('div',{class:'plan-route','aria-live':'polite'});
  this.previewButton=p.button('경로 확인',()=>this.preview(),{disabled:!s.snapshot?.map.available||s.busy,'data-plan-preview':''});
  const canvas=p.el('section',{class:'plan-canvas'},p.el('div',{class:'plan-section-heading'},p.el('h3',{},'지도에 구역 배치'),this.previewButton),this.map,p.el('div',{class:'plan-map-caption'},p.el('span',{},'밝음: 빈 공간 · 진함: 장애물 · 회색: 미관측'),p.el('span',{},'단위 m · 동선 지도와 같은 방향')),this.route);
  this.inspector=p.el('aside',{class:'plan-inspector'});
  const mobile=p.document.defaultView.matchMedia?.('(max-width:899px)').matches;
  const settings=(node,label,key)=>{const d=p.el('details',{class:node.className+' plan-settings',...(s[key]??!mobile?{open:''}:{})},p.el('summary',{},label),node);d.addEventListener('toggle',()=>{s[key]=d.open;});return d;};
  this.root.append(p.el('div',{class:'plan-layout'},settings(itinerary,'순찰 순서 설정','itineraryOpen'),canvas,settings(this.inspector,'구역 상세 설정','inspectorOpen')));
  const radius=s.snapshot?.arrival_radius_m;
  this.root.append(p.el('footer',{class:'plan-footer'},p.el('div',{},p.el('strong',{},'계획 → 지도 확인 → 저장 → 운용'),p.el('p',{},'저장한 계획은 다음 로봇 서버 시작에 적용됩니다. 경로 확인은 저장 지도의 계산 결과이며, 실물 주행 검증은 별도입니다.')),p.button('제어 · 장치로 이동',()=>p.onNavigate('missions'))));
  this.root.append(p.el('details',{class:'plan-mode-guide'},p.el('summary',{},'점검 기준 상세'),p.el('p',{},p.el('strong',{},'경비 모드'), ' · 구역 순찰과 신원 확인·암구호'),p.el('p',{},p.el('strong',{},'공장 모드'),' · 구역별 보호구 검사·물품 변화·위험물 점검'),p.note('구역은 점검 지점'+(radius!=null?' 반경 '+radius.toFixed(2)+'m':' 반경')+'입니다. 경계 밖이나 위치가 불확실하면 안전모·조끼를 모두 검사합니다. 위험 구역은 출입 금지가 아니라 기존 위험물 판정 대상입니다.')));
  this.root.append(p.note('순서와 점검 기준은 선택한 로봇에 저장합니다. 같은 지도 폴더를 사용하는 로봇끼리는 구역 좌표를 공유합니다.'));
  // 편집 중에 완료 응답이 초안을 덮어쓰지 않도록 저장/계산 동안 입력을 잠근다.
  if(s.busy||s.loading)for(const element of this.root.querySelectorAll('input,button'))element.disabled=true;
  this.drawList();this.drawMap();this.drawInspector();this.drawRoute();this.updateStatus();
 }
 drawList(){
  if(!this.list)return;
  const p=this.p,s=this.current,focused=p.document.activeElement;
  const focusZone=this.list.contains(focused)?focused.dataset.planZone||focused.dataset.planReorder:null,focusAction=focusZone?focused.getAttribute('aria-label'):null;
  this.list.replaceChildren();
  s.draft.zones.forEach((z,index)=>{
   const select=p.button([p.el('span',{class:'plan-order'},String(index+1).padStart(2,'0')),p.el('span',{},p.el('strong',{},z.name||z.id),p.el('small',{},z.id+' · '+(z.x==null?'위치 미지정':z.x.toFixed(2)+', '+z.y.toFixed(2))+(z.hazard?' · 위험물 점검':'')))],()=>{s.selected=z.id;this.drawList();this.drawMap();this.drawInspector()},{class:'plan-stop'+(z.id===s.selected?' selected':''),'aria-pressed':z.id===s.selected,'data-plan-zone':z.id,disabled:s.busy||s.loading});
   const move=(delta)=>{const other=index+delta;[s.draft.zones[index],s.draft.zones[other]]=[s.draft.zones[other],s.draft.zones[index]];this.changed()};
   this.list.append(p.el('div',{class:'plan-stop-row'},select,p.el('div',{class:'plan-reorder'},p.button('위',()=>move(-1),{'data-plan-reorder':z.id,'aria-label':z.id+' 순서 위로',disabled:s.busy||s.loading||index===0}),p.button('아래',()=>move(1),{'data-plan-reorder':z.id,'aria-label':z.id+' 순서 아래로',disabled:s.busy||s.loading||index===s.draft.zones.length-1}))));
  });
  if(focusZone){const buttons=[...this.list.querySelectorAll('button')];(buttons.find(b=>focusAction&&b.getAttribute('aria-label')===focusAction&&!b.disabled)||buttons.find(b=>b.dataset.planZone===focusZone))?.focus()}
 }
 add(){
  const s=this.current;let n=1;while(s.draft.zones.some(z=>z.id==='Z'+n))n++;
  const z=blankZone('Z'+n);z.name='새 구역';s.draft.zones.push(z);s.selected=z.id;this.changed();this.draw();this.inspector.querySelector('[name="구역 이름"]').focus();
 }
 svg(tag,attrs={},text){const node=this.p.document.createElementNS(NS,tag);for(const [key,value]of Object.entries(attrs))node.setAttribute(key,String(value));if(text!=null)node.textContent=text;return node}
 drawMap(){
  if(!this.map)return;const p=this.p,s=this.current,meta=s.mapMeta;this.map.replaceChildren();
  if(!s.snapshot?.map?.available||!meta?.patrol_to_px||!meta?.px_to_patrol){this.map.append(p.el('div',{class:'plan-map-empty'},p.el('div',{class:'plan-empty-grid','aria-hidden':'true'}),p.el('h3',{},'현장 지도를 기다리고 있어요'),p.el('p',{},s.mapError?'지도 표시 정보를 읽지 못했습니다. '+s.mapError:'라이다로 만든 지도를 연결하면 구역 위치를 찍고 실제 지도 위의 이동 경로를 확인할 수 있습니다.'),p.el('small',{},'예시 공장의 좌표는 실제 순찰에 사용하지 않습니다.')));return}
  const svg=this.svg('svg',{viewBox:'0 0 1000 1000',preserveAspectRatio:'none',class:'plan-map-svg','aria-label':'저장된 현장 지도. 구역 선택 후 클릭하면 위치가 바뀝니다.'});
  svg.style.aspectRatio=meta.width+'/'+meta.height;
  const imageUrl=(s.link.baseUrl||'')+'/api/map.png?rev='+s.snapshot.revision;
  if(this.imageUrl!==imageUrl){this.imageUrl=imageUrl;this.mapImage=mapImage(this.svg.bind(this),s.link.baseUrl,s.snapshot.revision)}
  svg.append(this.mapImage);
  for(const segment of s.route?.segments||[])if(segment.reachable)svg.append(this.svg('polyline',{points:segment.points.map(([x,y])=>mapPoint(meta,x,y).join(',')).join(' '),class:'plan-path','vector-effect':'non-scaling-stroke'}));
  const r=s.snapshot.arrival_radius_m;
  s.draft.zones.forEach((z,index)=>{
   if(z.x==null)return;const [x,y]=mapPoint(meta,z.x,z.y),selected=z.id===s.selected;
   const g=this.svg('g',{class:'plan-map-zone'+(selected?' selected':''),'data-zone':z.id});
   const outline=Array.from({length:65},(_,i)=>mapPoint(meta,z.x+r*Math.cos(i*Math.PI/32),z.y+r*Math.sin(i*Math.PI/32)).join(','));
   g.append(this.svg('polygon',{points:outline.join(' '),class:z.hazard?'plan-radius hazard':'plan-radius','vector-effect':'non-scaling-stroke'}));
   this.drawAim(g,z,meta,r);this.drawLegacyHeading(g,z,meta,r);
   g.append(this.svg('circle',{cx:x,cy:y,r:17,class:'plan-pin','vector-effect':'non-scaling-stroke'}),this.svg('text',{x,y:y+1,'text-anchor':'middle','dominant-baseline':'middle',class:'plan-pin-label'},index+1));
   g.addEventListener('click',event=>{event.stopPropagation();if(s.busy||s.loading)return;s.selected=z.id;this.drawList();this.drawMap();this.drawInspector()});svg.append(g);
  });
  svg.addEventListener('click',event=>{
   if(s.busy||s.loading)return;const rect=svg.getBoundingClientRect();if(!rect.width||!rect.height)return;
   const [x,y]=worldPoint(meta,(event.clientX-rect.left)/rect.width,(event.clientY-rect.top)/rect.height),zone=s.draft.zones.find(z=>z.id===s.selected);
   zone.x=+x.toFixed(3);zone.y=+y.toFixed(3);this.changed();this.drawInspector();
  });
  this.map.append(svg,p.el('span',{class:'plan-map-hint'},'선택한 구역을 지도에 클릭 · 정확한 위치는 오른쪽 좌표 입력'));
 }
 drawAim(group,zone,meta,radius){
  if(!Number.isFinite(zone.aim_deg))return;
  const angle=zone.aim_deg*Math.PI/180,dx=Math.cos(angle),dy=Math.sin(angle);
  const length=Math.max(radius*1.8,Math.min(meta.width,meta.height)*meta.resolution_m*.045),head=length*.28;
  const tipX=zone.x+dx*length,tipY=zone.y+dy*length,point=(x,y)=>mapPoint(meta,x,y).join(' ');
  const label=zone.id+' 카메라 방향 '+zone.aim_deg+'°';
  const arrow=this.svg('path',{d:'M '+point(zone.x,zone.y)+' L '+point(tipX,tipY)+' M '+point(tipX-dx*head-dy*head*.55,tipY-dy*head+dx*head*.55)+' L '+point(tipX,tipY)+' L '+point(tipX-dx*head+dy*head*.55,tipY-dy*head-dx*head*.55),class:'plan-heading plan-aim','data-aim-deg':zone.aim_deg,'vector-effect':'non-scaling-stroke',role:'img','aria-label':label});
  arrow.append(this.svg('title',{},label));group.append(arrow);
 }
 drawLegacyHeading(group,zone,meta,radius){
  if(Number.isFinite(zone.aim_deg)||!Number.isFinite(zone.yaw_deg))return;
  const angle=zone.yaw_deg*Math.PI/180,[x,y]=mapPoint(meta,zone.x,zone.y),[dx,dy]=mapPoint(meta,zone.x+Math.cos(angle)*radius*1.8,zone.y+Math.sin(angle)*radius*1.8),label=zone.id+' 기존 점검 방향 '+zone.yaw_deg+'°';
  const line=this.svg('line',{x1:x,y1:y,x2:dx,y2:dy,class:'plan-heading plan-legacy-heading','stroke-dasharray':'5 4','vector-effect':'non-scaling-stroke',role:'img','aria-label':label});
  line.append(this.svg('title',{},label));group.append(line);
 }
 aimHelp(zone){
  return '도착 후 카메라 방향으로 몸을 돌린 다음 점검합니다. 순찰 좌표 기준 0°는 +X, +90°는 +Y이며 지도 회전이 반영됩니다. 범위는 −180°~180°입니다. '+(zone.yaw_deg!=null?'카메라 방향이 빈칸이면 기존 점검 방향을 사용합니다(지도 점선). 기존 점검 방향까지 비우면 도착 방향을 유지합니다.':'빈칸이면 도착 방향을 유지합니다.');
 }
 drawInspector(){
  if(!this.inspector)return;const p=this.p,s=this.current,z=s.draft.zones.find(z=>z.id===s.selected);this.inspector.replaceChildren();if(!z)return;
  const text=(label,key,max)=>p.field(label,p.el(key==='note'?'textarea':'input',{name:label,value:key==='note'?null:z[key],maxlength:max,rows:key==='note'?3:null,disabled:s.busy||s.loading,oninput:e=>{z[key]=e.target.value;this.changed(false)}},key==='note'?z[key]:null));
  const number=(label,key)=>{
   const invalidKey=z.id+':'+key;
   const direction=key==='aim_deg'||key==='yaw_deg';
   return p.field(label,p.el('input',{name:label,type:'number',step:'0.001',min:direction?-180:-10000,max:direction?180:10000,value:s.invalidNumbers[invalidKey]??z[key]??'','aria-invalid':invalidKey in s.invalidNumbers,placeholder:'미지정',disabled:s.busy||s.loading,oninput:e=>{
    if(!e.target.checkValidity()){s.invalidNumbers[invalidKey]=e.target.value;e.target.setAttribute('aria-invalid','true');this.changed();return}
    delete s.invalidNumbers[invalidKey];e.target.setAttribute('aria-invalid','false');
    const value=e.target.value===''?null:Number(e.target.value);z[key]=value;
    const sync=(other,label,value)=>{z[other]=value;delete s.invalidNumbers[z.id+':'+other];const input=this.inspector.querySelector('[name="'+label+'"]');if(input){input.value=value??'';input.setAttribute('aria-invalid','false')}};
    if((key==='x'||key==='y')&&value===null){sync('x','X (m)',null);sync('y','Y (m)',null);sync('yaw_deg','기존 점검 방향 (°)',null);sync('aim_deg','카메라 방향 (°)',null)}
    else if(key==='x'&&z.y==null)sync('y','Y (m)',0);else if(key==='y'&&z.x==null)sync('x','X (m)',0);
    this.aimNote.textContent=this.aimHelp(z);
    this.changed();
   }}));
  };
  const check=(label,key,description)=>p.el('label',{class:'plan-check'},p.el('input',{type:'checkbox',name:key,checked:z[key],disabled:s.busy||s.loading,onchange:e=>{z[key]=e.target.checked;this.changed()}}),p.el('span',{},p.el('strong',{},label),p.el('small',{},description)));
  this.aimNote=p.note(this.aimHelp(z));this.aimNote.dataset.planAimHelp='';
  const legacy=z.yaw_deg!=null||z.id+':yaw_deg' in s.invalidNumbers;
  this.inspector.append(p.el('div',{class:'plan-section-heading'},p.el('h3',{},'구역 설정'),p.badge(z.id)),text('구역 이름','name',60),p.el('div',{class:'plan-coordinate'},number('X (m)','x'),number('Y (m)','y')),number('카메라 방향 (°)','aim_deg'),...(legacy?[number('기존 점검 방향 (°)','yaw_deg')]:[]),this.aimNote,p.el('h4',{},'공장 모드 점검'),check('안전모 필수','helmet','머리 영역의 보호구 착용 확인'),check('안전조끼 필수','vest','몸통 영역의 보호구 착용 확인'),check('위험물 점검 구역','hazard','기존 위험물 판정을 이 구역에서 사용'),text('현장 메모','note',300));
  this.inspector.append(p.button('선택 구역 삭제',()=>{s.draft.zones=s.draft.zones.filter(item=>item!==z);for(const key of Object.keys(s.invalidNumbers))if(key.startsWith(z.id+':'))delete s.invalidNumbers[key];s.selected=s.draft.zones[0].id;this.changed();this.draw()},{disabled:s.busy||s.loading||s.draft.zones.length===1,class:'op-button plan-delete'}));
 }
 drawRoute(){
  if(!this.route)return;const p=this.p,s=this.current;this.route.replaceChildren();
  if(!s.route){this.route.append(p.note(s.snapshot?.map.available?'위치를 정한 뒤 경로 확인을 누르세요. 장애물과 미관측 영역을 제외해 계산합니다.':'지도 연결 후 경로를 계산할 수 있습니다.'));return}
  this.route.append(p.el('strong',{},s.route.all_reachable?'첫 순회 경로 확인 · '+s.route.total_m.toFixed(1)+' m':'연결되지 않는 구간이 있습니다'),p.el('div',{class:'plan-segments'},s.route.segments.map(segment=>p.el('span',{'data-blocked':!segment.reachable},segment.from+' → '+segment.to+' · '+(segment.reachable?segment.length_m.toFixed(1)+' m':'경로 없음')))),p.note(s.route.scope));
 }
 async preview(){
  const s=this.current;if(s.busy||s.loading||Object.keys(s.invalidNumbers).length)return;s.busy=true;s.error='';s.route=null;this.draw();
  try{s.route=await s.link.post('/api/planning/preview',{...clone(s.draft),revision:s.revision},30000)}catch(error){s.error='경로 확인 실패 · '+error.message}
  finally{s.busy=false;if(this.visible(s))this.draw()}
 }
 async save(){
  const s=this.current;if(!s.snapshot||s.busy||s.loading||Object.keys(s.invalidNumbers).length)return;
  if(s.draft.zones.some(z=>z.x==null&&(z.aim_deg!=null||z.yaw_deg!=null))){s.error='위치가 없는 구역의 점검 방향을 지워 주세요.';this.draw();return}
  s.busy=true;s.error='';this.draw();
  try{
   const previousRevision=s.revision;
   const result=await s.link.post('/api/planning',{...clone(s.draft),revision:s.revision});s.snapshot=result;s.revision=result.revision;s.draft=clone(result.saved);s.dirty=false;this.forgetDraft(s);
   this.p.routePlanner?.planningSaved(result,previousRevision,s.key);
   s.notice='계획을 저장했습니다. '+(result.pending_restart?'실행 중인 로봇 서버를 다음에 시작할 때 적용됩니다.':'로봇 서버가 시작할 때 이 설정을 읽습니다.');
  }catch(error){s.error='저장하지 못했습니다. 작성 내용은 유지됩니다. '+error.message}
  finally{s.busy=false;if(this.visible(s))this.draw()}
 }
}
