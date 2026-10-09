/** Read-only pose state. Coordinates are already in pose_out's plan frame. */
export function createPoseStore(getRevision,clock=Date.now,mono=()=>performance.now()){
 let raw=null,lastValid=null,lastSample=null,trail=[],receivedAt=0,receivedMono=0,sourceError=null,recorded=null;
 const counts={accepted:0,rejected:0},STALE=500;
 const finitePose=p=>p&&[p.x_m,p.y_m,p.yaw_rad].every(v=>typeof v==='number'&&Number.isFinite(v))&&Math.abs(p.x_m)<100&&Math.abs(p.y_m)<100;
 function accept(p,source){
 if(!finitePose(p)||typeof p.lost!=='boolean'||!Number.isSafeInteger(p.ts_ms)||p.ts_ms>clock()+50||clock()-p.ts_ms>5000){counts.rejected++;return false;}
 if(raw&&source===raw.source&&p.ts_ms<=raw.ts_ms){counts.rejected++;return false;}
 if(source!==raw?.source||p.demo!==raw?.demo){trail=[];lastValid=null;lastSample=null;}
 raw={...p,source};receivedAt=clock();receivedMono=mono();counts.accepted++;sourceError=null;
 if(!p.lost&&clock()-p.ts_ms<=STALE){lastValid={x_m:p.x_m,y_m:p.y_m,yaw_rad:p.yaw_rad};const breakPath=lastSample&&(Math.hypot(p.x_m-lastSample.x_m,p.y_m-lastSample.y_m)>.5||p.ts_ms-lastSample.ts_ms>1000);
 if(!lastSample||breakPath||Math.hypot(p.x_m-lastSample.x_m,p.y_m-lastSample.y_m)>.02){trail.push({...lastValid,ts_ms:p.ts_ms,break_before:!!breakPath});trail=trail.slice(-600);lastSample={...lastValid,ts_ms:p.ts_ms};}}
 return true;
 }
 function snapshot(input){
 if(recorded)return recorded;
 const now=clock(),age=raw?Math.max(now-receivedAt,mono()-receivedMono):null,sourceAge=raw?Math.max(now-raw.ts_ms,age):null,status=!raw?'unregistered':raw.lost?'lost':sourceAge>STALE?'stale':'valid',pose=raw&&status==='valid'?raw:lastValid;
 return {schema_version:1,scene_revision:getRevision(),mode:raw?.demo?'offline_test':'live',pose:{status,visible:!!pose,frame_id:'floorplan_xy_m_z_up',x_m:pose?.x_m??null,y_m:pose?.y_m??null,yaw_rad:pose?.yaw_rad??null,source_kind:raw?.source,registration_status:pose?'validated':'missing',anchor_frame:'base_link',reason:status==='valid'?'pose_out_plan_coordinates':'last_valid_pose_ghost'},freshness:{age_ms:age,source_age_ms:sourceAge,stale_after_ms:STALE,clock_status:'unix_ms'},source:{source_timestamp_ms:raw?.ts_ms,received_at_unix_ms:receivedAt||null,ts_ms:raw?.ts_ms},registration:{status:pose?'validated':'missing',transform_id:'pose_out_plan_frame',transform:{translation_m:[0,0],yaw_rad:0}},moving:raw?.moving??null,trail:trail.filter(p=>now-p.ts_ms<60000),counts:{...counts},source_error:sourceError,commands_enabled:false,navigation_ready:false,demo:!!raw?.demo,input};
 }
 return{accept,snapshot,finitePose,reject(){counts.rejected++;},error(e){sourceError=e;},clear(){raw=null;lastValid=null;lastSample=null;trail=[];recorded=null;},setRecorded(value){recorded=value;}};
}
