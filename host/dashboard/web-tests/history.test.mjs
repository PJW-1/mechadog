// 사건 이력 화면 (WBS 4.6.7 · ADR-46) — 서버 `/api/history/*`(4.6.6)를 가짜 fetch 로 대신한다.
import test from 'node:test';
import assert from 'node:assert/strict';
import {JSDOM} from 'jsdom';
import {Operations} from '../static/operations.js';
import {OperationalPanels} from '../static/panels.js';
import {HistoryLink} from '../static/history-link.js';

const incident=(id,extra={})=>({incident_id:id,mission_id:'mechdog-02-1000',robot_id:'mechdog-02',zone_id:'A',occurred_at:1759900000000,event_type:'PPE_VIOLATION',mode:'factory',state:'OBSERVE',escalation_level:'L2',confidence:0.91,snapshot_path:'e1/snapshot.jpg',blackbox_entry:'e1',detail:{ppe_required:['helmet'],ppe_missing:['helmet'],zone:'A'},reviewed:false,resolution:'',reviewed_at:null,...extra});
const ITEMS=[incident('mechdog-02_e1'),incident('mechdog-02_e2',{event_type:'hazard_notice',zone_id:'C',blackbox_entry:'e2',snapshot_path:'e2/snapshot.jpg',detail:{zone:'C'}}),incident('1759900000500_auth_granted_ab12cd34',{event_type:'auth_granted',zone_id:null,snapshot_path:null,blackbox_entry:null,detail:{}})];
const RUNS=[{mission_id:'mechdog-02-1000',robot_id:'mechdog-02',mode:'factory',started_at:1759899000000,ended_at:1759901000000,result:'stopped',zones_visited:['A','C'],incident_count:2,stop_reason:'patrol_stop'}];

function fakeServer({status=200,error=null}={}){
 const calls=[];
 const fetch=async(url,init={})=>{
  const u=new URL(url,'http://127.0.0.1:8000');calls.push({url:u,method:init.method||'GET',body:init.body?JSON.parse(init.body):null});
  const reply=(body,code=200)=>({ok:code>=200&&code<300,status:code,json:async()=>body});
  if(error)return reply({error},status);
  const path=u.pathname;
  if(path==='/api/history/incidents')return reply({items:ITEMS,total:ITEMS.length,limit:100,offset:0});
  if(path==='/api/history/runs')return reply({items:RUNS,total:1,limit:50,offset:0});
  if(path==='/api/history/robots')return reply({items:[{robot_id:'mechdog-02',display_name:'mechdog-02',active:true}]});
  if(path==='/api/history/zones')return reply({items:[{zone_id:'A',zone_name:'위험구역 A',active:true},{zone_id:'C',zone_name:'C',active:true}]});
  const review=path.match(/^\/api\/history\/incidents\/([^/]+)\/review$/);
  if(review){const body=JSON.parse(init.body);return reply({...ITEMS.find(i=>i.incident_id===decodeURIComponent(review[1])),...body,reviewed_at:body.reviewed?1759902000000:null})}
  const one=path.match(/^\/api\/history\/incidents\/([^/]+)$/);
  if(one){const found=ITEMS.find(i=>i.incident_id===decodeURIComponent(one[1]));return found?reply(found):reply({error:'incident_not_found'},404)}
  return reply({error:'not_found'},404);
 };
 return {fetch,calls,last:path=>calls.filter(c=>c.url.pathname===path).at(-1)};
}
function setup(server=fakeServer()){
 const dom=new JSDOM('<h2 id="title"></h2><div id="content"></div>',{url:'http://127.0.0.1:8000'}),document=dom.window.document;
 const store=new Operations({storage:dom.window.localStorage}),messages=[],navigation=[];
 const panels=new OperationalPanels({store,container:document.querySelector('#content'),title:document.querySelector('#title'),onNavigate:view=>{navigation.push(view);panels.render(view)},onToast:message=>messages.push(message),history:{baseUrl:'http://127.0.0.1:8000',fetch:server.fetch,snapshotBase:id=>'http://127.0.0.1:8000/robots/'+id},document});
 store.subscribe(reason=>panels.refresh(reason));
 return {dom,document,store,panels,messages,navigation,server};
}
const settle=()=>new Promise(resolve=>setTimeout(resolve,0));
const open=async ctx=>{ctx.panels.render('history');await ctx.panels.historyLoad;await settle()};
const chooseFilter=(document,name,index)=>{const trigger=document.querySelector(`[data-filter="${name}"]`);trigger.click();trigger.parentElement.querySelectorAll('.op-choice-option')[index].click()};
const change=(dom,element,value,type='change')=>{element.value=value;element.dispatchEvent(new dom.window.Event(type,{bubbles:true}))};
const rows=document=>[...document.querySelectorAll('.op-event-row')];

test('history view lists stored incidents and server filters go into the query',async()=>{
 const ctx=setup();await open(ctx);const {dom,document,server}=ctx;
 assert.equal(document.querySelector('#title').textContent,'사건 이력');
 assert.equal(rows(document).length,3);
 assert.match(document.querySelector('.op-result-count').textContent,/3건/);
 assert.equal(server.last('/api/history/incidents').url.searchParams.get('event'),null);
 chooseFilter(document,'이력 사건 종류',[...document.querySelectorAll('[data-filter="이력 사건 종류"] + .op-choice-list .op-choice-option')].findIndex(o=>o.textContent.includes('보호구 미착용')));
 await ctx.panels.historyLoad;assert.equal(server.last('/api/history/incidents').url.searchParams.get('event'),'PPE_VIOLATION');
 chooseFilter(document,'이력 대응 단계',3);await ctx.panels.historyLoad;assert.equal(server.last('/api/history/incidents').url.searchParams.get('escalation'),'L2');
 chooseFilter(document,'이력 장치',1);await ctx.panels.historyLoad;assert.equal(server.last('/api/history/incidents').url.searchParams.get('robot'),'mechdog-02');
 chooseFilter(document,'이력 구역',1);await ctx.panels.historyLoad;assert.equal(server.last('/api/history/incidents').url.searchParams.get('zone'),'A');
 chooseFilter(document,'이력 검토 상태',2);await ctx.panels.historyLoad;assert.equal(server.last('/api/history/incidents').url.searchParams.get('reviewed'),'true');
 change(dom,document.querySelector('[name="이력 시작일"]'),'2026-10-08');await ctx.panels.historyLoad;
 const since=Number(server.last('/api/history/incidents').url.searchParams.get('since'));assert.equal(since,new Date('2026-10-08T00:00:00').getTime());
 change(dom,document.querySelector('[name="이력 종료일"]'),'2026-10-08');await ctx.panels.historyLoad;
 const until=Number(server.last('/api/history/incidents').url.searchParams.get('until'));assert.equal(until,new Date('2026-10-08T23:59:59.999').getTime());
 // 검색어는 받은 목록 안에서 거른다 — 서버에 다시 묻지 않는다.
 const asked=server.calls.length,search=document.querySelector('[name="이력 검색"]');search.focus();change(dom,search,'화기','input');
 assert.equal(server.calls.length,asked);assert.equal(document.activeElement,search);assert.equal(rows(document).length,1);assert.match(rows(document)[0].textContent,/화기 위험물/);
});

test('selecting a stored incident opens the shared detail view with its snapshot',async()=>{
 const ctx=setup();await open(ctx);const {document}=ctx;
 rows(document)[1].click();
 assert.match(document.querySelector('.op-detail-title').textContent,/화기 위험물 경고/);
 const image=document.querySelector('.op-event-detail .op-evidence-image img');
 assert.equal(image.getAttribute('src'),'http://127.0.0.1:8000/robots/mechdog-02/events/e2/snapshot.jpg');
 assert.match(document.querySelector('.op-event-detail').textContent,/서버 이력 기록/);
 rows(document)[2].click();
 assert.ok(document.querySelector('.op-evidence-empty'),'사진 없는 사건은 빈 증거 칸을 보인다');
});

test('mission runs come from the server and narrow incidents to one run',async()=>{
 const ctx=setup();await open(ctx);const {document,server}=ctx;
 const table=document.querySelector('[data-history-runs]');
 assert.match(table.textContent,/mechdog-02/);assert.match(table.textContent,/A → C/);assert.match(table.textContent,/순찰 정지/);assert.match(table.textContent,/2건/);
 [...table.querySelectorAll('button')].find(b=>b.textContent==='이 판의 사건').click();await ctx.panels.historyLoad;
 assert.equal(server.last('/api/history/incidents').url.searchParams.get('mission'),'mechdog-02-1000');
 assert.match(document.querySelector('.op-result-count').textContent,/순찰 판/);
});

test('review is saved on the server and the returned state is shown',async()=>{
 const ctx=setup();await open(ctx);const {dom,document,server,messages}=ctx;
 const text=document.querySelector('.op-event-detail').textContent;
 assert.doesNotMatch(text,/서버에 저장되지 않습니다/);
 change(dom,document.querySelector('[name="이력 검토 결과"]'),'reviewed');
 change(dom,document.querySelector('[name="이력 처리 메모"]'),'현장 확인 · 안전모 지급','input');
 document.querySelector('.op-review-form').dispatchEvent(new dom.window.Event('submit',{bubbles:true,cancelable:true}));
 await ctx.panels.historySave;await settle();
 const post=server.last('/api/history/incidents/mechdog-02_e1/review');
 assert.equal(post.method,'POST');assert.deepEqual(post.body,{reviewed:true,resolution:'현장 확인 · 안전모 지급'});
 assert.match(messages.at(-1),/서버에 검토를 저장/);
 assert.match(document.querySelector('.op-event-detail').textContent,/서버 저장됨/);
 assert.match(rows(document)[0].textContent,/검토 완료/);
});

test('technician role cannot submit a server review',async()=>{
 const ctx=setup();ctx.store.setRole('technician');await open(ctx);
 assert.equal(ctx.document.querySelector('.op-review-form button[type="submit"]').disabled,true);
});

test('history_disabled shows a clear notice instead of failing',async()=>{
 const ctx=setup(fakeServer({status:503,error:'history_disabled'}));await open(ctx);
 const text=ctx.document.querySelector('#content').textContent;
 assert.match(text,/이력 저장이 꺼져 있습니다/);assert.equal(rows(ctx.document).length,0);
});

test('without a dashboard server the history view explains it has nothing to read',()=>{
 const dom=new JSDOM('<h2 id="title"></h2><div id="content"></div>'),document=dom.window.document;
 const panels=new OperationalPanels({store:new Operations(),container:document.querySelector('#content'),title:document.querySelector('#title'),onNavigate:()=>{},onToast:()=>{},document});
 panels.render('history');assert.match(document.querySelector('#content').textContent,/관제 서버에 연결된 화면/);
});

test('live events point to the server history review instead of the old unsaved note',async()=>{
 const ctx=setup();const {document,store,panels,navigation,server}=ctx;
 store.setDemo(false);store.ingestLiveEvent({seq:1,event:'PPE_VIOLATION',ts_ms:1759900000000,entry:'e1',snapshot:'snapshot.jpg',escalation:'L2',state:'OBSERVE',telemetry:{device_id:'mechdog-02'}},'http://127.0.0.1:8000');
 panels.render('events');
 const detail=document.querySelector('.op-event-detail').textContent;
 assert.doesNotMatch(detail,/서버에 저장되지 않습니다/);assert.match(detail,/사건 이력/);
 [...document.querySelectorAll('.op-event-detail button')].find(b=>b.textContent==='사건 이력에서 검토').click();
 await panels.historyLoad;await settle();
 assert.deepEqual(navigation,['history']);
 assert.ok(server.calls.some(c=>c.url.pathname==='/api/history/incidents/mechdog-02_e1'));
 assert.match(document.querySelector('.op-detail-title').textContent,/보호구 미착용/);
 assert.ok(document.querySelector('[name="이력 처리 메모"]'));
});

test('a single server opens the history incident by profile name, not the firmware telemetry id',async()=>{
 // 이력의 robot_id 는 런타임 --device(프로필)다. 사건에 실린 텔레메트리 device_id 는 펌웨어의 MAC 이름이다.
 const ctx=setup();const {document,store,panels,server}=ctx;
 store.setDemo(false);
 store.setTelemetry({state:'live',snapshot:{deviceId:'mechdog-02',state:'OBSERVE',escalation:'L2',stale:false,runtimeStale:false},rateHz:10,lost:0,history:[]});
 store.ingestLiveEvent({seq:2,event:'PPE_VIOLATION',ts_ms:1759900000000,entry:'e1',snapshot:'snapshot.jpg',escalation:'L2',state:'OBSERVE',telemetry:{device_id:'mechdog-3c8a1f333208'}},'http://127.0.0.1:8000');
 panels.render('events');
 [...document.querySelectorAll('.op-event-detail button')].find(b=>b.textContent==='사건 이력에서 검토').click();
 await panels.historyLoad;await settle();
 assert.ok(server.calls.some(c=>c.url.pathname==='/api/history/incidents/mechdog-02_e1'));
 assert.ok(!server.calls.some(c=>c.url.pathname.includes('mechdog-3c8a1f333208')));
});

test('history link turns server refusals into coded errors',async()=>{
 const link=new HistoryLink({fetch:fakeServer({status:404,error:'incident_not_found'}).fetch});
 await assert.rejects(link.incident('x'),error=>error.code==='incident_not_found'&&error.status===404&&/이력에 없는 사건/.test(error.message));
 const offline=new HistoryLink({fetch:async()=>{throw new Error('down')}});
 await assert.rejects(offline.runs(),error=>error.code==='network');
});
