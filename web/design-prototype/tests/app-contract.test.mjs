import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {JSDOM} from 'jsdom';
import {Operations,ROBOTS} from '../operations.js';
import {OperationalPanels} from '../panels.js';
import {registerPageTools} from '../webmcp.js';

const html=await readFile(new URL('../index.html',import.meta.url),'utf8');
const source=(await readFile(new URL('../app.js',import.meta.url),'utf8')).replace(/^import .+;\r?\n/gm,'');
const layout=JSON.parse(await readFile(new URL('../factory-layout.json',import.meta.url),'utf8'));
async function boot(hash='dashboard'){
 const dom=new JSDOM(html,{url:'http://127.0.0.1:4175/#'+hash,runScripts:'outside-only',pretendToBeVisual:true}),window=dom.window,document=window.document,registered=new Map(),revoked=[];
 let store,panels,view,robotView,robotViewCount=0;const failures=[];window.addEventListener('error',event=>failures.push(event.error));
 window.fetch=async()=>({ok:true,json:async()=>layout});window.URL.createObjectURL=()=> 'blob:local-test';window.URL.revokeObjectURL=url=>revoked.push(url);
 document.modelContext={registerTool:tool=>registered.set(tool.name,tool)};
 window.HTMLDialogElement.prototype.showModal=function(){this.open=true};
 document.documentElement.requestFullscreen=async()=>{document.documentElement.dataset.fullscreenRequested='true'};
 class TestOperations extends Operations{constructor(options){super(options);store=this}}
 class TestPanels extends OperationalPanels{constructor(options){super({...options,document});panels=this}}
 class View{
  constructor(options){view=this;this.onRobot=options.onRobot;this.onObservation=options.onObservation;this.onError=options.onError}selectRobot(id){this.selected=id}setPatrolRobot(id){this.patrolRobot=id}setPlaying(value){this.playing=value}setCameraVisible(value){this.cameraVisible=value}setWorldVisible(value){this.worldVisible=value}setActive(value){this.active=value}setView(mode){this.mode=mode}focusZone(zone){this.zone=zone}zoom(){}orbit(){}resize(){}dispose(){this.disposed=true}
 }
 class RobotView{constructor({canvas}){robotView=this;this.canvas=canvas;robotViewCount++}setActive(value){this.active=value}resize(){}orbit(angle){this.angle=angle}zoom(value){this.zoomValue=value}reset(){this.resetCalled=true}dispose(){this.disposed=true}}
 await window.eval('(async function(FactoryView,renderRobotPreviews,icon,renderIcons,Operations,ROBOTS,OperationalPanels,registerPageTools,RobotDetailView){'+source+'\n})')(View,()=>{},()=>'<svg aria-hidden="true"></svg>',()=>{},TestOperations,ROBOTS,TestPanels,registerPageTools,RobotView);
 return {dom,window,document,store,panels,view,registered,revoked,failures,get robotView(){return robotView},get robotViewCount(){return robotViewCount}};
}

test('application opens direct hash, aligns 3D and camera selection, routes named scene buttons',async()=>{
 const {dom,document,store,view,failures}=await boot('events');assert.equal(document.querySelector('#panel-title').textContent,'사건 검토');assert.equal(document.querySelector('#detail-panel').hidden,false);
 document.querySelector('[data-camera="top"]').click();assert.equal(document.querySelector('#panel-title').textContent,'공간 · 구역');
 document.querySelector('[data-camera="robot"]').click();assert.equal(document.querySelector('#panel-title').textContent,'장치 상태');
 document.querySelector('[data-robot="MD-02"]').click();assert.equal(store.selected,'MD-02');assert.equal(view.selected,'MD-02');assert.match(document.querySelector('#camera-title').textContent,/MD-02/);assert.deepEqual(failures,[]);dom.window.close();
});
test('navigation releases manual control, real-data mode hides preview without claiming connection',async()=>{
 const {dom,document,store,view}=await boot('missions');store.claim();store.move('FORWARD');document.querySelector('[data-view="events"]').click();assert.equal(store.command,'STOP');assert.equal(store.control,null);
 store.setDemo(false);assert.equal(document.querySelector('#app').classList.contains('data-waiting'),true);assert.equal(view.cameraVisible,false);assert.equal(view.playing,false);assert.match(document.querySelector('#frame-source').textContent,/미수신/);dom.window.close();
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
