import fs from 'node:fs';import {ROOT,read,write,mapData} from './unify.mjs';
const m=mapData(),p=read('data/presentation.json'),meta=m.meta,w=meta.width,h=meta.height;
const cell=pt=>{const xy=m.toMap(...pt);return[Math.floor((xy[0]-meta.origin_x)/meta.resolution),Math.floor((xy[1]-meta.origin_y)/meta.resolution)];};
function free(x,y){if(x<0||y<0||x>=w||y>=h)return false;const v=m.nav.values[y*w+x];return v<0;}
function snap(pt){const [x,y]=cell(pt);for(let r=0;r<12;r++){let best=null;for(let dx=-r;dx<=r;dx++)for(let dy=-r;dy<=r;dy++)if(free(x+dx,y+dy)&&(!best||Math.hypot(dx,dy)<best.d))best={x:x+dx,y:y+dy,d:Math.hypot(dx,dy)};if(best)return best;}throw Error('No free cell');}
function astar(a,b){const s=snap(a),e=snap(b),key=(x,y)=>y*w+x,start=key(s.x,s.y),end=key(e.x,e.y),open=new Set([start]),g=new Map([[start,0]]),parents=new Map();let found=false;
 while(open.size){let cur=null,best=Infinity;for(const k of open){const v=g.get(k)+Math.hypot(k%w-e.x,Math.floor(k/w)-e.y);if(v<best){cur=k;best=v;}}if(cur===end){found=true;break;}open.delete(cur);const x=cur%w,y=Math.floor(cur/w);for(const [dx,dy]of [[1,0],[-1,0],[0,1],[0,-1],[1,1],[1,-1],[-1,1],[-1,-1]]){if(!free(x+dx,y+dy)||dx&&dy&&(!free(x+dx,y)||!free(x,y+dy)))continue;const k=key(x+dx,y+dy),cost=g.get(cur)+Math.hypot(dx,dy);if(cost<(g.get(k)??Infinity)){g.set(k,cost);parents.set(k,cur);open.add(k);}}}
 if(!found)throw Error('No S0/B/A map route');let k=end,route=[k];while(k!==start){k=parents.get(k);route.push(k);}return route.reverse().map(k=>m.toPlan(meta.origin_x+(k%w+.5)*meta.resolution,meta.origin_y+(Math.floor(k/w)+.5)*meta.resolution));}
const f=m.frame,s=p.S0.S0_patrol,S0=m.toPlan(s.x,s.y),B=p.zones.find(z=>z.id==='B').plan,A=p.zones.find(z=>z.id==='A').plan;
const route=[...astar(S0,[B.x,B.y]),...astar([B.x,B.y],[A.x,A.y]).slice(1)];write('data/demo_route.json',{route,waypoints:{S0,B,A},frame:'floorplan_xy_m_z_up',note:'A* on observed free navigation map; visualization test only, no robot footprint clearance claim'});console.log({waypoints:route.length,first:route[0],last:route.at(-1),grid:m.nav.dtype,values:[...new Set(m.nav.values)].slice(0,8)});

