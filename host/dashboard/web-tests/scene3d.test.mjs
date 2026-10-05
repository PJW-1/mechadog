import test from 'node:test';
import assert from 'node:assert/strict';
import {JSDOM} from 'jsdom';
import {Scene3D, sceneURL} from '../static/scene3d.js';

function fixture() {
  const dom = new JSDOM('<div id="mount"></div><div id="controls"></div><p id="notice"></p>');
  const document = dom.window.document; let now = 0;
  const scene = new Scene3D({document, mount:document.querySelector('#mount'), controls:document.querySelector('#controls'), notice:document.querySelector('#notice'), now:()=>now, setInterval:fn=>{sceneTick=fn; return 0;}});
  function signal(ready, extra = {}) {dom.window.dispatchEvent(new dom.window.MessageEvent('message', {data:{type:'house-sim-status',ready}, origin:'http://127.0.0.1:8791', source:scene.frame?.contentWindow, ...extra}));}
  return {scene, document, signal, tick:ms=>{now=ms;sceneTick();}, dom};
}
let sceneTick;
test('empty URL preserves 2D and rejects executable or credentialed URLs', () => {
  const {scene, document, dom} = fixture(); scene.setUrl('');
  assert.equal(scene.shown, false); assert.equal(document.querySelector('#controls').hidden, true); assert.equal(scene.frame, undefined);
  for (const url of ['javascript:alert(1)', 'file:///tmp/house', 'http://u:p@localhost/']) assert.equal(sceneURL(url), '');
  assert.equal(sceneURL('http://localhost:8791/?view=plan'), 'http://localhost:8791/?view=plan&embed=1'); dom.window.close();
});
test('configured iframe waits for authenticated readiness and toggles 2D/3D', () => {
  const {scene, document, signal, dom} = fixture(); scene.setUrl('http://127.0.0.1:8791/');
  assert.match(scene.frame.src, /embed=1/); assert.equal(scene.shown, false);
  signal(true,{origin:'http://attacker.test'}); signal(true,{source:null}); assert.equal(scene.shown, false);
  signal(true); assert.equal(scene.shown, true);
  document.querySelector('[data-map-view="2d"]').click(); assert.equal(scene.shown, false);
  document.querySelector('[data-map-view="3d"]').click(); assert.equal(scene.shown, true); dom.window.close();
});
test('startup timeout, explicit failure and stopped heartbeat fall back; retry reconnects', () => {
  const {scene, signal, tick, dom} = fixture(); scene.setUrl('http://127.0.0.1:8791/');
  tick(12001); assert.equal(scene.mode,'2d'); assert.match(scene.notice.textContent,/2D 지도로 전환/);
  scene.select('3d'); signal(true); tick(16502); assert.equal(scene.mode,'2d'); assert.equal(scene.frame,null);
  scene.select('3d'); signal(false); assert.equal(scene.mode,'2d');
  scene.select('3d'); signal(true); scene.setActive(false); assert.equal(scene.frame,null); scene.setActive(true); assert.ok(scene.frame); assert.equal(scene.shown,false); signal(true); assert.equal(scene.shown,true); dom.window.close();
});
