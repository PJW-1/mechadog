import {read,write} from './unify.mjs';
export async function runDemo({seconds=70,url='http://127.0.0.1:8790',log=true}={}){
 if(url!=='http://127.0.0.1:8790'||seconds<5||seconds>3600)throw Error('Isolated 8790 demo only; 5..3600 seconds');
 const {route}=read('data/demo_route.json'),lengths=[0];for(let i=1;i<route.length;i++)lengths.push(lengths.at(-1)+Math.hypot(route[i][0]-route[i-1][0],route[i][1]-route[i-1][1]));
 const post=async(p,v)=>{const r=await fetch(url+p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(v)});if(!r.ok)throw Error(await r.text());};
 const start=performance.now();let count=0,last=null,stopping=false;const samples=[];
 const stop=()=>stopping=true;process.once('SIGINT',stop);process.once('SIGTERM',stop);
 await post('/api/demo/session',{enabled:true});
 try{
  while(!stopping&&(performance.now()-start)/1000<seconds){
   const elapsed=(performance.now()-start)/1000,cycle=elapsed%70;let dist=Math.max(0,Math.min(1,(cycle-2)/58))*lengths.at(-1),i=1;
   while(i<lengths.length-1&&lengths[i]<dist)i++;
   const f=(dist-lengths[i-1])/(lengths[i]-lengths[i-1]||1),a=route[i-1],b=route[i];
   const yaw=Math.atan2(b[1]-a[1],b[0]-a[0])+(cycle>62?Math.min(cycle-62,4)*Math.PI/8:0);
   last={x_m:a[0]+(b[0]-a[0])*f,y_m:a[1]+(b[1]-a[1])*f,yaw_rad:yaw,lost:false,moving:cycle>2&&cycle<60||cycle>62&&cycle<66,ts_ms:Date.now(),demo:true};
   await post('/api/demo/pose',last);if(count%10===0)samples.push({...last,elapsed_s:elapsed});count++;
   const next=start+count*100,wait=next-performance.now();if(wait>0)await new Promise(r=>setTimeout(r,wait));
  }
 }finally{
  if(last)await post('/api/demo/pose',{...last,lost:true,moving:false,ts_ms:Date.now()});
  if(log)write('out/demo_stream.json',{transport:'isolated HTTP 8790',count,hz:10,elapsed_s:(performance.now()-start)/1000,samples,commands_sent:0,sensor_ports_accessed:[]});
  process.removeListener('SIGINT',stop);process.removeListener('SIGTERM',stop);
 }
 return {count,samples};
}
