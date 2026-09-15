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
test('voice panel renders phrase manager with categories and custom delete',async()=>{
 const {dom,document,panels,store}=setup();
 const link={
  baseUrl:'http://127.0.0.1:8090',
  status:()=>Promise.resolve({robot:'mechadog-01',mode:'active',activity:'idle',say_queue:0,events:[]}),
  say:()=>Promise.resolve({}),mode:()=>Promise.resolve({}),transcript:()=>Promise.resolve([]),
  phrases:()=>Promise.resolve([{category:'greeting',count:2,lines:[{text:'안녕하세요',custom:false},{text:'관리자가 넣은 말',custom:true}]}]),
  addPhrase:(cat,text)=>{added=[cat,text];return Promise.resolve({category:cat,added:true})},
  deletePhrase:(cat,text)=>{deleted=[cat,text];return Promise.resolve({removed:true})},
 };
 let added=null,deleted=null;
 const p=new OperationalPanels({store,container:document.querySelector('#content'),title:document.querySelector('#title'),onNavigate:()=>{},onFocusZone:()=>{},onFocusRobot:()=>{},onToast:()=>{},voiceLink:link,document});
 p.render('voice');
 await new Promise(resolve=>setTimeout(resolve,10));
 // 멘트 관리 섹션 + 카테고리 표시
 assert.ok([...document.querySelectorAll('h3')].some(h=>h.textContent==='멘트 관리'));
 const sel=document.querySelector('select[name="카테고리"]');
 assert.ok(sel);assert.match(sel.textContent,/greeting/);
 // 기본 문구는 삭제 버튼 없음, 추가 문구만 삭제 버튼
 const rows=[...document.querySelectorAll('.op-voice-row')];
 assert.equal(rows.length,2);
 assert.equal(rows[0].querySelector('button'),null);
 assert.ok(rows[1].querySelector('button'));
 // 추가 폼 동작
 document.querySelector('[name="문구"]').value='테스트 문구입니다';
 button(document,'문구 추가').click();
 await new Promise(resolve=>setTimeout(resolve,10));
 assert.deepEqual(added,['greeting','테스트 문구입니다']);
 // 삭제 버튼은 custom 문구만 호출
 rows[1].querySelector('button').click();
 await new Promise(resolve=>setTimeout(resolve,10));
 assert.deepEqual(deleted,['greeting','관리자가 넣은 말']);
 p.clearVoicePoll();
});
test('voice panel phrase section is disabled without a link',()=>{
 const {document,panels}=setup();panels.render('voice');
 assert.ok([...document.querySelectorAll('h3')].some(h=>h.textContent==='멘트 관리'));
 assert.equal(button(document,'문구 보기').disabled,true);
 assert.equal(button(document,'문구 추가').disabled,true);
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
