import {read,mapData} from './unify.mjs';

export function hull(points){
 const p=[...new Map(points.map(v=>[v.map(x=>x.toFixed(7)).join(','),v])).values()].sort((a,b)=>a[0]-b[0]||a[1]-b[1]);
 const cross=(a,b,c)=>(b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]);
 const half=xs=>{const h=[];for(const v of xs){while(h.length>1&&cross(h.at(-2),h.at(-1),v)<=1e-10)h.pop();h.push(v);}return h;};
 if(p.length<3)return [];return [...half(p).slice(0,-1),...half([...p].reverse()).slice(0,-1)];
}
export function overlaps(a,b){
 if(a.length<3||b.length<3)return false;
 for(const p of [a,b])for(let i=0;i<p.length;i++){
  const u=p[i],v=p[(i+1)%p.length],nx=u[1]-v[1],ny=v[0]-u[0];
  let alo=Infinity,ahi=-Infinity,blo=Infinity,bhi=-Infinity;
  for(const q of a){const x=q[0]*nx+q[1]*ny;alo=Math.min(alo,x);ahi=Math.max(ahi,x);}
  for(const q of b){const x=q[0]*nx+q[1]*ny;blo=Math.min(blo,x);bhi=Math.max(bhi,x);}
  if(ahi<=blo+1e-9||bhi<=alo+1e-9)return false;
 }return true;
}
export function footprints(o,zlo=.005,zhi=.24,parts=o.parts){
 const a=o.yaw*Math.PI/180,c=Math.cos(a),s=Math.sin(a),out=[];
 for(const part of parts){
  if(part.collision===false)continue;
  // Clip to the robot's vertical slab. Elevated tops are excluded, and
  // distinct legs stay separate instead of using a solid furniture envelope.
  const lo=zlo-o.position[2],hi=zhi-o.position[2],points=[];
  for(const v of part.vertices)if(v[2]>=lo&&v[2]<=hi)points.push(v.slice(0,2));
  for(const t of part.triangles)for(let k=0;k<3;k++){
   const u=part.vertices[t[k]],v=part.vertices[t[(k+1)%3]];
   for(const z of [lo,hi])if((u[2]<z&&v[2]>z)||(v[2]<z&&u[2]>z)){
    const f=(z-u[2])/(v[2]-u[2]);points.push([u[0]+f*(v[0]-u[0]),u[1]+f*(v[1]-u[1])]);
   }
  }
  const polygon=hull(points).map(([x,y])=>[o.position[0]+c*x-s*y,o.position[1]+s*x+c*y]);
  if(polygon.length>2)out.push({name:part.name,polygon});
 }return out;
}
export function routeSamples(route,step=.01){
 const out=[];let previousYaw;
 for(let i=1;i<route.length;i++){
  const a=route[i-1],b=route[i],d=Math.hypot(b[0]-a[0],b[1]-a[1]);if(d<1e-9)continue;
  const yaw=Math.atan2(b[1]-a[1],b[0]-a[0]),n=Math.ceil(d/step);
  if(previousYaw!==undefined){const turn=Math.atan2(Math.sin(yaw-previousYaw),Math.cos(yaw-previousYaw));for(let j=1;j<Math.ceil(Math.abs(turn)/.1);j++)out.push({x:a[0],y:a[1],yaw:previousYaw+Math.sign(turn)*j*.1});}
  for(let j=0;j<=n;j++)out.push({x:a[0]+(b[0]-a[0])*j/n,y:a[1]+(b[1]-a[1])*j/n,yaw});
  previousYaw=yaw;
 }return out;
}
export function robotPolygon(p,margin=.03){
 const c=Math.cos(p.yaw),s=Math.sin(p.yaw);return [[-.107-margin,-.063-margin],[.107+margin,-.063-margin],[.107+margin,.063+margin],[-.107-margin,.063+margin]].map(([x,y])=>[p.x+c*x-s*y,p.y+s*x+c*y]);
}
export function routeCollisions(scene,route=read('data/demo_route.json').route){
 const samples=routeSamples(route),boxes=samples.map(p=>robotPolygon(p)),robots=boxes.map((box,i)=>i?hull([...boxes[i-1],...box]):box),hits=[];
 for(const o of scene.objects){if(!o.visible||o.category!=='furniture')continue;
  const parts=footprints(o),lod=o.lod_parts?footprints(o,.005,.24,o.lod_parts):[];
  const indices=[];const names=new Set();let canonical=0,lodHits=0;
  for(let i=0;i<robots.length;i++){
   const a=parts.filter(p=>overlaps(p.polygon,robots[i])),b=lod.filter(p=>overlaps(p.polygon,robots[i]));
   if(a.length||b.length){indices.push(i);for(const p of [...a,...b])names.add(p.name);if(a.length)canonical++;if(b.length)lodHits++;}
  }
  if(indices.length)hits.push({id:o.id,label:o.label,sample_count:indices.length,canonical_samples:canonical,lod_samples:lodHits,parts:[...names],first:samples[indices[0]]});
 }return {samples:samples.length,step_m:.01,robot_envelope_m:[.214,.126,.24],margin_m:.03,hits};
}
export function mapAgreement(o,m=mapData()){
 const parts=footprints(o,.2249,.2251);let free=0,occupied=0,unknown=0;
 for(let y=0;y<m.meta.height;y++)for(let x=0;x<m.meta.width;x++){
  const center=m.toPlan(m.meta.origin_x+(x+.5)*m.meta.resolution,m.meta.origin_y+(y+.5)*m.meta.resolution);
  const cell=[[center[0]-.024,center[1]-.024],[center[0]+.024,center[1]-.024],[center[0]+.024,center[1]+.024],[center[0]-.024,center[1]+.024]];
  if(!parts.some(p=>overlaps(p.polygon,cell)))continue;
  const v=m.nav.values[y*m.meta.width+x];if(v<0)free++;else if(v>=.65)occupied++;else unknown++;
 }return {free,occupied,unknown};
}
