import test from 'node:test';
import assert from 'node:assert/strict';
import {JSDOM} from 'jsdom';
import {LiveMap,applyAffine,describeNav} from '../static/live-map.js';

const meta={width:200,height:100,resolution_m:0.0125,patrol_to_px:[[-80,0,100],[0,80,50]],px_to_patrol:[[-0.0125,0,1.25],[0,0.0125,-0.625]],zones:[{id:'A',x:0.5,y:0,color:'#aabbcc'}]};

test('affine helpers round trip and describe trust honestly',()=>{
 const [u,v]=applyAffine(meta.patrol_to_px,0.5,-0.25);
 assert.deepEqual(applyAffine(meta.px_to_patrol,u,v).map(n=>+n.toFixed(6)),[0.5,-0.25]);
 assert.match(describeNav({available:true,stale:false,verified:false,seeded:false}),/위치 미확인 — 이동 불가/);
 assert.match(describeNav({available:true,stale:true,verified:true}),/위치 상실/);
 assert.match(describeNav({available:true,stale:false,verified:true,zone:'B',goal:[1,2]}),/위치 확인됨 · 구역 B · 찍은 곳으로 이동 중/);
 assert.match(describeNav({available:true,verified:true,holding_goal:true}),/찍은 곳 도착/);
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
