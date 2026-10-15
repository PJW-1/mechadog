import test from 'node:test';
import assert from 'node:assert/strict';
import {JSDOM} from 'jsdom';
import {ControlMinimap} from '../static/control-minimap.js';
import {describeEvidence,eventCategory,EVENT_TITLES} from '../static/operations.js';
import {describeNav} from '../static/live-map.js';

test('blockage evidence exposes metres, severity, frame availability and episode',()=>{
  const {rows}=describeEvidence('zone_skipped',{judgement:{zone:'A',x:2.8,y:3,blockage_id:7,severity:'medium',camera_available:false}});
  assert.ok(rows.some(([k,v])=>k==='장애물 위치 (순찰 m)'&&v==='2.8, 3'));
  assert.ok(rows.some(([k,v])=>k==='심각도'&&v==='중간'));
  assert.ok(rows.some(([k,v])=>k==='사진'&&v==='카메라 프레임 없음'));
  assert.equal(eventCategory('obstacle_detour'),'SAFETY');
  assert.match(EVENT_TITLES.obstacle_detour,/치워 주세요/);
  assert.match(describeNav({available:true,pose:[1,2,0],verified:true,blockage:{waiting:true,skipped_zones:['A']}}),/순찰 불가.*건너뜀: A/);
});

test('minimap renders exact skipped zone runs and red blockage on the same affine frame',async()=>{
  const dom=new JSDOM('<main></main>'),document=dom.window.document;
  const meta={width:200,height:100,patrol_to_px:[[10,0,0],[0,-10,100]],zones:[{id:'A',x:2,y:3,runs:[[2,3,.5,.05]]}]};
  let nav={available:true,pose:[1,2,0],verified:true,blockage:{skipped_zones:['A'],obstacles:[{id:7,x:2.8,y:3,points:[[2.8,3]]}]}};
  const link={baseUrl:'',get:async path=>path==='/api/map/meta'?meta:nav};
  const map=new ControlMinimap({document,getLink:()=>link,onOpen:()=>{},setInterval:()=>0});
  document.querySelector('main').append(map.root);await map.tick();
  const area=map.canvas.querySelector('[data-skipped-zone="A"]');
  assert.equal(area.getAttribute('points'),'20,70 25,70 25,69.5 20,69.5');
  assert.equal(area.getAttribute('fill'),'#9ca3af');
  const obstacle=map.canvas.querySelector('[data-blockage-id="7"]');
  assert.equal(obstacle.getAttribute('cx'),'28');assert.equal(obstacle.getAttribute('cy'),'70');
  assert.equal(obstacle.getAttribute('fill'),'#dc2626');
  nav={...nav,blockage:{skipped_zones:[],obstacles:[]}};await map.tick();
  assert.equal(map.canvas.querySelector('[data-skipped-zone]'),null);
  assert.equal(map.canvas.querySelector('[data-blockage-id]'),null);
  dom.window.close();
});
