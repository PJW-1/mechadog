import {read,write} from './unify.mjs';
import {gaitFeet,legPose} from '../web/robot-motion.mjs';
const asset=read('data/assets/robot/mechdog02_visual.json');
let min=[Infinity,Infinity,Infinity],max=[-Infinity,-Infinity,-Infinity],poses=0;
for(const forward of [-.5,-.05,0,.05,.5])for(const turn of [-2,0,2])for(let phase=0;phase<Math.PI*2;phase+=.1){
 const motion={forward,turn,phase,blend:1},bob=Math.sin(phase*2)*.0015,feet=gaitFeet(motion);
 for(const part of asset.parts){
  let theta=0,offset=[0,0,0],roll=0;
  const f=feet.find(f=>part.name.startsWith(f.id+'_'));
  if(f){
   const ik=legPose(f,bob);
   if(part.name.endsWith('_foot'))offset=[f.x-ik.restFoot[0],0,f.z-ik.restFoot[1]];
   else{theta=part.name.endsWith('_upper')?ik.upperAngle:ik.lowerAngle;const pivot=part.name.endsWith('_upper')?ik.restHip:ik.restKnee,target=part.name.endsWith('_upper')?ik.hip:ik.knee,c=Math.cos(theta),s=Math.sin(theta);offset=[target[0]-c*pivot[0]+s*pivot[1],0,target[1]-s*pivot[0]-c*pivot[1]];}
  }else{offset[2]=bob;roll=Math.sin(phase)*.012;}
  for(const v of part.vertices){const c=Math.cos(theta),s=Math.sin(theta),cr=Math.cos(roll),sr=Math.sin(roll),p=[c*v[0]-s*v[2]+offset[0],cr*v[1]-sr*v[2],sr*v[1]+cr*(s*v[0]+c*v[2])+offset[2]];for(let k=0;k<3;k++){min[k]=Math.min(min[k],p[k]);max[k]=Math.max(max[k],p[k]);}}
 }
 poses++;
}
const report={poses,min,max,required_xy_margin_m:Math.max(-.107-min[0],max[0]-.107,-.063-min[1],max[1]-.063),vertical_clearance_m:.24-max[2],note:'Shared IK applied to actual 22 canonical meshes; visual rig, no dynamics'};
write('out/gait_envelope.json',report);console.log(report);
if(report.required_xy_margin_m>.03||report.vertical_clearance_m<0)throw Error('Gait exceeds collision audit envelope');
