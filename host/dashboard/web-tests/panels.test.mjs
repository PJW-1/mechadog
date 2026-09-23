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

for(const page of ['missions','events','records','zones','devices','settings'])test(page+' panel renders labeled actionable content without a browser',()=>{
 const {document,panels}=setup();panels.render(page);assert.ok(document.querySelector('#title').textContent);assert.ok(document.querySelector('.op-intro'));assert.ok(document.querySelector('.op-section'));assert.ok(document.querySelector('button'));assert.equal(document.querySelectorAll('script').length,0);
});
test('event filtering updates list and keeps the search input focused',()=>{
 const {dom,document,panels}=setup();panels.render('events');const search=document.querySelector('[name="사건 검색"]');search.focus();change(dom,search,'안전모','input');assert.equal(document.activeElement,search);assert.equal(document.querySelectorAll('.op-event-row').length,1);assert.match(document.querySelector('.op-detail-title').textContent,/안전모/);
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
test('local policy form saves, zone selection opens real workspace callback',()=>{
 const {dom,document,panels,store,navigation}=setup();panels.render('settings');const helmet=[...document.querySelectorAll('input[type=checkbox]')][0];helmet.checked=true;helmet.dispatchEvent(new dom.window.Event('input'));document.querySelector('form').dispatchEvent(new dom.window.Event('submit',{bubbles:true,cancelable:true}));assert.equal(store.policies['central-corridor'].helmet,true);
 button(document,'사원증').click();assert.ok(document.querySelector('.op-preview'));assert.equal(document.querySelector('form'),null);
 panels.render('zones');button(document,'PPE 초안 작성').click();panels.render(navigation.pop());assert.ok(document.querySelector('[name="PPE 정책 구역"]'));assert.equal(document.querySelector('[data-settings="display"]').getAttribute('aria-pressed'),'true');
 panels.render('zones');button(document,'3D에서 위치 보기').click();assert.deepEqual(navigation,['central-corridor']);
});
test('blackbox file validation rejects oversized JSON and spoofed JPEG',async()=>{
 const {panels}=setup();await assert.rejects(()=>panels.importFiles([{name:'meta.json',size:3*1024*1024}]),/2MB/);
 const raw={ts_ms:100,event:'person_found',state:'OBSERVE',escalation:'L1',tracks:[],detections:[],telemetry:{}};
 await assert.rejects(()=>panels.importFiles([{name:'meta.json',size:100,text:async()=>JSON.stringify(raw)},{name:'snapshot.jpg',size:3,slice:()=>({arrayBuffer:async()=>new Uint8Array([1,2,3]).buffer})}]),/실제 JPEG/);
});
test('import selects stored record, does not enable live data, and does not duplicate files',async()=>{
 const {panels,store,document}=setup();panels.render('events');const raw={ts_ms:100,event:'person_found',state:'OBSERVE',escalation:'L1',tracks:[],detections:[],telemetry:{device_id:'mechdog-01'}};
 const file={name:'meta.json',size:100,text:async()=>JSON.stringify(raw)};store.setDemo(false);await panels.importFiles([file]);assert.equal(store.demo,false);assert.equal(document.querySelector('.op-detail-title').textContent,'person_found');await panels.importFiles([file]);assert.equal(store.queryEvents().length,1);
});
test('STOP preempts a held direction on pointerdown, not only click',()=>{
 const {dom,document,panels,store}=setup();panels.render('missions');store.claim();const forward=document.querySelector('[data-drive="FORWARD"]');forward.setPointerCapture=()=>{};
 const down=(element,id)=>{const event=new dom.window.Event('pointerdown',{bubbles:true,cancelable:true});Object.assign(event,{pointerId:id,button:0});element.dispatchEvent(event)};
 down(forward,1);assert.equal(store.command,'FORWARD');down(document.querySelector('[data-drive="STOP"]'),2);assert.equal(store.command,'STOP');
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
 const type=document.querySelector('[aria-label="사건 유형"]');change(dom,type,'PPE');
 assert.equal(document.querySelectorAll('.op-event-row').length,1,'PPE 필터가 PPE 사건만 걸러낸다');
 const detail=document.querySelector('.op-event-detail').textContent;assert.match(detail,/보호구 미착용 확정/);assert.match(detail,/위반 · 안전모 미착용 1500ms/);assert.match(detail,/판정 근거/);assert.match(detail,/#4/);
 change(dom,type,'SAFETY');assert.equal(document.querySelectorAll('.op-event-row').length,3);
 const titles=[...document.querySelectorAll('.op-event-row')].map(row=>row.textContent).join('|');assert.match(titles,/대응 단계 → L3/);assert.match(titles,/안전 잠금/);assert.match(titles,/쓰러짐 감지/);
});
test('an old record without judgement says the field is missing, not a verdict',()=>{
 const {store}=setup();
 const event=store.ingestLiveEvent({seq:9,ts_ms:1,event:'PPE_UNDETERMINED',state:'ALERT',escalation:'L1',tracks:[],detections:[],telemetry:{},entry:'e',snapshot:null});
 assert.equal(event.ppe,'판정 근거 미수신');assert.equal(event.category,'PPE');
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
 assert.equal(document.querySelector('[data-pose]'),null);assert.equal(button(document,'앞쪽 들기').disabled,true);
});
test('the escalation table reads server values and says when it could not (B7)',()=>{
 const {document,panels,store}=setup();panels.render('settings');
 const table=()=>[...document.querySelectorAll('.op-section')].find(s=>s.querySelector('h3')?.textContent==='대응 단계 · 안전 해제 구분').textContent;
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
 button(document,'실제 순찰 시작').click();
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
