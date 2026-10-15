import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
export const ROOT=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
export const read=p=>JSON.parse(fs.readFileSync(path.join(ROOT,p),'utf8'));
export const write=(p,v)=>{const d=path.join(ROOT,p);fs.mkdirSync(path.dirname(d),{recursive:true});fs.writeFileSync(d,JSON.stringify(v,null,v?.objects?0:2));};
export const revision=s=>crypto.createHash('sha256').update(JSON.stringify({...s,revision:undefined,updated_at:undefined})).digest('hex').slice(0,16);
export function npy(file){
 const b=fs.readFileSync(path.join(ROOT,file)),start=b[6]===1?10:12,len=b[6]===1?b.readUInt16LE(8):b.readUInt32LE(8),h=b.subarray(start,start+len).toString();
 const shape=h.match(/'shape':\s*\(([^)]+)\)/)[1].split(',').map(Number).filter(x=>x>0),dtype=h.match(/'descr':\s*'([^']+)'/)[1],buf=b.subarray(start+len),values=[];
 if(!['<f4','<i4','|i1','|u1','<f8','<i8'].includes(dtype)||h.includes('True'))throw Error('Unsupported NPY '+h);
 for(let i=0;i<shape.reduce((a,b)=>a*b,1);i++)values.push(dtype==='<f4'?buf.readFloatLE(i*4):dtype==='<i4'?buf.readInt32LE(i*4):dtype==='<f8'?buf.readDoubleLE(i*8):dtype==='<i8'?Number(buf.readBigInt64LE(i*8)):dtype==='|i1'?buf.readInt8(i):buf.readUInt8(i));
 return {shape,values,dtype};
}
export function meshBox(name,center,size,material,yaw=0){
 const vertices=[],a=yaw*Math.PI/180,c=Math.cos(a),s=Math.sin(a);
 for(const z of [-1,1])for(const y of [-1,1])for(const x of [-1,1]){const u=x*size[0]/2,v=y*size[1]/2;vertices.push([center[0]+c*u-s*v,center[1]+s*u+c*v,center[2]+z*size[2]/2]);}
 return {name,material,vertices,triangles:[[0,2,1],[1,2,3],[4,5,6],[5,7,6],[0,1,4],[1,5,4],[2,6,3],[3,6,7],[0,4,2],[2,4,6],[1,3,5],[3,7,5]],collision:true};
}
export function sliceObject(o,height=.225){
 const out=[],a=o.yaw*Math.PI/180,c=Math.cos(a),s=Math.sin(a);
 for(const p of o.parts){if(p.collision===false)continue;for(const f of p.triangles){const hits=[];for(let j=0;j<3;j++){const u=p.vertices[f[j]],v=p.vertices[f[(j+1)%3]],h=height-o.position[2];if((u[2]<h&&v[2]>=h)||(v[2]<h&&u[2]>=h)){const t=(h-u[2])/(v[2]-u[2]);hits.push([u[0]+t*(v[0]-u[0]),u[1]+t*(v[1]-u[1])]);}}
 if(hits.length!==2)continue;const n=Math.max(1,Math.ceil(Math.hypot(hits[1][0]-hits[0][0],hits[1][1]-hits[0][1])/.05));for(let j=0;j<=n;j++){const t=j/n,x=hits[0][0]*(1-t)+hits[1][0]*t,y=hits[0][1]*(1-t)+hits[1][1]*t;out.push([c*x-s*y+o.position[0],s*x+c*y+o.position[1]]);}}}
 return [...new Map(out.map(v=>[v.map(x=>Math.round(x/.025)).join(','),v])).values()];
}
export function mapData(){
 const grid=npy('data/map/slam_map_loc.npy'),nav=npy('data/map/slam_map.npy'),meta=read('data/map/map_meta.json'),frame=read('data/map/pose_frame.json'),a=frame.rotate_deg*Math.PI/180,c=Math.cos(a),s=Math.sin(a);
 const toPlan=(x,y)=>[c*x-s*y+frame.translate_x,s*x+c*y+frame.translate_y],toMap=(x,y)=>{const u=x-frame.translate_x,v=y-frame.translate_y;return[c*u+s*v,-s*u+c*v];};
 const occupied=[];grid.values.forEach((v,i)=>{if(v>=.65)occupied.push(toPlan(meta.origin_x+(i%meta.width+.5)*meta.resolution,meta.origin_y+(Math.floor(i/meta.width)+.5)*meta.resolution));});
 return {grid,nav,meta,frame,toPlan,toMap,occupied};
}
function migrateClaude(s){
 const source=fs.readFileSync(path.join(ROOT,'reference/furnish.py'),'utf8');
 for(const m of source.matchAll(/"(\w+)":\s*\(\(([^)]+)\),\s*([\d.]+),\s*([\d.]+)\)/g))if(s.materials[m[1]])Object.assign(s.materials[m[1]],{color:m[2].split(',').map(Number),roughness:Number(m[3]),metalness:Number(m[4])});
 const usd=fs.readFileSync(path.join(ROOT,'reference/house_sim_v2.usda'),'utf8'),mapping={office_main_desk:'study_window_desk',office_side_desk:'study_side_desk',sofa:'living_sofa',living_desk:'living_desk'},imported=[];
 for(const [name,id] of Object.entries(mapping)){
 const start=usd.indexOf('def "'+name+'"');if(start<0)continue;const open=start;let end=usd.indexOf('\n        def "',start+10);if(end<0)end=usd.length;
 const chunk=usd.slice(open,end),o=s.objects.find(o=>o.id===id),parts=[];if(!o)continue;
 for(const m of chunk.matchAll(/def Xform "(p\d+)"\s*\{([\s\S]*?)(?=\n            def Xform|$)/g)){const p=m[2],t=p.match(/xformOp:translate = \(([^)]+)\)/),sz=p.match(/xformOp:scale = \(([^)]+)\)/),mat=p.match(/material:binding = <\/World\/Looks\/(\w+)>/),yaw=p.match(/xformOp:rotateZ = ([\d.eE+-]+)/);if(!t||!sz||!p.includes('def Cube')||!s.materials[mat?.[1]?.replace(/^F_/, '')])continue;parts.push(meshBox(m[1],t[1].split(',').map(Number),sz[1].split(',').map(Number),mat[1].replace(/^F_/, ''),Number(yaw?.[1]||0)));}
 if(!parts.length)continue;
 const angle=-o.yaw*Math.PI/180,c=Math.cos(angle),sn=Math.sin(angle);for(const p of parts)for(const v of p.vertices){const x=v[0],y=v[1];v[0]=c*x-sn*y;v[1]=sn*x+c*y;}
 const bounds=ps=>{const v=ps.flatMap(p=>p.vertices);return [Array.from({length:3},(_,i)=>Math.min(...v.map(p=>p[i]))),Array.from({length:3},(_,i)=>Math.max(...v.map(p=>p[i])))];},[lo,hi]=bounds(parts),[tl,th]=bounds(o.parts);
 // Discard RTAB envelope; color/form fit the existing metric envelope only.
 for(const p of parts)for(const v of p.vertices)for(let k=0;k<3;k++)v[k]=tl[k]+(v[k]-lo[k])/Math.max(hi[k]-lo[k],1e-6)*(th[k]-tl[k]);o.lod_parts=parts;imported.push({id,parts:parts.length,source:name,envelope:'Astra envelope; RTAB size/position discarded'});
 }
 return imported;
}
export function build(){
 if(fs.existsSync(path.join(ROOT,'out/migration.json')) && (!process.argv.includes('--rebuild') || read('data/scene.json').revision!==read('out/migration.json').revision))throw Error('Migration exists: refusing to overwrite user edits.');
 const source=path.join(ROOT,'../astra-house-sim'),s=JSON.parse(fs.readFileSync(path.join(source,'data/scene.json'),'utf8')),base=JSON.parse(fs.readFileSync(path.join(source,'data/baseline.json'),'utf8'));write('data/source_scene.json',s);
 const m=mapData(),bin=new Map();for(const p of m.occupied){const key=p.map(v=>Math.floor(v/.25)).join(',');if(!bin.has(key))bin.set(key,[]);bin.get(key).push(p);}
 const dist=(x,y)=>{let best=.6,ix=Math.floor(x/.25),iy=Math.floor(y/.25);for(let dx=-3;dx<=3;dx++)for(let dy=-3;dy<=3;dy++)for(const p of bin.get(`${ix+dx},${iy+dy}`)||[])best=Math.min(best,Math.hypot(p[0]-x,p[1]-y));return best;},audit=[];
 for(const o of s.objects){if(!o.visible||o.tags?.includes('floor')||o.tags?.includes('ceiling')||(!o.id.startsWith('wall_')&&o.category!=='furniture'))continue;
 const b=base.objects.find(q=>q.id===o.id),protectedFields=['position','yaw','visible'].filter(k=>JSON.stringify(o[k])!==JSON.stringify(b?.[k])),points=sliceObject(o);
 if(points.length<8){audit.push({id:o.id,status:'no_lidar_height_outline',protected_fields:protectedFields});continue;}
 const subset=points.filter((_,i)=>i%Math.max(1,Math.ceil(points.length/100))===0),score=(dx,dy)=>subset.reduce((a,p)=>a+Math.min(.4,dist(p[0]+dx,p[1]+dy)),0)/subset.length,before=score(0,0);let best={score:before,dx:0,dy:0};
 for(let dx=-.2;dx<=.2001;dx+=.025)for(let dy=-.2;dy<=.2001;dy+=.025){if(o.id.startsWith('wall_')){const xs=points.map(p=>p[0]),ys=points.map(p=>p[1]),horizontal=Math.max(...xs)-Math.min(...xs)>Math.max(...ys)-Math.min(...ys);if(horizontal?Math.abs(dx)>.001:Math.abs(dy)>.001)continue;}const v=score(dx,dy)+.04*Math.hypot(dx,dy);if(v<best.score)best={score:v,dx,dy};}
 const after=score(best.dx,best.dy),gain=before-after,support=subset.filter(p=>dist(p[0]+best.dx,p[1]+best.dy)<.1).length/subset.length,supported=gain>.035&&gain/before>.25&&support>=.9&&before<.3&&Math.abs(best.dx)<.19&&Math.abs(best.dy)<.19&&after<.05&&points.length>=18,apply=supported&&!protectedFields.length,old=[...o.position];
 if(apply){o.position[0]+=best.dx;o.position[1]+=best.dy;if(o.id==='wall_bedroom_south'){s.objects.find(p=>p.id==='opening_bedroom_door').position[1]+=best.dy;}}
 audit.push({id:o.id,label:o.label,protected_fields:protectedFields,status:apply?'lidar_adjusted':protectedFields.length?'user_edit_preserved':'insufficient_unique_support',before_position:old,position:o.position,proposed_shift_m:[best.dx,best.dy],mean_outline_distance_before_m:before,mean_outline_distance_after_m:after,support_fraction:support,outline_samples:points.length});
 }
 const lod=migrateClaude(s);s.title='우리 집 · 통합 SIM';s.source_review.unified='LiDAR 20261004 authority; RTAB color/form only; edited transforms preserved';s.updated_at=new Date().toISOString();s.revision=revision(s);write('data/scene.json',s);write('data/baseline.json',s);
 const labels=npy('data/map/zone_labels.npy');write('data/presentation.json',{schema_version:1,frame:s.frame,pose_frame:m.frame,map_meta:m.meta,occupied:m.occupied,zones:read('data/map/zones_plan.json').zones,zone_cells:labels.values.map((v,i)=>v>0&&m.nav.values[i]<0?{zone:v,xy:m.toPlan(m.meta.origin_x+(i%m.meta.width+.5)*m.meta.resolution,m.meta.origin_y+(Math.floor(i/m.meta.width)+.5)*m.meta.resolution)}:null).filter(Boolean),S0:read('data/map/S0.json'),cell_size:m.meta.resolution});
 const provenance=[...fs.readdirSync(path.join(ROOT,'data/map')).filter(f=>/\.json$|\.npy$/.test(f)).map(f=>path.join(ROOT,'data/map',f)),path.join(source,'data/scene.json'),path.join(source,'data/baseline.json')].map(p=>({path:p,sha256:crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex')}));
 write('out/migration.json',{created_at:new Date().toISOString(),source_revision:read('data/source_scene.json').revision,revision:s.revision,lod_imports:lod,audit,provenance,limits:['nearest-occupancy audit is not semantic identification','unobserved walls/door sizes keep plan estimates','user edits protected','RTAB dimensions discarded']});console.log(JSON.stringify({revision:s.revision,occupied:m.occupied.length,lod,adjusted:audit.filter(a=>a.status==='lidar_adjusted'),protected:audit.filter(a=>a.protected_fields?.length).map(a=>a.id)},null,2));
}
if(process.argv[1]===fileURLToPath(import.meta.url))build();

