import {PlanningPanel} from './planning-panel.js';
import {RoutePlanner} from './route-planner.js';
import {EVENT_CATEGORIES,EVENT_TITLES,REVIEW_STATES,ROBOTS,historyEvent} from './operations.js';
import {HistoryLink} from './history-link.js';
import {icon} from './icons.js';
import {MODE_NAMES,describeTelemetry} from './telemetry-feed.js';
import {LiveMap} from './live-map.js';
import {ControlMinimap} from './control-minimap.js';

const VOICE_ROLES={user:'현장 발화',robot:'로봇 응답',admin:'경고 방송',system:'시스템',robot_evt:'로봇 사건'};
const TITLES={missions:'제어 · 장치',events:'사건 검토',history:'사건 이력',records:'운영 기록',zones:'구역 · 동선',devices:'장치 상태',voice:'음성 중계',settings:'운영 설정'};
const STATE_NAMES={IDLE:'대기',PATROL:'순찰',OBSERVE:'관찰',MANUAL:'수동 제어',ESTOP:'긴급 정지',AUTH_WAIT:'인증 대기',TRACK:'대상 추적',FAILSAFE:'안전 정지'};
// 순찰 판의 끝 (DATA_MODEL 5절 `result`). 진행 중이면 NULL 이다.
const RUN_RESULTS={stopped:'순찰 정지',manual:'수동 전환',failsafe:'안전 정지',shutdown:'런타임 종료',interrupted:'비정상 종료'};
const HISTORY_FILTERS=()=>({event:'all',escalation:'all',robot:'all',zone:'all',reviewed:'all',since:'',until:'',mission:null});
const STATUS={idle:'시작 전',running:'예시 진행 중',paused:'일시정지',ended:'종료'};
const MANUAL_KEYS={KeyW:'FORWARD',KeyA:'LEFT',KeyS:'BACKWARD',KeyD:'RIGHT'};
const time=value=>value==null?'—':new Date(value).toLocaleString('ko-KR',{hour12:false});
// 목록 행에 넣는 짧은 시각. 같은 사건이 여러 건일 때 이것 말고는 서로를 가를 것이 없다.
const clock=value=>new Date(value).toLocaleTimeString('ko-KR',{hour12:false});

// All record content is text, never HTML. Files stay in this browser session.
export class OperationalPanels {
 constructor({store,container,title,onNavigate,onFocusZone,onFocusRobot,onRobotViewAction,onRobotPreview,onManualObservation,onToast,voiceLink=null,history=null,getVisionStatus=()=>({state:'off'}),document=globalThis.document}){
  Object.assign(this,{store,container,title,onNavigate,onFocusZone,onFocusRobot,onRobotPreview,onRobotViewAction,onManualObservation,onToast,voiceLink,getVisionStatus,document});
  // 사건 이력 (WBS 4.6.7). 관제 서버가 내보낸 화면에서만 있다 — 없으면 이력 화면이 그렇다고 적는다.
  this.historyLink=history?new HistoryLink(history):null;this.historyFilters=HISTORY_FILTERS();this.historyQuery='';this.historyItems=[];this.historyRuns=[];this.historyTotal=0;this.historyMeta=null;this.historyFocus=null;this.historyId=null;this.historyToken=0;
  this.view='dashboard';this.zones=[];this.eventId=null;this.zoneId=null;
  this.filters={type:'all',status:'all',robot:'all',query:''};this.urls=new Set();
  this.reviewDrafts=new Map();this.policyDrafts=new Map();this.activeHold=null;
  // 이미 목록에 있던 사건. **첫 그림에는 표시하지 않는다** — 열자마자 전부 깜빡이면 새것이 묻힌다.
  this.seenEvents=null;
  this.missionDraft=null;this.settingsSection='display';this.planning=new PlanningPanel(this);this.routePlanner=new RoutePlanner(this);
  this.manualPressedKeys=new Set();this.keyboardEnabled=true;
  this.manualKeyDown=event=>this.handleManualKeyDown(event);
  this.manualKeyUp=event=>this.handleManualKeyUp(event);
  this.manualFocus=()=>{if(this.activeHold?.kind==='shortcut')this.stopManualInput('키보드 초점 변경')};
  this.manualBlur=()=>{this.manualPressedKeys.clear();if(this.activeHold?.kind==='shortcut')this.stopManualInput('키보드 창 이탈')};
  this.document.addEventListener('keydown',this.manualKeyDown,true);
  this.document.addEventListener('keyup',this.manualKeyUp,true);
  this.document.addEventListener('focusin',this.manualFocus);
  this.document.addEventListener('click',event=>{for(const choice of this.container.querySelectorAll('.op-choice.open'))if(!choice.contains(event.target))this.closeFilterChoice(choice)});
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
 details(title,...content){return this.el('details',{class:'op-section op-details'},this.el('summary',{},title),content)}
 // Unconnected screen outlines: labels and layout only, with no submission or storage.
 previewSection(title,...content){
  const section=this.details(title+' · 준비 중',this.note('등록·저장·적용은 준비 중입니다.'),content);
  section.classList.add('op-preview');return section;
 }
 previewField(label,placeholder,type='text'){
  return this.field(label,this.el('input',{type,disabled:true,placeholder}));
 }
 previewSelect(label,options){
  const select=this.select(label,[['','선택 준비 중'],...options.map(value=>[value,value])],'');select.disabled=true;
  return this.field(label,select);
 }
 previewButton(label){return this.button(label+' · 준비 중',()=>{},{disabled:true})}
 openDisplaySettings(){this.settingsSection='display';this.onNavigate('settings')}
 emptyTable(headers,message){
  return this.el('div',{class:'op-table-wrap'},this.el('table',{class:'op-table'},this.el('caption',{},message),this.el('thead',{},this.el('tr',{},headers.map(label=>this.el('th',{scope:'col'},label)))),this.el('tbody',{},this.el('tr',{},this.el('td',{colspan:headers.length,class:'op-table-empty'},'연결된 기록이 없습니다.')))));
 }
 setZones(zones){this.zones=zones;this.zoneId??=zones[0]?.id;if(['zones','settings','missions'].includes(this.view))this.render(this.view)}
 render(view){
  if(this.activeHold)this.store.stop('패널 갱신');
  // Return the shared camera before replacing its temporary workspace parent.
  this.onManualObservation?.(null);
  this.clearVoicePoll();
  this.activeHold=null;this.view=view;this.title.textContent=TITLES[view]||'';
  this.container.dataset.page=view;this.container.replaceChildren();if(!TITLES[view])return;
  const intro=this.el('div',{class:'op-intro'},this.note({missions:'로봇 관측·제어와 현재 장치 상태를 한곳에서 확인합니다.',events:'사건을 찾고, 증거와 판단 근거를 함께 검토합니다.',history:'서버에 저장된 지난 사건과 순찰 판을 찾고, 검토 결과를 서버에 남깁니다.',records:'순찰 결과와 운영 기록을 확인합니다.',zones:'구역을 살펴보고 점검 기준을 구성합니다.',devices:'로봇의 모습과 연결·센서 상태를 함께 확인합니다.',voice:'로봇 음성 상태와 발화 기록을 보고, 시나리오와 멘트를 관리합니다.',settings:'표시 방식과 운영 정책, 관리 항목을 구성합니다.'}[view]),this.badge(this.store.live?this.store.runtimeLabel:this.store.demo?'예시 모드':'실제 데이터 대기',this.store.demo?'amber':''));
  this.container.append(intro,this.el('div',{class:'op-feedback',role:'status','aria-live':'polite'}));
  this[view==='zones'?'zonePage':view]();
 }
 refresh(reason){
  if(this.view==='dashboard')return;
  if(this.view==='zones'){this.planning.refresh();this.routePlanner.refresh();return}
  // 초당 10번 오는 로봇 상태는 게이지만 고친다 — 화면을 통째로 다시 그리면 입력·초점·3D 미리보기가 날아간다.
  if(reason==='telemetry'){this.refreshTelemetry();return}
  if(reason==='command'){this.refreshControl();return}
  // 이력은 서버에서 읽은 것이다 — 실시간 사건·상태 변화로 다시 묻지 않는다(입력·선택이 날아간다).
  if(this.view==='history')return;
  // 재렌더가 패널을 통째로 갈아끼우므로 스크롤을 보존한다 — 안 하면
  // 텔레메트리 갱신(2초)·명령 응답마다 화면이 맨 위로 튄다.
  const top=this.container.scrollTop;
  this.render(this.view);
  this.container.scrollTop=top;
  if(reason==='review'&&this.view==='events')this.eventList.querySelector('.op-event-row.selected')?.focus();
 }
 closeFilterChoice(choice){
  choice.classList.remove('open','open-up');choice.querySelector('.op-choice-trigger').setAttribute('aria-expanded','false');choice.querySelector('.op-choice-list').hidden=true;
 }
 filterChoice(label,name,options,value,onchange){
  const selected=options.find(([key])=>key===value)||options[0];
  const trigger=this.el('button',{type:'button',class:'op-choice-trigger','data-filter':name,'aria-label':name+' · '+selected[1],'aria-haspopup':'listbox','aria-expanded':'false'},this.el('span',{class:'op-choice-value'},selected[1]),this.el('span',{class:'op-choice-chevron','aria-hidden':'true'}));
  const list=this.el('div',{class:'op-choice-list',role:'listbox','aria-label':name,hidden:true});
  const choice=this.el('div',{class:'op-choice'},trigger,list);
  const items=options.map(([key,text])=>{
   const item=this.el('button',{type:'button',class:'op-choice-option',role:'option',tabindex:'-1','aria-selected':key===value},this.el('span',{},text),this.el('span',{class:'op-choice-check','aria-hidden':'true'}));
   item.addEventListener('click',()=>{
    trigger.querySelector('.op-choice-value').textContent=text;
    trigger.setAttribute('aria-label',name+' · '+text);
    for(const option of items)option.setAttribute('aria-selected',String(option===item));
    this.closeFilterChoice(choice);trigger.focus();onchange(key);
   });
   item.addEventListener('keydown',event=>{
    if(event.key==='Escape'){event.preventDefault();this.closeFilterChoice(choice);trigger.focus()}
    else if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)){
     event.preventDefault();const index=items.indexOf(item);
     items[event.key==='Home'?0:event.key==='End'?items.length-1:(index+(event.key==='ArrowDown'?1:-1)+items.length)%items.length].focus();
    }
   });
   return item;
  });
  list.append(...items);
  choice.addEventListener('focusout',event=>{if(!choice.contains(event.relatedTarget))this.closeFilterChoice(choice)});
  trigger.addEventListener('click',()=>{
   const opening=!choice.classList.contains('open');
   for(const other of this.container.querySelectorAll('.op-choice.open'))this.closeFilterChoice(other);
   choice.classList.toggle('open',opening);trigger.setAttribute('aria-expanded',String(opening));list.hidden=!opening;
   if(opening){
    const bounds=trigger.getBoundingClientRect(),panel=this.container.getBoundingClientRect();
    const below=Math.min(this.document.defaultView.innerHeight,panel.bottom)-bounds.bottom;
    const above=bounds.top-Math.max(0,panel.top);
    choice.classList.toggle('open-up',below<Math.min(list.scrollHeight,340)&&above>below);
    items.find(item=>item.getAttribute('aria-selected')==='true')?.focus();
   }
  });
  trigger.addEventListener('keydown',event=>{
   if(['ArrowDown','ArrowUp'].includes(event.key)){event.preventDefault();if(!choice.classList.contains('open'))trigger.click();else items[event.key==='ArrowDown'?0:items.length-1].focus()}
   if(event.key==='Escape'&&choice.classList.contains('open')){event.preventDefault();this.closeFilterChoice(choice)}
  });
  return this.el('div',{class:'op-field op-choice-field'},this.el('span',{},label),choice);
 }
 // 중요한 모드 변경(서비스·안전 해제·순찰 시작)은 확인 대화상자를 거친다.
 // 대화상자를 쓸 수 없으면 브라우저 기본 확인 창으로 묻는다 — 확인 없이 보내는 길을 두지 않는다(#172).
 confirmDevice({icon:name='lock',title,body,confirm='예, 실행합니다',danger=false,action}){
  const dialog=this.document.getElementById('confirm-dialog');
  if(!dialog||typeof dialog.showModal!=='function'){if(this.document.defaultView?.confirm?.(title+'\n\n'+body)===true)this.run(action);return}
  this.document.getElementById('confirm-icon').innerHTML=icon(name);
  this.document.getElementById('confirm-title').textContent=title;
  this.document.getElementById('confirm-body').textContent=body;
  const yes=this.document.getElementById('confirm-yes');
  yes.textContent=confirm;yes.className=danger?'estop':'';
  dialog.returnValue='';
  dialog.onclose=()=>{if(dialog.returnValue==='yes')this.run(action)};
  dialog.showModal();
  // 초점은 "아니요"에 둔다 — 창이 뜨자마자 누른 Enter 로 래치가 풀리거나 로봇이 걸으면 안 된다.
  this.document.getElementById('confirm-no').focus();
 }
 events(){
  const feed=this.store.liveFeed||{state:'off'};
  const feedText={off:'실시간 수신 미연결 — 예시 사건과 가져온 저장 기록을 구분해 검토합니다.',connecting:'사건 채널 연결 중…',live:'실시간 수신 연결됨 · 사건 '+feed.received+'건 수신'+(feed.dropped?' · 놓친 사건 '+feed.dropped+'건(서버 통보)':''),closed:'사건 채널 끊김 · 재연결 대기 — 그 사이 사건은 재연결 뒤 따라옵니다'}[feed.state]||feed.state;
  this.container.append(this.note(feedText,feed.state==='off'||feed.state==='closed'?'warning':''));
  const toolbar=this.el('div',{class:'op-toolbar'});
  const file=this.el('input',{type:'file',accept:'.json,.jpg,.jpeg',multiple:true,'aria-label':'블랙박스 파일 선택',class:'op-file'});
  file.addEventListener('change',()=>this.run(async()=>{await this.importFiles([...file.files]);file.value=''}));
  toolbar.append(this.el('label',{class:'op-button file-button'},'블랙박스 가져오기',file),this.button('검토 JSON 내보내기',()=>this.download(this.store.exportReview(),'mechadog-review.json','application/json')));
  this.container.append(toolbar,this.note('같은 사건 폴더의 meta.json + snapshot.jpg를 함께 선택하세요. 서버 업로드 없이 열며, 가져온 파일·메모는 새로고침하면 사라집니다.'));
  const search=this.el('input',{type:'search',name:'사건 검색',placeholder:'사건명 · 장치 · 구역 · 메모',value:this.filters.query,oninput:event=>{this.filters.query=event.target.value;this.renderEventList()}});
  const robotOptions=[...new Set(this.store.events.map(e=>e.robot))].map(id=>[id,id]);
  const filters=this.el('div',{class:'op-filters'},this.field('검색',search),this.filterChoice('유형','사건 유형',[['all','전체 유형'],...Object.entries(EVENT_CATEGORIES)],this.filters.type,value=>{this.filters.type=value;this.renderEventList()}),this.filterChoice('검토 상태','검토 상태 필터',[['all','전체 상태'],...Object.entries(REVIEW_STATES)],this.filters.status,value=>{this.filters.status=value;this.renderEventList()}),this.filterChoice('장치','사건 장치',[['all','전체 장치'],...robotOptions],this.filters.robot,value=>{this.filters.robot=value;this.renderEventList()}),this.button('초기화',()=>{this.filters={type:'all',status:'all',robot:'all',query:''};this.render('events')}));
  this.eventCount=this.el('p',{class:'op-result-count',role:'status'});
  this.eventList=this.el('div',{class:'op-event-list','aria-label':'사건 목록'});
  this.eventDetail=this.el('section',{class:'op-event-detail','aria-label':'선택한 사건'});
  this.container.append(filters,this.eventCount,this.el('div',{class:'op-evidence-workspace'},this.eventList,this.eventDetail));this.renderEventList();
 }
 renderEventList(){
  const records=this.store.queryEvents(this.filters);
  if(!records.some(e=>e.id===this.eventId))this.eventId=records[0]?.id||null;
  this.eventCount.textContent=records.length+'건 · 현재 조건';
  // 새로 들어온 사건만 한 번 짚어 준다 (WBS 4.6.4 실시간 피드) — 실시간 사건은 조용히 끼어든다.
  const firstDraw=this.seenEvents===null;
  if(firstDraw)this.seenEvents=new Set();
  const fresh=new Set();
  for(const event of records){if(!firstDraw&&!this.seenEvents.has(event.id))fresh.add(event.id);this.seenEvents.add(event.id);}
  this.eventList.replaceChildren(...records.map(event=>this.button([
   this.el('span',{class:'op-row-meta'},event.robot,this.badge(event.source==='DEMO'||event.simulated?'예시':event.source==='LIVE_FEED'?'실시간':'저장 파일',event.source==='LIVE_FEED'?'':'')),
   this.el('strong',{},event.title),
   // ⚠️ **시각이 없으면 같은 이름의 사건을 고를 수 없다.** 09-27 실측에서 `person_found` 11건이
   // 글자까지 똑같이 나열돼 화면으로는 구분이 되지 않았다. 시각이 없는 사건(예시)은 비워 둔다.
   this.el('span',{class:'op-row-meta'},event.zone,
    event.ts_ms?this.el('time',{class:'op-row-time',datetime:new Date(event.ts_ms).toISOString()},clock(event.ts_ms)):null),
   this.el('span',{class:'op-row-foot'},this.badge(REVIEW_STATES[event.review],event.review==='pending'?'amber':''),this.el('span',{},event.escalation))
  ],()=>{this.eventId=event.id;this.renderEventList();this.eventList.querySelector('.op-event-row.selected')?.focus()},{class:'op-event-row'+(event.id===this.eventId?' selected':'')+(fresh.has(event.id)?' just-arrived':''),'aria-pressed':event.id===this.eventId})));
  this.eventList.parentElement.classList.toggle('empty',!records.length);
  if(!records.length)this.eventList.append(this.note(this.store.queryEvents().length?'검색 결과가 없습니다. 검색어나 필터를 바꾸세요.':'검토할 사건이 없습니다. 블랙박스 파일을 가져오세요.'));
  this.renderEventDetail(this.store.events.find(e=>e.id===this.eventId));
 }
 renderEventDetail(event){
  this.eventDetail.replaceChildren();this.eventDetail.hidden=!event;if(!event)return;
  this.eventDetail.append(this.el('div',{class:'op-row-meta'},event.id,this.badge(event.simulated||event.source==='DEMO'?'예시 사건':event.source==='LIVE_FEED'?'실시간 수신 사건':event.source==='HISTORY'?'서버 이력 기록':'과거 저장 기록')),this.el('h3',{class:'op-detail-title'},event.title),this.note(event.detail),this.facts([['상태',STATE_NAMES[event.state]??event.state],['대응 단계',event.escalation],['운용 모드',MODE_NAMES[event.mode]??event.mode??'기록 없음'],['출입 인증',event.auth],['보호구',event.ppe]]));
  if(event.evidence?.length)this.eventDetail.append(this.section('판정 근거',this.facts(event.evidence)));
  if(event.snapshot)this.eventDetail.append(this.evidenceImage(event));
  else this.eventDetail.append(this.el('div',{class:'op-evidence-empty'},this.el('strong',{},'첨부된 스냅샷 없음'),this.el('span',{},'3D 예시 화면은 이 사건의 증거가 아닙니다.')));
  if(event.meta){
   this.eventDetail.append(this.note('처리 완료 시각 '+time(event.ts_ms)+' · 촬영 시각과 다를 수 있음'));
   const tracks=event.meta.tracks.map(track=>['추적 #'+track.track_id,Math.round(track.score*100)+'% · ['+track.box.join(', ')+']']);
   const detections=event.meta.detections.map(detection=>[detection.label,Math.round(detection.score*100)+'% · ['+detection.box.join(', ')+']']);
   this.eventDetail.append(this.section('당시 검출·추적 근거',this.note('추적 ID는 영구 신원이 아닙니다. 박스 좌표는 원본 JPEG의 픽셀 기준입니다.'),tracks.length||detections.length?this.facts([...tracks,...detections]):this.note('저장된 검출·추적 항목이 없습니다.')),this.el('details',{class:'op-raw'},this.el('summary',{},'당시 텔레메트리 원문'),this.el('pre',{},JSON.stringify(event.meta.telemetry,null,2))));
  }
  // 실시간 사건은 이력 저장소에도 남는다(4.6.6) — 검토는 그 기록에 서버로 남긴다.
  if(event.source==='LIVE_FEED'){
   this.eventDetail.append(this.section('검토 기록',this.note('실시간 사건의 검토는 «사건 이력» 화면에서 서버에 저장합니다.'),this.button('사건 이력에서 검토',()=>this.openHistoryFor(event),{disabled:!this.historyLink})));
   return;
  }
  if(event.source==='HISTORY'){this.eventDetail.append(this.historyReview(event));return}
  const draft=this.reviewDrafts.get(event.id)||{status:event.review,note:event.note};
  const remember=()=>this.reviewDrafts.set(event.id,{status:status.value,note:memo.value});
  const status=this.select('검토 결과',Object.entries(REVIEW_STATES),draft.status,remember);
  const memo=this.el('textarea',{name:'검토 메모',rows:4,maxlength:2000,placeholder:'판단 근거와 후속 조치를 남겨 주세요. 오탐은 근거 필수.',oninput:remember},draft.note);
  const form=this.el('form',{class:'op-review-form',onsubmit:submit=>{submit.preventDefault();this.run(()=>{this.store.reviewEvent(event.id,status.value,memo.value);this.reviewDrafts.delete(event.id);this.onToast(event.source==='DEMO'&&this.store.storageAvailable?'이 브라우저에 검토를 저장했어요.':'이번 세션에 검토를 저장했어요. 내보내기로 보관하세요.')})}},this.field('검토 결과',status),this.field('검토 메모',memo),this.el('button',{type:'submit',class:'op-button primary',disabled:this.store.role==='technician'},'임시 검토 저장'));
  this.eventDetail.append(this.section('검토 기록',this.note('확인 불가와 오탐은 다릅니다. 이 검토는 경보 확인이나 기체 안전 리셋을 실행하지 않습니다.'),form,this.note(event.source==='DEMO'?(this.store.storageAvailable?'예시 검토는 이 브라우저에 저장됩니다.':'브라우저 저장을 사용할 수 없습니다. 새로고침 전 내보내세요.'):'가져온 기록의 검토는 세션에만 보관됩니다.')));
 }
 evidenceImage(event){
  const image=this.el('img',{src:event.snapshot,alt:event.title+' 원본 저장 스냅샷'}),overlay=this.el('div',{class:'op-box-overlay','aria-hidden':'true'});
  const wrap=this.el('figure',{class:'op-evidence-image'},this.el('div',{class:'op-image-inner'},image,overlay),this.el('figcaption',{},event.simulated?'예시 합성 이미지 · 실제 사건 사진 아님':'원본 저장 JPEG · 현재 영상 아님'));
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
 // ── 사건 이력 (WBS 4.6.7 · ADR-46) ──────────────────────────────
 // 서버 저장소(`/api/history/*`)를 읽는다. 종류·단계·장치·구역·검토·날짜·순찰 판은 서버가 거르고,
 // 검색어는 받은 목록 안에서 거른다(서버에 글자 검색이 없다). 상세 보기는 사건 검토와 같은 `renderEventDetail` 이다.
 history(){
  if(!this.historyLink){this.container.append(this.section('사건 이력',this.note('관제 서버에 연결된 화면에서만 사건 이력을 조회합니다. 지금 화면에는 읽을 저장소가 없습니다.','warning')));return}
  const f=this.historyFilters;
  this.container.append(this.note('관제 서버의 사건 이력 저장소에서 읽습니다. 검토 결과는 서버에 저장되어 새로고침 뒤에도 남습니다.'));
  const search=this.el('input',{type:'search',name:'이력 검색',placeholder:'사건명 · 장치 · 구역 · 처리 메모',value:this.historyQuery,oninput:event=>{this.historyQuery=event.target.value;this.renderHistoryList()}});
  const date=(name,key)=>this.el('input',{type:'date',name,'aria-label':name,value:f[key],onchange:event=>{f[key]=event.target.value;this.loadHistory()}});
  this.historyFilterBar=this.el('div',{class:'op-filters'});
  this.container.append(this.el('div',{class:'op-filters'},this.field('검색',search),this.field('시작일',date('이력 시작일','since')),this.field('종료일',date('이력 종료일','until')),this.button('초기화',()=>{this.historyFilters=HISTORY_FILTERS();this.historyQuery='';this.render('history')})),this.historyFilterBar);
  this.historyStatus=this.el('p',{class:'op-result-count',role:'status'});
  this.eventList=this.el('div',{class:'op-event-list','aria-label':'이력 사건 목록'});
  this.eventDetail=this.el('section',{class:'op-event-detail','aria-label':'선택한 이력 사건',hidden:true});
  this.historyRunsEl=this.el('div',{class:'op-table-wrap','data-history-runs':''});
  this.container.append(this.historyStatus,this.el('div',{class:'op-evidence-workspace empty'},this.eventList,this.eventDetail),this.section('순찰 판',this.note('순찰 한 판은 대기·수동·안전 정지를 떠날 때 열리고 돌아올 때 닫힙니다. «이 판의 사건» 은 위 목록을 그 판으로 좁힙니다.'),this.historyRunsEl));
  if(this.historyMeta)this.renderHistoryFilters();
  this.loadHistory();
 }
 renderHistoryFilters(){
  const f=this.historyFilters,set=key=>value=>{f[key]=value;this.loadHistory()},{robots,zones}=this.historyMeta;
  this.historyFilterBar.replaceChildren(
   this.filterChoice('종류','이력 사건 종류',[['all','전체 종류'],...Object.entries(EVENT_TITLES)],f.event,set('event')),
   this.filterChoice('대응 단계','이력 대응 단계',[['all','전체 단계'],...['L0','L1','L2','L3','F'].map(level=>[level,level])],f.escalation,set('escalation')),
   this.filterChoice('장치','이력 장치',[['all','전체 장치'],...robots.map(robot=>[robot.robot_id,robot.display_name||robot.robot_id])],f.robot,set('robot')),
   this.filterChoice('구역','이력 구역',[['all','전체 구역'],...zones.map(zone=>[zone.zone_id,zone.zone_name&&zone.zone_name!==zone.zone_id?zone.zone_id+' · '+zone.zone_name:zone.zone_id])],f.zone,set('zone')),
   this.filterChoice('검토 상태','이력 검토 상태',[['all','전체 상태'],['no','검토 대기'],['yes','검토 완료']],f.reviewed,set('reviewed')));
 }
 loadHistory(){
  const token=++this.historyToken,f=this.historyFilters,focus=this.historyFocus,pick=key=>f[key]==='all'?null:f[key];
  const day=(value,end)=>value?new Date(value+(end?'T23:59:59.999':'T00:00:00')).getTime():null;
  this.historyFocus=null;this.historyStatus.textContent='이력을 불러오는 중…';this.historyStatus.dataset.tone='';
  const meta=this.historyMeta??Promise.all([this.historyLink.robots(),this.historyLink.zones()]).then(([robots,zones])=>({robots:robots.items,zones:zones.items}));
  this.historyLoad=Promise.all([meta,this.historyLink.incidents({event:pick('event'),escalation:pick('escalation'),robot:pick('robot'),zone:pick('zone'),reviewed:f.reviewed==='all'?null:f.reviewed==='yes',since:day(f.since),until:day(f.until,true),mission:f.mission,limit:100}),this.historyLink.runs({robot:pick('robot'),limit:20}),focus?this.historyLink.incident(focus).catch(error=>{this.onToast(error.message);return null}):null]).then(([meta,incidents,runs,focused])=>{
   if(token!==this.historyToken||this.view!=='history')return;
   if(!this.historyMeta){this.historyMeta=meta;this.renderHistoryFilters()}
   const event=row=>historyEvent(row,this.historyLink.snapshotBase(row.robot_id));
   this.historyItems=incidents.items.map(event);this.historyTotal=incidents.total;this.historyRuns=runs.items;
   if(focused){const found=event(focused);if(!this.historyItems.some(e=>e.id===found.id))this.historyItems.unshift(found);this.historyId=found.id;this.historyQuery=''}
   this.renderHistoryList();this.renderHistoryRuns();
  }).catch(error=>{
   if(token!==this.historyToken||this.view!=='history')return;
   this.historyItems=[];this.historyRuns=[];this.historyStatus.textContent=error.message;this.historyStatus.dataset.tone='error';
   this.eventList.replaceChildren(this.note(error.message,'warning'));this.eventDetail.hidden=true;this.historyRunsEl.replaceChildren();
  });
  return this.historyLoad;
 }
 renderHistoryList(){
  const q=this.historyQuery.trim().toLocaleLowerCase(),f=this.historyFilters;
  const records=this.historyItems.filter(e=>!q||[e.incidentId,e.title,e.robot,e.zone,e.event,e.resolution].join(' ').toLocaleLowerCase().includes(q));
  if(!records.some(e=>e.id===this.historyId))this.historyId=records[0]?.id||null;
  this.historyStatus.textContent=records.length+'건 표시 · 서버 '+this.historyTotal+'건'+(f.mission?' · 순찰 판 '+f.mission:'')+(this.historyTotal>this.historyItems.length?' · 최근 '+this.historyItems.length+'건만 받았습니다 — 조건을 좁히세요':'');
  this.eventList.replaceChildren(...records.map(event=>this.button([
   this.el('span',{class:'op-row-meta'},event.robot,this.badge('서버 이력')),
   this.el('strong',{},event.title),
   this.el('span',{class:'op-row-meta'},event.zone,event.ts_ms?this.el('time',{class:'op-row-time',datetime:new Date(event.ts_ms).toISOString()},time(event.ts_ms)):null),
   this.el('span',{class:'op-row-foot'},this.badge(event.reviewed?'검토 완료':'검토 대기',event.reviewed?'':'amber'),this.el('span',{},event.escalation))
  ],()=>{this.historyId=event.id;this.renderHistoryList();this.eventList.querySelector('.op-event-row.selected')?.focus()},{class:'op-event-row'+(event.id===this.historyId?' selected':''),'aria-pressed':event.id===this.historyId})));
  this.eventList.parentElement.classList.toggle('empty',!records.length);
  if(!records.length)this.eventList.append(this.note(this.historyItems.length?'검색 결과가 없습니다. 검색어를 바꾸세요.':'조건에 맞는 저장된 사건이 없습니다.'));
  this.renderEventDetail(records.find(e=>e.id===this.historyId));
 }
 renderHistoryRuns(){
  const f=this.historyFilters,rows=this.historyRuns.map(run=>this.el('tr',{},
   this.el('td',{},time(run.started_at)+' → '+(run.ended_at==null?'진행 중':time(run.ended_at))),this.el('td',{},run.robot_id),this.el('td',{},MODE_NAMES[run.mode]??run.mode),
   this.el('td',{},run.result==null?'진행 중':RUN_RESULTS[run.result]??run.result),this.el('td',{},(run.zones_visited||[]).join(' → ')||'—'),this.el('td',{},run.incident_count+'건'),
   this.el('td',{},this.button('이 판의 사건',()=>{f.mission=run.mission_id;this.loadHistory()},{'aria-pressed':f.mission===run.mission_id}))));
  this.historyRunsEl.replaceChildren(this.el('table',{class:'op-table'},this.el('caption',{},'최근 순찰 판 '+this.historyRuns.length+'개'+(f.mission?' · ':''),f.mission?this.button('순찰 판 조건 해제',()=>{f.mission=null;this.loadHistory()}):null),
   this.el('thead',{},this.el('tr',{},['시작 → 종료','로봇','모드','결과','방문 구역','사건','사건 보기'].map(label=>this.el('th',{scope:'col'},label)))),
   this.el('tbody',{},rows.length?rows:this.el('tr',{},this.el('td',{colspan:7,class:'op-table-empty'},'저장된 순찰 판이 없습니다.')))));
 }
 historyReview(event){
  const draft=this.reviewDrafts.get(event.id)||{status:event.reviewed?'reviewed':'pending',note:event.resolution};
  const remember=()=>this.reviewDrafts.set(event.id,{status:status.value,note:memo.value});
  const status=this.select('이력 검토 결과',[['pending','검토 대기'],['reviewed','검토 완료']],draft.status,remember);
  const memo=this.el('textarea',{name:'이력 처리 메모',rows:4,maxlength:2000,placeholder:'판단 근거와 후속 조치를 남겨 주세요.',oninput:remember},draft.note);
  const form=this.el('form',{class:'op-review-form',onsubmit:submit=>{submit.preventDefault();this.historySave=this.run(()=>this.saveHistoryReview(event,status.value==='reviewed',memo.value))}},this.field('검토 결과',status),this.field('처리 메모',memo),this.el('button',{type:'submit',class:'op-button primary',disabled:this.store.role==='technician'},'서버에 검토 저장'));
  const saved=event.reviewed?'서버 저장됨 · 검토 완료'+(event.reviewedAt?' '+time(event.reviewedAt):''):event.resolution?'서버 저장됨 · 검토 대기':'아직 검토하지 않은 사건입니다.';
  return this.section('검토 기록',this.note('검토는 관제 서버의 이력 저장소에 남습니다. 경보 확인이나 기체 안전 리셋을 실행하지 않습니다.'),this.badge(saved,event.reviewed?'':'amber'),form);
 }
 async saveHistoryReview(event,reviewed,resolution){
  if(this.store.role==='technician')throw new Error('운영자 또는 검토자 시연 역할에서 검토하세요.');
  const row=await this.historyLink.review(event.incidentId,{reviewed,resolution:resolution.trim().slice(0,2000)});
  const updated=historyEvent(row,this.historyLink.snapshotBase(row.robot_id));
  this.historyItems=this.historyItems.map(e=>e.id===updated.id?updated:e);this.reviewDrafts.delete(event.id);
  this.store.log('사건 검토 서버 저장',row.incident_id+' · '+(reviewed?'검토 완료':'검토 대기'),'HISTORY_REVIEW');
  if(this.view==='history')this.renderHistoryList();
  this.onToast('서버에 검토를 저장했어요.');
 }
 // 실시간 사건 → 이력의 같은 사건. 블랙박스 사건의 이력 ID 는 `<기체>_<기록 폴더>` 다(DATA_MODEL 3.4).
 // 사진 없는 사건은 ID 를 알 수 없어 같은 종류로 좁혀 연다.
 openHistoryFor(event){
  this.historyFilters=HISTORY_FILTERS();this.historyQuery='';
  if(event.entry)this.historyFocus=this.store.historyIncidentId(event);else this.historyFilters.event=event.event;
  this.onNavigate('history');
 }
 // ── 음성 중계 (WBS 4.7.14) ───────────────────────────────────────
 // 음성 링크는 메인 루프가 단독 소유하고 웹은 큐로 요청한다. 타자로 친 임의
 // 문장 방송은 폐기했다(ADR-38) — 로봇 스피커(MP3)는 미리 녹음한 문장만 낸다.
 // 폴링은 이 화면을 보고 있을 때만 돈다.
 clearVoicePoll(){
  clearTimeout(this.voiceTimer);this.voiceTimer=null;
  // Invalidate responses from a panel that was closed or replaced.
  this.voiceGeneration=(this.voiceGeneration||0)+1;this.voicePending=null;
 }
 voice(){
  const link=this.voiceLink;
  const generation=this.voiceGeneration;
  this.voiceStatusEl=this.el('div',{class:'op-facts'});
  this.voiceEventsEl=this.el('div',{class:'op-voice-log','aria-live':'polite'});
  // ── 멘트 관리: 문구 라이브러리 열람 (출력은 TF 카드에 미리 녹음한 문장뿐이라 여기서 추가하지 않는다) ──
  this.phraseCatEl=this.el('div',{class:'op-facts'});
  this.phraseListEl=this.el('div',{class:'op-voice-log'});
  const catSel=this.el('select',{name:'카테고리','aria-label':'멘트 카테고리'});
  const refreshPhrases=()=>this.run(async()=>{
   if(!link)return;
   const cats=await link.phrases();
   if(this.view!=='voice'||generation!==this.voiceGeneration)return;
   const keep=catSel.value;
   catSel.replaceChildren(...cats.map(c=>{
    const opt=this.el('option',{value:c.category},`${c.category} (${c.count})`);
    return opt;
   }));
   if(keep&&cats.some(c=>c.category===keep))catSel.value=keep;
   this.renderPhraseLines(cats);
  });
  catSel.addEventListener('change',()=>refreshPhrases());
  this.container.append(
   this.section('로봇 음성 상태',
    link?this.note('음성 중계 연결됨'+(this.store.simulated===true?' · 예시 발화':'')):this.note('음성 서버 미연결 · 운영 PC에서 음성 중계를 켜 주세요.','warning'),
    this.voiceStatusEl,
    this.el('div',{class:'op-toolbar'},
     this.button('대기/깨우기',()=>this.run(async()=>{await link.mode();this.pollVoice()}),{disabled:!link}))),
   this.section('멘트 관리',
    this.note('녹음된 방송 문구를 확인합니다.'),
    this.phraseCatEl,
    this.el('div',{class:'op-toolbar'},
     catSel,
     this.button('문구 보기',()=>refreshPhrases(),{disabled:!link})),
    this.phraseListEl),
   this.section('최근 발화',this.voiceEventsEl));
  // ── 시연 시나리오 · 당일 리포트 · 전체 기록 (B5) — 음성 서버에 있었는데 화면이 부르지 않던 API ──
  const scenarioSel=this.el('select',{name:'시나리오','aria-label':'시연 시나리오',disabled:!link},this.el('option',{value:''},link?'불러오는 중…':'음성 서버 미연결'));
  const reportEl=this.el('div',{class:'op-voice-report'}),transcriptEl=this.el('div',{class:'op-voice-log op-voice-transcript'});
  const runScenario=()=>{
   const name=scenarioSel.value;if(!name)throw new Error('실행할 시나리오를 고르세요.');
   const label=scenarioSel.selectedOptions[0]?.textContent||name;
   this.confirmDevice({icon:'play',title:'시나리오를 실행하겠습니까?',body:'「'+label+'」 — '+(this.store.simulated===true?'예시 기록을 추가합니다. 실제 방송은 없습니다.':'로봇 스피커로 방송합니다. 로봇은 움직이지 않습니다.'),confirm:'예, 실행합니다',action:async()=>{await link.runScenario(name);this.onToast('시나리오를 대기열에 넣었어요.');this.pollVoice()}});
  };
  const showReport=()=>this.run(async()=>{
   let r;try{r=await link.report()}catch(error){if(/404/.test(error.message)){reportEl.replaceChildren(this.note('음성 저널이 꺼져 있어 리포트가 없습니다. voice_pipeline 을 저널과 함께 실행하세요.','warning'));return}throw error}
   const kinds=Object.entries(r.robot_events||{}).map(([kind,n])=>kind+' '+n+'건').join(' · ')||'없음';
   const runs=Object.entries(r.scenario_runs||{}).map(([name,n])=>name+' '+n+'회').join(' · ')||'없음';
   reportEl.replaceChildren(this.facts([['날짜 · 구간',(r.date||'—')+' · '+(r.first_ts||'—')+' ~ '+(r.last_ts||'—')],['전체 기록',r.total+'건'],['현장 발화',(r.by_role?.user??0)+'건'],['로봇 응답 · 경고 방송',(r.by_role?.robot??0)+'건 · '+(r.by_role?.admin??0)+'건'],['로봇 사건',kinds],['시나리오 실행',runs],['경고 · 보안',(r.warnings||[]).length+'건'],['비상 접수',(r.emergencies||[]).length+'건'],['시나리오 실패',(r.scenario_failures||[]).length+'건']]));
  });
  const showTranscript=()=>this.run(async()=>{
   const rows=await link.transcript();
   transcriptEl.replaceChildren(...rows.slice().reverse().map(e=>this.el('div',{class:'op-voice-row '+e.role},this.el('span',{class:'op-row-meta'},e.ts,this.badge(VOICE_ROLES[e.role]||e.role)),this.el('span',{},e.text))));
   if(!rows.length)transcriptEl.append(this.note('이번 실행에서 기록된 것이 없습니다.'));
  });
  this.container.append(
   this.section('시연 시나리오',this.el('div',{class:'op-toolbar'},scenarioSel,this.button('시나리오 실행',()=>runScenario(),{disabled:!link})),this.note('음성 파이프라인의 등록 시나리오입니다. 실행은 확인 창을 거칩니다.')),
   this.section('오늘 음성·순찰 리포트',this.el('div',{class:'op-toolbar'},this.button('리포트 불러오기',()=>showReport(),{disabled:!link})),reportEl),
   this.section('이번 실행 전체 기록',this.el('div',{class:'op-toolbar'},this.button('전체 기록 불러오기',()=>showTranscript(),{disabled:!link})),this.note('인증 대기 중의 현장 발화 원문은 음성 쪽이 남기지 않습니다(암구호 보호).'),transcriptEl));
  if(link){
   this.pollVoice();refreshPhrases();
   this.run(async()=>{const list=await link.scenarios();scenarioSel.replaceChildren(this.el('option',{value:''},'시나리오 선택'),...list.map(s=>this.el('option',{value:s.name},s.desc+' ('+s.name+')')))});
  }
 }
 renderPhraseLines(cats){
  const sel=this.container.querySelector('select[name="카테고리"]');
  const cat=cats.find(c=>c.category===(sel?sel.value:cats[0]?.category))||cats[0];
  this.phraseCatEl.replaceChildren(...[
   ['카테고리',cat?cat.category:'—'],['문구 수',cat?cat.count+'개':'0개'],
   ['전체 카테고리',cats.length+'개'],
  ].map(([k,v])=>this.el('div',{},this.el('dt',{},k),this.el('dd',{},v))));
  if(!cat){this.phraseListEl.replaceChildren(this.note('문구가 없습니다.'));return}
  this.phraseListEl.replaceChildren(...cat.lines.map(line=>
   this.el('div',{class:'op-voice-row robot'},this.el('span',{},line.text))));
 }
 async pollVoice(){
  if(this.view!=='voice'||!this.voiceLink)return;
  const generation=this.voiceGeneration;
  if(this.voicePending===generation)return;
  clearTimeout(this.voiceTimer);this.voiceTimer=null;this.voicePending=generation;
  const current=()=>this.view==='voice'&&generation===this.voiceGeneration;
  try{
   const s=await this.voiceLink.status();
   if(!current())return;
   const mode={active:'대화 활성',standby:'대기 모드'}[s.mode]||s.mode;
   this.voiceStatusEl.replaceChildren(...[
    ['로봇',s.robot],['모드',mode],['동작',s.activity],['경고 대기',s.say_queue+'건'],
   ].map(([k,v])=>this.el('div',{},this.el('dt',{},k),this.el('dd',{},v))));
   this.voiceEventsEl.replaceChildren(...s.events.slice().reverse().map(e=>
    this.el('div',{class:'op-voice-row '+e.role},
     this.el('span',{class:'op-row-meta'},e.ts,this.badge(VOICE_ROLES[e.role]||e.role)),
     this.el('span',{},e.text))));
   if(!s.events.length)this.voiceEventsEl.append(this.note('아직 기록된 발화가 없습니다.'));
  }catch(error){
   if(current())this.voiceStatusEl.replaceChildren(this.note('음성 서버 응답 없음 — '+error.message+' · 자동 재연결 중','warning'));
  }finally{
   if(current()){
    this.voicePending=null;
    // One request at a time; a temporary failure must not stop status updates forever.
    this.voiceTimer=setTimeout(()=>this.pollVoice(),2000);
   }
  }
 }
 missions(){
  const store=this.store,active=['running','paused'].includes(store.mission.status),live=store.live;
  // ⚠️ **실제 연결에서는 웹 시연 임무를 띄우지 않는다.** "예시 임무 시작" 이 "실제 순찰 시작" 옆에 있으면
  // 어느 쪽이 로봇을 움직이는지 헷갈린다 — 실제 순찰은 아래 "실제 장비 명령" 에만 있다.
  if(!live){
  const draft=this.missionDraft||{robot:store.selected,zone:store.mission.zone,acknowledged:false};
  const robot=this.select('임무 로봇',ROBOTS.map(id=>[id,id]),draft.robot);
  const zone=this.select('점검 대상 구역',this.zones.map(z=>[z.label,z.label]),draft.zone);
  if(!zone.value&&this.zones.length)zone.value=this.zones[0].label;
  const check=this.el('input',{type:'checkbox',name:'예시 임무 확인',checked:draft.acknowledged});
  for(const element of [robot,zone,check])element.addEventListener('change',()=>{this.missionDraft={robot:robot.value,zone:zone.value,acknowledged:check.checked}});
  const form=this.el('form',{onsubmit:event=>{event.preventDefault();this.run(()=>{store.startMission({robot:robot.value,zone:zone.value,acknowledged:check.checked});this.missionDraft=null})}},this.el('div',{class:'op-form-grid'},this.field('로봇',robot),this.field('점검 대상 · 계획 메모',zone)),this.el('label',{class:'op-check'},check,'실제 임무가 아닌 웹 시연임을 확인했습니다.'),this.el('button',{type:'submit',class:'op-button primary',disabled:store.blocked||active||!this.zones.length},'예시 임무 시작'));
  this.container.append(this.section('순찰 세션',active?this.facts([['세션',store.mission.id],['점검 대상',store.mission.zone],['상태',STATUS[store.mission.status]],['실제 임무', '전송 안 함']]):form,this.el('div',{class:'op-toolbar'},this.button('일시정지',()=>store.pauseMission(),{disabled:store.mission.status!=='running'}),this.button('재개',()=>store.resumeMission(),{disabled:store.blocked||store.mission.status!=='paused'}),this.button('예시 임무 종료',()=>store.endMission(),{disabled:!active||store.role!=='operator'})),this.note('3D는 공통 예시 경로를 재생합니다. 구역별 실제 경로·충돌 회피는 연결되지 않았습니다.')));
  }
  const name=store.robotName(store.selected);
  const claim=this.button(live?'수동 제어권 요청':'예시 제어권 요청',()=>store.claim(),{disabled:store.blocked,'data-control':'claim'});
  const release=this.button('반납',()=>store.release(),{'data-control':'release'});
  const pad=this.el('div',{class:'op-drive-pad','aria-label':live?'누르는 동안 실제 이동':'누르는 동안 예시 이동'});
  for(const [command,label]of [['FORWARD','전진'],['LEFT','좌회전'],['STOP','정지'],['RIGHT','우회전'],['BACKWARD','후진']]){
   const shortcut=command==='STOP'?'Space':Object.keys(MANUAL_KEYS).find(key=>MANUAL_KEYS[key]===command).slice(-1);
   const button=this.el('button',{type:'button',class:'op-drive '+command.toLowerCase(),'data-drive':command,'aria-label':label+(live?' · '+store.transmissionLabel:' · 예시'),'aria-keyshortcuts':shortcut});
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
  const target=this.select('수동 제어 대상',store.robots.map(id=>[id,store.robotName(id)]),store.selected,id=>store.selectRobot(id));
  const cameraMount=this.el('div',{class:'op-manual-camera'+(this.observationError?' has-observation-error':''),'data-manual-camera':''},
   this.el('div',{class:'op-observation-error',role:'status',hidden:!this.observationError},this.el('h4',{},name+' · 관측 화면 표시 중단'),this.el('p',{'data-observation-error':''},this.observationError||''),this.button('관측 화면 다시 열기',()=>this.document.defaultView.location.reload())));
  const controls=this.el('section',{class:'op-manual-controls','aria-label':name+' 수동 조작'},
   this.el('h4',{},name+' · 수동 조작'),this.el('div',{class:'op-toolbar'},claim,release),pad,
   this.el('p',{class:'op-command-state','data-control':'state',role:'status'}),
   this.el('div',{class:'op-keyboard-help'},
    this.el('label',{class:'op-keyboard-toggle'},this.el('input',{type:'checkbox',checked:this.keyboardEnabled,onchange:event=>{this.keyboardEnabled=event.target.checked;this.stopManualInput('키보드 조작 설정 변경')}}),'키보드 조작'),
    this.el('span',{},this.el('kbd',{},'W A S D'),' 이동 · ',this.el('kbd',{},'Space'),' 정지')),
   // ⚠️ **실제 연결이면 로봇이 움직인다.** 예전에는 연결돼 있어도 "로봇은 움직이지 않습니다" 라고 적혀 있었다.
   // 서버가 받은 것과 로봇이 걸은 것은 다르므로 "반영" 은 상태 칸에서 확인하라고 적는다.
   this.note(live?'누르는 동안 이동 · 놓으면 정지. '+store.runtimeLabel+'에 명령을 보냅니다.':'누르는 동안만 유지 · 놓으면 STOP. 웹 시연이며 로봇과 3D 모델은 움직이지 않습니다.',live?'warning':'quiet'),
   this.el('p',{class:'op-transmission'},live?'관제 서버로 전송 · 로봇 반영은 아래 상태 칸에서 확인':'명령 미전송 · ACK 없음'),
   this.el('section',{class:'op-camera-posture','aria-label':'본체 자세로 시야 조절'},
    this.el('div',{class:'op-posture-heading'},this.el('h4',{},'본체 자세로 시야 조절'),this.badge(live?store.transmissionLabel+' · 수동 중에만':'연결 준비',live?'amber':'')),
    // 각도는 서버가 config(posture.pitch_up_deg)에서 정한다 — 화면은 세 가지 중 하나만 고른다.
    this.el('div',{class:'op-posture-buttons'},[['up','앞쪽 들기'],['level','기준 자세'],['down','앞쪽 낮추기']].map(([preset,label])=>live?this.button(label,()=>store.requestPose(preset),{'data-pose':preset}):this.previewButton(label))),
    this.note(live?'수동 제어권을 잡고 멈춘 상태에서만 보냅니다. 각도는 PPE 자세 상승과 같은 ±15°이며, 반납하면 기준 자세로 되돌립니다. 반영은 장치 화면의 IMU pitch로 확인하세요.':'기체의 앞뒤 기울임 기능을 이용하는 방식입니다. 실제 연결에서 수동 제어 중에만 보냅니다.'),
    this.el('p',{class:'op-camera-capability'},'카메라 독립 회전 · 지원 미확인')));
  const manual=this.el('section',{class:'op-manual-workspace','aria-label':'로봇 관측과 수동 제어'},
   this.el('div',{class:'op-manual-heading'},this.el('h3',{},'로봇을 보며 제어'),this.field('관측 · 제어 대상',target),live?this.badge(store.runtimeLabel+' · '+store.transmissionLabel,'amber'):this.badge('실제 장비 미연결')),
   this.el('div',{class:'op-observation-layout'},cameraMount,controls,this.manualLocation()));
  const firstSection=this.container.querySelector('.op-section');
  if(firstSection)firstSection.before(manual);else this.container.append(manual);
  this.onManualObservation?.(cameraMount);
  this.updateRobotObservation(this.robotObservation);
  this.statusLive=this.el('div',{class:'robot-status-live','aria-live':'off'});this.fillRobotStatus();
  this.container.append(this.el('section',{class:'robot-status-sheet','aria-label':name+' 상태'},this.statusLive));
  this.guardReadiness=this.el('div',{class:'op-device-facts','data-guard-readiness':'','aria-live':'off'});
  this.container.append(this.section('경비 실측 연결 점검',this.guardReadiness,this.note('서버 연결이나 영상 수신만으로 경비 기능 성공을 판정하지 않습니다. 사람 감지·인증·경보는 실물 시험에서 따로 확인하세요. 이 점검 칸은 로봇을 움직이지 않습니다.')));
  this.refreshGuardReadiness();
  // 실제 장비 명령 — 폐기된 /live 최소 화면에만 있던 기능을 관제로 옮긴 것.
  // 상태 칸은 /ws/telemetry 피드로 초당 10번 고친다(refreshTelemetry). ⚠️ **버튼은 교체하지 않고 글자만 바꾼다** —
  // 누르는 순간 버튼이 새로 만들어지면 클릭이 사라진다. 서비스 토글은 누를 때의 실측값으로 방향을 정한다.
  {
   this.deviceFacts=this.el('div',{class:'op-device-facts'});
   this.serviceButton=this.button('',()=>store.serviceMode===true
    ?this.confirmDevice({icon:'lock',title:'서비스 모드를 해제하겠습니까?',body:'루프 워치독(750ms 감시)이 해제되고 이동 명령 차단이 풀립니다. 안전 래치는 그대로 남아 로봇은 아직 움직이지 않습니다 — 보행 복귀에는 "안전 해제"가 따로 필요합니다.',confirm:'예, 해제합니다',action:()=>store.requestService(false)})
    :this.confirmDevice({icon:'lock',title:'서비스 모드에 진입하겠습니까?',body:'로봇이 그 자리에 주차하고 이동·자세 명령이 모두 거부됩니다. 루프 워치독이 750ms 데드라인으로 걸려 펌웨어 행거를 감지합니다. 패치·OTA 점검용 모드입니다.',confirm:'예, 진입합니다',action:()=>store.requestService(true)}),{disabled:!store.live||store.readOnly,'data-service':'toggle'});
   this.fillDeviceCommands();
   this.container.append(this.section('실제 장비 명령',
    this.deviceFacts,
    this.el('div',{class:'op-toolbar'},
     this.button('순찰 시작',()=>this.confirmDevice({icon:'play',title:store.runtimeLabel+' 순찰을 시작하겠습니까?',body:'로봇이 자율 순찰을 시작합니다. 안전 래치가 해제된 상태여야 하며, 주행 경로에 사람·장애물이 없는지 먼저 확인하세요.',confirm:'예, 순찰을 시작합니다',action:()=>store.requestPatrol(true)}),{disabled:!store.live||store.readOnly}),
     this.button('실제 순찰 정지',()=>store.requestPatrol(false),{disabled:!store.live||store.readOnly}),
     this.serviceButton,
     this.modeButton('guard','경비'),this.modeButton('factory','공장'),
     this.button('경보 확인 (L3 해제)',()=>this.confirmDevice({icon:'stop',title:'경보를 확인했습니까?',body:'현장 상황을 직접 확인한 뒤에만 누르세요. 경보 단계(L3)가 내려가고 순찰이 이어집니다. 안전 래치(F)는 이 버튼으로 풀리지 않습니다 — 그쪽은 "안전 해제"가 따로 필요합니다.',confirm:'예, 확인했습니다',danger:true,action:()=>store.requestAlarmConfirm()}),{disabled:!store.live||store.readOnly,'data-alarm-confirm':'confirm'}),
     this.button('안전 해제 (RESET_SAFE)',()=>this.confirmDevice({icon:'stop',title:'안전 래치를 해제하겠습니까?',body:'안전 정지 원인이 제거됐고 로봇 주변에 사람이 없는지 먼저 확인하세요. 래치가 풀리면 다음 이동 명령부터 로봇이 움직일 수 있습니다 — 잠금만 해제되며 자동 보행은 시작하지 않습니다.',confirm:'예, 해제합니다',danger:true,action:()=>store.requestResetSafe()}),{disabled:!store.live||store.readOnly,'data-reset-safe':'confirm'})),
    store.live?null:this.note('실제 장비 미연결 — 이 버튼들은 명령을 보내지 않습니다.','warning'),
    this.note('운용 모드는 로봇이 멈춰 있을 때(대기·수동)만 바꿀 수 있고, 바꿔도 경보(L3)와 안전 정지(F)는 풀리지 않습니다. 선행 기능이 없는 모드는 서버가 거절하며 사유를 알려 줍니다.'),
    this.note('모드 변경 버튼은 누르면 확인 창이 뜹니다. 순찰 정지·비상 정지처럼 안전으로 가는 명령은 확인 없이 즉시 보냅니다. 서비스 모드 해제 후에도 안전 래치는 남습니다.')));
   // 위치 알려주기 — 들어 옮긴 뒤 집 안 비슷한 자리를 구별 못 해 위치를 못 잡을 때, 사람이 구역을 알려준다.
   const zones=store.patrolZones;
   this.container.append(this.section('위치 알려주기',
    zones.length?this.el('div',{class:'op-toolbar','data-locate':'zones'},...zones.map(zone=>this.button('구역 '+zone,()=>this.confirmDevice({icon:'target',title:'로봇이 지금 구역 '+zone+' 안에 있습니까?',body:'로봇이 지금 믿고 있는 위치를 버리고 구역 '+zone+' 안에서만 다시 찾습니다. 찾을 때까지 로봇은 멈춰 섭니다. 잘못 알려주면 엉뚱한 자리로 잡힐 수 있으니 실제로 있는 구역만 누르세요.',confirm:'예, 구역 '+zone+' 입니다',action:async()=>{const result=await store.requestLocate(zone);this.onToast(result?.detail||'위치 다시 찾기를 요청했어요.')}}),{disabled:!store.live,'data-locate-zone':zone}))):this.note('구역 목록을 받지 못했습니다 — 서버 연결을 확인하세요.','warning'),
    store.live?null:this.note('실제 장비 미연결 — 이 버튼들은 명령을 보내지 않습니다.','warning'),
    this.note('로봇을 들어 옮긴 뒤 위치를 못 잡을 때 씁니다. 로봇은 방향과 상관없이 그 구역 안에서 위치를 찾고, 구역 안에서도 비슷한 자리가 여럿이면 계속 멈춰 있습니다(1분 뒤 포기).')));
  }
  // 관제 PC 스피커 방송 음량 · 무음 (`4.8.2`) — 로봇 스피커(SOUND)와 별개, 방송기는 플릿 전체가 하나를 나눠 쓴다.
  {
   const b=store.broadcast;
   const label=this.el('span',{},'방송 음량 ('+b.volume+')');
   const volume=this.el('input',{type:'range',name:'방송 음량',min:0,max:100,step:1,value:b.volume,disabled:!store.live||store.readOnly||!b.available,oninput:event=>label.textContent='방송 음량 ('+event.target.value+')'});
   volume.addEventListener('change',()=>this.run(()=>store.requestBroadcastVolume(Number(volume.value))));
   const muted=this.el('input',{type:'checkbox',name:'방송 무음',checked:b.muted,disabled:!store.live||store.readOnly||!b.available,onchange:event=>this.run(()=>store.requestBroadcastMuted(event.target.checked))});
   this.container.append(this.section('관제 PC 방송',
    b.available?null:this.note('방송 없음 — 방송기가 연결되지 않았습니다 (piper 미설치 등).','warning'),
    this.el('label',{class:'op-field'},label,volume),
    this.el('label',{class:'op-check'},muted,'무음'),
    this.note(store.live?'관제 PC 스피커로 나가는 경고 방송 음량입니다. 로봇 스피커(SOUND)와는 별개이며, 어느 로봇 화면에서 바꿔도 같은 방송기가 바뀝니다.':'실제 장비 미연결 — 이 조절은 서버로 전송되지 않습니다.')));
  }
  if(store.blocked)this.container.append(this.note(store.estop?'예시 정지 잠금 상태입니다. 설정에서 웹 예시 잠금만 초기화할 수 있습니다.':'예시 모드·수신 상태·운영자 시연 역할을 설정에서 확인하세요.','warning'),this.button('운영 설정',()=>this.openDisplaySettings()));
  this.refreshControl();
 }
 manualLocation(){
  if(this.store.live){
   if(!this.controlMinimap)this.controlMinimap=new ControlMinimap({document:this.document,getLink:()=>this.view==='missions'&&this.store.live?this.store.link:null,onOpen:()=>this.onNavigate('zones')});
   this.controlMinimap.tick();
   return this.controlMinimap.root;
  }
  const section=this.el('section',{class:'op-location','aria-label':'선택 로봇 위치'});
  section.append(this.el('div',{class:'op-location-heading'},this.el('h4',{},this.store.robotName(this.store.selected)+' · 위치와 방향'),this.badge('예시 배치 · 실측 아님')));
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
  if(state)state.textContent=(store.control?store.robotName(store.control)+(store.live?' · 수동 제어권 (서버 MANUAL 요청)':' · 예시 제어권'):'제어권 없음')+' / '+store.command;
  for(const button of this.container.querySelectorAll('[data-drive]')){const command=button.dataset.drive;button.disabled=command!=='STOP'&&(store.blocked||store.control!==store.selected);button.classList.toggle('pressed',command===store.command&&command!=='STOP')}
  for(const button of this.container.querySelectorAll('[data-pose]'))button.disabled=!store.live||store.blocked||store.control!==store.selected||store.command!=='STOP';
  const claim=this.container.querySelector('[data-control="claim"]'),release=this.container.querySelector('[data-control="release"]');
  if(claim)claim.disabled=store.blocked||store.control===store.selected;if(release)release.disabled=!store.control;
 }
 records(){
  this.container.append(this.note('이 페이지는 로컬 UI 작업 기록입니다. 실물 운행·서버 감사 로그가 아닙니다. 최대 500건을 세션에 보관하며 새로고침하면 초기화됩니다.'),this.el('div',{class:'op-toolbar'},this.button('조작 이력 CSV',()=>{this.download(this.store.exportRecords(),'mechadog-ui-records.csv','text/csv');this.render('records')}),this.button('사건 검토 JSON',()=>this.download(this.store.exportReview(),'mechadog-review.json','application/json'))));
  this.container.append(this.section('예시 순찰 세션',this.store.sessions.length?this.el('div',{class:'op-session-list'},this.store.sessions.map(session=>this.el('article',{},this.el('strong',{},session.robot+' · '+session.zone),this.badge(STATUS[session.id===this.store.mission.id?this.store.mission.status:session.status]),this.el('p',{},time(session.startedAt)+' → '+time(session.endedAt))))):this.note('이 세션에서 시작한 예시 임무가 없습니다.')));
  const list=this.el('div',{class:'op-record-list'}),count=this.el('p',{class:'op-result-count',role:'status'});
  const render=term=>{const rows=this.store.records.filter(row=>[row.action,row.detail,row.source].join(' ').toLowerCase().includes(term.toLowerCase()));count.textContent=rows.length+'건 · 로컬 기록';list.replaceChildren(...rows.map(row=>this.el('article',{class:'op-record'},this.el('time',{datetime:new Date(row.ts_ms).toISOString()},time(row.ts_ms)),this.el('div',{},this.el('strong',{},row.action),this.el('p',{},row.detail),this.el('small',{},({'LOCAL_UI_PREVIEW_ONLY':'화면 조작','LOCAL_IMPORTED_REVIEW':'가져온 사건 검토','HISTORY_REVIEW':'서버 이력 검토'}[row.source]??row.source))))));if(!rows.length)list.append(this.note('조건에 맞는 기록이 없습니다.'))};
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
 confirmGoto(x,y,where=''){
  const link=this.store.link,robot=this.store.selected;
  return this.confirmDevice({icon:'route',title:'로봇을 이 곳으로 보낼까요?',body:'찍은 곳 ('+(where?where+', ':'')+'x '+x.toFixed(2)+' m, y '+y.toFixed(2)+' m)까지 경로를 찾아 걸어갑니다. 순찰 중이 아니면 순찰이 함께 시작됩니다. 경로에 사람·물건이 없는지 먼저 확인하세요. 도착하면 그 자리에 서고, «실제 순찰 시작»을 누르면 구역 순찰로 돌아갑니다.',confirm:'예, 보냅니다',action:async()=>{if(this.store.link!==link||this.store.selected!==robot)throw new Error('대상 로봇이 바뀌었습니다. 새 지도에서 목적지를 다시 찍어 주세요.');const result=await this.store.requestGoto(x,y);this.onToast(result?.detail||'이동을 요청했어요.')}});
 }
 confirmLocatePoint(x,y,where=''){
  const link=this.store.link,robot=this.store.selected;
  return this.confirmDevice({icon:'target',title:'로봇이 여기 있다고 알려줄까요?',body:'찍은 곳 ('+(where?where+', ':'')+'x '+x.toFixed(2)+' m, y '+y.toFixed(2)+' m) 주변에서 로봇의 현재 위치를 다시 찾습니다. 위치가 확인될 때까지 로봇은 멈춰 있습니다. 실제로 로봇이 있는 곳인지 확인하세요.',confirm:'예, 위치를 알려줍니다',action:async()=>{
   if(this.store.link!==link||this.store.selected!==robot)throw new Error('대상 로봇이 바뀌었습니다. 새 지도에서 위치를 다시 찍어 주세요.');
   const result=await this.store.requestLocatePoint(x,y);this.onToast(result?.detail||'찍은 곳 주변에서 위치 다시 찾기를 요청했어요.');
  }});
 }
 locateZones(){
  const store=this.store,zones=store.patrolZones;
  return this.el('div',{'data-locate-support':''},this.el('h4',{},'구역으로 알려주기 (보조)'),
   zones.length?this.el('div',{class:'op-toolbar','data-locate':'zones'},...zones.map(zone=>this.button('구역 '+zone,()=>this.confirmDevice({icon:'target',title:'로봇이 지금 구역 '+zone+' 안에 있습니까?',body:'로봇이 지금 믿고 있는 위치를 버리고 구역 '+zone+' 안에서만 다시 찾습니다. 찾을 때까지 로봇은 멈춰 섭니다. 잘못 알려주면 엉뚱한 자리로 잡힐 수 있으니 실제로 있는 구역만 누르세요.',confirm:'예, 구역 '+zone+' 입니다',action:async()=>{const result=await store.requestLocate(zone);this.onToast(result?.detail||'위치 다시 찾기를 요청했어요.')}}),{disabled:!store.live||store.readOnly,'data-locate-zone':zone}))):this.note('구역 목록을 받지 못했습니다 — 서버 연결을 확인하세요.','warning'),
   this.note('지도에서 정확한 자리를 찍기 어려우면 구역을 알려 주세요. 구역 안에서도 비슷한 자리가 여럿이면 로봇은 계속 멈춰 있습니다.'));
 }
 zonePage(){
  // One route map owns move / locate / draw. Zone policy editing stays below it.
  this.routePlanner.open();
  this.container.append(this.section('로봇 위치 알려주기',this.locateZones()));
  this.planning.open()
 }
 devices(){
  const store=this.store,name=store.robotName(store.selected);
  this.robotCanvas??=this.el('canvas',{class:'robot-detail-canvas','aria-label':'로봇 3D 예시 모델'});
  this.robotCanvas.setAttribute('aria-label',name+' 로봇 3D 예시 모델. 드래그로 회전하며 아래 버튼으로도 조작할 수 있습니다.');
  this.robotPreviewNotice=this.el('p',{class:'robot-preview-error',role:'status',hidden:!this.robotPreviewError},this.robotPreviewError);
  const preview=this.el('section',{class:'robot-inspection','aria-label':name+' 3D 모델'},
   this.el('div',{class:'robot-inspection-heading'},this.el('h2',{},name),this.badge('표시용 3D 모델')),
   this.el('div',{class:'robot-model-stage'},this.robotCanvas,this.robotPreviewNotice),
   this.el('div',{class:'robot-view-controls','aria-label':'로봇 모델 시야 조작'},[['left','왼쪽 회전'],['right','오른쪽 회전'],['in','확대'],['out','축소'],['reset','시점 초기화']].map(([action,label])=>this.button(label,()=>this.onRobotViewAction?.(action),{'data-robot-action':action}))),
   this.note('드래그로 회전 · 휠로 확대 · 공통 예시 형상이며 실제 자세·관절 상태를 나타내지 않습니다.'));
  this.statusLive=this.el('div',{class:'robot-status-live','aria-live':'off'});this.fillRobotStatus();
  const status=this.el('section',{class:'robot-status-sheet','aria-label':name+' 상태'},
   this.statusLive,
   this.el('div',{class:'op-toolbar'},this.button('관제에서 가상 시점 보기',()=>this.onFocusRobot(store.selected),{class:'op-button primary'}),this.button('제어 · 장치 열기',()=>this.onNavigate('missions'))));
  // 연결 여부만 적는다 — 로봇 수신 상태는 10Hz 로 바뀌므로 아래 "로봇 상태" 배지가 말한다.
  this.container.append(this.el('div',{class:'op-device-switch','aria-label':'상세 로봇 선택'},store.robots.map(id=>this.button([this.el('strong',{},store.robotName(id)),this.el('span',{},store.live?(store.isRegistered(id)?'':'미등록 · ')+describeTelemetry(store.telemetryOf(id)).badge[0]:'미연결')],()=>store.selectRobot(id),{'aria-label':store.robotName(id)+' 상태 보기','aria-pressed':id===store.selected,class:'op-button'+(id===store.selected?' selected':'')}))),this.el('div',{class:'robot-detail-layout'},preview,status));
  this.onRobotPreview?.(this.robotCanvas);
  this.container.append(this.details('장치 지원 상세',this.facts([['Motion ESP32','MOVE·STOP·ESTOP·RESET_SAFE·POSE·ACTION·LED·SOUND·SERVICE 적용 경로 존재'],['Vision XIAO / 카메라','/ws/vision 검출 영상 수신 구현'],['Host / 블랙박스','사건별 JPEG + meta.json 저장 구현'],['실시간 웹 전송','/ws/vision + /ws/events 구현'],['LiDAR / 기준기','2대 운용 계획 · 개체별 배정 미확정'],['전도 자동 감지','Phase 2 이연 · 정상 작동 추정 금지']]),this.note('Git 코드 구현 상태이며, 이 기체에서 동작한다는 검증 결과가 아닙니다. GAIT는 펌웨어가 해석만 하고 적용하지 않습니다. STATE는 저장·반향만 하며 안전 래치를 풀지 않습니다. 경고 음성은 PC 음성 파이프라인이 문장을 TF 카드 트랙 번호로 바꿔 SOUND 로 재생합니다(--robot-speaker).')));
  this.container.append(this.details('명령 응답 상세',this.facts([['ok','파서 수락 여부'],['applied','명령 처리 적용 여부'],['actuators','실제 액추에이터 활성 정보'],['ACK safe_latched','명령 응답의 안전 필드'],['telemetry safety_latched','텔레메트리의 안전 필드']]),this.note('ACK 한 항목만 보고 정지 완료 또는 안전 복구 완료로 판단하지 않습니다.')));
  this.container.append(this.previewSection('개체 프로파일 · 노드 진단',
   this.facts([['선택 장치',store.selected+' · 웹 표시 이름'],['실제 개체 프로파일',store.live?(store.slot()?.device||store.telemetry?.snapshot?.deviceId||'미수신'):'미연결'],['Phase 1 기준기','미지정'],['Phase 2 기준기','미지정']]),
   this.previewSelect('진단 노드',['MechDog 모션','XIAO 비전','LiDAR 중계']),
   this.facts([['마지막 접속','미수신'],['펌웨어 버전','미수신'],['IP · 포트','미수신'],['최근 오류','미수신 · 오류 없음으로 판단하지 않음']]),
   this.previewButton('진단 기록 조회'),this.note('개체별 역할·기준기 배정은 아직 확정하지 않았습니다. 네트워크 비밀번호와 비밀키는 표시하지 않습니다.')));
 }
 // 로봇 상태 게이지 (WBS 4.6.2). 10Hz 로 다시 채우므로 **버튼은 이 안에 두지 않는다** — 누르는 중인
 // 버튼이 교체되면 클릭이 사라진다. 값은 describeTelemetry 한 곳에서 문구로 만든다.
 fillRobotStatus(){
  const store=this.store,text=describeTelemetry(store.telemetry),live=store.live;
  const rows=live&&text.rows?text.rows:[['연결 · 마지막 수신','미연결 / —'],['실제 device_id','미수신'],['FSM · 대응 단계','미수신 / 미수신'],['배터리 전압','— V · 미수신'],['전방 거리','— cm · 미수신'],['IMU pitch / roll / yaw','— / — / —'],['링크 · 안전 래치','미수신 / 미수신'],['마지막 명령 수락 나이','— ms · 미수신']];
  const [badgeText,badgeTone]=live?text.badge:['연결 대기',''];
  const note=!live?this.note('실제 장비에서 수신된 값이 없습니다. 마지막 수신 시각과 상태는 연결 후 표시됩니다.')
   :text.tone==='stale'?this.note('마지막으로 받은 값입니다 — 지금 상태로 판단하지 마세요.','warning')
   :text.tone==='closed'?this.note('상태 채널이 끊겨 다시 연결하는 중입니다. 아래는 끊기기 전 값입니다.','warning')
   :!text.rows?this.note('관제 서버에는 붙었지만 로봇에서 받은 상태가 아직 없습니다.')
   :this.note('새로 받은 상태를 표시합니다. 일부 항목이 없으면 미수신으로 표시합니다.');
  const history=store.telemetry?.history||[];
  const charts=live&&text.rows?this.el('div',{class:'robot-status-charts'},this.sparkline(history.filter(point=>Number.isFinite(point.battV)),'battV','배터리','V',2),this.sparkline(history.filter(point=>Number.isFinite(point.distCm)),'distCm','전방 거리','cm',0)):null;
  this.statusLive.replaceChildren(...[this.el('div',{class:'op-status-heading'},this.el('h2',{},'로봇 상태'),this.badge(badgeText,badgeTone)),note,this.facts(rows),charts].filter(Boolean));
 }
 refreshTelemetry(){
  if(['devices','missions'].includes(this.view)&&this.statusLive?.isConnected)this.fillRobotStatus();
  if(this.view==='missions'&&this.deviceFacts?.isConnected)this.fillDeviceCommands();
  this.refreshGuardReadiness();
 }
 refreshGuardReadiness(){
  if(this.view!=='missions'||!this.guardReadiness?.isConnected)return;
  const store=this.store,tone=store.live?describeTelemetry(store.telemetry).tone:'waiting';
  const fresh=tone==='live'&&!!store.deviceTelemetry;
  const vision=store.live?this.getVisionStatus()?.state:'off';
  const body=!store.live?'실물 상태 수신 안 함':fresh?'새 상태 수신 중':tone==='stale'?'수신 중단 · 마지막 값만 있음':tone==='closed'?'상태 채널 끊김':'상태 수신 대기';
  const camera=!store.live?'실제 영상 미연결':({live:'새 검출 프레임 수신 중',waiting:'영상 채널 연결 · 프레임 대기',stale:'새 프레임 없음 · 마지막 영상',closed:'영상 채널 끊김',off:'비전 채널 없음',connecting:'영상 연결 중'})[vision]??'영상 연결 중';
  const mode=fresh?(MODE_NAMES[store.missionMode]??store.missionMode??'미수신'):'현재 모드 확인 불가';
  const safety=fresh&&store.deviceTelemetry?.safetyLatched!=null?(store.safetyLatched?'안전 잠금 · 이동 금지':'잠금 해제 상태 수신 · 이동 허가 아님'):'현재 안전 상태 확인 불가';
  this.guardReadiness.replaceChildren(this.facts([['관제 서버',store.live?'연결됨 · 로봇 연결과 별개':'미연결 · 웹 미리보기'],['본체 상태',body],['카메라 검출 영상',camera],['현재 운용 모드',mode],['안전 래치',safety]]));
 }
 // 운용 모드 전환 버튼 (FR-4.7 · FR-11.3). ⚠️ **지금 모드는 누를 수 없게 둔다** —
 // 같은 모드로 바꾸는 것은 거절이 아니지만, 누를 수 있으면 «바뀌었나» 를 되묻게 된다.
 modeButton(name,label){
  const store=this.store;
  return this.button(label+' 모드',()=>this.confirmDevice({icon:'lock',title:label+' 모드로 바꾸겠습니까?',body:'순찰 하나는 모드 하나로 돕니다. 경비는 인증, 공장은 보호구·쓰러짐·구역 위험만 봅니다. 로봇이 멈춰 있을 때만 바뀌며 경보와 안전 정지는 풀리지 않습니다.',confirm:'예, 바꿉니다',action:()=>store.requestMode(name)}),{disabled:!store.live||store.readOnly||store.missionMode===name,'data-mode':name});
 }
 fillDeviceCommands(){
  const store=this.store,t=store.live?store.deviceTelemetry:null,text=describeTelemetry(store.telemetry),svc=store.serviceMode;
  const received=t?(text.tone==='live'?'실시간':text.tone==='stale'?'끊김 · '+text.age+' · 마지막 값':'채널 끊김 · 마지막 값'):'미수신';
  this.deviceFacts.replaceChildren(this.facts([
   ['상태 수신',received],
   ['운용 모드',store.missionMode?(MODE_NAMES[store.missionMode]??store.missionMode):'미수신 — 판단 규칙을 알 수 없음'],
   ['FSM · 온보드',t?(store.fsmState||'기동 전')+' · 온보드 '+(t.state||'—'):'미수신'],
   ['안전 래치',t?(t.safetyLatched==null?'미수신':t.safetyLatched?'걸림':'해제됨'):'미수신'],
   ['서비스 모드',!t?'미수신':svc===null?'모름 · 펌웨어가 알리지 않음':svc?'켜짐 · 루프 워치독 동작 중':'꺼짐']]));
  this.serviceButton.textContent=svc===true?'서비스 모드 해제 — 워치독 끄기':'서비스 모드 진입 — 패치용 워치독';
 }
 /** 최근 60초 추이. 새 seq 로 받은 값만 점이 된다. 그림은 SVG 선 하나와 최소·최대·마지막 값. */
 sparkline(history,key,label,unit,digits){
  const points=history.filter(point=>Number.isFinite(point[key]));
  const figure=this.el('figure',{class:'op-spark'},this.el('figcaption',{},label+' · 최근 60초'));
  if(points.length<2){figure.append(this.el('p',{class:'op-spark-empty'},'추이 수집 중'));return figure}
  const values=points.map(point=>point[key]),min=Math.min(...values),max=Math.max(...values);
  // ⚠️ **변화가 없을 때 없던 진동을 그리던 자리다.** `(max-min)||1` 은 값이 모두 같으면
  // 폭을 1 로 바꿔 버려서, 43·44 처럼 **한 눈금 차이가 그래프 높이 전체**로 튀었다.
  // 캡션이 「최소 43 · 최대 43」이라고 적는 동안 선은 위아래를 오갔다 — 화면이 거짓을 말한 것이다.
  // 표시 눈금(`digits`) 네 칸을 최소 폭으로 두고 가운데를 기준으로 그린다. 실제 변화가
  // 그보다 크면 예전과 똑같이 그려지고, 작으면 가운데에 가깝게 눕는다.
  const tick=Math.pow(10,-digits),floor=tick*4;
  const span=Math.max(max-min,floor),base=(max+min)/2-span/2;
  const t0=points[0].t,dt=(points.at(-1).t-t0)||1,W=240,H=48;
  const svg=this.document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('viewBox',`0 0 ${W} ${H}`);svg.setAttribute('preserveAspectRatio','none');svg.setAttribute('aria-hidden','true');
  const line=this.document.createElementNS('http://www.w3.org/2000/svg','polyline');
  line.setAttribute('points',points.map(point=>`${((point.t-t0)/dt*W).toFixed(1)},${(H-3-(point[key]-base)/span*(H-6)).toFixed(1)}`).join(' '));
  svg.append(line);
  const fmt=value=>value.toFixed(digits)+' '+unit;
  figure.append(svg,this.el('p',{class:'op-spark-range'},'최소 '+fmt(min)+' · 최대 '+fmt(max)+' · 지금 '+fmt(values.at(-1))));
  return figure;
 }
 setRobotPreviewError(message){
  this.robotPreviewError=message||'';
  if(this.robotPreviewNotice){this.robotPreviewNotice.textContent=this.robotPreviewError;this.robotPreviewNotice.hidden=!message}
 }
 settings(){
  const store=this.store;
  this.container.append(this.el('nav',{class:'op-subnav','aria-label':'설정 항목'},[['display','화면 · 정책'],['badges','사원증 · 준비 중'],['accounts','사용자 · 권한 · 준비 중'],['audit','감사 기록 · 준비 중']].map(([id,label])=>this.button(label,()=>{this.settingsSection=id;this.render('settings');this.container.querySelector('[data-settings="'+id+'"]').focus()},{'data-settings':id,'aria-pressed':id===this.settingsSection,class:'op-button'+(id===this.settingsSection?' selected':'')}))));
  const nav=this.container.querySelector('.op-subnav');
  const management=this.el('details',{class:'op-management-menu',...(this.settingsSection!=='display'?{open:''}:{})},this.el('summary',{},'관리 기능 · 준비 중'));
  for(const button of nav.querySelectorAll('[data-settings]'))if(button.dataset.settings!=='display')management.append(button);
  nav.append(management);
  if(this.settingsSection!=='display'){this.managementPreview();return}
  this.container.append(this.section('화면 데이터 · 시연 권한',this.note('아래 역할은 UI 흐름을 시험하는 선택입니다. 계정 로그인이나 실제 접근 제어가 아닙니다.'),this.el('div',{class:'op-form-grid'},this.field('표시 데이터',this.select('표시 데이터',[['demo','예시 데이터 표시'],['real','실제 데이터 대기']],store.demo?'demo':'real',value=>store.setDemo(value==='demo'))),this.field('시연 역할',this.select('시연 역할',[['operator','운영자 · 시연'],['reviewer','검토자 · 시연'],['technician','기술자 · 시연']],store.role,value=>store.setRole(value)))),this.el('div',{class:'op-toolbar'},this.button(store.stale?'예시 수신 상태 복구':'예시 수신 만료 시험',()=>store.setStale(!store.stale),{disabled:!store.demo}),this.button('웹 예시 정지 잠금 초기화',()=>store.clearPreviewStop(),{disabled:!store.estop})),this.note('복구·역할 변경·정지 잠금 초기화 후에는 자동으로 움직이지 않습니다. 실물 안전 잠금은 이 화면에서 해제할 수 없습니다.','warning')));
  this.container.append(this.section('구역 · 점검 정책',this.note('순찰 순서, 구역 위치와 공장 모드의 안전모·조끼·위험물 점검 기준을 함께 설정합니다.'),this.button('구역 · 동선에서 설정',()=>this.onNavigate('zones'),{class:'op-button primary'})));
  // 숫자는 서버의 /api/policy (config.yaml 을 판정 코드와 같은 키로 읽은 값)에서 온다 (B7).
  // 받지 못했으면 설계 문서의 기준값을 쓰되 그렇다고 적는다 — 서버 값인 척하지 않는다.
  const p=store.policy,v=(key,fallback)=>p?.[key]??fallback;
  const led=level=>p?.led?.[{L0:'l0_patrol',L1:'l1_observe',L2:'l2_auth_request',L3:'l3_alarm',F:'failsafe'}[level]];
  const auth='인증 '+v('auth_timeout_s',30)+'초 초과 / '+v('auth_max_attempts',2)+'회 실패'+(p?.auth_require_both?' · 암구호 뒤 사원증까지 확인':'')+(p?.auth_verdict_grace_s?' · 발화 판정 대기 1회 '+p.auth_verdict_grace_s+'초 유예':'');
  this.container.append(this.details('대응 단계 · 안전 해제 상세',this.el('ol',{class:'op-escalation'},[
   ['L0','평상 단계','L1에서 사람 미검출 '+v('target_lost_timeout_s',5)+'초면 L0 복귀'],['L1','대상 관찰',v('detect_window_ms',300)+'ms 내 '+v('detect_hits_required',3)+'회 검출 (경비: 정렬 후 고개를 든 순간부터) · 미인증 '+v('l1_to_l2_hold_s',10)+'초면 L2'],['L2','인증 대응','미검출 '+v('target_lost_timeout_s',5)+'초 / '+auth+' 시 L3 · 인증 유효 '+v('auth_session_valid_s',60)+'초'],['L3','관리자 판단 필요','관리자 확인과 조치로 해제 · PPE 판정과 별개'+(p?.l3_warning?' · 음성 경고 「'+p.l3_warning+'」':'')],['F','안전 잠금','원인 확인 → RESET_SAFE → 래치 해제 보고 확인']
  ].map(([level,title,detail])=>this.el('li',{},this.el('span',{class:'op-level'},level),this.el('div',{},this.el('strong',{},title+(led(level)?' · 눈 '+led(level):'')),this.el('p',{},detail))))),this.note((p?'관제 서버가 기동 때 읽은 config.yaml 값을 읽기 전용으로 표시합니다.':'서버 설정값 미수신 — 설계 기준값을 표시합니다. 실제 값과 다를 수 있습니다.')+' F 이전에 L3였다면 F 해제 뒤 L3가 유지됩니다. 사건 검토 저장은 두 잠금을 모두 해제하지 않습니다.',p?'':'warning')));
  this.container.append(this.details('연결과 저장 상세',this.facts([['실제 로봇',store.live?'관제 서버 연결됨 · '+(store.robots.length>1?store.robots.length+'대 ('+store.robots.map(id=>store.robotName(id)).join(', ')+') · 비상정지는 고른 로봇에만':'로봇 상태는 제어 · 장치 화면에서'):'연결 안 됨'],['구역 · 동선','설정 서버 저장 · 다음 로봇 서버 시작에 적용'],['사원증 관리','등록 화면 미구현 · 기존 인증 기능과 별개'],['저장 범위','구역 계획: 서버 / 작성 중 초안·예시 검토: 브라우저']])));
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
 dispose(){this.controlMinimap?.dispose();this.routePlanner.dispose();this.clearVoicePoll();this.store.release('패널 종료');this.document.removeEventListener('keydown',this.manualKeyDown,true);this.document.removeEventListener('keyup',this.manualKeyUp,true);this.document.removeEventListener('focusin',this.manualFocus);this.document.defaultView.removeEventListener('blur',this.manualBlur);this.manualPressedKeys.clear();for(const url of this.urls)this.document.defaultView.URL.revokeObjectURL(url);this.urls.clear()}
}
