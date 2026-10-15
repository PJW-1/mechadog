import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {JSDOM} from 'jsdom';
const source=await readFile(new URL('../glass-preview/motion.js',import.meta.url),'utf8');
function setup(t,reduced=false){
  const dom=new JSDOM('<main id="stage"><aside id="camera-dock"><button id="expand-camera">Expand</button></aside></main><div id="panel-content" data-page="events"></div>',{runScripts:'outside-only'});
  t.after(()=>dom.window.close());
  const win=dom.window,media=new win.EventTarget();media.matches=reduced;win.matchMedia=()=>media;
  const animations=[],frames=new Map();let next=0;
  win.requestAnimationFrame=fn=>{frames.set(++next,fn);return next};win.cancelAnimationFrame=id=>frames.delete(id);
  win.Element.prototype.animate=function(keyframes,options){
    const animation=new win.EventTarget();animation.cancelled=false;
    animation.cancel=()=>{animation.cancelled=true;animation.dispatchEvent(new win.Event('cancel'))};
    animations.push({element:this,keyframes,options,animation});return animation;
  };
  win.eval(source);
  return {win,doc:win.document,media,animations,frames};
}
test('presentation animates a new page once, not telemetry updates or same-page rebuilds',async t=>{
  const {doc,animations}=setup(t),panel=doc.querySelector('#panel-content');
  panel.dataset.page='settings';await Promise.resolve();assert.equal(animations.length,1);
  panel.innerHTML='<p>new status</p>';panel.dataset.page='settings';await Promise.resolve();assert.equal(animations.length,1);
  panel.dataset.page='voice';await Promise.resolve();assert.equal(animations.length,2);assert.equal(animations[0].animation.cancelled,true);
});
test('reduced motion skips spatial animation and a preference change cancels an active animation',async t=>{
  const {doc,media,win,animations}=setup(t),panel=doc.querySelector('#panel-content');
  panel.dataset.page='settings';await Promise.resolve();assert.equal(animations.length,1);
  media.matches=true;media.dispatchEvent(new win.Event('change'));assert.equal(animations[0].animation.cancelled,true);
  panel.dataset.page='voice';await Promise.resolve();assert.equal(animations.length,1);
});
test('dock animation does not delay the click handler and page exit cancels pending work',t=>{
  const {doc,win,frames}=setup(t);let clicked=false;
  const button=doc.querySelector('#expand-camera');button.addEventListener('click',()=>{clicked=true});button.click();
  assert.equal(clicked,true);assert.equal(frames.size,1);
  win.dispatchEvent(new win.Event('pagehide'));assert.equal(frames.size,0);
  button.click();assert.equal(frames.size,0);
});
