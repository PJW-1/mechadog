import * as THREE from 'three';
export function initPresentation({world,request,robotLink,changeView,focusRobot,getCamera,getControls,renderNow,rendererInfo}){
 const $=id=>document.getElementById(id),root=new THREE.Group();root.name='UnifiedPresentation';world.add(root);
 const colors=[0x7db2ee,0x64ceb5,0xe8bb66,0xb7a0e7],labels=['A · 작업방','B · 거실','C · 주방','D · 침실'];
 let data,mode='plan',routeGroup=new THREE.Group(),trace=new THREE.Group(),occupancy=new THREE.Group(),frameTimes=[],lastFrame=0,lastTraceKey='',lastStatus='',cameraPose=null,eyeAnchor=null,lastHud=0,warmUntil=0;
 root.add(routeGroup,trace,occupancy);occupancy.visible=false;
 function textSprite(text,color){const c=document.createElement('canvas');c.width=512;c.height=96;const x=c.getContext('2d');x.fillStyle='rgba(255,255,255,.94)';x.fillRect(0,0,512,96);x.fillStyle=color;x.font='bold 42px "Malgun Gothic"';x.textAlign='center';x.fillText(text,256,62);const texture=new THREE.CanvasTexture(c);const sprite=new THREE.Sprite(new THREE.SpriteMaterial({map:texture,depthTest:false}));sprite.scale.set(1.5,.281,1);return sprite;}
 function clear(g){for(const c of [...g.children]){g.remove(c);c.geometry?.dispose();c.material?.dispose();}}
 function line(points,color,dashed=false){const g=new THREE.BufferGeometry().setFromPoints(points.map(p=>new THREE.Vector3(p[0],p[1],.035))),mat=dashed?new THREE.LineDashedMaterial({color,dashSize:.1,gapSize:.07}):new THREE.LineBasicMaterial({color});const l=new THREE.Line(g,mat);if(dashed)l.computeLineDistances();return l;}
 function plan(p){const f=data.pose_frame,a=f.rotate_deg*Math.PI/180;return[Math.cos(a)*p.x-Math.sin(a)*p.y+f.translate_x,Math.sin(a)*p.x+Math.cos(a)*p.y+f.translate_y];}
 async function load(){
 data=await request('/api/presentation');
 for(let z=1;z<=4;z++){const cells=data.zone_cells.filter(c=>c.zone===z),g=new THREE.PlaneGeometry(data.cell_size,data.cell_size),m=new THREE.MeshBasicMaterial({color:colors[z-1],transparent:true,opacity:.25,depthWrite:false,side:THREE.DoubleSide}),inst=new THREE.InstancedMesh(g,m,cells.length),matrix=new THREE.Matrix4();cells.forEach((c,i)=>{matrix.makeTranslation(c.xy[0],c.xy[1],.008);inst.setMatrixAt(i,matrix);});root.add(inst);}
 for(const z of data.zones){const n=z.id.charCodeAt(0)-65,s=textSprite(labels[n],'#245a64');s.position.set(z.plan.x,z.plan.y,.08);root.add(s);}
 const g=new THREE.BufferGeometry().setFromPoints(data.occupied.map(p=>new THREE.Vector3(p[0],p[1],.225)));occupancy.add(new THREE.Points(g,new THREE.PointsMaterial({color:0x31586b,size:.025})));
 const f=data.pose_frame,s=data.S0.S0_patrol,xy=plan(s),start=textSprite('S0 · 출발','#b57428');start.position.set(xy[0],xy[1],.12);start.scale.multiplyScalar(.65);root.add(start);
 $('presentation-note').textContent='실측 지도 · A 작업방 / B 거실 / C 주방 / D 침실';
 const audit=await request('/out/realism.json');
 const details=document.createElement('details'),summary=document.createElement('summary');summary.textContent='가구 정합 · 보존한 사용자 편집';details.append(summary);
 const explanation=document.createElement('p');explanation.textContent='시연 경로의 가구 겹침은 제거했습니다. 다른 장소의 자유 셀 대조는 단면 높이와 지도 관측 한계가 있어 추가 확인이 필요합니다.';details.append(explanation);
 for(const item of audit.audit.filter(a=>a.status==='user_edit_conflict_preserved')){const p=document.createElement('p');p.textContent=item.label+' · 사용자 편집 보존 · 지도 자유 셀과 충돌 후보';details.append(p);}
 $('presentation-note').after(details);
 }
 async function routes(){try{const r=await request('/api/routes');clear(routeGroup);const active=r.active??r.saved;for(const row of active||[])if(Array.isArray(row.points)&&row.points.length>1)routeGroup.add(line(row.points.map(plan),0xd9963c,true));$('route-note').textContent=r.available?(r.active?'현재 동선':'저장 동선 · 런타임 미연결'):'동선 API 미연결';}catch{$('route-note').textContent='동선 연결 대기';}finally{routeTimer=setTimeout(routes,5000);}}
 function select(next){frameTimes=[];lastFrame=0;warmUntil=performance.now()+2000;mode=next;cameraPose=null;eyeAnchor=null;for(const id of ['plan','follow','eye'])$('present-'+id).setAttribute('aria-pressed',String(id===mode));
 if(mode==='plan'){changeView('plan');getControls().enabled=true;document.getElementById('render-1080').dispatchEvent(new Event('change')); }else{const p=robotLink.getDisplayPose();if(p?.visible){focusRobot(p,robotLink.getModel()?.sensor_mount,mode==='eye');getControls().enabled=false;}}
 $('view-label').textContent={plan:'위에서 보기',follow:'로봇 따라가기',eye:'로봇 눈높이 · 가상 시점'}[mode];}
 for(const id of ['plan','follow','eye'])$('present-'+id).addEventListener('click',()=>select(id));
 $('show-map').addEventListener('change',()=>occupancy.visible=$('show-map').checked);
 $('resume-live').addEventListener('click',async()=>{await request('/api/robot/pose',{mode:'clear'});robotLink.refresh();});
 let routeTimer;load().then(()=>{select('plan');routes();}).catch(e=>$('presentation-note').textContent=e.message);
 window.addEventListener('pagehide',()=>clearTimeout(routeTimer));
 function metrics(){const ordered=[...frameTimes].sort((a,b)=>a-b),sum=ordered.reduce((a,b)=>a+b,0);return {samples:ordered.length,window_s:sum/1000,mean_fps:ordered.length?1000*ordered.length/sum:0,p95_frame_ms:ordered[Math.floor(ordered.length*.95)]||0,viewport:[window.innerWidth,window.innerHeight],canvas:[$('scene-canvas').width,$('scene-canvas').height],mode,warm_up_ms:2000,...rendererInfo()};}
 $('capture-evidence').addEventListener('click',async()=>{try{const canvas=$('scene-canvas');const metricsNow=metrics();if(metricsNow.samples<120)throw Error('시점 변경 후 최소 120프레임을 측정하세요.');renderNow();await request('/api/evidence',{view:mode,png:canvas.toDataURL('image/png'),metrics:metricsNow});$('evidence-note').textContent=`out/${mode}.png · ${metricsNow.mean_fps.toFixed(1)} FPS 저장`; }catch(e){$('evidence-note').textContent=e.message;}});
 function update(t){
 if(lastFrame&&t>warmUntil&&document.visibilityState==='visible'){frameTimes.push(t-lastFrame);while(frameTimes.length>600)frameTimes.shift();}lastFrame=t;
 const value=robotLink.getState(),p=robotLink.getDisplayPose(),ghost=['lost','stale'].includes(value?.pose?.status),sourceAge=value?.freshness?.source_age_ms;
 if(t-lastHud>250){lastHud=t;
 $('live-summary').textContent=value?.demo?'시연 자세 · 실물 로봇 없음':value?.pose?.status==='valid'?'실시간 위치 수신':ghost?'측위 상실/지연 · 마지막 정상 위치':'위치 입력 대기';
 $('live-delay').textContent=`${Number.isFinite(sourceAge)?Math.round(sourceAge)+' ms':'—'} 지연 · ${metrics().mean_fps.toFixed(0)} FPS`;
 }
 const key=value?.trail?.at(-1)?.ts_ms+':'+value?.trail?.length;
 if(key!==lastTraceKey&&value?.trail){lastTraceKey=key;clear(trace);let segment=[];for(const pt of value.trail){if(pt.break_before&&segment.length){trace.add(line(segment,0x288e89));segment=[];}segment.push([pt.x_m,pt.y_m]);}if(segment.length>1)trace.add(line(segment,0x288e89));}
 if(mode!=='plan'&&p?.visible){const camera=getCamera(),controls=getControls(),forward=new THREE.Vector3(Math.cos(p.yaw_rad),Math.sin(p.yaw_rad),0),target=new THREE.Vector3(p.x_m,p.y_m,.16);
 if(!cameraPose){focusRobot(p,undefined,mode==='eye');cameraPose=[p.x_m,p.y_m];document.getElementById('render-1080').dispatchEvent(new Event('change')); }
 const current=getCamera();controls.enabled=false;
 if(mode==='eye'){const mount=robotLink.getModel()?.sensor_mount?.camera_xyz_m||[0,0,.14],left=new THREE.Vector3(-forward.y,forward.x,0),pitch=THREE.MathUtils.degToRad(robotLink.getModel()?.sensor_mount?.camera_pitch_up_deg||0);current.position.set(p.x_m,p.y_m,mount[2]).addScaledVector(forward,mount[0]).addScaledVector(left,mount[1]);controls.target.copy(current.position).addScaledVector(forward,2*Math.cos(pitch));controls.target.z+=2*Math.sin(pitch);}
 else{const desired=target.clone().addScaledVector(forward,-1.5);desired.z=1.6;current.position.lerp(desired,.12);controls.target.lerp(target,.2);}
 current.lookAt(controls.target);
 }
 }
 return {update,metrics,select};
}
