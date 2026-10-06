import {createPoseStore} from './src/pose-state.mjs';
﻿import fs from 'node:fs';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import http from 'node:http';
import dgram from 'node:dgram';
import {gzipSync} from 'node:zlib';
import {ROOT,read,write,revision} from './src/unify.mjs';
import {exportUSD} from './src/export-usd.mjs';
const args=process.argv.slice(2),arg=(n,d)=>{let i=args.indexOf('--'+n);return i<0?d:args[i+1];},port=Number(arg('port','8789')),input=arg('input','mirror'),udpPort=Number(arg('udp-port','5398'));
if(![8789,8791].includes(port))throw Error('HTTP port must be 8789 or isolated test port 8791');
if(port===8791&&input!=='off')throw Error('Isolated test port requires input=off');
if(!['mirror','udp','off'].includes(input))throw Error('input=mirror|udp|off');
if(![5398,5399,5206].includes(udpPort))throw Error('Only pose observer UDP ports allowed');
const sourceURL='http://127.0.0.1:8788/api/robot/state';
let scene=read('data/scene.json');const files=new Map(),store=createPoseStore(()=>scene.revision),finitePose=store.finitePose,accept=store.accept,snapshot=()=>store.snapshot(input);
async function mirror(){
 try{const r=await fetch(sourceURL,{signal:AbortSignal.timeout(1200)});if(!r.ok)throw Error('8788 HTTP '+r.status);const v=await r.json();if(v.mode!=='live')throw Error('8788 is not live');const p=v.pose?.status==='valid'?v.pose:(v.source_pose||v.pose);
 if(v.pose?.frame_id!=='floorplan_xy_m_z_up'||v.pose?.registration_status!=='validated'||!finitePose(p))throw Error('8788 frame/registration invalid');
 accept({...p,lost:v.pose.status!=='valid',moving:v.moving??null,ts_ms:v.source.source_timestamp_ms,demo:demoSession},'mirror_8788');
 }catch(e){store.error(e.message);}finally{setTimeout(mirror,80).unref();}}
let demoSession=false;
if(input==='mirror')mirror();
if(input==='udp'){
 const socket=dgram.createSocket({type:'udp4',reuseAddr:false});socket.on('message',(buf)=>{try{const p=JSON.parse(buf);if(p.type==='MAP_POSE')accept({...p,lost:!p.valid,ts_ms:p.ts,demo:demoSession},'udp_5206');else accept(p,'udp_pose_out_'+udpPort);}catch{store.reject();}});
 socket.on('error',e=>{store.error(e.message);console.error('Exclusive UDP bind failed; existing process untouched:',e.message);socket.close();});socket.bind(udpPort,'127.0.0.1');
}
let routesCache={available:false,saved:[],active:null,reason:'동선 API 미설정'},routesAt=0;
const routesURL=arg('routes-url','http://127.0.0.1:8020/api/routes');
if(routesURL){const u=new URL(routesURL);if(!['127.0.0.1','localhost'].includes(u.hostname)||['5101','5201','8000'].includes(u.port)||u.pathname!=='/api/routes')throw Error('Only local non-field /api/routes allowed');}
async function routes(){if(!routesURL)return routesCache;if(Date.now()-routesAt<3000)return routesCache;routesAt=Date.now();try{const r=await fetch(routesURL,{signal:AbortSignal.timeout(800)});if(!r.ok)throw Error('HTTP '+r.status);const v=await r.json();routesCache={available:true,saved:v.saved||[],active:v.active,frame:'patrol',pending_restart:v.pending_restart};}catch(e){routesCache={available:false,saved:[],active:null,reason:e.message};}return routesCache;}
function respond(res,status,value){const body=Buffer.from(JSON.stringify(value));res.writeHead(status,{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store','Content-Length':body.length});res.end(body);}
function staticPath(url){let base=path.join(path.dirname(fileURLToPath(import.meta.url)),'web'),rel=decodeURIComponent(url);for(const [prefix,dir]of [['/assets/','data/assets'],['/exports/','exports'],['/audit/','audit'],['/out/','out']])if(rel.startsWith(prefix)){base=path.join(ROOT,dir);rel=rel.slice(prefix.length);break;}if(url==='/')rel='index.html';const p=path.resolve(base,rel.replace(/^\/+/,''));return p.startsWith(base+path.sep)?p:null;}
async function post(req,max=128000){if(req.headers['content-type']?.split(';')[0]!=='application/json')throw Error('JSON required');if(req.headers.origin&&req.headers.origin!=='http://'+req.headers.host)throw Error('Foreign origin');let n=0,chunks=[];for await(const c of req){n+=c.length;if(n>max)throw Error('Body too large');chunks.push(c);}return JSON.parse(Buffer.concat(chunks));}
export const server=http.createServer(async(req,res)=>{try{
 const url=new URL(req.url,'http://localhost').pathname;
 if(req.method==='GET'){
 if(url==='/api/scene')return respond(res,200,scene);
 if(url==='/api/baseline')return respond(res,200,read('data/baseline.json'));
 if(url==='/api/presentation')return respond(res,200,read('data/presentation.json'));
 if(url==='/api/robot/state')return respond(res,200,snapshot());
 if(url==='/api/routes')return respond(res,200,await routes());
 if(url==='/api/status')return respond(res,200,{service_id:'house-sim-unified',root_path:ROOT,process_id:process.pid,revision:scene.revision,usd_revision:read('exports/manifest.json').revision,sensors:{camera:'disconnected',lidar:'disconnected',pose:snapshot().pose.status},commands_enabled:false,navigation_ready:false,input});
 if(url.startsWith('/api/'))return respond(res,404,{error:'Unknown API'});
 const p=staticPath(url);if(!p||!fs.existsSync(p)||!fs.statSync(p).isFile())return respond(res,404,{error:'File not found'});
 const stat=fs.statSync(p),mime={'.js':'text/javascript','.mjs':'text/javascript','.css':'text/css','.json':'application/json','.html':'text/html; charset=utf-8','.jpg':'image/jpeg','.png':'image/png','.usda':'text/plain'}[path.extname(p)]||'application/octet-stream';
 let cached=files.get(p);if(!cached||cached.mtime!==stat.mtimeMs){const buffer=fs.readFileSync(p);cached={buffer,gzip:gzipSync(buffer),mtime:stat.mtimeMs};files.set(p,cached);}const zipped=/gzip/.test(req.headers['accept-encoding']||'')&&/javascript|json|html|css/.test(mime),data=zipped?cached.gzip:cached.buffer;res.writeHead(200,{'Content-Type':mime,'Content-Length':data.length,'Cache-Control':'no-cache','X-Content-Type-Options':'nosniff',...(zipped?{'Content-Encoding':'gzip','Vary':'Accept-Encoding'}:{})});return res.end(data);
 }
 if(req.method!=='POST')return respond(res,405,{error:'Method not allowed'});
 const body=await post(req,url==='/api/evidence'?12000000:128000);
 if(url==='/api/save'){
 if(body.expected_revision!==scene.revision)return respond(res,409,{error:'다른 창에서 장면이 바뀌었습니다.',revision:scene.revision});
 const updated=structuredClone(scene),seen=new Set();if(!Array.isArray(body.objects)||body.objects.length>scene.objects.length)throw Error('Invalid edits');
 for(const edit of body.objects){const o=updated.objects.find(o=>o.id===edit.id);if(!o||seen.has(edit.id)||Object.keys(edit).some(k=>!['id','position','yaw','visible'].includes(k)))throw Error('Invalid object');seen.add(edit.id);
 const p=edit.position;if(!Array.isArray(p)||p.length!==3||!p.every(Number.isFinite)||p[0]<-2||p[0]>13||p[1]<-2||p[1]>10||p[2]<-.2||p[2]>4||!Number.isFinite(edit.yaw)||typeof edit.visible!=='boolean')throw Error('Invalid transform');
 if(!o.editable){if(JSON.stringify(p)!==JSON.stringify(o.position)||edit.yaw!==o.yaw||edit.visible!==o.visible)throw Error('Structure is locked');}else Object.assign(o,{position:p,yaw:(edit.yaw%360+540)%360-180,visible:edit.visible});}
 updated.updated_at=new Date().toISOString();updated.revision=revision(updated);const report=exportUSD(updated);write('data/history/'+scene.revision+'.json',scene);write('data/scene.json.tmp',updated);fs.renameSync(path.join(ROOT,'data/scene.json.tmp'),path.join(ROOT,'data/scene.json'));scene=updated;return respond(res,200,{ok:true,scene,revision:scene.revision,usd_revision:report.revision});
 }
 if(url==='/api/export'){exportUSD(scene);return respond(res,200,{ok:true,revision:scene.revision,url:'/exports/house_sim.usda'});}
 if(url==='/api/demo/session'){demoSession=body.enabled===true;store.clear();return respond(res,200,{ok:true,demo:demoSession});}
 if(url==='/api/evidence'){
 const name=body.view;if(!['plan','follow','eye'].includes(name)||typeof body.png!=='string'||body.png.length>12000000||!body.png.startsWith('data:image/png;base64,'))throw Error('Invalid evidence');fs.writeFileSync(path.join(ROOT,'out/'+name+'.png'),Buffer.from(body.png.split(',')[1],'base64'));write('out/fps_'+name+'.json',{...body.metrics,view:name,captured_at:new Date().toISOString(),state:snapshot(),renderer:'Chrome WebGL',real_robot:false});return respond(res,200,{ok:true});
 }
 if(url==='/api/robot/pose'){
 if(body.mode==='clear'){store.setRecorded(null);return respond(res,200,{ok:true,state:snapshot()});}
 if(body.mode!=='recorded'||body.scene_revision!==scene.revision)throw Error('Recorded mode/revision required');
 const r=read('audit/lidar20261001/observations.json').records.find(r=>r.id===body.stop_id);if(!r)throw Error('Unknown stop');
 const archive=read('audit/lidar20261001/house_registration/registration.json'),a=archive.stops?.find(s=>s.id===r.id),ap=a?.pose||a?.candidate_pose,ok=archive.coordinates?.frame==='house_floorplan'&&archive.coordinates?.scale===1&&archive.scene_revision===read('data/source_scene.json').revision;const p=ok&&ap?[ap.x_m,ap.y_m,ap.yaw_rad]:null;
 const recorded={schema_version:1,scene_revision:scene.revision,mode:'recorded',pose:{status:p?'recorded_candidate':'unregistered',visible:!!p,x_m:p?.[0],y_m:p?.[1],yaw_rad:p?.[2],stop_id:r.id,frame_id:'floorplan_xy_m_z_up',registration_status:'candidate',anchor_frame:'laser',source_kind:'recorded',reason:'historical_metric_candidate_original_scene_not_current_fit'},source:{recorded_at:r.time},freshness:{age_ms:null,stale_after_ms:500},commands_enabled:false,navigation_ready:false};store.setRecorded(recorded);return respond(res,200,{ok:true,state:recorded});
 }
 return respond(res,404,{error:'Unknown API'});
 }catch(e){respond(res,400,{error:e.message});}});
if(!fs.existsSync(path.join(ROOT,'exports/manifest.json'))||read('exports/manifest.json').revision!==scene.revision)exportUSD(scene);
server.listen(port,'127.0.0.1',()=>console.log(`Unified SIM http://127.0.0.1:${port}/ input=${input}; commands disabled`));


