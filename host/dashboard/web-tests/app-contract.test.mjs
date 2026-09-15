import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {JSDOM} from 'jsdom';
import {Operations,ROBOTS} from '../static/operations.js';
import {OperationalPanels} from '../static/panels.js';
import {registerPageTools} from '../static/webmcp.js';
import {describeTelemetry} from '../static/telemetry-feed.js';

const html=await readFile(new URL('../static/index.html',import.meta.url),'utf8');
const source=(await readFile(new URL('../static/app.js',import.meta.url),'utf8')).replace(/^import .+;\r?\n/gm,'');
const layout=JSON.parse(await readFile(new URL('../static/factory-layout.json',import.meta.url),'utf8'));
async function boot(hash='dashboard',{health=null,search=''}={}){
 const dom=new JSDOM(html,{url:'http://127.0.0.1:4175/'+search+'#'+hash,runScripts:'outside-only',pretendToBeVisual:true}),window=dom.window,document=window.document,registered=new Map(),revoked=[];
 let store,panels,view,robotView,robotViewCount=0;const failures=[];window.addEventListener('error',event=>failures.push(event.error));
 // health 를 주면 대시보드 서버가 내보낸 화면처럼 실제 연결 경로로 뜬다.
 window.fetch=async url=>({ok:true,json:async()=>health&&String(url).endsWith('/health')?health:layout});window.URL.createObjectURL=()=> 'blob:local-test';window.URL.revokeObjectURL=url=>revoked.push(url);
 document.modelContext={registerTool:tool=>registered.set(tool.name,tool)};
 window.HTMLDialogElement.prototype.showModal=function(){this.open=true};
 document.documentElement.requestFullscreen=async()=>{document.documentElement.dataset.fullscreenRequested='true'};
 class TestOperations extends Operations{constructor(options){super(options);store=this}}
 class TestPanels extends OperationalPanels{constructor(options){super({...options,document});panels=this}}
 class View{
  constructor(options){view=this;this.onRobot=options.onRobot;this.onObservation=options.onObservation;this.onError=options.onError}selectRobot(id){this.selected=id}setPatrolRobot(id){this.patrolRobot=id}setPlaying(value){this.playing=value}setCameraVisible(value){this.cameraVisible=value}setWorldVisible(value){this.worldVisible=value}setActive(value){this.active=value}setView(mode){this.mode=mode}focusZone(zone){this.zone=zone}zoom(){}orbit(){}resize(){}dispose(){this.disposed=true}
 }
 let visionFeed=null,eventFeed=null,telemetryFeed=null;
 const linkCalls=[];
 class Link{manual(){return Promise.resolve({})}drive(){return Promise.resolve({})}estop(){linkCalls.push('estop');return Promise.resolve({})}service(mode){linkCalls.push('service:'+mode);return Promise.resolve({accepted:true})}patrol(action){linkCalls.push('patrol:'+action);return Promise.resolve({accepted:true})}resetSafe(){linkCalls.push('reset');return Promise.resolve({accepted:true})}}
 class Feed{constructor(options){visionFeed=this;this.options=options}start(){this.started=true}stop(){this.stopped=true}}
 class TelemetryStub{constructor(options){telemetryFeed=this;this.options=options}start(){this.started=true}stop(){this.stopped=true}}
 class EventStub{constructor(options){eventFeed=this;this.options=options}start(){this.started=true}stop(){this.stopped=true}}
 class RobotView{constructor({canvas}){robotView=this;this.canvas=canvas;robotViewCount++}setActive(value){this.active=value}resize(){}orbit(angle){this.angle=angle}zoom(value){this.zoomValue=value}reset(){this.resetCalled=true}dispose(){this.disposed=true}}
 // 음성 중계는 시험에서 연결하지 않는다 — 링크가 없을 때의 패널만 검증한다.
 class VoiceStub{status(){return Promise.resolve({})}transcript(){return Promise.resolve([])}say(){return Promise.resolve({})}mode(){return Promise.resolve({})}phrases(){return Promise.resolve([])}addPhrase(){return Promise.resolve({})}deletePhrase(){return Promise.resolve({})}}
 const resolveVoiceBase=async()=>null;
 await window.eval('(async function(FactoryView,renderRobotPreviews,icon,renderIcons,Operations,ROBOTS,OperationalPanels,registerPageTools,RobotDetailView,RobotLink,VoiceLink,resolveVoiceBase,VisionFeed,EventFeed,TelemetryFeed,describeTelemetry){'+source+'\n})')(View,()=>{},()=>'<svg aria-hidden="true"></svg>',()=>{},TestOperations,ROBOTS,TestPanels,registerPageTools,RobotView,Link,VoiceStub,resolveVoiceBase,Feed,EventStub,TelemetryStub,describeTelemetry);
 return {dom,window,document,store,panels,view,registered,revoked,failures,get robotView(){return robotView},get robotViewCount(){return robotViewCount},get visionFeed(){return visionFeed},get eventFeed(){return eventFeed},get telemetryFeed(){return telemetryFeed},linkCalls};
}

test('application opens direct hash, aligns 3D and camera selection, routes named scene buttons',async()=>{
 const {dom,document,store,view,failures}=await boot('events');assert.equal(document.querySelector('#map-placeholder').hidden,true);assert.equal(document.querySelector('#panel-title').textContent,'사건 검토');assert.equal(document.querySelector('#detail-panel').hidden,false);
 document.querySelector('[data-camera="top"]').click();assert.equal(document.querySelector('#panel-title').textContent,'공간 · 구역');
 document.querySelector('[data-camera="robot"]').click();assert.equal(document.querySelector('#panel-title').textContent,'장치 상태');
 document.querySelector('[data-robot="MD-02"]').click();assert.equal(store.selected,'MD-02');assert.equal(view.selected,'MD-02');assert.match(document.querySelector('#camera-title').textContent,/MD-02/);assert.deepEqual(failures,[]);dom.window.close();
});
test('navigation releases manual control, real-data mode hides preview without claiming connection',async()=>{
 const {dom,document,store,view}=await boot('missions');store.claim();store.move('FORWARD');document.querySelector('[data-view="events"]').click();assert.equal(store.command,'STOP');assert.equal(store.control,null);
 store.setDemo(false);assert.match(document.querySelector('.actual-status').textContent,/장비 미연결/);document.querySelector('[data-view="settings"]').click();assert.match(document.querySelector('#panel-content').textContent,/실제 로봇연결 안 됨/);document.querySelector('[data-view="events"]').click();assert.equal(document.querySelector('#app').classList.contains('data-waiting'),true);assert.equal(view.cameraVisible,false);assert.equal(view.playing,false);assert.match(document.querySelector('#frame-source').textContent,/미수신/);dom.window.close();
});

test('WASD requires ownership, holds one direction and Space cancels without automatic restart',async()=>{
 const {dom,document,store}=await boot('missions');
 const key=(type,code,extra={},target=document)=>{const event=new dom.window.KeyboardEvent(type,{code,key:code==='Space'?' ':code.slice(-1).toLowerCase(),bubbles:true,cancelable:true,...extra});target.dispatchEvent(event);return event};
 key('keydown','KeyW');assert.equal(store.command,'STOP');key('keyup','KeyW');store.claim();
 for(const [code,command]of [['KeyW','FORWARD'],['KeyA','LEFT'],['KeyS','BACKWARD'],['KeyD','RIGHT']]){
  key('keydown',code);assert.equal(store.command,command);const count=store.records.length;key('keydown',code,{repeat:true});assert.equal(store.records.length,count);key('keyup',code);assert.equal(store.command,'STOP');
 }
 const forward=document.querySelector('[data-drive="FORWARD"]');forward.focus();key('keydown','KeyW');assert.equal(store.command,'FORWARD');
 assert.equal(key('keydown','Space',{},forward).defaultPrevented,true);assert.equal(store.command,'STOP');
 key('keyup','Space',{},forward);key('keydown','KeyW',{repeat:true});assert.equal(store.command,'STOP');key('keyup','KeyW');
 key('keydown','KeyW');key('keydown','KeyD');assert.equal(store.command,'FORWARD');key('keyup','KeyW');assert.equal(store.command,'STOP');key('keydown','KeyD',{repeat:true});assert.equal(store.command,'STOP');key('keyup','KeyD');
 key('keydown','Space');key('keydown','KeyA');assert.equal(store.command,'STOP');
 forward.dispatchEvent(new dom.window.KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));assert.equal(store.command,'STOP');
 forward.setPointerCapture=()=>{};const pointer=new dom.window.Event('pointerdown',{bubbles:true,cancelable:true});Object.assign(pointer,{pointerId:1,button:0});forward.dispatchEvent(pointer);assert.equal(store.command,'STOP');
 key('keyup','Space');key('keyup','KeyA');
 assert.equal(document.querySelectorAll('.op-camera-posture button:disabled').length,3);assert.match(document.querySelector('.op-camera-posture').textContent,/지원 미확인/);
 dom.window.close();
});

test('manual shortcuts leave forms, IME, browser shortcuts and other pages alone; context changes stop input',async()=>{
 const {dom,document,store,panels}=await boot('missions');
 const key=(type,code,target=document,extra={})=>{const event=new dom.window.KeyboardEvent(type,{code,key:code==='Space'?' ':code.slice(-1).toLowerCase(),bubbles:true,cancelable:true,...extra});target.dispatchEvent(event);return event};
 store.claim();const input=document.querySelector('[name="임무 로봇"]');
 assert.equal(key('keydown','KeyW',input).defaultPrevented,false);assert.equal(store.command,'STOP');assert.equal(key('keydown','Space',input).defaultPrevented,false);
 for(const extra of [{ctrlKey:true},{altKey:true},{metaKey:true},{isComposing:true}]){assert.equal(key('keydown','KeyW',document,extra).defaultPrevented,false);assert.equal(store.command,'STOP')}
 const editable=document.createElement('div');editable.contentEditable='true';editable.setAttribute('contenteditable','true');document.querySelector('#panel-content').append(editable);key('keydown','KeyW',editable);assert.equal(store.command,'STOP');
 key('keydown','KeyW');assert.equal(store.command,'FORWARD');input.focus();assert.equal(store.command,'STOP');key('keyup','KeyW');
 key('keydown','KeyW');dom.window.dispatchEvent(new dom.window.Event('blur'));assert.equal(store.command,'STOP');assert.equal(store.control,null);key('keyup','KeyW');
 store.claim();key('keydown','KeyD');store.selectRobot('MD-02');assert.equal(store.command,'STOP');assert.equal(store.control,null);key('keydown','KeyD',document,{repeat:true});assert.equal(store.command,'STOP');key('keyup','KeyD');
 store.claim();panels.keyboardEnabled=false;key('keydown','KeyW');assert.equal(store.command,'STOP');store.move('FORWARD');key('keydown','Space');assert.equal(store.command,'STOP');key('keyup','Space');
 document.querySelector('[data-view="events"]').click();assert.equal(key('keydown','KeyW').defaultPrevented,false);assert.equal(store.command,'STOP');dom.window.close();
});

test('manual observation reuses the camera, aligns target and releases held control on selection',async()=>{
 const {dom,document,store,view,failures}=await boot('missions');
 const dock=document.querySelector('#camera-dock'),canvas=document.querySelector('#fpv');
 assert.ok(dock.closest('[data-manual-camera]'));assert.equal(view.worldVisible,false);assert.equal(view.cameraVisible,true);
 view.onObservation({id:'MD-01',x:-17,z:3.5,heading:0});
 assert.match(document.querySelector('[data-location-marker]').getAttribute('transform'),/translate\(-17 3.5\)/);
 store.claim();store.move('FORWARD');assert.equal(document.querySelector('#fpv'),canvas);
 const target=document.querySelector('[name="수동 제어 대상"]');target.value='MD-02';target.dispatchEvent(new dom.window.Event('change',{bubbles:true}));
 assert.equal(store.selected,'MD-02');assert.equal(store.control,null);assert.equal(store.command,'STOP');assert.equal(view.selected,'MD-02');assert.equal(document.querySelector('#fpv'),canvas);
 assert.match(document.querySelector('.op-manual-controls').getAttribute('aria-label'),/MD-02/);assert.match(document.querySelector('#camera-title').textContent,/MD-02/);
 view.onObservation({id:'MD-02',x:-1,z:-8,heading:Math.PI/2});assert.match(document.querySelector('[data-location-marker]').getAttribute('transform'),/rotate\(-90\)/);
 store.setStale(true);assert.equal(view.cameraVisible,false);assert.equal(view.active,false);assert.equal(document.querySelector('.op-location-map').style.display,'none');
 store.setStale(false);assert.equal(store.control,null);assert.equal(store.command,'STOP');
 document.querySelector('#close-panel').click();assert.equal(dock.parentElement.id,'stage');assert.equal(document.querySelector('#fpv'),canvas);assert.equal(view.worldVisible,true);
 assert.deepEqual(failures,[]);dom.window.close();
});

test('manual graphics failure replaces stale observation with persistent recovery information',async()=>{
 const {dom,document,store,view}=await boot('missions');
 view.onObservation({id:'MD-01',x:-17,z:3.5,heading:0});store.claim();store.move('FORWARD');
 view.onError('그래픽 연결 중단');
 assert.equal(store.command,'STOP');assert.equal(store.control,null);assert.equal(view.active,false);assert.equal(view.cameraVisible,false);
 assert.equal(document.querySelector('.op-observation-error').hidden,false);assert.match(document.querySelector('[data-observation-error]').textContent,/그래픽 연결 중단/);assert.equal(document.querySelector('.op-location-map').style.display,'none');
 store.selectRobot('MD-02');assert.equal(document.querySelector('.op-observation-error').hidden,false);assert.match(document.querySelector('.op-location').textContent,/예시 위치를 표시할 수 없습니다/);
 assert.equal(document.querySelector('#fpv').closest('[data-manual-camera]')!==null,true);dom.window.close();
});
test('workspace replaces the dashboard; camera switch stays in place while robot cards open reusable 3D detail',async()=>{
 const state=await boot(),{dom,document,store,view,panels}=state;
 document.querySelector('.camera-robot-switch [data-robot="MD-02"]').click();assert.equal(document.querySelector('#detail-panel').hidden,true);
 const opener=document.querySelector('.robot-tab[data-robot="MD-03"]');opener.focus();opener.click();
 assert.equal(store.selected,'MD-03');assert.equal(document.querySelector('#stage').hidden,true);assert.equal(document.querySelector('.fleet-band').hidden,true);assert.equal(document.querySelector('.operation-rail').hidden,true);assert.ok(document.querySelector('#app').classList.contains('workspace-open'));assert.equal(view.active,false);assert.equal(view.cameraVisible,false);assert.equal(document.querySelector('#estop').closest('[hidden]'),null);
 const canvas=document.querySelector('.robot-detail-canvas');assert.match(canvas.getAttribute('aria-label'),/MD-03/);assert.equal(state.robotView.active,true);store.selectRobot('MD-01');assert.equal(document.querySelector('.robot-detail-canvas'),canvas);assert.equal(state.robotViewCount,1);
 document.querySelector('.skip-link').click();assert.equal(dom.window.location.hash,'#devices');assert.equal(document.activeElement,document.querySelector('#panel-title'));
 document.querySelector('[data-robot-action="left"]').click();assert.ok(state.robotView.angle<0);document.querySelector('[data-robot-action="reset"]').click();assert.equal(state.robotView.resetCalled,true);
 panels.setRobotPreviewError('3D 표시 실패');assert.equal(document.querySelector('.robot-preview-error').hidden,false);assert.match(document.querySelector('.robot-status-sheet').textContent,/미수신/);
 document.querySelector('#close-panel').click();assert.equal(view.active,true);assert.equal(state.robotView.active,false);assert.equal(document.activeElement,opener);assert.equal(document.querySelector('#stage').hidden,false);dom.window.close();
});
test('3D picking and page navigation preserve mission state while rendering only the visible page',async()=>{
 const state=await boot(),{dom,document,view,store}=state;store.startMission({robot:'MD-01',acknowledged:true});view.onRobot('MD-02');
 assert.equal(document.querySelector('#panel-title').textContent,'장치 상태');assert.equal(store.mission.status,'running');assert.equal(store.mission.robot,'MD-01');assert.equal(view.active,false);
 document.dispatchEvent(new dom.window.KeyboardEvent('keydown',{key:'Escape'}));assert.equal(document.querySelector('#detail-panel').hidden,true);assert.equal(view.active,true);assert.equal(store.mission.status,'running');assert.equal(state.robotView.active,false);dom.window.close();
});
test('stale and E-stop pause renderer and both mission records with no recovery auto-resume',async()=>{
 const {dom,document,store,view}=await boot();store.startMission({robot:'MD-03',acknowledged:true});assert.equal(view.playing,true);assert.equal(view.patrolRobot,'MD-03');store.setStale(true);assert.equal(view.playing,false);assert.equal(store.sessions[0].status,'paused');store.setStale(false);assert.equal(view.playing,false);
 store.resumeMission();document.querySelector('#estop').click();assert.equal(view.playing,false);assert.equal(document.querySelector('#stop-dialog').open,true);document.querySelector('#stop-preview').dispatchEvent(new dom.window.Event('click'));assert.equal(store.estop,true);assert.ok(document.querySelector('#estop').classList.contains('preview-latched'));dom.window.close();
});
test('BFCache pagehide preserves imported image URLs; final unload revokes them',async()=>{
 const {dom,window,panels,revoked}=await boot();panels.urls.add('blob:evidence');window.dispatchEvent(new window.PageTransitionEvent('pagehide',{persisted:true}));assert.deepEqual(revoked,[]);window.dispatchEvent(new window.PageTransitionEvent('pagehide',{persisted:false}));assert.deepEqual(revoked,['blob:evidence']);dom.window.close();
});
test('fullscreen includes external workspace panels as well as the factory app',async()=>{
 const {dom,document}=await boot();document.querySelector('#fullscreen').click();assert.equal(document.documentElement.dataset.fullscreenRequested,'true');dom.window.close();
});

test('served by the dashboard, robot state from /ws/telemetry fills the status card and the device gauges in place',async()=>{
 const state=await boot('devices',{health:{service:'telemetry',vision_clients:0}}),{dom,document,store,failures}=state,feed=state.telemetryFeed;
 assert.equal(feed.options.url,'ws://127.0.0.1:4175/ws/telemetry');assert.equal(feed.started,true);assert.deepEqual(failures,[]);
 const card=()=>document.querySelector('.actual-status').textContent,sheet=()=>document.querySelector('.robot-status-sheet').textContent;
 const snapshot=(extra={})=>({deviceId:'mechdog-01',state:'PATROL',escalation:'L1',ageMs:40,stale:false,runtimeStale:false,telemetry:{deviceId:'mechdog-3c8a1f333208',bootId:'b',seq:9,state:'IDLE',battV:7.64,distCm:52,imu:{pitch:0.4,roll:-1.2,yaw:180},lastCmdAgeMs:70,safetyLatched:false,flags:{lowbatt:false,tipped:false,obstacle:false,linkOk:true}},...extra});
 const history=[{t:0,battV:7.7,distCm:60},{t:1000,battV:7.66,distCm:55},{t:2000,battV:7.64,distCm:52}];
 const canvas=document.querySelector('.robot-detail-canvas'),views=state.robotViewCount,button=document.querySelector('.robot-status-sheet .op-toolbar button');
 feed.options.onUpdate({state:'live',snapshot:snapshot(),rateHz:10,lost:0,history});
 assert.match(card(),/mechdog-01 · PATROL · L1/);assert.match(card(),/배터리 7\.64 V · 거리 52 cm · 수신 10\.0 Hz/);
 assert.match(sheet(),/7\.64 V/);assert.match(sheet(),/PATROL \/ L1 · 온보드 IDLE/);assert.match(sheet(),/실시간/);assert.match(sheet(),/최소 7\.64 V · 최대 7\.70 V/);
 // 10Hz 로 다시 채워도 3D 미리보기와 버튼은 그대로다 — 화면 전체를 다시 그리지 않는다.
 assert.equal(document.querySelector('.robot-detail-canvas'),canvas);assert.equal(state.robotViewCount,views);assert.equal(document.querySelector('.robot-status-sheet .op-toolbar button'),button);
 // 로봇이 끊기면 마지막 값은 남기되 끊겼다고 말한다.
 feed.options.onUpdate({state:'live',snapshot:snapshot({stale:true,ageMs:4200}),rateHz:0,lost:0,history});
 assert.match(card(),/로봇 수신 끊김 · 4\.2 s 전/);assert.match(sheet(),/수신 끊김/);assert.match(sheet(),/지금 상태로 판단하지 마세요/);
 assert.equal(document.querySelector('.actual-status').dataset.tone,'stale');
 dom.window.dispatchEvent(new dom.window.PageTransitionEvent('pagehide',{persisted:false}));assert.equal(feed.stopped,true);
 dom.window.close();
});
test('device commands on 순찰·제어 follow the telemetry feed without rebuilding their buttons',async()=>{
 const state=await boot('missions',{health:{service:'telemetry',vision_clients:0}}),{dom,document,failures,linkCalls}=state,feed=state.telemetryFeed;
 const section=()=>[...document.querySelectorAll('.op-section')].find(s=>s.querySelector('h3')?.textContent==='실제 장비 명령');
 const toggle=()=>section().querySelector('[data-service="toggle"]');
 assert.match(section().textContent,/서비스 모드미수신/);assert.equal(toggle().disabled,false);
 const snap=service=>({deviceId:'mechdog-01',state:'IDLE',escalation:'L0',ageMs:30,stale:false,runtimeStale:false,telemetry:{deviceId:'x',bootId:'b',seq:1,state:'FAILSAFE',battV:8.1,distCm:90,imu:{pitch:0,roll:0,yaw:0},lastCmdAgeMs:20,safetyLatched:true,flags:{lowbatt:false,tipped:false,obstacle:false,linkOk:true,service}}});
 const button=toggle();
 feed.options.onUpdate({state:'live',snapshot:snap(true),rateHz:10,lost:0,history:[]});
 assert.match(section().textContent,/서비스 모드켜짐/);assert.match(section().textContent,/안전 래치걸림/);assert.match(section().textContent,/IDLE · 온보드 FAILSAFE/);
 // 10Hz 로 칸을 고쳐도 버튼은 같은 요소다 — 누르는 순간 교체되면 클릭이 사라진다.
 assert.equal(toggle(),button);assert.match(button.textContent,/서비스 모드 해제/);
 // 모드 변경은 확인 대화상자를 거친다 — 취소하면 명령이 나가지 않고, 예를 눌러야 전송된다.
 const dialog=document.querySelector('#confirm-dialog'),confirm=()=>{dialog.returnValue='yes';dialog.dispatchEvent(new dom.window.Event('close'))},dismiss=()=>{dialog.returnValue='';dialog.dispatchEvent(new dom.window.Event('close'))};
 button.click();assert.equal(dialog.open,true);assert.match(dialog.querySelector('#confirm-title').textContent,/해제/);dismiss();
 await new Promise(resolve=>setTimeout(resolve,0));assert.deepEqual(linkCalls,[]);
 button.click();confirm();await new Promise(resolve=>setTimeout(resolve,0));
 assert.deepEqual(linkCalls,['service:exit']);
 const reset=section().querySelector('[data-reset-safe="confirm"]');
 reset.click();assert.equal(dialog.open,true);assert.match(dialog.querySelector('#confirm-body').textContent,/원인이 제거.*주변에 사람이 없는지/);dismiss();
 await new Promise(resolve=>setTimeout(resolve,0));assert.deepEqual(linkCalls,['service:exit']);
 reset.click();confirm();await new Promise(resolve=>setTimeout(resolve,0));
 assert.deepEqual(linkCalls,['service:exit','reset']);
 feed.options.onUpdate({state:'live',snapshot:snap(null),rateHz:10,lost:0,history:[]});
 assert.match(section().textContent,/모름 · 펌웨어가 알리지 않음/);assert.deepEqual(failures,[]);
 dom.window.close();
});
test('without the dashboard server there is no telemetry feed and the gauges stay unfilled',async()=>{
 const state=await boot('devices'),{dom,document}=state;
 assert.equal(state.telemetryFeed,null);assert.match(document.querySelector('.robot-status-sheet').textContent,/미수신/);assert.match(document.querySelector('.actual-status').textContent,/장비 미연결/);
 dom.window.close();
});
test('served by the dashboard, the robot view draws /ws/vision and says what it actually receives',async()=>{
 const state=await boot('dashboard',{health:{service:'telemetry',vision_clients:0}}),{dom,document,store,failures}=state,feed=state.visionFeed;
 const text=id=>document.querySelector('#'+id).textContent;
 assert.equal(store.live,true);assert.deepEqual(failures,[]);
 assert.equal(feed.options.url,'ws://127.0.0.1:4175/ws/vision');assert.equal(feed.options.canvas,document.querySelector('#vision-frame'));assert.equal(feed.started,true);
 assert.equal(document.querySelector('#vision-frame').hidden,false);assert.equal(document.querySelector('#fpv').hidden,true);
 // 실제로 붙어 있는데 하단 상태 칸이 "장비 미연결" 이라고 하면 안 된다 — 로봇 상태를 아는 척도 하지 않는다.
 assert.doesNotMatch(document.querySelector('.actual-status').textContent,/미연결/);assert.match(document.querySelector('.actual-status').textContent,/상태 채널 연결 중/);
 // 지도가 없으니 예시 공장 3D 를 그리지 않고 "지도 없음" 을 말한다 (#159 가 되살렸던 예시 로봇·경고 표시 포함).
 assert.equal(state.view.active,false);assert.equal(document.querySelector('#map-placeholder').hidden,false);assert.match(document.querySelector('#scene-subtitle').textContent,/지도 없음/);
 // 설정 화면의 "실제 로봇" 칸도 연결을 사실대로 말한다 (고정 "연결 안 됨" 이 아니다).
 document.querySelector('[data-view="settings"]').click();assert.match(document.querySelector('#panel-content').textContent,/실제 로봇관제 서버 연결됨 · 로봇 상태는 장치 화면에서/);document.querySelector('#close-panel').click();
 // 연결만 됐다고 "실시간" 이라 하지 않는다.
 assert.equal(text('frame-source'),'영상 연결 중');assert.doesNotMatch(text('camera-status'),/실시간/);
 feed.options.onStatus({state:'live',lastFrameAt:Date.UTC(2026,8,14,10,0,0),frameSeq:12,width:640,height:480,detections:3,persons:2});
 assert.equal(text('frame-source'),'실시간 · 검출 박스');assert.equal(text('camera-status'),'실시간 수신 중 · 검출 3건 · 사람 2명');assert.equal(text('camera-resolution'),'640 × 480');assert.doesNotMatch(text('frame-time'),/—/);
 assert.equal(document.querySelector('#app').classList.contains('vision-has-frame'),true);
 feed.options.onStatus({state:'stale',lastFrameAt:Date.UTC(2026,8,14,10,0,0),frameSeq:12,width:640,height:480,detections:3,persons:2});
 // 다른 화면 갱신이 멈춘 상태를 "실시간" 으로 덮어쓰지 않는다.
 store.selectRobot('MD-02');
 assert.equal(text('frame-source'),'영상 멈춤 · 마지막 장면');assert.equal(text('camera-status'),'새 영상 없음');
 dom.window.dispatchEvent(new dom.window.PageTransitionEvent('pagehide',{persisted:false}));assert.equal(feed.stopped,true);
 dom.window.close();
});
test('served by the dashboard, live events flow from /ws/events into the review list',async()=>{
 const state=await boot('dashboard',{health:{service:'telemetry',vision_clients:0}}),{dom,document,store,failures}=state,feed=state.eventFeed;
 assert.equal(feed.options.url,'ws://127.0.0.1:4175/ws/events');assert.equal(feed.started,true);
 feed.options.onStatus({state:'live',received:0,dropped:0,malformed:0});
 assert.equal(store.liveFeed.state,'live');
 // 서버가 보내는 사건 하나 — person_found 는 검토 대상으로 들어온다.
 feed.options.onEvent({seq:7,event:'person_found',ts_ms:1000,state:'OBSERVE',escalation:'L1',tracks:[{track_id:3,box:[1,2,3,4],score:0.9}],detections:[],telemetry:{device_id:'MD-01'},entry:'20260914_120000_person_found',snapshot:'snapshot.jpg'});
 const event=store.events.find(e=>e.id==='LIVE-7');assert.ok(event);assert.equal(event.source,'LIVE_FEED');assert.equal(event.robot,'MD-01');assert.equal(event.review,'pending');
 document.querySelector('[data-view="events"]').click();
 assert.match(document.querySelector('#panel-content').textContent,/실시간 수신 연결됨/);assert.match(document.querySelector('.op-event-list').textContent,/person_found/);assert.match(document.querySelector('.op-event-list').textContent,/실시간/);
 feed.options.onGap(3);assert.match(store.records[0].detail,/3건/);
 dom.window.dispatchEvent(new dom.window.PageTransitionEvent('pagehide',{persisted:false}));assert.equal(feed.stopped,true);
 assert.deepEqual(failures,[]);dom.window.close();
});
test('without a vision channel the robot view does not try to connect',async()=>{
 const state=await boot('dashboard',{health:{service:'telemetry',vision_clients:null}}),{dom,document}=state;
 assert.equal(state.visionFeed,null);assert.equal(document.querySelector('#camera-status').textContent,'비전 채널 없음');
 dom.window.close();
});
test('a page from another origin cannot point itself at the robot API with ?api=',async()=>{
 // 다른 출처의 API 로 붙으면 서버 출처 검사가 비상정지·WS 를 전부 거절한다 — 연결된 척하는 화면이 된다.
 const {dom,store,failures}=await boot('dashboard',{search:'?api=http://127.0.0.1:8000'});
 assert.equal(store.live,false);assert.deepEqual(failures,[]);
 dom.window.close();
});
test('camera expand, collapse and reopen keep labels and rendering in sync',async()=>{
 const {dom,document,view,store}=await boot();
 const dock=document.querySelector('#camera-dock'),expand=document.querySelector('#expand-camera'),collapse=document.querySelector('#collapse-camera');
 expand.click();assert.ok(dock.classList.contains('expanded'));assert.match(expand.textContent,/축소/);
 collapse.click();assert.ok(dock.classList.contains('collapsed'));assert.equal(dock.classList.contains('expanded'),false);
 assert.equal(expand.getAttribute('aria-label'),'로봇 시점 크게 보기');assert.match(expand.textContent,/확대/);
 assert.equal(collapse.getAttribute('aria-expanded'),'false');assert.match(collapse.textContent,/펼치기/);assert.equal(view.cameraVisible,false);
 collapse.click();assert.equal(dock.classList.contains('collapsed'),false);assert.match(collapse.textContent,/접기/);assert.equal(view.cameraVisible,true);
 store.setDemo(false);expand.click();assert.equal(view.cameraVisible,false);
 dom.window.close();
});
test('page-tool contracts use UI state and reject invalid actions (mock registry, not browser WebMCP verification)',async()=>{
 const {dom,document,store,registered}=await boot();assert.equal(registered.size,3);const read=registered.get('read_operations_summary'),open=registered.get('open_operations_panel'),select=registered.get('select_observation_robot');assert.equal(read.annotations.readOnlyHint,true);assert.equal(read.execute({}).liveConnected,false);
 open.execute({page:'events'});assert.equal(document.querySelector('#panel-title').textContent,'사건 검토');select.execute({robot:'MD-03'});assert.equal(store.selected,'MD-03');assert.throws(()=>select.execute({robot:'external-robot'}));assert.equal(store.selected,'MD-03');assert.throws(()=>open.execute({page:'delete'}));assert.throws(()=>read.execute({extra:true}));dom.window.close();
});
test('unsupported and rejected WebMCP registration do not break normal use',async()=>{
 const dom=new JSDOM(''),store=new Operations(),errors=[];assert.doesNotThrow(()=>registerPageTools({document:dom.window.document,store,navigate:()=>{}})());
 dom.window.document.modelContext={registerTool:()=>Promise.reject(new Error('unavailable'))};const cleanup=registerPageTools({document:dom.window.document,store,navigate:()=>{},onError:error=>errors.push(error.message)});await new Promise(resolve=>setImmediate(resolve));assert.equal(errors.length,3);cleanup();dom.window.close();
});

test('connected, the E-Stop shortcut sends straight away instead of opening the notice',async()=>{
 // FR-4.4 는 단축키를 요구한다. 예전에는 Shift+E 가 모달만 열어 연결돼 있어도 한 번
 // 더 눌러야 나갔다 — 버튼 쪽 주석이 "모달을 한 단계 끼우면 급할 때 그만큼 늦다" 고
 // 적어 둔 바로 그 문제를 단축키만 안고 있었다.
 const state=await boot('dashboard',{health:{service:'telemetry',vision_clients:0}}),{dom,document,store}=state;
 assert.equal(store.live,true);
 document.dispatchEvent(new dom.window.KeyboardEvent('keydown',{key:'E',shiftKey:true}));
 assert.deepEqual(state.linkCalls,['estop']);
 assert.equal(document.querySelector('#stop-dialog').open,false,'연결돼 있으면 안내를 끼우지 않는다');
 dom.window.close();
});
test('without a link the E-Stop shortcut still opens the notice',async()=>{
 // 보낼 곳이 없을 때는 안내가 맞다 — 그때만 모달이다.
 const {dom,document,store}=await boot();
 assert.equal(store.live,false);
 document.dispatchEvent(new dom.window.KeyboardEvent('keydown',{key:'E',shiftKey:true}));
 assert.equal(document.querySelector('#stop-dialog').open,true);
 dom.window.close();
});
