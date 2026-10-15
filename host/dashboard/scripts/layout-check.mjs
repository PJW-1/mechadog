// Optional real-browser layout regression check. Only serves local read-only fixtures.
import {createServer} from 'node:http';
import {readFile, mkdir, writeFile} from 'node:fs/promises';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
const root = fileURLToPath(new URL('../static/',import.meta.url));
const output = fileURLToPath(new URL('../../../out/aa/',import.meta.url));
await mkdir(output,{recursive:true});
const server = createServer(async (req,res) => {
  const path = new URL(req.url,'http://localhost').pathname;
  if (req.method !== 'GET') {res.writeHead(405);res.end();return;}
  const json = value => {res.setHeader('Content-Type','application/json');res.end(JSON.stringify(value));};
  if (path==='/health') return json({service:'telemetry',read_only:true,vision_clients:null,capabilities:{voice_path:'/api/voice'},dashboard:{scene3d_url:`http://127.0.0.1:${server.address().port}/sim`}});
  if (path==='/api/map/meta') return json({width:800,height:400,resolution_m:.05,revision:1,patrol_to_px:[[-20,0,600],[0,20,200]],px_to_patrol:[[-.05,0,30],[0,.05,-10]],zones:[{id:'A',name:'시작 구역',x:0,y:0,color:'#597660'},{id:'B',name:'점검 구역',x:10,y:3,color:'#597660'}]});
  if (path==='/api/nav') return json({available:true,pose:[4,1,90],seeded:true,path:[[6,1],[10,3]],route:{id:'mock',name:'목업 순찰',points:[{x:0,y:0},{x:6,y:1},{x:10,y:3}]}});
  if (path==='/api/map.png') {res.setHeader('Content-Type','image/svg+xml');res.end('<svg xmlns="http://www.w3.org/2000/svg" width="800" height="400"><rect width="800" height="400" fill="#eef2ea"/><path d="M40 40H760V360H40Z M400 40V140 M400 260V360" fill="none" stroke="#888" stroke-width="12"/></svg>');return;}
  if (path==='/sim') {res.setHeader('Content-Type','text/html');res.end('<body style="background:#eef2ea;font:24px sans-serif">3D 연결 목업 · iframe 배치 검사<script>setInterval(()=>parent.postMessage({type:"house-sim-status",ready:true},location.origin),250)</script>');return;}
  if (path.startsWith('/api/')) return json({});
  try {const name=path==='/'?'index.html':path.slice(1);if(name.includes('..'))throw Error();let data=await readFile(root+name);if(name==='index.html')data=Buffer.from(data.toString()+inspect);res.setHeader('Content-Type',name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':name.endsWith('.json')?'application/json':'text/html');res.end(data);}catch{res.writeHead(404);res.end();}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const base=`http://127.0.0.1:${server.address().port}`;
const inspect = `<script>setInterval(()=>{try{const r=s=>document.querySelector(s).getBoundingClientRect().toJSON();let data;
if(location.hash==='#missions'&&document.querySelector('.minimap-robot')){const svg=document.querySelector('.control-minimap svg'),label=svg.querySelector('text');data={camera:r('.op-manual-camera'),map:r('.control-minimap'),svg:r('.control-minimap svg'),controls:r('.op-manual-controls'),viewBox:svg.getAttribute('viewBox'),fit:svg.getAttribute('preserveAspectRatio'),labelPixels:parseFloat(getComputedStyle(label).fontSize)*label.getScreenCTM().a,overflow:document.documentElement.scrollWidth>innerWidth};document.querySelector('.control-minimap').scrollIntoView({block:'center'});}
if(location.hash==='#dashboard'&&document.querySelector('#stage').classList.contains('scene3d-active'))data={mount:r('#scene3d-mount'),frame:r('#scene3d-mount iframe'),camera:r('#camera-dock'),collapsed:document.querySelector('#camera-dock').classList.contains('collapsed')};
if(data)document.body.dataset.layout=btoa(JSON.stringify(data));}catch{}},100);</script>`;
// Inject only the measurement hook; use the shipped app, CSS and read-only API fixtures.
const browser=spawn(process.env.LAYOUT_BROWSER || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', ['--headless=new','--disable-gpu','--disable-gpu-sandbox','--no-sandbox','--no-first-run','--no-default-browser-check','--remote-debugging-port=0',`--user-data-dir=${output}browser-profile`,'about:blank'],{windowsHide:true,stdio:['ignore','ignore','pipe']});
let stderr='';browser.stderr.on('data',d=>stderr+=d);
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
let ws;
try {
  for(let i=0;i<100&&!stderr.includes('DevTools listening');i++)await delay(100);
  const endpoint=stderr.match(/DevTools listening on (ws:\/\/\S+)/)?.[1];assert.ok(endpoint,stderr);
  const targets=await fetch(`http://${new URL(endpoint).host}/json`,{signal:AbortSignal.timeout(5000)}).then(r=>r.json());
  ws=new WebSocket(targets.find(t=>t.type==='page').webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{ws.addEventListener('open',resolve,{once:true});ws.addEventListener('error',reject,{once:true})});
  let seq=0;const pending=new Map();
  ws.addEventListener('message',e=>{const m=JSON.parse(e.data),p=pending.get(m.id);if(p){pending.delete(m.id);m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m.result)}});
  const send=(method,params={})=>new Promise((resolve,reject)=>{const id=++seq,timer=setTimeout(()=>reject(Error('CDP timeout '+method)),10000);pending.set(id,{resolve:r=>{clearTimeout(timer);resolve(r)},reject:e=>{clearTimeout(timer);reject(e)}});ws.send(JSON.stringify({id,method,params}))});
  const run=async expression=>{const r=await send('Runtime.evaluate',{expression,returnByValue:true});assert.ok(!r.exceptionDetails,JSON.stringify(r.exceptionDetails));return r.result.value;};
  const evidence=[];
  await send('Page.enable');
  for(const width of [1440,800,390])for(const page of ['missions','dashboard']) {
    await send('Emulation.setDeviceMetricsOverride',{width,height:1000,deviceScaleFactor:1,mobile:false});
    await send('Page.navigate',{url:base+'/#'+page});
    let encoded;
    for(let i=0;i<100;i++){encoded=await run(`(()=>{const value=document.body?.dataset.layout;return location.hash==='#${page}'&&value&&JSON.parse(atob(value)).${page==='missions'?'svg':'mount'}?value:null})()`);if(encoded)break;await delay(100);}
    assert.ok(encoded,'Layout measurement missing');assert.equal(await run('innerWidth'),width);
    await delay(300);encoded=await run('document.body.dataset.layout');
    const data=JSON.parse(Buffer.from(encoded,'base64').toString());
    if(page==='missions') {
      assert.ok(data.svg.height>=320);assert.equal(data.viewBox,'0 0 800 400');assert.equal(data.fit,'xMidYMid meet');assert.equal(data.overflow,false);assert.ok(data.labelPixels>=11.9);
      assert.ok(data.map.y>=data.camera.y+data.camera.height-1);
      for(const rect of [data.camera,data.map,data.svg,data.controls])assert.ok(rect.x>=0&&rect.right<=width+.5,'control observation stays inside the viewport');
      if(width===1440)assert.ok(data.controls.x>=data.map.x+data.map.width);else assert.ok(data.controls.y>=data.map.y+data.map.height-1);
    } else {
      assert.ok(data.collapsed);assert.ok(Math.abs(data.frame.width-data.mount.width)<1);assert.ok(Math.abs(data.frame.height-data.mount.height)<1);
      assert.ok(data.mount.x>=0&&data.mount.right<=width+.5);
      const a=data.mount,b=data.camera;assert.ok(a.x+a.width<=b.x||b.x+b.width<=a.x||a.y+a.height<=b.y||b.y+b.height<=a.y,'3D and camera must not overlap');
    }
    const shot=await send('Page.captureScreenshot',{format:'png'});await writeFile(output+`${page}_${width}.png`,Buffer.from(shot.data,'base64'));
    evidence.push({width,page,...data});console.log('PASS',width,page);
  }
  await writeFile(output+'layout-evidence.json',JSON.stringify(evidence,null,2));
} finally {ws?.close();browser.kill();server.closeAllConnections();server.close();}
