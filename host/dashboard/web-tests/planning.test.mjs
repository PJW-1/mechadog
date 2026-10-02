import test from 'node:test';
import assert from 'node:assert/strict';
import {JSDOM} from 'jsdom';
import {Operations} from '../static/operations.js';
import {OperationalPanels} from '../static/panels.js';
import {mapPoint,worldPoint} from '../static/planning-panel.js';

const zone=id=>({id,name:id,x:0,y:0,yaw_deg:null,helmet:true,vest:true,hazard:false,note:''});
const snapshot=(revision='v1')=>({device:'mechdog-02',revision,saved:{zones:[zone('A'),zone('B')],random_after_first_cycle:false},active:{zones:[zone('A'),zone('B')],random_after_first_cycle:false},pending_restart:false,map:{available:false},arrival_radius_m:.3});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function setup(link=null){
 const dom=new JSDOM('<h1></h1><div id="panel"></div>',{url:'http://127.0.0.1:8003'}),document=dom.window.document;
 const store=new Operations({storage:dom.window.localStorage});store.demo=false;store.link=link;
 const p=new OperationalPanels({store,container:document.querySelector('#panel'),title:document.querySelector('h1'),document,onToast:()=>{},onNavigate:()=>{}});
 store.subscribe(reason=>p.refresh(reason));p.render('zones');
 return {dom,document,store,p};
}
function input(ctx,name,value){const field=ctx.document.querySelector(`[name="${name}"]`);field.value=value;field.dispatchEvent(new ctx.dom.window.Event('input',{bubbles:true}));return field}

test('coordinates map world positive Y upward and invert exactly',()=>{
 const extent=[-4,6,-2,3];assert.deepEqual(mapPoint(extent,-4,3),[0,0]);assert.deepEqual(mapPoint(extent,6,-2),[1000,1000]);
 assert.deepEqual(worldPoint(extent,.25,.2),[-1.5,2]);
});
test('offline drafts survive navigation, incoming events and local reload without claiming server save',()=>{
 const ctx=setup();const field=input(ctx,'구역 이름','입구');field.focus();
 for(const reason of ['telemetry','event','policy','device','command'])ctx.p.refresh(reason);
 assert.equal(ctx.document.activeElement,field);assert.equal(field.value,'입구');assert.equal(ctx.document.querySelector('[data-plan-save]').disabled,true);
 ctx.p.render('settings');ctx.p.render('zones');assert.equal(ctx.document.querySelector('[name="구역 이름"]').value,'입구');
 ctx.p.planning.sessions.clear();ctx.p.render('zones');assert.equal(ctx.document.querySelector('[name="구역 이름"]').value,'입구');
 ctx.dom.window.close();
});
test('reorder and PPE checkboxes save the actual ordered plan and show pending restart',async()=>{
 const sent=[],link={baseUrl:'',get:async()=>snapshot(),post:async(path,body)=>{sent.push([path,body]);return {...snapshot('v2'),saved:{zones:body.zones,random_after_first_cycle:body.random_after_first_cycle},pending_restart:true}}};
 const ctx=setup(link);await tick();ctx.document.querySelector('[aria-label="B 순서 위로"]').click();
 ctx.document.querySelector('[data-plan-zone="B"]').click();const helmet=ctx.document.querySelector('[name="helmet"]');helmet.checked=false;helmet.dispatchEvent(new ctx.dom.window.Event('change'));
 await ctx.p.planning.save();assert.equal(sent[0][0],'/api/planning');assert.deepEqual(sent[0][1].zones.map(z=>z.id),['B','A']);assert.equal(sent[0][1].zones[0].helmet,false);assert.equal(sent[0][1].revision,'v1');
 assert.match(ctx.document.querySelector('.plan-state').textContent,/재시작 후 적용/);ctx.dom.window.close();
});
test('failed save keeps the draft and old revision, including after reload',async()=>{
 const calls=[],link={baseUrl:'',get:async()=>snapshot(),post:async(_,body)=>{calls.push(body);throw new Error('다른 화면에서 변경 (HTTP 409)')}};
 const ctx=setup(link);await tick();input(ctx,'구역 이름','현장 초안');await ctx.p.planning.save();assert.equal(ctx.p.planning.current.dirty,true);assert.match(ctx.document.querySelector('#panel').textContent,/작성 내용은 유지/);
 ctx.p.planning.sessions.clear();link.get=async()=>snapshot('v3');ctx.p.render('zones');await tick();await ctx.p.planning.save();
 assert.equal(calls.at(-1).revision,'v1');assert.equal(calls.at(-1).zones[0].name,'현장 초안');ctx.dom.window.close();
});
test('late response for another robot cannot repaint the selected plan',async()=>{
 let resolve;const first={baseUrl:'/robots/a',get:()=>new Promise(r=>{resolve=r})},ctx=setup(first);
 const second={baseUrl:'/robots/b',get:async()=>({...snapshot(),device:'robot-b'})};ctx.store.link=second;ctx.p.refresh('robot');await tick();
 resolve({...snapshot(),device:'robot-a'});await tick();assert.match(ctx.document.querySelector('.plan-header').textContent,/robot-b/);assert.doesNotMatch(ctx.document.querySelector('.plan-header').textContent,/robot-a/);ctx.dom.window.close();
});
test('route preview uses only planning API and is invalidated by changed position',async()=>{
 const requests=[],link={baseUrl:'',get:async()=>({...snapshot(),map:{available:true,extent:[-2,2,-2,2],width:40,height:40}}),post:async(path)=>{requests.push(path);return {segments:[{from:'A',to:'B',reachable:false,points:[],length_m:null}],all_reachable:false,total_m:0,scope:'저장 지도'}}};
 const ctx=setup(link);await tick();await ctx.p.planning.preview();assert.match(ctx.document.querySelector('.plan-route').textContent,/경로 없음/);
 const field=ctx.document.querySelector('[name="X (m)"]');field.value='1';field.dispatchEvent(new ctx.dom.window.Event('input'));assert.equal(ctx.p.planning.current.route,null);assert.deepEqual(requests,['/api/planning/preview']);ctx.dom.window.close();
});

test('invalid numeric edits remain visible and block save across zone switches',async()=>{
 const calls=[],ctx=setup({get:async()=>snapshot(),post:async()=>calls.push('save')});await tick();
 input(ctx,'구역 이름','수정');input(ctx,'X (m)','10001');
 assert.equal(ctx.document.querySelector('[data-plan-save]').disabled,true);await ctx.p.planning.save();assert.deepEqual(calls,[]);
 ctx.document.querySelector('[data-plan-zone="B"]').click();ctx.document.querySelector('[data-plan-zone="A"]').click();
 assert.equal(ctx.document.querySelector('[name="X (m)"]').value,'10001');assert.match(ctx.document.querySelector('.plan-state').textContent,/좌표 확인 필요/);
 input(ctx,'X (m)','1.234');assert.equal(ctx.document.querySelector('[name="X (m)"]').checkValidity(),true);assert.equal(ctx.p.planning.current.draft.zones[0].x,1.234);assert.equal(ctx.document.querySelector('[data-plan-save]').disabled,false);ctx.dom.window.close();
});
test('replacement reload locks editing until the saved plan has arrived',async()=>{
 let finish;const link={get:async()=>snapshot()},ctx=setup(link);await tick();input(ctx,'구역 이름','초안');
 link.get=()=>new Promise(resolve=>{finish=resolve});const pending=ctx.p.planning.load(ctx.p.planning.current,true);
 assert.equal(ctx.document.querySelector('[name="구역 이름"]').disabled,true);assert.equal(ctx.document.querySelector('[data-plan-add]').disabled,true);
 finish(snapshot('v2'));await pending;assert.equal(ctx.document.querySelector('[name="구역 이름"]').disabled,false);ctx.dom.window.close();
});
test('reorder preserves keyboard focus and falls back to its zone at list boundaries',async()=>{
 const ctx=setup({get:async()=>snapshot()});await tick();
 const move=ctx.document.querySelector('[aria-label="B 순서 위로"]');move.focus();move.click();
 assert.equal(ctx.document.activeElement.dataset.planZone,'B');
 const select=ctx.document.querySelector('[data-plan-zone="A"]');select.focus();select.click();assert.equal(ctx.document.activeElement.dataset.planZone,'A');ctx.dom.window.close();
});
