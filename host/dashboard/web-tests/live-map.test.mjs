import test from 'node:test';
import assert from 'node:assert/strict';
import {JSDOM} from 'jsdom';
import {LiveMap,applyAffine,describeNav} from '../static/live-map.js';

const meta={width:200,height:100,resolution_m:0.0125,patrol_to_px:[[-80,0,100],[0,80,50]],px_to_patrol:[[-0.0125,0,1.25],[0,0.0125,-0.625]],zones:[{id:'A',x:0.5,y:0,color:'#aabbcc'}]};

test('affine helpers round trip and describe trust honestly',()=>{
 const [u,v]=applyAffine(meta.patrol_to_px,0.5,-0.25);
 assert.deepEqual(applyAffine(meta.px_to_patrol,u,v).map(n=>+n.toFixed(6)),[0.5,-0.25]);
 assert.match(describeNav({available:true,pose:[0,0,0],stale:false,verified:false,seeded:false}),/위치 미확인 — 이동 불가/);
 assert.match(describeNav({available:true,pose:[0,0,0],stale:true,verified:true}),/위치 상실/);
 assert.match(describeNav({available:true,pose:[0,0,0],stale:false,verified:true,zone:'B',goal:[1,2]}),/위치 확인됨 · 구역 B · 찍은 곳으로 이동 중/);
 assert.match(describeNav({available:true,pose:[0,0,0],verified:true,holding_goal:true}),/찍은 곳 도착/);
});

test('clicking the map converts screen pixels to patrol metres even when scaled by CSS',async()=>{
 const dom=new JSDOM('<div id="host"></div>',{url:'http://127.0.0.1:8000'});const document=dom.window.document;
 const picks=[],asked=[];
 const link={baseUrl:'',get:async path=>{asked.push(path);return path==='/api/map/meta'?meta:{available:true,pose:[0,0,0],verified:true,stale:false}}};
 const map=new LiveMap({document,getLink:()=>link,onPick:(x,y,where)=>picks.push([x,y,where]),setInterval:()=>0});
 document.querySelector('#host').append(map.root);
 await map.tick();
 assert.deepEqual(asked,['/api/map/meta','/api/nav']);
 assert.match(map.status.textContent,/위치 확인됨/);
 map.canvas.getBoundingClientRect=()=>({left:10,top:20,width:400,height:200});
 // 화면 (10+2·60, 20+2·50) = 그림 (60, 50) → 순찰 (0.5, 0)
 map.canvas.dispatchEvent(new dom.window.MouseEvent('click',{clientX:130,clientY:120,bubbles:true}));
 assert.equal(picks.length,1);
 assert.ok(Math.abs(picks[0][0]-0.5)<1e-9&&Math.abs(picks[0][1])<1e-9);
 assert.equal(picks[0][2],'구역 A 근처');
});

test('a detached map does not poll the server',async()=>{
 const dom=new JSDOM('<div></div>');const asked=[];
 const map=new LiveMap({document:dom.window.document,getLink:()=>({baseUrl:'',get:async p=>{asked.push(p);return {}}}),onPick:()=>{},setInterval:()=>0});
 await map.tick();
 assert.deepEqual(asked,[]);
});

test('switching robots drops the old map and late replies before any click can send a goal',async()=>{
 const dom=new JSDOM('<div id="host"></div>',{url:'http://127.0.0.1:8000'});const document=dom.window.document;
 const picks=[];let release;
 const metaB={...meta,px_to_patrol:[[1,0,0],[0,1,0]]};
 const linkA={baseUrl:'',get:async path=>path==='/api/map/meta'?meta:new Promise(r=>{release=()=>r({available:true,pose:[0,0,0],verified:true})})};
 const linkB={baseUrl:'',get:async path=>path==='/api/map/meta'?metaB:{available:true,pose:[5,5,0],verified:true}};
 let current=linkA;
 const map=new LiveMap({document,getLink:()=>current,onPick:(x,y)=>picks.push([x,y]),setInterval:()=>0});
 document.querySelector('#host').append(map.root);
 const slow=map.tick();await new Promise(r=>setTimeout(r,0));
 current=linkB;await map.tick();
 release();await slow;
 assert.equal(map.meta,metaB,'새 로봇의 지도');assert.deepEqual(map.nav.pose,[5,5,0],'옛 로봇의 늦은 응답은 버린다');
 map.canvas.getBoundingClientRect=()=>({left:0,top:0,width:200,height:100});
 current=linkA;map.canvas.dispatchEvent(new dom.window.MouseEvent('click',{clientX:10,clientY:10,bubbles:true}));
 assert.deepEqual(picks,[],'지도와 위치가 다른 로봇 것이면 찍지 않는다');
});

test('rejections and blocked holds are told, not shown as arrival',()=>{
 assert.match(describeNav({available:true,pose:[0,0,0],verified:true,goal_feedback:{accepted:false,detail:'지금 자리에서 그곳으로 가는 길이 없다 (goal_unreachable)'}}),/이동 거절 — 지금 자리에서 그곳으로 가는 길이 없다/);
 assert.match(describeNav({available:true,pose:[0,0,0],verified:true,holding_goal:true,goal_hold_reason:'blocked'}),/목표 도달 불가 · 이동 종료/);
 assert.doesNotMatch(describeNav({available:true,pose:[0,0,0],verified:true,holding_goal:true,goal_hold_reason:'blocked'}),/도착/);
 assert.match(describeNav({available:true,starting:true}),/측위 시작 대기/);
});

test('point hint uses the server radius and affine transform, disappears on verification and robot switch',async()=>{
 const dom=new JSDOM('<div id="host"></div>'),document=dom.window.document,lines=[];
 const ctx={setTransform(){},clearRect(){},beginPath(){},arc(){},fill(){},stroke(){},fillText(){},setLineDash(){},closePath(){},moveTo(...p){lines.push(['move',...p])},lineTo(...p){lines.push(['line',...p])}};
 let nav={available:true,pose:[0,0,0],verified:false,point_hint:{x:0,y:0,radius:0.6}};
 let current={baseUrl:'',get:async path=>path==='/api/map/meta'?meta:nav};
 const map=new LiveMap({document,getLink:()=>current,onPick:()=>{},setInterval:()=>0});map.canvas.getContext=()=>ctx;
 document.querySelector('#host').append(map.root);await map.tick();
 assert.deepEqual(lines[0],['move',52,50]);assert.equal(lines.filter(([kind])=>kind==='line').length,66,'원판의 64 선분과 로봇의 2 선분');
 assert.match(map.status.textContent,/알려준 점 주변/);
 lines.length=0;nav={...nav,verified:true};await map.tick();assert.equal(lines.filter(([kind])=>kind==='line').length,2);assert.doesNotMatch(map.status.textContent,/알려준 점 주변/);
 lines.length=0;current={baseUrl:'',get:async path=>path==='/api/map/meta'?meta:{available:true,pose:[0,0,0],verified:false}};await map.tick();
 assert.equal(map.nav.point_hint,undefined);assert.equal(lines.filter(([kind])=>kind==='line').length,2);
});

test('canvas backing store follows device pixels while clicks remain in server coordinates', async () => {
 const dom=new JSDOM('<div id="host"></div>'),document=dom.window.document,transforms=[];
 Object.defineProperty(dom.window,'devicePixelRatio',{value:3});
 const context={setTransform(...args){transforms.push(args)},clearRect(){},drawImage(){},beginPath(){},arc(){},fill(){},stroke(){},fillText(){}};
 const map=new LiveMap({document,getLink:()=>null,onPick:()=>{},setInterval:()=>0});
 map.meta=meta;map.canvas.getContext=()=>context;map.image={};
 map.canvas.getBoundingClientRect=()=>({left:0,top:0,width:400,height:200});
 map.draw();assert.equal(map.canvas.width,1200);assert.equal(map.canvas.height,600);
 assert.deepEqual(transforms.at(-1),[6,0,0,6,0,0]);assert.equal(context.imageSmoothingEnabled,false);
 assert.deepEqual(map.toPatrol(120,100),[.5,0]);map.dispose();dom.window.close();
});

test('zoom and rotation preserve inverse click coordinates and reset restores the view',()=>{
 const dom=new JSDOM('<div></div>');const picks=[];
 const map=new LiveMap({document:dom.window.document,getLink:()=>null,onPick:(...point)=>picks.push(point),setInterval:()=>0});
 map.meta=meta;map.canvas.getBoundingClientRect=()=>({left:10,top:20,width:400,height:200});
 map.zoom(.5);map.orbit(Math.PI/2);
 // map pixel (120,50) rotates about (100,50), then zooms to screen (210,200).
 const point=map.toPatrol(210,200);assert.ok(Math.abs(point[0]+.25)<1e-8);assert.ok(Math.abs(point[1])<1e-8);
 map.resetView();assert.deepEqual(map.toPatrol(250,120),[-.25,0]);
 map.dispose();dom.window.close();
});
