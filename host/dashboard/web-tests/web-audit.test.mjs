import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {JSDOM} from 'jsdom';
import {Operations} from '../static/operations.js';
import {OperationalPanels} from '../static/panels.js';

const flush=async()=>{for(let i=0;i<6;i++)await Promise.resolve()};
const status=robot=>({robot,mode:'standby',activity:'대기',say_queue:0,events:[]});
function panel(t,voiceLink=null){
 const dom=new JSDOM('<h1 id="title"></h1><div id="content"></div>',{url:'http://localhost/'});
 const document=dom.window.document;
 const panels=new OperationalPanels({store:new Operations({storage:null}),document,voiceLink,
  container:document.querySelector('#content'),title:document.querySelector('#title'),onToast:()=>{},onNavigate:()=>{}});
 t.after(()=>{panels.clearVoicePoll();dom.window.close()});
 return {panels,document,dom};
}

test('Tab leaves the event filter and closes its popup; arrows retain internal focus',t=>{
 const {panels,document,dom}=panel(t);panels.render('events');
 const trigger=document.querySelector('[data-filter="사건 유형"]');trigger.click();
 const options=[...trigger.parentElement.querySelectorAll('[role="option"]')];
 assert.ok(options.every(option=>option.tabIndex===-1),'options must not add a Tab stop for every value');
 options[0].dispatchEvent(new dom.window.KeyboardEvent('keydown',{key:'ArrowDown',bubbles:true}));
 assert.equal(document.activeElement,options[1]);assert.equal(trigger.getAttribute('aria-expanded'),'true');
 document.querySelector('[data-filter="검토 상태 필터"]').focus();
 assert.equal(trigger.getAttribute('aria-expanded'),'false');
 assert.equal(trigger.parentElement.querySelector('[role="listbox"]').hidden,true);
});

test('voice status retries after a temporary failure and recovers without reopening the page',async t=>{
 t.mock.timers.enable({apis:['setTimeout','setInterval']});
 let calls=0;
 const {panels,document}=panel(t,{status:async()=>{if(++calls===1)throw new Error('연결 끊김');return status('recovered')},phrases:async()=>[],scenarios:async()=>[]});
 panels.render('voice');await flush();
 assert.match(document.querySelector('#content').textContent,/자동 재연결 중/);
 t.mock.timers.tick(2000);await flush();
 assert.equal(calls,2);assert.match(panels.voiceStatusEl.textContent,/recovered/);
});

test('a slow voice status request never overlaps another poll',async t=>{
 t.mock.timers.enable({apis:['setTimeout','setInterval']});
 let calls=0,resolve;
 const {panels}=panel(t,{status:()=>{calls++;return new Promise(done=>{resolve=done})},phrases:async()=>[],scenarios:async()=>[]});
 panels.render('voice');await flush();t.mock.timers.tick(6000);panels.pollVoice();
 assert.equal(calls,1);resolve(status('current'));await flush();
});

for(const oldFails of [false,true])test('late voice '+(oldFails?'failure':'response')+' cannot overwrite a reopened panel',async t=>{
 t.mock.timers.enable({apis:['setTimeout','setInterval']});
 const requests=[];
 const {panels}=panel(t,{status:()=>new Promise((resolve,reject)=>requests.push({resolve,reject})),phrases:async()=>[],scenarios:async()=>[]});
 panels.render('voice');panels.render('events');panels.render('voice');
 requests[1].resolve(status('new-session'));await flush();
 if(oldFails)requests[0].reject(new Error('old-failure'));else requests[0].resolve(status('old-session'));
 await flush();assert.match(panels.voiceStatusEl.textContent,/new-session/);
 assert.doesNotMatch(panels.voiceStatusEl.textContent,/old-session|old-failure/);
 assert.ok(panels.voiceTimer,'the new panel must continue polling');
});

const shell=await readFile(new URL('../glass-preview/index.html',import.meta.url),'utf8');
test('disposing a voice panel prevents its pending response from restarting polling',async t=>{
 t.mock.timers.enable({apis:['setTimeout','setInterval']});
 let calls=0,resolve;
 const {panels}=panel(t,{status:()=>{calls++;return new Promise(done=>{resolve=done})},phrases:async()=>[],scenarios:async()=>[]});
 panels.render('voice');panels.dispose();resolve(status('closed'));await flush();
 t.mock.timers.tick(10000);await flush();assert.equal(calls,1);assert.equal(panels.voiceTimer,null);
});
for(const page of ['records','zones','devices','voice','settings'])test('glass wrapper reopens the '+page+' URL',async t=>{
 const dom=new JSDOM(shell,{url:'http://localhost:8000/glass-preview/?view='+page,runScripts:'dangerously'});
 t.after(()=>dom.window.close());await flush();
 assert.equal(new URL(dom.window.document.querySelector('#preview').src).hash,'#'+page);
});

test('inner navigation updates the wrapper URL without creating another history step',async t=>{
 const dom=new JSDOM(shell,{url:'http://localhost:8000/glass-preview/?rail=open',runScripts:'dangerously'});
 t.after(()=>dom.window.close());await flush();
 const frame=dom.window.document.querySelector('#preview');
 const inner=new JSDOM('<div id="app"></div>',{url:'http://localhost:8000/static/index.html#events'});
 t.after(()=>inner.window.close());
 Object.defineProperties(frame,{contentDocument:{configurable:true,value:inner.window.document},contentWindow:{configurable:true,value:inner.window}});
 const length=dom.window.history.length;frame.dispatchEvent(new dom.window.Event('load'));
 assert.equal(new URL(dom.window.location).searchParams.get('view'),'events');
 assert.equal(new URL(dom.window.location).searchParams.get('rail'),'open');
 inner.window.history.replaceState(null,'','#settings');inner.window.dispatchEvent(new inner.window.HashChangeEvent('hashchange'));
 assert.equal(new URL(dom.window.location).searchParams.get('view'),'settings');
 assert.equal(dom.window.history.length,length);
});
