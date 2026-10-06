import {read,write} from './unify.mjs';
import {ROBOT_FIT} from '../web/robot-motion.mjs';
const p='data/assets/robot/mechdog02_visual.json',asset=read(p);
if(!asset.task_T_fit){
 write('out/robot_before_T.json',asset);
 for(const part of asset.parts){
  if(['payload_mount','lidar_housing','camera_housing'].includes(part.name))continue;
  for(const v of part.vertices){v[0]=(v[0]-ROBOT_FIT.xCenter)*ROBOT_FIT.xScale;v[1]*=ROBOT_FIT.yScale;v[2]=Math.max(0,v[2])*ROBOT_FIT.zScale;}
  delete part.normals;
 }
 asset.task_T_fit={...ROBOT_FIT,stock_envelope_m:[.214,.126,.138],lidar_height_m:.225,basis:'retained stock envelope + user-measured sensor height; articulated visual fit only'};
 write(p,asset);
}
console.log(asset.task_T_fit);
