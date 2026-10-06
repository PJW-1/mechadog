import test from 'node:test';
import assert from 'node:assert/strict';
import {JSDOM} from 'jsdom';
import {Operations} from '../static/operations.js';
import {OperationalPanels} from '../static/panels.js';

const zones=[{id:'central-corridor',label:'중앙 순찰 통로',center:[0,0],size:[6,29]}];
function setup(){
 const dom=new JSDOM('<h2 id="title"></h2><div id="content"></div>',{url:'http://127.0.0.1:4175'}),document=dom.window.document;
 const store=new Operations({storage:dom.window.localStorage}),messages=[],navigation=[];
 const panels=new OperationalPanels({store,container:document.querySelector('#content'),title:document.querySelector('#title'),onNavigate:view=>navigation.push(view),onFocusZone:zone=>navigation.push(zone.id),onFocusRobot:robot=>navigation.push(robot),onToast:message=>messages.push(message),document});
 panels.setZones(zones);store.subscribe(reason=>panels.refresh(reason));
 return {dom,document,store,panels,messages,navigation};
}
const button=(document,label)=>[...document.querySelectorAll('button')].find(element=>element.textContent===label);
const change=(dom,element,value,type='change')=>{element.value=value;element.dispatchEvent(new dom.window.Event(type,{bubbles:true}))};
const chooseFilter=(document,name,value)=>{const trigger=document.querySelector(`[data-filter="${name}"]`);trigger.click();trigger.parentElement.querySelectorAll('.op-choice-option')[value].click()};

test('goto motion-lock rejection preserves the server reason in the visible toast',async()=>{
 const {dom,store,panels,messages}=setup();
 const detail='보행 잠금 중 — 이동할 수 없다',calls=[];
 store.link={goto:async(x,y)=>{calls.push([x,y]);return{accepted:false,detail}}};
 store.setDemo(false);dom.window.confirm=()=>true;
 panels.confirmGoto(2,3);
 await new Promise(resolve=>setTimeout(resolve,0));
 assert.deepEqual(calls,[[2,3]]);
 assert.equal(messages.at(-1),'거절됨 — '+detail);
});

for(const page of ['missions','events','records','zones','devices','settings'])test(page+' panel renders labeled actionable content without a browser',()=>{
 const {document,panels}=setup();panels.render(page);assert.ok(document.querySelector('#title').textContent);assert.ok(document.querySelector('.op-intro'));assert.ok(document.querySelector('.op-section'));assert.ok(document.querySelector('button'));assert.equal(document.querySelectorAll('script').length,0);
});
test('event filtering updates list and keeps the search input focused',()=>{
 const {dom,document,panels}=setup();panels.render('events');const search=document.querySelector('[name="사건 검색"]');search.focus();change(dom,search,'안전모','input');assert.equal(document.activeElement,search);assert.equal(document.querySelectorAll('.op-event-row').length,1);assert.match(document.querySelector('.op-detail-title').textContent,/안전모/);
});
test('guard check separates server, fresh robot state, and fresh vision frames without sending commands',()=>{
 const {document,panels,store}=setup();
 const check=()=>document.querySelector('[data-guard-readiness]').textContent;
 panels.render('missions');assert.match(check(),/웹 미리보기/);assert.match(check(),/실물 상태 수신 안 함/);
 store.link={};store.setDemo(false);panels.render('missions');
 assert.match(check(),/관제 서버.*연결됨/);assert.match(check(),/상태 수신 대기/);assert.match(check(),/현재 안전 상태 확인 불가/);
 const snapshot={deviceId:'mechdog-02',mode:'guard',stale:false,telemetry:{bootId:'boot',seq:1,battV:7.4,distCm:90,imu:{pitch:0,roll:0,yaw:0},flags:{},safetyLatched:true}};
 store.setTelemetry({state:'live',snapshot});
 assert.match(check(),/새 상태 수신 중/);assert.match(check(),/경비 모드/);assert.match(check(),/안전 잠금 · 이동 금지/);
 panels.getVisionStatus=()=>({state:'live'});panels.refreshGuardReadiness();assert.match(check(),/새 검출 프레임 수신 중/);
 store.setTelemetry({state:'live',snapshot:{...snapshot,stale:true}});
 assert.match(check(),/수신 중단/);assert.match(check(),/현재 모드 확인 불가/);assert.match(check(),/현재 안전 상태 확인 불가/);
});
test('event filter menu exposes selection and supports arrow, Escape and outside click',()=>{
 const {dom,document,panels}=setup();panels.render('events');
 const trigger=document.querySelector('[data-filter="사건 유형"]');trigger.focus();
 trigger.dispatchEvent(new dom.window.KeyboardEvent('keydown',{key:'ArrowDown',bubbles:true}));
 assert.equal(trigger.getAttribute('aria-expanded'),'true');
 const choices=[...trigger.parentElement.querySelectorAll('.op-choice-option')];
 assert.equal(document.activeElement,choices[0]);
 choices[0].dispatchEvent(new dom.window.KeyboardEvent('keydown',{key:'ArrowDown',bubbles:true}));
 assert.equal(document.activeElement,choices[1]);
 choices[1].dispatchEvent(new dom.window.KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
 assert.equal(trigger.getAttribute('aria-expanded'),'false');assert.equal(document.activeElement,trigger);
 trigger.click();document.querySelector('[data-filter="사건 장치"]').click();
 assert.equal(trigger.getAttribute('aria-expanded'),'false','opening another filter closes the first');
 document.body.click();assert.equal(document.querySelector('[data-filter="사건 장치"]').getAttribute('aria-expanded'),'false');
 chooseFilter(document,'사건 유형',1);
 assert.match(trigger.getAttribute('aria-label'),/PPE/);assert.equal(document.querySelectorAll('.op-event-row').length,1);
});
test('review form validates false positive, stores evidence note and keeps drafts between selections',()=>{
 const {dom,document,panels,store,messages}=setup();panels.render('events');
 change(dom,document.querySelector('[name="검토 결과"]'),'false_positive');document.querySelector('.op-review-form').dispatchEvent(new dom.window.Event('submit',{bubbles:true,cancelable:true}));assert.match(messages.at(-1),/근거/);assert.equal(store.events[0].review,'pending');
 change(dom,document.querySelector('[name="검토 메모"]'),'설비에 가려 확인 불가','input');document.querySelectorAll('.op-event-row')[1].click();document.querySelectorAll('.op-event-row')[0].click();assert.equal(document.querySelector('[name="검토 메모"]').value,'설비에 가려 확인 불가');
 change(dom,document.querySelector('[name="검토 결과"]'),'unverifiable');document.querySelector('.op-review-form').dispatchEvent(new dom.window.Event('submit',{bubbles:true,cancelable:true}));assert.equal(store.events[0].review,'unverifiable');
});
test('review text is never treated as executable HTML',()=>{
 const {document,panels,store}=setup();store.events[0].title='<img src=x onerror=alert(1)>';store.events[0].note='<script>bad</script>';panels.render('events');assert.equal(document.querySelectorAll('script,img').length,0);assert.ok(document.querySelector('.op-detail-title').textContent.includes('<img'));
});
test('event detail displays the mission mode recorded with the event',()=>{
 const {document,panels,store}=setup();store.events[0].mode='factory';panels.render('events');assert.match(document.querySelector('.op-event-detail').textContent,/공장/);
});
test('manual keyboard hold stops on keyup and blur without replacing held DOM',()=>{
 const {dom,document,panels,store}=setup();panels.render('missions');button(document,'예시 제어권 요청').click();const forward=document.querySelector('[data-drive="FORWARD"]');
 forward.dispatchEvent(new dom.window.KeyboardEvent('keydown',{key:'Enter',bubbles:true}));assert.equal(store.command,'FORWARD');assert.equal(document.querySelector('[data-drive="FORWARD"]'),forward);
 forward.dispatchEvent(new dom.window.KeyboardEvent('keyup',{key:'Enter',bubbles:true}));assert.equal(store.command,'STOP');
 forward.dispatchEvent(new dom.window.KeyboardEvent('keydown',{key:'Enter',bubbles:true}));assert.equal(store.command,'FORWARD');forward.dispatchEvent(new dom.window.Event('blur'));assert.equal(store.command,'STOP');
});
test('pointer hold ends on cancel; second pointer cannot hijack control',()=>{
 const {dom,document,panels,store}=setup();panels.render('missions');button(document,'예시 제어권 요청').click();const forward=document.querySelector('[data-drive="FORWARD"]'),right=document.querySelector('[data-drive="RIGHT"]');forward.setPointerCapture=()=>{};right.setPointerCapture=()=>{};
 const pointer=(element,type,id)=>{const event=new dom.window.Event(type,{bubbles:true,cancelable:true});Object.assign(event,{pointerId:id,button:0});element.dispatchEvent(event)};
 pointer(forward,'pointerdown',1);assert.equal(store.command,'FORWARD');pointer(right,'pointerdown',2);assert.equal(store.command,'FORWARD');pointer(right,'pointerup',2);assert.equal(store.command,'FORWARD');pointer(forward,'pointercancel',1);assert.equal(store.command,'STOP');
});
test('rerender while held stops command; technician cannot edit a review',()=>{
 const {dom,document,panels,store}=setup();panels.render('missions');store.claim();const forward=document.querySelector('[data-drive="FORWARD"]');forward.dispatchEvent(new dom.window.KeyboardEvent('keydown',{key:'Enter'}));panels.render('missions');assert.equal(store.command,'STOP');store.setRole('technician');panels.render('events');assert.equal(document.querySelector('.op-review-form button').disabled,true);
});
test('settings sends PPE editing to the real zone planner, preserving management previews',()=>{
 const {document,panels,navigation}=setup();panels.render('settings');
 button(document,'구역 · 동선에서 설정').click();assert.deepEqual(navigation,['zones']);
 button(document,'사원증 · 준비 중').click();assert.ok(document.querySelector('.op-preview'));assert.equal(document.querySelector('form'),null);
 panels.render('zones');assert.ok(document.querySelector('[name="helmet"]'));assert.ok(document.querySelector('[data-plan-add]'));
 assert.match(document.querySelector('#content').textContent,/초안 작성/);
});

test('blackbox file validation rejects oversized JSON and spoofed JPEG',async()=>{
 const {panels}=setup();await assert.rejects(()=>panels.importFiles([{name:'meta.json',size:3*1024*1024}]),/2MB/);
 const raw={ts_ms:100,event:'person_found',state:'OBSERVE',escalation:'L1',tracks:[],detections:[],telemetry:{}};
 await assert.rejects(()=>panels.importFiles([{name:'meta.json',size:100,text:async()=>JSON.stringify(raw)},{name:'snapshot.jpg',size:3,slice:()=>({arrayBuffer:async()=>new Uint8Array([1,2,3]).buffer})}]),/실제 JPEG/);
});
test('import selects stored record, does not enable live data, and does not duplicate files',async()=>{
 const {panels,store,document}=setup();panels.render('events');const raw={ts_ms:100,event:'person_found',state:'OBSERVE',escalation:'L1',tracks:[],detections:[],telemetry:{device_id:'mechdog-01'}};
 const file={name:'meta.json',size:100,text:async()=>JSON.stringify(raw)};store.setDemo(false);await panels.importFiles([file]);assert.equal(store.demo,false);assert.equal(document.querySelector('.op-detail-title').textContent,'사람 확인');await panels.importFiles([file]);assert.equal(store.queryEvents().length,1);
});
test('STOP preempts a held direction on pointerdown, not only click',()=>{
 const {dom,document,panels,store}=setup();panels.render('missions');store.claim();const forward=document.querySelector('[data-drive="FORWARD"]');forward.setPointerCapture=()=>{};
 const down=(element,id)=>{const event=new dom.window.Event('pointerdown',{bubbles:true,cancelable:true});Object.assign(event,{pointerId:id,button:0});element.dispatchEvent(event)};
 down(forward,1);assert.equal(store.command,'FORWARD');down(document.querySelector('[data-drive="STOP"]'),2);assert.equal(store.command,'STOP');
});
test('broadcast controls are disabled and say «방송 없음» without a live broadcaster',()=>{
 const {document,panels}=setup();panels.render('missions');
 assert.equal(document.querySelector('[name="방송 음량"]').disabled,true);
 assert.equal(document.querySelector('[name="방송 무음"]').disabled,true);
 assert.match(document.querySelector('.op-section:last-of-type').textContent,/방송 없음/);
});
test('broadcast controls enable once live and available, and send volume/mute requests',async()=>{
 const {dom,document,panels,store}=setup();
 const calls=[];
 store.slots['MD-01'].link={setBroadcast:patch=>{calls.push(patch);return Promise.resolve({available:true,volume:patch.volume??70,muted:patch.muted??false})}};
 store.setDemo(false);store.setBroadcast({available:true,volume:70,muted:false});
 panels.render('missions');
 const volume=document.querySelector('[name="방송 음량"]'),muted=document.querySelector('[name="방송 무음"]');
 assert.equal(volume.disabled,false);assert.equal(muted.disabled,false);
 assert.equal(volume.value,'70');
 change(dom,volume,'30');await new Promise(resolve=>setImmediate(resolve));
 assert.deepEqual(calls[0],{volume:30});assert.equal(store.broadcast.volume,30);
 muted.checked=true;muted.dispatchEvent(new dom.window.Event('change',{bubbles:true}));await new Promise(resolve=>setImmediate(resolve));
 assert.deepEqual(calls[1],{muted:true});assert.equal(store.broadcast.muted,true);
});
test('mission draft survives delayed layout arrival',()=>{
 const {dom,document,panels}=setup();panels.render('missions');change(dom,document.querySelector('[name="임무 로봇"]'),'MD-02');const check=document.querySelector('[name="예시 임무 확인"]');check.checked=true;check.dispatchEvent(new dom.window.Event('change'));panels.setZones(zones);assert.equal(document.querySelector('[name="임무 로봇"]').value,'MD-02');assert.equal(document.querySelector('[name="예시 임무 확인"]').checked,true);
});
test('voice panel renders a read-only phrase library',async()=>{
 const {dom,document,panels,store}=setup();
 const link={
  baseUrl:'http://127.0.0.1:8090',
  status:()=>Promise.resolve({robot:'mechadog-01',mode:'active',activity:'idle',say_queue:0,events:[]}),
  mode:()=>Promise.resolve({}),transcript:()=>Promise.resolve([]),
  phrases:()=>Promise.resolve([{category:'greeting',count:2,lines:[{text:'안녕하세요'},{text:'반갑습니다'}]}]),
 };
 const p=new OperationalPanels({store,container:document.querySelector('#content'),title:document.querySelector('#title'),onNavigate:()=>{},onFocusZone:()=>{},onFocusRobot:()=>{},onToast:()=>{},voiceLink:link,document});
 p.render('voice');
 await new Promise(resolve=>setTimeout(resolve,10));
 // 멘트 관리 섹션 + 카테고리 표시
 assert.ok([...document.querySelectorAll('h3')].some(h=>h.textContent==='멘트 관리'));
 // 임의 문장 방송은 폐기했다(ADR-38) — 입력칸도 방송 버튼도 없다. 대기/깨우기는 남는다.
 assert.equal(document.querySelector('[name="방송 문장"]'),null);
 const labels=[...document.querySelectorAll('button')].map(b=>b.textContent);
 assert.ok(!labels.includes('말하기')&&!labels.includes('긴급 방송'));
 assert.ok(labels.includes('대기/깨우기'));
 const sel=document.querySelector('select[name="카테고리"]');
 assert.ok(sel);assert.match(sel.textContent,/greeting/);
 // 출력은 TF 카드 녹음뿐이라 문구는 보기만 한다 — 추가 입력칸도 삭제 버튼도 없다.
 const rows=[...document.querySelectorAll('.op-voice-row')];
 assert.equal(rows.length,2);
 assert.ok(rows.every(row=>row.querySelector('button')===null));
 assert.equal(document.querySelector('[name="문구"]'),null);
 assert.ok(!labels.includes('문구 추가'));
 p.clearVoicePoll();
});
test('voice panel reaches scenarios, the daily report and the full transcript (B5)',async t=>{
 const {dom,document,store}=setup();const ran=[];
 const link={status:()=>Promise.resolve({robot:'r',mode:'active',activity:'idle',say_queue:0,events:[]}),phrases:()=>Promise.resolve([]),
  scenarios:()=>Promise.resolve([{name:'greet',desc:'인사'}]),runScenario:name=>{ran.push(name);return Promise.resolve({queued:name})},
  report:()=>Promise.resolve({date:'2026-09-23',total:7,by_role:{user:2,robot:3,admin:1},robot_events:{escalation_changed:1},scenario_runs:{greet:1},warnings:[],emergencies:[],scenario_failures:[],first_ts:'10:00',last_ts:'10:05'}),
  transcript:()=>Promise.resolve([{ts:'10:00:00',role:'robot_evt',text:'escalation_changed L3'},{ts:'10:00:01',role:'admin',text:'경보가 발령되었습니다.'}])};
 const p=new OperationalPanels({store,container:document.querySelector('#content'),title:document.querySelector('#title'),onNavigate:()=>{},onFocusZone:()=>{},onFocusRobot:()=>{},onToast:()=>{},voiceLink:link,document});
 t.after(()=>p.clearVoicePoll());
 p.render('voice');await new Promise(resolve=>setTimeout(resolve,10));
 const scenario=document.querySelector('select[name="시나리오"]');assert.match(scenario.textContent,/인사 \(greet\)/);
 change(dom,scenario,'greet');dom.window.confirm=()=>true;button(document,'시나리오 실행').click();await new Promise(resolve=>setTimeout(resolve,10));
 assert.deepEqual(ran,['greet'],'확인 뒤에만 실행한다');
 button(document,'리포트 불러오기').click();await new Promise(resolve=>setTimeout(resolve,10));
 const report=document.querySelector('.op-voice-report').textContent;assert.match(report,/7건/);assert.match(report,/escalation_changed 1건/);assert.match(report,/greet 1회/);
 button(document,'전체 기록 불러오기').click();await new Promise(resolve=>setTimeout(resolve,10));
 const log=document.querySelector('.op-voice-transcript').textContent;assert.match(log,/로봇 사건/);assert.match(log,/경보가 발령되었습니다/);
});
test('a report from a pipeline without a journal says so instead of failing silently',async()=>{
 const {document,store}=setup();
 const link={status:()=>new Promise(()=>{}),phrases:()=>Promise.resolve([]),scenarios:()=>Promise.resolve([]),report:()=>Promise.reject(new Error('음성 서버 응답 오류 (HTTP 404)'))};
 const p=new OperationalPanels({store,container:document.querySelector('#content'),title:document.querySelector('#title'),onNavigate:()=>{},onFocusZone:()=>{},onFocusRobot:()=>{},onToast:()=>{},voiceLink:link,document});
 p.render('voice');button(document,'리포트 불러오기').click();await new Promise(resolve=>setTimeout(resolve,10));
 assert.match(document.querySelector('.op-voice-report').textContent,/음성 저널이 꺼져/);p.clearVoicePoll();
});
test('live PPE, fall and escalation events are classified and show their evidence (B2·B3)',()=>{
 const {dom,document,panels,store}=setup();store.setDemo(false);
 const base={state:'ALERT',escalation:'L1',tracks:[],detections:[],telemetry:{device_id:'mechdog-01'},entry:'e',snapshot:null};
 store.ingestLiveEvent({...base,seq:1,ts_ms:1,event:'PPE_VIOLATION',judgement:{track_id:4,state:'위반',reason:'안전모 미착용 1500ms'}});
 store.ingestLiveEvent({...base,seq:2,ts_ms:2,event:'person_fallen',judgement:{fallen:true,aspect:0.62,still_ms:3200,confirm_ms:3000}});
 store.ingestLiveEvent({...base,seq:3,ts_ms:3,event:'escalation_changed',escalation:'L3',entry:null,reason:'PPE_VIOLATION',warning:'경보가 발령되었습니다.'});
 store.ingestLiveEvent({...base,seq:4,ts_ms:4,event:'failsafe_entered',escalation:'F',entry:null,trigger:'ESTOP',previous:'PATROL'});
 panels.render('events');
 chooseFilter(document,'사건 유형',1);
 assert.equal(document.querySelectorAll('.op-event-row').length,1,'PPE 필터가 PPE 사건만 걸러낸다');
 const detail=document.querySelector('.op-event-detail').textContent;assert.match(detail,/보호구 미착용 확정/);assert.match(detail,/위반 · 안전모 미착용 1500ms/);assert.match(detail,/판정 근거/);assert.match(detail,/#4/);
 assert.match(detail,/조회만 가능/);assert.equal(document.querySelector('.op-review-form'),null);
 assert.throws(()=>store.reviewEvent('LIVE-1','confirmed','검토'),/서버 저장 기능/);
 chooseFilter(document,'사건 유형',3);assert.equal(document.querySelectorAll('.op-event-row').length,3);
 const titles=[...document.querySelectorAll('.op-event-row')].map(row=>row.textContent).join('|');assert.match(titles,/대응 단계 → L3/);assert.match(titles,/안전 잠금/);assert.match(titles,/쓰러짐 감지/);
});
test('light warnings get their own titles and categories (hazard item · path blocked)',()=>{
 const {store}=setup();store.setDemo(false);
 const base={state:'PATROL',escalation:'L0',tracks:[],detections:[],telemetry:{device_id:'mechdog-01'},entry:'e',snapshot:null};
 const hazard=store.ingestLiveEvent({...base,seq:1,ts_ms:1,event:'hazard_notice',judgement:{zone:'C',items:['hazard_item'],source:'vlm'}});
 assert.equal(hazard.category,'OBJECT');assert.equal(hazard.title,'화기 위험물 경고');assert.equal(hazard.zone,'C');
 const blocked=store.ingestLiveEvent({...base,seq:2,ts_ms:2,event:'path_blocked',judgement:{x:1.2,y:0.4,target:'D',source:'lidar'}});
 assert.equal(blocked.category,'SAFETY');assert.equal(blocked.title,'통로 막힘 · 우회');
 assert.deepEqual(hazard.evidence,[['구역','C']],'구역 행이 근거 표에 든다');assert.deepEqual(blocked.evidence,[],'LiDAR 막힘은 구역이 없다');
 const inZone=store.ingestLiveEvent({...base,seq:4,ts_ms:4,event:'path_blocked',judgement:{zone:'C',source:'vlm'}});
 assert.equal(inZone.category,'SAFETY');assert.equal(inZone.title,'통로 막힘 경고');assert.equal(inZone.zone,'C');assert.deepEqual(inZone.evidence,[['구역','C']]);
 const detected=store.ingestLiveEvent({...base,seq:5,ts_ms:5,event:'hazard_notice',judgement:{zone:'C',items:['lighter','powerbank'],source:'detector',vlm:true}});
 assert.equal(detected.title,'화기 위험물 경고');assert.deepEqual(detected.evidence,[['구역','C'],['검출 대상','라이터 · 보조배터리'],['확정 근거','위험물 검출기 + VLM 판독']],'검출기 확정은 무엇을 봤는지 보인다');
});
test('a confirmed zone change is filed under zones and lists its VLM findings (FR-8.3)',()=>{
 const {document,panels,store}=setup();store.setDemo(false);
 const base={state:'ALERT',escalation:'L3',tracks:[],detections:[],telemetry:{device_id:'mechdog-01'},entry:'e',snapshot:null};
 const zone=(seq,judgement)=>store.ingestLiveEvent({...base,seq,ts_ms:seq,event:'zone_changed',judgement});
 const event=zone(1,{zone:'A',changes:[{kind:'fallen_object',source:'vlm'}]});
 assert.equal(event.category,'OBJECT');assert.match(event.title,/^구역 위험 확정$/);assert.equal(event.zone,'A','목록 줄의 구역 칸도 판정의 구역을 쓴다');
 assert.deepEqual(event.evidence,[['구역','A'],['넘어짐·무너짐','VLM 판독 · 위치 없음']]);
 panels.render('events');chooseFilter(document,'사건 유형',4);
 assert.equal(document.querySelectorAll('.op-event-row').length,1,'구역 · 물품 필터에 걸린다');
 assert.match(document.querySelector('.op-event-detail').textContent,/판정 근거.*넘어짐·무너짐VLM 판독 · 위치 없음/);
 assert.deepEqual(zone(4,{zone:'D',changes:'bottle'}).evidence,[['구역','D'],['변화 내역','미수신']]);
 assert.equal(zone(5,{zone:'E',changes:Array.from({length:20},()=>({kind:'fallen_object',source:'vlm'}))}).evidence.length,13,'변화 행은 12건까지');
});
test('VLM-read zone changes are named without inventing a label, count or place',()=>{
 const {store}=setup();store.setDemo(false);
 // 폐기한 반출·반입(WBS 3.6.1~3.6.3)이 실린 옛 기록은 종류 원문만 보이고 위치를 지어내지 않는다.
 const event=store.ingestLiveEvent({state:'ALERT',escalation:'L3',tracks:[],detections:[],telemetry:{},entry:'e',snapshot:null,seq:1,ts_ms:1,event:'zone_changed',
  judgement:{zone:'A',grid:[3,3],changes:[{kind:'fallen_object',source:'vlm'},{kind:'blocked_path',source:'vlm',cell:[0,0]},{kind:'removed',label:'bottle',count:1,cell:[2,0]}]}});
 assert.deepEqual(event.evidence,[['구역','A'],['넘어짐·무너짐','VLM 판독 · 위치 없음'],['통로 막힘','VLM 판독 · 위치 없음'],['removed','판독 출처 미수신']]);
});
test('an old record without judgement says the field is missing, not a verdict',()=>{
 const {store}=setup();
 const event=store.ingestLiveEvent({seq:9,ts_ms:1,event:'PPE_UNDETERMINED',state:'ALERT',escalation:'L1',tracks:[],detections:[],telemetry:{},entry:'e',snapshot:null});
 assert.equal(event.ppe,'판정 근거 미수신');assert.equal(event.category,'PPE');
});
test('a fall read by the VLM leaves out the rule rows it has no values for',()=>{
 const {store}=setup();
 const event=store.ingestLiveEvent({seq:9,ts_ms:1,event:'person_fallen',state:'ZONE_INSPECT',escalation:'L3',tracks:[],detections:[],telemetry:{},entry:'e',snapshot:null,judgement:{fallen:true,source:'vlm',zone:'B'}});
 assert.deepEqual(event.evidence.filter(([,value])=>/—/.test(value)),[]);
});
test('pose buttons are live only under manual control and send the chosen preset (B6)',async()=>{
 const calls=[];const link={manual:async()=>({accepted:true}),drive:async()=>({accepted:true}),pose:async preset=>{calls.push(preset);return {accepted:true}}};
 const {document,panels,store}=setup();store.link=link;store.setDemo(false);panels.render('missions');
 const up=()=>document.querySelector('[data-pose="up"]');
 assert.equal(up().disabled,true,'제어권 없이는 누를 수 없다');assert.throws(()=>store.requestPose('up'),/수동 제어권/);
 button(document,'수동 제어권 요청').click();assert.equal(up().disabled,false);
 up().click();await new Promise(resolve=>setTimeout(resolve,0));assert.deepEqual(calls,['up']);
 assert.throws(()=>store.requestPose('tilt'),/허용되지 않은/);
});
test('pose buttons stay preview-only without a link',()=>{
 const {document,panels}=setup();panels.render('missions');
 assert.equal(document.querySelector('[data-pose]'),null);assert.equal(button(document,'앞쪽 들기 · 준비 중').disabled,true);
});
test('the escalation table reads server values and says when it could not (B7)',()=>{
 const {document,panels,store}=setup();panels.render('settings');
 const table=()=>[...document.querySelectorAll('.op-section')].find(s=>s.querySelector('summary')?.textContent==='대응 단계 · 안전 해제 상세').textContent;
 assert.match(table(),/서버 설정값 미수신/);
 store.setPolicy({l1_to_l2_hold_s:12,target_lost_timeout_s:7,auth_timeout_s:40,auth_max_attempts:3,auth_session_valid_s:60,detect_window_ms:300,detect_hits_required:3,l3_warning:'경보입니다.',led:{l3_alarm:'red'}});
 assert.match(table(),/미인증 12초면 L2/);assert.match(table(),/미검출 7초/);assert.match(table(),/인증 40초 초과 \/ 3회 실패/);assert.match(table(),/「경보입니다.」/);assert.match(table(),/눈 red/);assert.match(table(),/config\.yaml/);
 store.setPolicy({type:'FeatureCollection'});assert.equal(store.policy,null,'형식이 다른 응답은 받지 않는다');
});
test('voice panel phrase section is disabled without a link',()=>{
 const {document,panels}=setup();panels.render('voice');
 assert.ok([...document.querySelectorAll('h3')].some(h=>h.textContent==='멘트 관리'));
 assert.equal(button(document,'문구 보기').disabled,true);
});
test('device commands still ask before sending when the dialog is unavailable',()=>{
 const calls=[],prompts=[];
 const link={service:async mode=>{calls.push(['service',mode]);return{accepted:true}},resetSafe:async()=>{calls.push(['reset']);return{accepted:true}},patrol:async action=>{calls.push(['patrol',action]);return{accepted:true}},manual:async()=>({accepted:true}),drive:async()=>({accepted:true})};
 const {dom,document,panels,store}=setup();store.link=link;store.setDemo(false);panels.render('missions');
 // 이 DOM 에는 #confirm-dialog 가 없다 — 확인을 건너뛰지 않고 브라우저 기본 확인 창으로 묻는다.
 let answer=false;dom.window.confirm=message=>{prompts.push(message);return answer};
 button(document,'서비스 모드 진입 — 패치용 워치독').click();
 button(document,'안전 해제 (RESET_SAFE)').click();
 button(document,'순찰 시작').click();
 assert.deepEqual(calls,[]);assert.equal(prompts.length,3);assert.match(prompts[1],/원인이 제거.*주변에 사람이 없는지/);
 // 확인 창 자체가 없는 환경도 보내지 않는다.
 dom.window.confirm=undefined;button(document,'안전 해제 (RESET_SAFE)').click();assert.deepEqual(calls,[]);
 answer=true;dom.window.confirm=message=>{prompts.push(message);return answer};
 button(document,'서비스 모드 진입 — 패치용 워치독').click();
 assert.deepEqual(calls,[['service','enter']]);
 button(document,'안전 해제 (RESET_SAFE)').click();
 assert.deepEqual(calls[1],['reset']);
 // 정지는 안전 방향이라 묻지 않는다.
 const asked=prompts.length;button(document,'실제 순찰 정지').click();
 assert.deepEqual(calls[2],['patrol','stop']);assert.equal(prompts.length,asked);
});
test('locate buttons list patrol zones from the server and ask before sending',async()=>{
 const calls=[],prompts=[];
 const link={locate:async zone=>{calls.push(zone);return{accepted:true,detail:'구역 '+zone+' 안에서 위치를 다시 찾는다'}}};
 const {dom,document,panels,store,messages}=setup();
 panels.render('missions');assert.equal(document.querySelector('[data-locate-zone]'),null,'제어 · 장치에는 위치 힌트 묶음을 남기지 않는다');
 store.setPolicy({l1_to_l2_hold_s:12,target_lost_timeout_s:7,auth_timeout_s:40,auth_max_attempts:3,patrol_zones:['B','C','D','A','<x>']});
 store.link=link;store.setDemo(false);panels.render('zones');
 assert.deepEqual([...document.querySelectorAll('[data-locate-zone]')].map(b=>b.dataset.locateZone),['B','C','D','A'],'형식이 이상한 구역 id 는 버튼이 되지 않는다');
 let answer=false;dom.window.confirm=message=>{prompts.push(message);return answer};
 button(document,'구역 C').click();assert.deepEqual(calls,[]);assert.match(prompts[0],/구역 C 안에서만 다시 찾습니다/);
 answer=true;button(document,'구역 C').click();
 await new Promise(resolve=>setTimeout(resolve,0));
 assert.deepEqual(calls,['C']);assert.match(messages.at(-1),/구역 C 안에서 위치를 다시 찾는다/);
});
test('zone page uses one route map for move, locate and draw while preserving zone settings',()=>{
 const {document,panels,store}=setup();
 panels.render('zones');assert.equal(document.querySelector('[data-live-map]'),null,'예시 모드에서는 실제 지도를 지어내지 않는다');
 store.link={baseUrl:'',get:async path=>{if(path==='/api/planning')throw new Error('계획 서버 없음(시험)');return {}}};store.setDemo(false);panels.render('zones');
 assert.equal(document.querySelector('[data-live-map]'),null,'별도 이동 지도와 동선 지도를 중복으로 열지 않는다');
 assert.deepEqual([...document.querySelectorAll('[data-map-mode]')].map(button=>button.textContent),['이동','위치 알려주기','선 그리기']);
 assert.equal(document.querySelector('[data-locate-point]'),null,'위치 찍기 전용 버튼을 중복하지 않는다');
 assert.ok(document.querySelector('[data-locate-support]'),'구역으로 알려주기는 보조 기능으로 유지한다');
 assert.ok(document.querySelector('[data-plan-save]'),'기존 구역 정책 편집기는 보존한다');
 const session=panels.routePlanner.current;panels.render('zones');assert.equal(panels.routePlanner.current,session,'재렌더해도 같은 로봇 초안을 유지한다');
});
