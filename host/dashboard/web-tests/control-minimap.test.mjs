import test from 'node:test';
import assert from 'node:assert/strict';
import {JSDOM} from 'jsdom';
import {ControlMinimap} from '../static/control-minimap.js';
import {readFile} from 'node:fs/promises';

const meta = {width:200,height:100,patrol_to_px:[[10,0,0],[0,-10,100]],zones:[{id:'A',x:2,y:3,name:'작업방'}]};
const nav = {available:true,pose:[2,3,45],verified:true,zone:'A',path:[[3,3],[4,4]],route:{id:'r1',name:'현재 순찰',points:[{x:2,y:3},{x:5,y:3}]}};
test('minimap fits the native pixel frame without stretching and reserves a large observation area', async () => {
  const dom = new JSDOM('<main></main>'), document = dom.window.document;
  const link = {get:async p=>p==='/api/map/meta'?meta:nav};
  const map = new ControlMinimap({document,getLink:()=>link,onOpen:()=>{},setInterval:()=>0});
  document.querySelector('main').append(map.root); await map.tick();
  assert.equal(map.canvas.getAttribute('viewBox'),'0 0 200 100');
  assert.equal(map.canvas.getAttribute('preserveAspectRatio'),'xMidYMid meet');
  assert.equal(map.canvas.style.aspectRatio,'200/100');
  assert.equal(map.canvas.querySelector('image').getAttribute('width'),'200');
  assert.equal(map.canvas.querySelector('image').getAttribute('height'),'100');
  assert.equal(map.canvas.querySelector('.minimap-zone').getAttribute('cx'),'20');
  const css = await readFile(new URL('../static/panels.css',import.meta.url),'utf8');
  const styles = new JSDOM(`<style>${css}</style><section class="control-minimap"><svg></svg></section>`);
  const computed = styles.window.getComputedStyle(styles.window.document.querySelector('svg'));
  assert.equal(computed.minHeight,'320px'); assert.equal(computed.height,'auto');
  assert.match(css,/grid-template-areas:'camera controls' 'minimap controls'/);
  assert.match(css,/grid-template-areas:'camera' 'minimap' 'controls'/);
  styles.window.close(); dom.window.close();
});
test('10Hz updates only the overlay, retains raster and metadata, and never commands on click', async () => {
  const dom=new JSDOM('<main></main>'), document=dom.window.document, calls=[];let open=0,period;
  const link={baseUrl:'',get:async p=>{calls.push(p);return p==='/api/map/meta'?meta:nav;}};
  const map=new ControlMinimap({document,getLink:()=>link,onOpen:()=>open++,setInterval:(_,ms)=>{period=ms;return 0;}});document.querySelector('main').append(map.root);
  await map.tick(); const image=map.canvas.querySelector('image'), marker=map.canvas.querySelector('polygon').getAttribute('points');
  assert.equal(period,100); assert.match(map.root.textContent,/작업방/); assert.match(map.root.textContent,/현재 순찰/); assert.ok(map.canvas.querySelector('.minimap-path')); assert.ok(map.canvas.querySelector('.minimap-route'));
  nav.pose=[4,4,90]; for(let i=0;i<10;i++)await map.tick();
  assert.equal(map.canvas.querySelector('image'),image); assert.equal(calls.filter(p=>p==='/api/map/meta').length,1); assert.equal(calls.filter(p=>p==='/api/nav').length,11); assert.notEqual(map.canvas.querySelector('polygon').getAttribute('points'),marker);
  map.canvas.dispatchEvent(new dom.window.MouseEvent('click',{bubbles:true}));assert.equal(open,0);map.root.querySelector('button').click();assert.equal(open,1);assert.ok(calls.every(p=>['/api/nav','/api/map/meta'].includes(p)));
  map.root.remove();await map.tick();assert.equal(calls.length,12);dom.window.close();
});
test('late responses from another robot stay out of the current minimap; stale and failures are visible',async()=>{
  const dom=new JSDOM('<main></main>'),document=dom.window.document;let release;
  const a={baseUrl:'/a',get:async p=>p==='/api/map/meta'?meta:new Promise(r=>release=r)};
  const b={baseUrl:'/b',get:async p=>p==='/api/map/meta'?meta:{...nav,pose:[7,3,0],stale:true}};let link=a;
  const map=new ControlMinimap({document,getLink:()=>link,onOpen:()=>{},setInterval:()=>0});document.querySelector('main').append(map.root);
  const old=map.tick();await new Promise(r=>setTimeout(r,0));link=b;await map.tick();release(nav);await old;
  assert.equal(map.current.nav.pose[0],7);assert.match(map.canvas.querySelector('image').getAttribute('href'),/^\/b/);assert.match(map.status.textContent,/위치 상실/);assert.ok(map.canvas.querySelector('.uncertain'));
  b.get=async()=>{throw Error('offline');};await map.tick();assert.match(map.status.textContent,/연결 끊김/);link=null;await map.tick();assert.equal(map.canvas.hidden,true);assert.equal(map.canvas.querySelector('polygon'),null);dom.window.close();
});
