const angle=(a,b)=>Math.atan2(Math.sin(b-a),Math.cos(b-a));
const clamp=(x,a,b)=>Math.max(a,Math.min(b,x));
export const ROBOT_FIT={xScale:.214/(.111736+.08425),xCenter:(.111736-.08425)/2,yScale:.126/.117,zScale:.138/.133};
const fitX=x=>(x-ROBOT_FIT.xCenter)*ROBOT_FIT.xScale,fitZ=z=>z*ROBOT_FIT.zScale;

// A bounded, delayed sample buffer, with no extrapolation into unseen poses.
export function createMotion({delayMs=120}={}){
 let samples=[],signature='',source='',previous=null,lastTime=null,phase=0,blend=0,display=null;
 function ingest(state,now){
  const p=state?.pose;
  if(!p?.visible||![p.x_m,p.y_m,p.yaw_rad].every(Number.isFinite))return;
  const moving=['live','offline_test'].includes(state.mode)&&p.status==='valid';
  const key=[state.mode,state.scene_revision,p.source_kind,p.stop_id].join(':');
  const stamp=state.source?.source_timestamp_ms;
  const sig=[key,stamp,p.x_m,p.y_m,p.yaw_rad,p.status].join(':');
  if(sig===signature)return;signature=sig;
  const sample={x_m:p.x_m,y_m:p.y_m,yaw_rad:p.yaw_rad,time:now-Math.max(0,state.freshness?.source_age_ms||0),stamp,moving};
  const tail=samples.at(-1),jump=tail&&(Math.hypot(sample.x_m-tail.x_m,sample.y_m-tail.y_m)>.5||(Number.isFinite(stamp)&&stamp-tail.stamp>1000));
  if(key!==source||jump||!moving){samples=[sample];previous=null;blend=0;display={...sample};source=key;}
  else if(stamp!==tail?.stamp){sample.time=Math.max(sample.time,(tail?.time??-Infinity)+1);samples.push(sample);samples=samples.slice(-12);}
 }
 function update(now,active=true){
  if(!samples.length)return null;
  const dt=lastTime===null?0:clamp((now-lastTime)/1000,0,.1);lastTime=now;
  const time=now-delayMs;let a=samples[0],b=a;
  for(const p of samples){if(p.time<=time)a=p;else{b=p;break;}b=a;}
  const t=a===b?0:clamp((time-a.time)/(b.time-a.time),0,1);
  display={x_m:a.x_m+(b.x_m-a.x_m)*t,y_m:a.y_m+(b.y_m-a.y_m)*t,yaw_rad:a.yaw_rad+angle(a.yaw_rad,b.yaw_rad)*t};
  let speed=0,turn=0,forward=0;
  if(previous&&dt>0&&active&&samples.at(-1).moving){const dx=display.x_m-previous.x_m,dy=display.y_m-previous.y_m;speed=Math.hypot(dx,dy)/dt;turn=angle(previous.yaw_rad,display.yaw_rad)/dt;forward=(dx*Math.cos(display.yaw_rad)+dy*Math.sin(display.yaw_rad))/dt;}
  const walking=speed>.015||Math.abs(turn)>.12,target=active&&walking?1:0;
  blend+=(target-blend)*(1-Math.exp(-dt*16));if(blend<.001)blend=0;
  if(target)phase+=(1.4+Math.min(speed,.5)*4+Math.min(Math.abs(turn),2)*.25)*dt*2*Math.PI;
  previous={...display};return {...display,speed,turn,forward,phase,blend,walking:blend>.01};
 }
 return {ingest,update,getDisplay:()=>display};
}

export function gaitFeet(motion){
 return ['front_left','rear_left','front_right','rear_right'].map((id,i)=>{
  const front=id.startsWith('front'),left=id.endsWith('left'),offset=(i===0||i===3)?0:Math.PI;
  const p=motion.phase+offset,s=Math.sin(p),amount=motion.blend;
  const stride=clamp(Math.abs(motion.forward)*.18+Math.abs(motion.turn)*.008,.006,.028);
  const sign=Math.abs(motion.forward)>.01?Math.sign(motion.forward):Math.sign(motion.turn)*(left?-1:1);
  return {id,x:fitX(front?.05925:-.07125)-Math.cos(p)*stride*amount*sign,y:(left?.046:-.046)*ROBOT_FIT.yScale,z:fitZ(.008)+Math.max(0,s)*.017*amount,lift:Math.max(0,s)*.017*amount};
 });
}

// Two-link sagittal IK keeps upper/lower segments joined as the foot lifts.
export function legPose(foot,bodyZ=0){
 const front=foot.id.startsWith('front'),restHip=[fitX(front?.06025:-.06025),fitZ(.08)],hip=[restHip[0],restHip[1]+bodyZ],knee=[fitX(front?.10825:-.01625),fitZ(front?.042:.036)],rest=[fitX(front?.05925:-.07125),fitZ(.008)];
 const l1=Math.hypot(knee[0]-hip[0],knee[1]-restHip[1]),l2=Math.hypot(rest[0]-knee[0],rest[1]-knee[1]);
 const dx=foot.x-hip[0],dz=foot.z-hip[1],d=clamp(Math.hypot(dx,dz),Math.abs(l1-l2)+.0001,l1+l2-.0001);
 const base=Math.atan2(dz,dx),opening=Math.acos(clamp((l1*l1+d*d-l2*l2)/(2*l1*d),-1,1)),a=base+opening;
 const targetKnee=[hip[0]+l1*Math.cos(a),hip[1]+l1*Math.sin(a)];
 return {hip,knee:targetKnee,restHip,restKnee:knee,restFoot:rest,upperAngle:a-Math.atan2(knee[1]-restHip[1],knee[0]-hip[0]),lowerAngle:Math.atan2(foot.z-targetKnee[1],foot.x-targetKnee[0])-Math.atan2(rest[1]-knee[1],rest[0]-knee[0])};
}
