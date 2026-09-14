import {REVIEW_STATES,ROBOTS,SOURCE_REVISION} from './operations.js';
import {icon} from './icons.js';

const TITLES={missions:'순찰 · 제어',events:'사건 검토',records:'운영 기록',zones:'공간 · 구역',devices:'장치 상태',settings:'운영 설정'};
const STATUS={idle:'시작 전',running:'예시 진행 중',paused:'일시정지',ended:'종료'};
const MANUAL_KEYS={KeyW:'FORWARD',KeyA:'LEFT',KeyS:'BACKWARD',KeyD:'RIGHT'};
const time=value=>value==null?'—':new Date(value).toLocaleString('ko-KR',{hour12:false});

// All record content is text, never HTML. Files stay in this browser session.
export class OperationalPanels {
 constructor({store,container,title,onNavigate,onFocusZone,onFocusRobot,onRobotPreview,onRobotViewAction,onManualObservation,onToast,document=globalThis.document}){
  Object.assign(this,{store,container,title,onNavigate,onFocusZone,onFocusRobot,onRobotPreview,onRobotViewAction,onManualObservation,onToast,document});
  this.view='dashboard';this.zones=[];this.eventId=null;this.zoneId=null;
  this.filters={type:'all',status:'all',robot:'all',query:''};this.urls=new Set();
  this.reviewDrafts=new Map();this.policyDrafts=new Map();this.activeHold=null;
  this.missionDraft=null;this.settingsSection='display';
  this.manualPressedKeys=new Set();this.keyboardEnabled=true;
  this.manualKeyDown=event=>this.handleManualKeyDown(event);
  this.manualKeyUp=event=>this.handleManualKeyUp(event);
  this.manualFocus=()=>{if(this.activeHold?.kind==='shortcut')this.stopManualInput('키보드 초점 변경')};
  this.manualBlur=()=>{this.manualPressedKeys.clear();if(this.activeHold?.kind==='shortcut')this.stopManualInput('키보드 창 이탈')};
  this.document.addEventListener('keydown',this.manualKeyDown,true);
  this.document.addEventListener('keyup',this.manualKeyUp,true);
  this.document.addEventListener('focusin',this.manualFocus);
  this.document.defaultView.addEventListener('blur',this.manualBlur);
 }
 el(tag,attrs={},...children){
  const element=this.document.createElement(tag);
  for(const [key,value]of Object.entries(attrs)){
   if(key.startsWith('on'))element.addEventListener(key.slice(2),value);
   else if(key==='class')element.className=value;
   else if(['checked','disabled','hidden','multiple','required'].includes(key))element[key]=!!value;
   else if(value!=null)element.setAttribute(key,String(value));
  }
  for(const child of children.flat(Infinity))if(child!=null)element.append(child?.nodeType?child:this.document.createTextNode(String(child)));
  return element;
 }
 button(label,action,attrs={}){return this.el('button',{type:'button',class:'op-button',...attrs,onclick:()=>this.run(action)},label)}
 run(action){try{const result=action();if(result?.catch)result.catch(error=>this.error(error));return result}catch(error){this.error(error)}}
 error(error){this.onToast(error.message||'작업을 완료하지 못했습니다.');const status=this.container.querySelector('.op-feedback');if(status){status.textContent=error.message;status.dataset.tone='error'}}
 note(text,tone='quiet'){return this.el('p',{class:'op-note '+tone},text)}
 badge(text,tone=''){return this.el('span',{class:'op-badge '+tone},text)}
 field(label,input){return this.el('label',{class:'op-field'},this.el('span',{},label),input)}
 select(name,options,value,onchange){
  const select=this.el('select',{name,'aria-label':name,onchange:event=>onchange?.(event.target.value)},options.map(([key,label])=>this.el('option',{value:key},label)));
  select.value=value;return select;
 }
 facts(rows){return this.el('dl',{class:'op-facts'},rows.map(([label,value])=>this.el('div',{},this.el('dt',{},label),this.el('dd',{},value))))}
 section(title,...content){return this.el('section',{class:'op-section'},this.el('h3',{},title),content)}
 // Unconnected screen outlines: labels and layout only, with no submission or storage.
 previewSection(title,...content){
  const section=this.section(title,this.note('화면 미리보기 · 등록·저장·적용 준비 중'),content);
  section.classList.add('op-preview');return section;
 }
 previewField(label,placeholder,type='text'){
  return this.field(label,this.el('input',{type,disabled:true,placeholder}));
 }
 previewSelect(label,options){
  const select=this.select(label,[['','선택 준비 중'],...options.map(value=>[value,value])],'');select.disabled=true;
  return this.field(label,select);
 }
 previewButton(label){return this.button(label,()=>{},{disabled:true})}
 openDisplaySettings(){this.settingsSection='display';this.onNavigate('settings')}
 emptyTable(headers,message){
  return this.el('div',{class:'op-table-wrap'},this.el('table',{class:'op-table'},this.el('caption',{},message),this.el('thead',{},this.el('tr',{},headers.map(label=>this.el('th',{scope:'col'},label)))),this.el('tbody',{},this.el('tr',{},this.el('td',{colspan:headers.length,class:'op-table-empty'},'연결된 기록이 없습니다.')))));
 }
 setZones(zones){this.zones=zones;this.zoneId??=zones[0]?.id;if(['zones','settings','missions'].includes(this.view))this.render(this.view)}
 render(view){
  if(this.activeHold)this.store.stop('패널 갱신');
  // Return the shared camera before replacing its temporary workspace parent.
  this.onManualObservation?.(null);
  this.activeHold=null;this.view=view;this.title.textContent=TITLES[view]||'';
  this.container.dataset.page=view;this.container.replaceChildren();if(!TITLES[view])return;
  const intro=this.el('div',{class:'op-intro'},this.note({missions:'순찰 계획과 제어 상태를 한곳에서 확인합니다.',events:'사건을 찾고, 증거와 판단 근거를 함께 검토합니다.',records:'순찰 결과와 운영 기록을 확인합니다.',zones:'구역을 살펴보고 점검 기준을 구성합니다.',devices:'로봇의 모습과 연결·센서 상태를 함께 확인합니다.',settings:'표시 방식과 운영 정책, 관리 항목을 구성합니다.'}[view]),this.badge(this.store.demo?'예시 모드':'실제 데이터 대기',this.store.demo?'amber':''));
  this.container.append(intro,this.el('div',{class:'op-feedback',role:'status','aria-live':'polite'}));
  this[view==='zones'?'zonePage':view]();
 }
 refresh(reason){
  if(this.view==='dashboard')return;
  if(reason==='command'){this.refreshControl();return}
  this.render(this.view);
 }
 events(){
  this.container.append(this.note('실시간 수신은 미연결입니다. 예시 사건과 가져온 저장 기록을 구분해 검토합니다.'));
  const toolbar=this.el('div',{class:'op-toolbar'});
  const file=this.el('input',{type:'file',accept:'.json,.jpg,.jpeg',multiple:true,'aria-label':'블랙박스 파일 선택',class:'op-file'});
  file.addEventListener('change',()=>this.run(async()=>{await this.importFiles([...file.files]);file.value=''}));
  toolbar.append(this.el('label',{class:'op-button file-button'},'블랙박스 가져오기',file),this.button('검토 JSON 내보내기',()=>this.download(this.store.exportReview(),'mechadog-review.json','application/json')));
  this.container.append(toolbar,this.note('같은 사건 폴더의 meta.json + snapshot.jpg를 함께 선택하세요. 서버 업로드 없이 열며, 가져온 파일·메모는 새로고침하면 사라집니다.'));
  const search=this.el('input',{type:'search',name:'사건 검색',placeholder:'사건명 · 장치 · 구역 · 메모',value:this.filters.query,oninput:event=>{this.filters.query=event.target.value;this.renderEventList()}});
  const robotOptions=[...new Set(this.store.events.map(e=>e.robot))].map(id=>[id,id]);
  const filters=this.el('div',{class:'op-filters'},this.field('검색',search),this.field('유형',this.select('사건 유형',[['all','전체 유형'],['PPE','PPE'],['AUTH','출입 인증'],['OBJECT','물품'],['SYSTEM','저장 기록']],this.filters.type,value=>{this.filters.type=value;this.renderEventList()})),this.field('검토 상태',this.select('검토 상태 필터',[['all','전체 상태'],...Object.entries(REVIEW_STATES)],this.filters.status,value=>{this.filters.status=value;this.renderEventList()})),this.field('장치',this.select('사건 장치',[['all','전체 장치'],...robotOptions],this.filters.robot,value=>{this.filters.robot=value;this.renderEventList()})),this.button('초기화',()=>{this.filters={type:'all',status:'all',robot:'all',query:''};this.render('events')}));
  this.eventCount=this.el('p',{class:'op-result-count',role:'status'});
  this.eventList=this.el('div',{class:'op-event-list','aria-label':'사건 목록'});
  this.eventDetail=this.el('section',{class:'op-event-detail','aria-label':'선택한 사건'});
  this.container.append(filters,this.eventCount,this.el('div',{class:'op-evidence-workspace'},this.eventList,this.eventDetail));this.renderEventList();
 }
 renderEventList(){
  const records=this.store.queryEvents(this.filters);
  if(!records.some(e=>e.id===this.eventId))this.eventId=records[0]?.id||null;
  this.eventCount.textContent=records.length+'건 · 현재 조건';
  this.eventList.replaceChildren(...records.map(event=>this.button([
   this.el('span',{class:'op-row-meta'},event.robot,this.badge(event.source==='DEMO'?'예시':'저장 파일')),
   this.el('strong',{},event.title),this.el('span',{class:'op-row-meta'},event.zone),
   this.el('span',{class:'op-row-foot'},this.badge(REVIEW_STATES[event.review],event.review==='pending'?'amber':''),this.el('span',{},event.escalation))
  ],()=>{this.eventId=event.id;this.renderEventList()},{class:'op-event-row'+(event.id===this.eventId?' selected':''),'aria-pressed':event.id===this.eventId})));
  if(!records.length)this.eventList.append(this.note(this.store.queryEvents().length?'조건에 맞는 사건이 없습니다. 필터를 변경해 보세요.':'가져온 기록이 없습니다. 실시간 사건이 없다는 의미는 아닙니다.'));
  this.renderEventDetail(this.store.events.find(e=>e.id===this.eventId));
 }
 renderEventDetail(event){
  this.eventDetail.replaceChildren();if(!event){this.eventDetail.append(this.el('h3',{},'검토할 사건을 선택하세요'),this.note('예시 모드를 켜거나 저장된 블랙박스 파일을 가져올 수 있습니다.'));return}
  const draft=this.reviewDrafts.get(event.id)||{status:event.review,note:event.note};
  const remember=()=>this.reviewDrafts.set(event.id,{status:status.value,note:memo.value});
  const status=this.select('검토 결과',Object.entries(REVIEW_STATES),draft.status,remember);
  const memo=this.el('textarea',{name:'검토 메모',rows:4,maxlength:2000,placeholder:'판단 근거와 후속 조치를 남겨 주세요. 오탐은 근거 필수.',oninput:remember},draft.note);
  const form=this.el('form',{class:'op-review-form',onsubmit:submit=>{submit.preventDefault();this.run(()=>{this.store.reviewEvent(event.id,status.value,memo.value);this.reviewDrafts.delete(event.id);this.onToast(event.source==='DEMO'&&this.store.storageAvailable?'이 브라우저에 검토를 저장했어요.':'이번 세션에 검토를 저장했어요. 내보내기로 보관하세요.')})}},this.field('검토 결과',status),this.field('검토 메모',memo),this.el('button',{type:'submit',class:'op-button primary',disabled:this.store.role==='technician'},'검토 저장'));
  this.eventDetail.append(this.el('div',{class:'op-row-meta'},event.id,this.badge(event.source==='DEMO'?'실제 사건 아님':'과거 저장 기록')),this.el('h3',{class:'op-detail-title'},event.title),this.note(event.detail),this.facts([['FSM',event.state],['대응 단계',event.escalation],['출입 인증',event.auth],['PPE',event.ppe]]));
  if(event.snapshot)this.eventDetail.append(this.evidenceImage(event));
  else this.eventDetail.append(this.el('div',{class:'op-evidence-empty'},this.el('strong',{},'첨부된 스냅샷 없음'),this.el('span',{},'3D 예시 화면은 이 사건의 증거가 아닙니다.')));
  if(event.meta){
   this.eventDetail.append(this.note('처리 완료 시각 '+time(event.ts_ms)+' · 촬영 시각과 다를 수 있음'));
   const tracks=event.meta.tracks.map(track=>['추적 #'+track.track_id,Math.round(track.score*100)+'% · ['+track.box.join(', ')+']']);
   const detections=event.meta.detections.map(detection=>[detection.label,Math.round(detection.score*100)+'% · ['+detection.box.join(', ')+']']);
   this.eventDetail.append(this.section('당시 검출·추적 근거',this.note('추적 ID는 영구 신원이 아닙니다. 박스 좌표는 원본 JPEG의 픽셀 기준입니다.'),tracks.length||detections.length?this.facts([...tracks,...detections]):this.note('저장된 검출·추적 항목이 없습니다.')),this.el('details',{class:'op-raw'},this.el('summary',{},'당시 텔레메트리 원문'),this.el('pre',{},JSON.stringify(event.meta.telemetry,null,2))));
  }
  this.eventDetail.append(this.section('검토 기록',this.note('확인 불가와 오탐은 다릅니다. 이 검토는 경보 확인이나 기체 안전 리셋을 실행하지 않습니다.'),form,this.note(event.source==='DEMO'?(this.store.storageAvailable?'예시 검토는 이 브라우저에 저장됩니다.':'브라우저 저장을 사용할 수 없습니다. 새로고침 전 내보내세요.'):'가져온 기록의 검토는 세션에만 보관됩니다.')));
 }
 evidenceImage(event){
  const image=this.el('img',{src:event.snapshot,alt:event.title+' 원본 저장 스냅샷'}),overlay=this.el('div',{class:'op-box-overlay','aria-hidden':'true'});
  const wrap=this.el('figure',{class:'op-evidence-image'},this.el('div',{class:'op-image-inner'},image,overlay),this.el('figcaption',{},'원본 저장 JPEG · 현재 영상 아님'));
  image.addEventListener('load',()=>{
   overlay.replaceChildren();if(!event.meta||!image.naturalWidth||!image.naturalHeight)return;
   for(const track of event.meta.tracks){
    const [x1,y1,x2,y2]=track.box,w=image.naturalWidth,h=image.naturalHeight;
    if(x1<0||y1<0||x2>w||y2>h)continue;
    const box=this.el('span',{class:'op-track-box'},this.el('span',{},'#'+track.track_id+' · '+Math.round(track.score*100)+'%'));
    Object.assign(box.style,{left:x1/w*100+'%',top:y1/h*100+'%',width:(x2-x1)/w*100+'%',height:(y2-y1)/h*100+'%'});overlay.append(box);
   }
  });
  image.addEventListener('error',()=>wrap.replaceChildren(this.note('JPEG를 표시할 수 없습니다. 원본 파일을 확인해 주세요.','warning')));
  return wrap;
 }
 async importFiles(files){
  const json=files.filter(file=>/\.json$/i.test(file.name)),jpeg=files.filter(file=>/\.jpe?g$/i.test(file.name));
  if(json.length!==1||jpeg.length>1||files.length!==json.length+jpeg.length)throw new Error('meta.json 1개와 선택한 snapshot.jpg 1개만 가져오세요.');
  if(json[0].size>2*1024*1024)throw new Error('JSON은 2MB 이하만 열 수 있습니다.');
  let raw;try{raw=JSON.parse(await json[0].text())}catch{throw new Error('JSON 내용을 읽을 수 없습니다.')}
  let url=null;
  if(jpeg[0]){
   if(jpeg[0].size>15*1024*1024)throw new Error('JPEG는 15MB 이하만 열 수 있습니다.');
   const bytes=new Uint8Array(await jpeg[0].slice(0,3).arrayBuffer());
   if(bytes[0]!==255||bytes[1]!==216||bytes[2]!==255)throw new Error('실제 JPEG 형식의 스냅샷을 선택해 주세요.');
   url=this.document.defaultView.URL.createObjectURL(jpeg[0]);
  }
  try{
   const event=this.store.importBlackbox(raw,url);if(url&&event.snapshot!==url)this.document.defaultView.URL.revokeObjectURL(url);else if(url)this.urls.add(url);
   this.eventId=event.id;this.filters={type:'all',status:'all',robot:'all',query:''};
   if(this.view==='events')this.render('events');
   this.onToast('저장 기록을 열었어요. 실시간 연결은 변경되지 않습니다.');
  }catch(error){if(url)this.document.defaultView.URL.revokeObjectURL(url);throw error}
 }
 missions(){
  const store=this.store,active=['running','paused'].includes(store.mission.status);
  const draft=this.missionDraft||{robot:store.selected,zone:store.mission.zone,acknowledged:false};
  const robot=this.select('임무 로봇',ROBOTS.map(id=>[id,id]),draft.robot);
  const zone=this.select('점검 대상 구역',this.zones.map(z=>[z.label,z.label]),draft.zone);
  if(!zone.value&&this.zones.length)zone.value=this.zones[0].label;
  const check=this.el('input',{type:'checkbox',name:'예시 임무 확인',checked:draft.acknowledged});
  for(const element of [robot,zone,check])element.addEventListener('change',()=>{this.missionDraft={robot:robot.value,zone:zone.value,acknowledged:check.checked}});
  const form=this.el('form',{onsubmit:event=>{event.preventDefault();this.run(()=>{store.startMission({robot:robot.value,zone:zone.value,acknowledged:check.checked});this.missionDraft=null})}},this.el('div',{class:'op-form-grid'},this.field('로봇',robot),this.field('점검 대상 · 계획 메모',zone)),this.el('label',{class:'op-check'},check,'실제 임무가 아닌 웹 시연임을 확인했습니다.'),this.el('button',{type:'submit',class:'op-button primary',disabled:store.blocked||active||!this.zones.length},'예시 임무 시작'));
  this.container.append(this.section('순찰 세션',active?this.facts([['세션',store.mission.id],['점검 대상',store.mission.zone],['상태',STATUS[store.mission.status]],['실제 임무', '전송 안 함']]):form,this.el('div',{class:'op-toolbar'},this.button('일시정지',()=>store.pauseMission(),{disabled:store.mission.status!=='running'}),this.button('재개',()=>store.resumeMission(),{disabled:store.blocked||store.mission.status!=='paused'}),this.button('예시 임무 종료',()=>store.endMission(),{disabled:!active||store.role!=='operator'})),this.note('3D는 공통 예시 경로를 재생합니다. 구역별 실제 경로·충돌 회피는 연결되지 않았습니다.')));
  const claim=this.button('예시 제어권 요청',()=>store.claim(),{disabled:store.blocked,'data-control':'claim'});
  const release=this.button('반납',()=>store.release(),{'data-control':'release'});
  const pad=this.el('div',{class:'op-drive-pad','aria-label':'누르는 동안 예시 이동'});
  for(const [command,label]of [['FORWARD','전진'],['LEFT','좌회전'],['STOP','정지'],['RIGHT','우회전'],['BACKWARD','후진']]){
   const shortcut=command==='STOP'?'Space':Object.keys(MANUAL_KEYS).find(key=>MANUAL_KEYS[key]===command).slice(-1);
   const button=this.el('button',{type:'button',class:'op-drive '+command.toLowerCase(),'data-drive':command,'aria-label':label+' · 예시','aria-keyshortcuts':shortcut});
   if(command==='STOP')button.textContent='STOP';
   else{button.innerHTML=icon('arrow');button.append(this.el('kbd',{class:'op-drive-key'},shortcut))}
   if(command==='STOP'){
    const stop=()=>{this.activeHold=null;store.stop('정지 버튼')};
    button.addEventListener('pointerdown',event=>{if(event.button===0){event.preventDefault();stop()}});
    button.addEventListener('keydown',event=>{if([' ','Enter'].includes(event.key)){event.preventDefault();stop()}});
    button.addEventListener('click',stop);
   }
   else this.bindHold(button,command);
   pad.append(button);
  }
  const target=this.select('수동 제어 대상',ROBOTS.map(id=>[id,id]),store.selected,id=>store.selectRobot(id));
  const cameraMount=this.el('div',{class:'op-manual-camera'+(this.observationError?' has-observation-error':''),'data-manual-camera':''},
   this.el('div',{class:'op-observation-error',role:'status',hidden:!this.observationError},this.el('h4',{},store.selected+' · 관측 화면 표시 중단'),this.el('p',{'data-observation-error':''},this.observationError||''),this.button('관측 화면 다시 열기',()=>this.document.defaultView.location.reload())));
  const controls=this.el('section',{class:'op-manual-controls','aria-label':store.selected+' 수동 조작'},
   this.el('h4',{},store.selected+' · 수동 조작'),this.el('div',{class:'op-toolbar'},claim,release),pad,
   this.el('p',{class:'op-command-state','data-control':'state',role:'status'}),
   this.el('div',{class:'op-keyboard-help'},
    this.el('label',{class:'op-keyboard-toggle'},this.el('input',{type:'checkbox',checked:this.keyboardEnabled,onchange:event=>{this.keyboardEnabled=event.target.checked;this.stopManualInput('키보드 조작 설정 변경')}}),'키보드 조작'),
    this.el('span',{},this.el('kbd',{},'W A S D'),' 이동 · ',this.el('kbd',{},'Space'),' 정지')),
   this.note('누르는 동안만 유지 · 놓으면 STOP. 웹 시연이며 로봇과 3D 모델은 움직이지 않습니다.'),
   this.el('p',{class:'op-transmission'},'명령 미전송 · ACK 없음'),
   this.el('section',{class:'op-camera-posture','aria-label':'본체 자세로 시야 조절'},
    this.el('div',{class:'op-posture-heading'},this.el('h4',{},'본체 자세로 시야 조절'),this.badge('연결 준비')),
    this.el('div',{class:'op-posture-buttons'},['앞쪽 들기','기준 자세','앞쪽 낮추기'].map(label=>this.previewButton(label))),
    this.note('기체의 앞뒤 기울임 기능을 이용하는 방식입니다. 장착 방향과 동작 범위 확인 후 연결합니다.'),
    this.el('p',{class:'op-camera-capability'},'카메라 독립 회전 · 지원 미확인')));
  const manual=this.el('section',{class:'op-manual-workspace','aria-label':'로봇 관측과 수동 제어'},
   this.el('div',{class:'op-manual-heading'},this.el('h3',{},'로봇을 보며 제어'),this.field('관측 · 제어 대상',target),this.badge('실제 장비 미연결')),
   this.el('div',{class:'op-observation-layout'},cameraMount,controls,this.manualLocation()));
  this.container.querySelector('.op-section').before(manual);
  this.onManualObservation?.(cameraMount);
  this.updateRobotObservation(this.robotObservation);
  if(store.blocked)this.container.append(this.note(store.estop?'예시 정지 잠금 상태입니다. 설정에서 웹 예시 잠금만 초기화할 수 있습니다.':'예시 모드·수신 상태·운영자 시연 역할을 설정에서 확인하세요.','warning'),this.button('운영 설정',()=>this.openDisplaySettings()));
  this.refreshControl();
  this.container.append(this.previewSection('순찰 계획 · 구역별 진행',
   this.el('div',{class:'op-form-grid'},this.previewField('계획 이름','순찰 계획 이름'),this.previewSelect('대상 로봇',ROBOTS)),
   this.emptyTable(['순서','방문 구역','진행 상태','확인 시각'],'방문 순서 · 실제 경로 미설정'),
   this.el('div',{class:'op-toolbar'},this.previewButton('구역 추가'),this.previewButton('계획 저장')),
   this.facts([['연결 · 제어권','미확인'],['배터리 · 안전 상태','미확인'],['시작 가능 여부','실제 상태 수신 후 확인'],['현재 구역 · 다음 구역','미수신 / 미설정']]),
   this.note('구역 방문 순서와 시작 전 확인 항목을 배치한 화면입니다. 위쪽 웹 시연의 단일 구역 메모와 별도로, 실제 경로와 진행 결과는 아직 없습니다.')));
 }
 manualLocation(){
  const section=this.el('section',{class:'op-location','aria-label':'선택 로봇 위치'});
  section.append(this.el('div',{class:'op-location-heading'},this.el('h4',{},this.store.selected+' · 위치와 방향'),this.badge('예시 배치 · 실측 아님')));
  const svg=(tag,attrs={},text)=>{const node=this.document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [key,value]of Object.entries(attrs))node.setAttribute(key,String(value));if(text)node.textContent=text;return node};
  const zones=this.zones.filter(zone=>zone.center?.length===2&&zone.size?.length===2);
  if(zones.length){
   const minX=Math.min(...zones.map(z=>z.center[0]-z.size[0]/2))-2,maxX=Math.max(...zones.map(z=>z.center[0]+z.size[0]/2))+2;
   const minZ=Math.min(...zones.map(z=>-z.center[1]-z.size[1]/2))-2,maxZ=Math.max(...zones.map(z=>-z.center[1]+z.size[1]/2))+2;
   const map=svg('svg',{viewBox:`${minX} ${minZ} ${maxX-minX} ${maxZ-minZ}`,class:'op-location-map',role:'img','aria-label':this.store.selected+' 예시 구역 배치와 바라보는 방향'});
   for(const zone of zones){
    const group=svg('g'),label=svg('text',{x:zone.center[0],y:-zone.center[1]});
    if(zone.size[0]<zone.size[1]/2)label.setAttribute('transform',`rotate(-90 ${zone.center[0]} ${-zone.center[1]})`);
    label.textContent=zone.label;group.append(svg('rect',{x:zone.center[0]-zone.size[0]/2,y:-zone.center[1]-zone.size[1]/2,width:zone.size[0],height:zone.size[1]}),label);map.append(group);
   }
   const marker=svg('g',{'data-location-marker':'',visibility:'hidden'});
   marker.append(svg('circle',{r:1.25}),svg('path',{d:'M 1.8 -1.2 L 3.6 0 L 1.8 1.2',class:'op-map-direction'}));map.append(marker);
   section.append(map);
  }
  section.append(this.note('예시 위치를 준비하고 있습니다.'),this.el('p',{class:'op-location-readout','data-location-readout':''},'실제 위치 · 방향 · 마지막 수신 —'));
  return section;
 }
 updateRobotObservation(observation){
  this.robotObservation=observation;
  if(this.view!=='missions')return;
  const section=this.container.querySelector('.op-location');if(!section)return;
  const ready=!this.observationError&&this.store.demo&&!this.store.stale&&observation?.id===this.store.selected&&[observation.x,observation.z,observation.heading].every(Number.isFinite);
  const map=section.querySelector('svg');if(map)map.style.display=ready?'':'none';
  const marker=section.querySelector('[data-location-marker]');
  if(marker&&ready){marker.setAttribute('transform',`translate(${observation.x} ${observation.z}) rotate(${-observation.heading*180/Math.PI})`);marker.setAttribute('visibility','visible')}
  section.querySelector('.op-note').textContent=this.observationError?'그래픽 표시 중단 · 예시 위치를 표시할 수 없습니다.':ready?'선택 로봇의 예시 위치 · 화살표는 바라보는 방향':this.store.stale?'예시 수신 만료 · 위치 갱신 대기':'실제 위치 미수신 · 연결 후 지도에 표시됩니다.';
 }
 setObservationError(message){
  this.observationError=message;this.updateRobotObservation(null);
  const mount=this.container.querySelector('[data-manual-camera]');
  if(mount){mount.classList.add('has-observation-error');mount.querySelector('.op-observation-error').hidden=false;mount.querySelector('[data-observation-error]').textContent=message}
 }
 manualKeyCode(event){return event.code||({'w':'KeyW','a':'KeyA','s':'KeyS','d':'KeyD',' ':'Space'}[event.key?.toLowerCase()]||'')}
 isTypingTarget(target){return !!target?.closest?.('input,textarea,select,[contenteditable]:not([contenteditable="false"]),[role="textbox"],[role="combobox"]')}
 stopManualInput(reason){this.activeHold=null;this.store.stop(reason)}
 handleManualKeyDown(event){
  const code=this.manualKeyCode(event),movement=MANUAL_KEYS[code];
  if(this.view!=='missions'||(!movement&&code!=='Space')||this.document.hidden||this.document.querySelector('dialog[open]'))return;
  if(this.isTypingTarget(event.target)||event.isComposing||event.ctrlKey||event.altKey||event.metaKey)return;
  // Space always stops this workspace; capture prevents focused drive buttons from moving.
  if(code==='Space'){
   event.preventDefault();event.stopImmediatePropagation();this.manualPressedKeys.add(code);this.stopManualInput('Space 정지');return;
  }
  if(!this.keyboardEnabled)return;
  event.preventDefault();event.stopImmediatePropagation();
  if(event.repeat||this.manualPressedKeys.has(code))return;
  this.manualPressedKeys.add(code);
  if(this.manualPressedKeys.has('Space')||this.activeHold||this.store.blocked||this.store.control!==this.store.selected||this.observationError)return;
  this.run(()=>{this.store.move(movement);this.activeHold={kind:'shortcut',id:code}});
 }
 handleManualKeyUp(event){
  const code=this.manualKeyCode(event),wasPressed=this.manualPressedKeys.delete(code);
  if(this.activeHold?.kind==='shortcut'&&this.activeHold.id===code)this.stopManualInput('키보드 입력 해제');
  if(wasPressed){event.preventDefault();event.stopImmediatePropagation()}
 }
 bindHold(button,command){
  const start=(kind,id)=>{
   if(this.activeHold||this.manualPressedKeys.has('Space'))return false;
   try{this.store.move(command);this.activeHold={kind,id,button};return true}catch(error){this.error(error);return false}
  };
  const end=(kind,id)=>{if(this.activeHold?.kind===kind&&this.activeHold.id===id){this.activeHold=null;this.store.stop('입력 해제')}};
  button.addEventListener('pointerdown',event=>{if(event.button!==0)return;event.preventDefault();if(start('pointer',event.pointerId)){try{button.setPointerCapture(event.pointerId)}catch{this.activeHold=null;this.store.stop('포인터 캡처 실패')}}});
  for(const type of ['pointerup','pointercancel','lostpointercapture'])button.addEventListener(type,event=>end('pointer',event.pointerId));
  button.addEventListener('keydown',event=>{if(event.key!=='Enter')return;event.preventDefault();if(!event.repeat)start('key',event.key)});
  button.addEventListener('keyup',event=>{if(event.key==='Enter'){event.preventDefault();end('key',event.key)}});
  button.addEventListener('blur',()=>{if(this.activeHold?.button===button){this.activeHold=null;this.store.stop('버튼 초점 이탈')}});
 }
 refreshControl(){
  const store=this.store,state=this.container.querySelector('[data-control="state"]');
  if(store.command==='STOP')this.activeHold=null;
  if(state)state.textContent=(store.control?store.control+' · 예시 제어권':'제어권 없음')+' / '+store.command;
  for(const button of this.container.querySelectorAll('[data-drive]')){const command=button.dataset.drive;button.disabled=command!=='STOP'&&(store.blocked||store.control!==store.selected);button.classList.toggle('pressed',command===store.command&&command!=='STOP')}
  const claim=this.container.querySelector('[data-control="claim"]'),release=this.container.querySelector('[data-control="release"]');
  if(claim)claim.disabled=store.blocked||store.control===store.selected;if(release)release.disabled=!store.control;
 }
 records(){
  this.container.append(this.note('이 페이지는 로컬 UI 작업 기록입니다. 실물 운행·서버 감사 로그가 아닙니다. 최대 500건을 세션에 보관하며 새로고침하면 초기화됩니다.'),this.el('div',{class:'op-toolbar'},this.button('조작 이력 CSV',()=>{this.download(this.store.exportRecords(),'mechadog-ui-records.csv','text/csv');this.render('records')}),this.button('사건 검토 JSON',()=>this.download(this.store.exportReview(),'mechadog-review.json','application/json'))));
  this.container.append(this.section('예시 순찰 세션',this.store.sessions.length?this.el('div',{class:'op-session-list'},this.store.sessions.map(session=>this.el('article',{},this.el('strong',{},session.robot+' · '+session.zone),this.badge(STATUS[session.id===this.store.mission.id?this.store.mission.status:session.status]),this.el('p',{},time(session.startedAt)+' → '+time(session.endedAt))))):this.note('이 세션에서 시작한 예시 임무가 없습니다.')));
  const list=this.el('div',{class:'op-record-list'}),count=this.el('p',{class:'op-result-count',role:'status'});
  const render=term=>{const rows=this.store.records.filter(row=>[row.action,row.detail,row.source].join(' ').toLowerCase().includes(term.toLowerCase()));count.textContent=rows.length+'건 · 로컬 기록';list.replaceChildren(...rows.map(row=>this.el('article',{class:'op-record'},this.el('time',{datetime:new Date(row.ts_ms).toISOString()},time(row.ts_ms)),this.el('div',{},this.el('strong',{},row.action),this.el('p',{},row.detail),this.el('small',{},row.source)))));if(!rows.length)list.append(this.note('조건에 맞는 기록이 없습니다.'))};
  this.container.append(this.section('조작 이력',this.field('기록 검색',this.el('input',{type:'search',placeholder:'동작 · 메모 · 출처',oninput:event=>render(event.target.value)})),count,list));render('');
  this.container.append(this.previewSection('순찰 결과 · 세션 상세',
   this.el('div',{class:'op-form-grid'},this.previewField('조회 시작일','', 'date'),this.previewField('조회 종료일','', 'date')),
   this.emptyTable(['순찰 세션','로봇','시작 · 종료','완료 상태'],'실제 순찰 세션 목록'),
   this.facts([['선택한 세션','선택 전'],['완료 여부 · 중단 원인','미수신'],['방문 구역 · 사건 수','미수신'],['운영자 검토 결과','미수신']]),
   this.emptyTable(['시각','구분','상태 · 사건 · 명령'],'세션 타임라인'),
   this.emptyTable(['구역','인증 · PPE','물품 변화','안전 · 검토 결과'],'구역별 검사 결과'),
   this.el('div',{class:'op-toolbar'},this.previewButton('결과 JSON'),this.previewButton('결과 CSV')),
   this.note('저장된 영상이 없으므로 재생 버튼은 제공하지 않습니다. 위쪽 내보내기는 현재 브라우저의 예시·가져온 기록에만 해당합니다.')));
 }
 zonePage(){
  this.container.append(this.note('현재 공장은 신규 제작한 48 × 32m 예시 공간입니다. 실측 지도·SLAM·실제 로봇 좌표가 아닙니다.'));
  const nav=this.el('div',{class:'op-zone-list'});
  for(const zone of this.zones)nav.append(this.button([this.el('span',{},zone.label),this.el('small',{},zone.id)],()=>{this.zoneId=zone.id;this.render('zones')},{class:'op-zone-row'+(this.zoneId===zone.id?' selected':''),'aria-pressed':this.zoneId===zone.id}));
  this.container.append(nav);
  const zone=this.zones.find(z=>z.id===this.zoneId);if(!zone){this.container.append(this.note('공간 데이터를 불러오는 중입니다.'));return}
  const policy=this.store.policies[zone.id];
  this.container.append(this.section(zone.label,this.facts([['예시 크기',zone.size.join(' × ')+' m'],['예시 중심',zone.center.join(', ')+' m'],['실제 좌표 / 지도','미연결'],['기준 물품 비교','실제 기준 스냅샷 없음'],['로컬 PPE 초안',policy?('안전모 '+(policy.helmet?'필수':'미지정')+' · 안전조끼 '+(policy.vest?'필수':'미지정')):'작성 전']]),this.el('div',{class:'op-toolbar'},this.button('3D에서 위치 보기',()=>this.onFocusZone(zone),{class:'op-button primary'}),this.button('PPE 초안 작성',()=>this.openDisplaySettings())),this.note('중심·크기는 화면 설계를 위한 값입니다. 실물 경계나 임무 웨이포인트에 적용하지 않습니다.')));
  this.container.append(this.previewSection('기준 물품 · '+zone.label,
   this.emptyTable(['표시명','객체 클래스','수량','필수 여부'],'구역의 확정 기준 물품'),
   this.el('div',{class:'op-form-grid'},this.previewField('표시명','물품 이름'),this.previewField('객체 클래스','검출 클래스'),this.previewField('기준 수량','수량','number'),this.previewSelect('필수 여부',['필수','선택'])),
   this.el('div',{class:'op-toolbar'},this.previewButton('기준 물품 등록'),this.previewButton('선택 항목 수정')),
   this.facts([['기준 버전 · 적용 시각','미등록'],['변경 이력','연결 대기']])));
  this.container.append(this.previewSection('기준 · 현재 비교',
   this.el('div',{class:'op-comparison'},['확정 기준 스냅샷','현재 순찰 스냅샷'].map(label=>this.el('div',{class:'op-evidence-empty'},this.el('strong',{},label),this.el('span',{},'수신된 이미지 없음')))),
   this.emptyTable(['항목','기준 → 현재','변화 유형'],'없어짐 · 새로 생김 · 수량 변화 · 판정 불가'),
   this.el('div',{class:'op-form-grid'},this.previewSelect('변화 검토',['실제 변화','오탐','기준 갱신 필요','판정 불가']),this.previewField('검토 근거','비교 결과와 판단 근거')),
   this.previewButton('검토 제출'),this.note('기준 목록은 검토 승인 없이 자동 갱신하지 않습니다. 예시 3D 화면은 비교 증거로 사용하지 않습니다.')));
 }
 devices(){
  const store=this.store;
  this.robotCanvas??=this.el('canvas',{class:'robot-detail-canvas','aria-label':'로봇 3D 예시 모델'});
  this.robotCanvas.setAttribute('aria-label',store.selected+' 로봇 3D 예시 모델. 드래그로 회전하며 아래 버튼으로도 조작할 수 있습니다.');
  this.robotPreviewNotice=this.el('p',{class:'robot-preview-error',role:'status',hidden:!this.robotPreviewError},this.robotPreviewError);
  const preview=this.el('section',{class:'robot-inspection','aria-label':store.selected+' 3D 모델'},
   this.el('div',{class:'robot-inspection-heading'},this.el('h2',{},store.selected),this.badge('표시용 3D 모델')),
   this.el('div',{class:'robot-model-stage'},this.robotCanvas,this.robotPreviewNotice),
   this.el('div',{class:'robot-view-controls','aria-label':'로봇 모델 시야 조작'},[['left','왼쪽 회전'],['right','오른쪽 회전'],['in','확대'],['out','축소'],['reset','시점 초기화']].map(([action,label])=>this.button(label,()=>this.onRobotViewAction?.(action),{'data-robot-action':action}))),
   this.note('드래그로 회전 · 휠로 확대 · 공통 예시 형상이며 실제 자세·관절 상태를 나타내지 않습니다.'));
  const status=this.el('section',{class:'robot-status-sheet','aria-label':store.selected+' 상태'},
   this.el('div',{class:'op-status-heading'},this.el('h2',{},'로봇 상태'),this.badge('연결 대기')),
   this.note('실제 장비에서 수신된 값이 없습니다. 마지막 수신 시각과 상태는 연결 후 표시됩니다.'),
   this.facts([['연결 · 마지막 수신','미연결 / —'],['실제 device_id','미수신'],['FSM · 대응 단계','미수신 / 미수신'],['배터리 전압','— V · 미수신'],['전방 거리','— cm · 미수신'],['IMU pitch / roll / yaw','— / — / —'],['링크 · 안전 래치','미수신 / 미수신'],['마지막 명령 수락 나이','— ms · 미수신']]),
   this.el('div',{class:'op-toolbar'},this.button('관제에서 가상 시점 보기',()=>this.onFocusRobot(store.selected),{class:'op-button primary'}),this.button('순찰 · 제어 열기',()=>this.onNavigate('missions'))));
  this.container.append(this.el('div',{class:'op-device-switch','aria-label':'상세 로봇 선택'},ROBOTS.map(id=>this.button([this.el('strong',{},id),this.el('span',{},'미연결')],()=>store.selectRobot(id),{'aria-label':id+' 상태 보기','aria-pressed':id===store.selected,class:'op-button'+(id===store.selected?' selected':'')}))),this.el('div',{class:'robot-detail-layout'},preview,status));
  this.onRobotPreview?.(this.robotCanvas);
  this.container.append(this.section('장치 역할과 지원 상태',this.facts([['Motion ESP32','MOVE·STOP·ESTOP·RESET_SAFE 적용 경로 존재'],['Vision XIAO / 카메라','웹으로 영상 수신되지 않음'],['Host / 블랙박스','사건별 JPEG + meta.json 저장 구현'],['실시간 웹 전송','WS 푸시 미구현'],['LiDAR / 기준기','2대 운용 계획 · 개체별 배정 미확정'],['전도 자동 감지','Phase 2 이연 · 정상 작동 추정 금지']]),this.note('Git 코드 구현 상태이며, 이 기체에서 동작한다는 검증 결과가 아닙니다. POSE·GAIT·ACTION·LED·SOUND·STATE는 현재 펌웨어에서 적용되지 않습니다.')));
  this.container.append(this.section('응답을 읽는 기준',this.facts([['ok','파서 수락 여부'],['applied','명령 처리 적용 여부'],['actuators','실제 액추에이터 활성 정보'],['ACK safe_latched','명령 응답의 안전 필드'],['telemetry safety_latched','텔레메트리의 안전 필드']]),this.note('ACK 한 항목만 보고 정지 완료 또는 안전 복구 완료로 판단하지 않습니다.')));
  this.container.append(this.previewSection('개체 프로파일 · 노드 진단',
   this.facts([['선택 장치',store.selected+' · 웹 표시 이름'],['실제 개체 프로파일','미연결'],['Phase 1 기준기','미지정'],['Phase 2 기준기','미지정']]),
   this.previewSelect('진단 노드',['MechDog 모션','XIAO 비전','LiDAR 중계']),
   this.facts([['마지막 접속','미수신'],['펌웨어 버전','미수신'],['IP · 포트','미수신'],['최근 오류','미수신 · 오류 없음으로 판단하지 않음']]),
   this.previewButton('진단 기록 조회'),this.note('개체별 역할·기준기 배정은 아직 확정하지 않았습니다. 네트워크 비밀번호와 비밀키는 표시하지 않습니다.')));
 }
 setRobotPreviewError(message){
  this.robotPreviewError=message||'';
  if(this.robotPreviewNotice){this.robotPreviewNotice.textContent=this.robotPreviewError;this.robotPreviewNotice.hidden=!message}
 }
 settings(){
  const store=this.store;
  this.container.append(this.el('nav',{class:'op-subnav','aria-label':'설정 항목'},[['display','화면 · 정책'],['badges','사원증'],['accounts','사용자 · 권한'],['audit','감사 기록']].map(([id,label])=>this.button(label,()=>{this.settingsSection=id;this.render('settings');this.container.querySelector('[data-settings="'+id+'"]').focus()},{'data-settings':id,'aria-pressed':id===this.settingsSection,class:'op-button'+(id===this.settingsSection?' selected':'')}))));
  if(this.settingsSection!=='display'){this.managementPreview();return}
  this.container.append(this.section('화면 데이터 · 시연 권한',this.note('아래 역할은 UI 흐름을 시험하는 선택입니다. 계정 로그인이나 실제 접근 제어가 아닙니다.'),this.el('div',{class:'op-form-grid'},this.field('표시 데이터',this.select('표시 데이터',[['demo','예시 데이터 표시'],['real','실제 데이터 대기']],store.demo?'demo':'real',value=>store.setDemo(value==='demo'))),this.field('시연 역할',this.select('시연 역할',[['operator','운영자 · 시연'],['reviewer','검토자 · 시연'],['technician','기술자 · 시연']],store.role,value=>store.setRole(value)))),this.el('div',{class:'op-toolbar'},this.button(store.stale?'예시 수신 상태 복구':'예시 수신 만료 시험',()=>store.setStale(!store.stale),{disabled:!store.demo}),this.button('웹 예시 정지 잠금 초기화',()=>store.clearPreviewStop(),{disabled:!store.estop})),this.note('복구·역할 변경·정지 잠금 초기화 후에는 자동으로 움직이지 않습니다. 실물 안전 잠금은 이 화면에서 해제할 수 없습니다.','warning')));
  const zone=this.zones.find(z=>z.id===this.zoneId)||this.zones[0];
  if(zone){
   const draft=this.policyDrafts.get(zone.id)||store.policies[zone.id]||{helmet:false,vest:false,note:''};
   const helmet=this.el('input',{type:'checkbox',checked:draft.helmet}),vest=this.el('input',{type:'checkbox',checked:draft.vest}),memo=this.el('textarea',{rows:2,maxlength:500,name:'PPE 정책 메모',placeholder:'구역의 판정 조건과 검토 필요사항'},draft.note);
   const remember=()=>this.policyDrafts.set(zone.id,{helmet:helmet.checked,vest:vest.checked,note:memo.value});
   for(const element of [helmet,vest,memo])element.addEventListener('input',remember);
   this.container.append(this.section('구역 PPE 정책 · 로컬 초안',this.field('구역',this.select('PPE 정책 구역',this.zones.map(z=>[z.id,z.label]),zone.id,value=>{this.zoneId=value;this.render('settings')})),this.el('form',{onsubmit:event=>{event.preventDefault();this.run(()=>{store.savePolicy(zone.id,{helmet:helmet.checked,vest:vest.checked,note:memo.value});this.policyDrafts.delete(zone.id);this.onToast(store.storageAvailable?'이 브라우저에 정책 초안을 저장했어요.':'저장소를 사용할 수 없어 이번 세션에만 유지됩니다.')})}},this.el('div',{class:'op-toolbar'},this.el('label',{class:'op-check'},helmet,'안전모 필수'),this.el('label',{class:'op-check'},vest,'안전조끼 필수')),this.field('검토 메모',memo),this.el('button',{type:'submit',class:'op-button'},'로컬 초안 저장')),this.note('PPE 모델이나 서버 설정에는 적용되지 않습니다. 온보드 안전 임계값은 수정하지 않습니다.')));
  }
  this.container.append(this.section('대응 단계 · 안전 해제 구분',this.el('ol',{class:'op-escalation'},[
   ['L0','평상 단계','L1에서 사람 미검출 5초면 L0 복귀'],['L1','대상 관찰','300ms 내 3회 검출 · 미인증 10초면 L2'],['L2','인증 대응','미검출 5초 / 인증 30초 초과 / 2회 실패 시 L3'],['L3','관리자 판단 필요','관리자 확인과 조치로 해제 · PPE 판정과 별개'],['F','안전 잠금','원인 확인 → RESET_SAFE → 래치 해제 보고 확인']
  ].map(([level,title,detail])=>this.el('li',{},this.el('span',{class:'op-level'},level),this.el('div',{},this.el('strong',{},title),this.el('p',{},detail))))),this.note('정본의 설정값을 읽기 전용으로 표시합니다. F 이전에 L3였다면 F 해제 뒤 L3가 유지됩니다. 사건 검토 저장은 두 잠금을 모두 해제하지 않습니다.')));
  this.container.append(this.section('연결과 구현 기준',this.facts([['실제 로봇 / 인증 서버','연결 안 됨'],['Isaac Sim 카메라','이 웹의 수신 연결은 미완료'],['구역 / 사원증 관리 서버','미구현 · 실제 CRUD 제공 안 함'],['저장 범위','예시 검토·PPE 초안: 브라우저 / 나머지: 세션']]),this.el('a',{href:'https://github.com/PJW-1/mechadog/tree/'+SOURCE_REVISION,target:'_blank',rel:'noopener noreferrer',class:'op-source-link'},'확인한 Git 정본 · '+SOURCE_REVISION.slice(0,8)+' ↗'),this.note('새 공장 Isaac 씬은 별도 제작되었지만, 상세 씬 렌더링·로봇 물리 모델·이 화면과의 영상 연동은 검증 완료 상태가 아닙니다.')));
 }
 managementPreview(){
  if(this.settingsSection==='badges'){
   this.container.append(this.previewSection('사원증 목록 · 등록',
    this.emptyTable(['사번 · 표시명','ArUco ID','상태','유효기간'],'사원증 연결 대기 · 실제 등록 인원 미확인'),
    this.el('div',{class:'op-form-grid'},this.previewField('사번','사번'),this.previewField('표시명','이름 또는 표시명'),this.previewField('사원증 ArUco ID','마커 번호','number'),this.previewSelect('상태',['활성','비활성']),this.previewField('유효 시작일','','date'),this.previewField('유효 종료일','','date')),
    this.el('div',{class:'op-toolbar'},this.previewButton('사원증 등록'),this.previewButton('선택 항목 수정'),this.previewButton('비활성화')),
    this.note('입력 항목과 목록 구조를 확인하는 화면입니다. 사원증 발급·인증 적용은 아직 제공하지 않습니다.')));
  }else if(this.settingsSection==='accounts'){
   this.container.append(this.previewSection('사용자 · 권한 구성',
    this.emptyTable(['계정 ID','표시명','역할','상태'],'사용자 목록 연결 대기'),
    this.el('div',{class:'op-form-grid'},this.previewField('계정 ID','계정 식별자'),this.previewField('표시명','화면에 표시할 이름'),this.previewSelect('역할',['운영자','검토자','기술 담당자','관리자'])),
    this.el('div',{class:'op-toolbar'},this.previewButton('사용자 등록'),this.previewButton('권한 변경')),
    this.facts([['운영자','관제 · 순찰 · 사건 대응'],['검토자','증거 조회 · 검토'],['기술 담당자','장치 진단 · 안전 조건 확인'],['관리자','사용자 · 구역 · 정책 관리']]),
    this.note('명세의 역할 구분안입니다. 로그인 방식과 실제 접근 권한은 결정 대기이며, 화면·정책의 시연 역할 선택은 계정 권한을 바꾸지 않습니다.')));
  }else{
   this.container.append(this.previewSection('감사 기록 조회',
    this.el('div',{class:'op-form-grid'},this.previewField('조회 시작일','','date'),this.previewField('조회 종료일','','date'),this.previewField('사용자','계정 ID'),this.previewSelect('작업 유형',['로그인 · 로그아웃','임무 · 제어권','긴급 정지 · 해제','사건 검토','구역 · 사원증 · 정책 · 설정'])),
    this.previewButton('기록 조회'),this.emptyTable(['시각','사용자','작업 · 대상','결과'],'감사 기록 연결 대기'),
    this.facts([['선택한 기록','선택 전'],['변경 전 · 변경 후','미수신'],['적용 장치','미수신']]),
    this.note('감사 기록은 조회 전용으로 구성합니다. 운영 기록의 로컬 예시 이력과 구분하며, 이 화면에서 수정·삭제하지 않습니다.')));
  }
 }
 download(content,name,type){
  const window=this.document.defaultView,url=window.URL.createObjectURL(new window.Blob([content],{type:type+';charset=utf-8'}));
  const link=this.el('a',{href:url,download:name});this.document.body.append(link);link.click();link.remove();
  window.setTimeout(()=>window.URL.revokeObjectURL(url),1000);
 }
 dispose(){this.store.release('패널 종료');this.document.removeEventListener('keydown',this.manualKeyDown,true);this.document.removeEventListener('keyup',this.manualKeyUp,true);this.document.removeEventListener('focusin',this.manualFocus);this.document.defaultView.removeEventListener('blur',this.manualBlur);this.manualPressedKeys.clear();for(const url of this.urls)this.document.defaultView.URL.revokeObjectURL(url);this.urls.clear()}
}
