import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {JSDOM} from 'jsdom';

test('glass preview reveals the dashboard only after its light theme loads',async()=>{
 const html=await readFile(new URL('../glass-preview/index.html',import.meta.url),'utf8');
 const dom=new JSDOM(html,{url:'http://127.0.0.1:8000/glass-preview/',runScripts:'dangerously'});
 await new Promise(resolve=>setTimeout(resolve,0));
 const frame=dom.window.document.querySelector('#preview');
 assert.match(dom.window.document.querySelector('style').textContent,/iframe\{[^}]*visibility:hidden/);
 assert.equal(frame.classList.contains('ready'),false);
 const inner=new JSDOM('<div id="app"></div>',{url:'http://127.0.0.1:8000/static/index.html'});
 Object.defineProperty(frame,'contentDocument',{configurable:true,value:inner.window.document});
 frame.dispatchEvent(new dom.window.Event('load'));
 const theme=frame.contentDocument.querySelector('#glass-theme');
 assert.ok(theme,'the light stylesheet is attached to the inner page');
 assert.equal(frame.classList.contains('ready'),false,'the old dark page must remain hidden while CSS is pending');
 theme.dispatchEvent(new dom.window.Event('load'));
 assert.equal(frame.classList.contains('ready'),true);
 inner.window.close();dom.window.close();
});
