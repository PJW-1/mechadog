import * as THREE from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';
import {RoomEnvironment} from 'three/addons/environments/RoomEnvironment.js';
import {EffectComposer} from 'three/addons/postprocessing/EffectComposer.js';
import {RenderPass} from 'three/addons/postprocessing/RenderPass.js';
import {GTAOPass} from 'three/addons/postprocessing/GTAOPass.js';
import {OutputPass} from 'three/addons/postprocessing/OutputPass.js';
import {buildFactoryMeshes,makeRobot,disposeFactoryResources} from './scene-materials.js';

const OVERVIEW = new THREE.Vector3(46,43,55);
const LOOK_AT = new THREE.Vector3(0,.3,-.7);

// Fit the complete factory in the overview at every aspect ratio.
// Only eight bounds corners are measured when the viewport changes.
export function getOverviewFrustum(bounds,aspect){
  const reference=new THREE.PerspectiveCamera();
  reference.position.copy(OVERVIEW);reference.lookAt(LOOK_AT);reference.updateMatrixWorld();
  let extentX=0,extentY=0;
  for(const x of [bounds.min.x,bounds.max.x])for(const y of [bounds.min.y,bounds.max.y])for(const z of [bounds.min.z,bounds.max.z]){
    const point=new THREE.Vector3(x,y,z).applyMatrix4(reference.matrixWorldInverse);
    extentX=Math.max(extentX,Math.abs(point.x));extentY=Math.max(extentY,Math.abs(point.y));
  }
  const halfHeight=Math.max(extentY,extentX/aspect)*1.06,halfWidth=halfHeight*aspect;
  return {left:-halfWidth,right:halfWidth,top:halfHeight,bottom:-halfHeight};
}

export class FactoryView {
  constructor({worldCanvas,fpvCanvas,layout,onRobot,onError,onProject,onObservation}) {
    Object.assign(this,{layout,onRobot,onError,onProject,onObservation,selected:'MD-01',playing:false,elapsed:0,frame:0,last:performance.now(),disposed:false,visible:true,active:true,worldVisible:true,cameraVisible:true,viewMode:'overview'});
    this.reduced=matchMedia('(prefers-reduced-motion: reduce)');
    this.scene=new THREE.Scene();
    // ACES at exposure 1.12 maps this input to the approved scene ground (#1e1f2c).
    this.scene.background=new THREE.Color(0x2c2d38);
    this.renderer=this.createRenderer(worldCanvas,true);
    this.fpvRenderer=this.createRenderer(fpvCanvas,true);
    this.camera=new THREE.OrthographicCamera(-32,32,14.4,-14.4,.1,200);
    this.camera.position.copy(OVERVIEW);
    this.fpvCamera=new THREE.PerspectiveCamera(74,1,.045,120);
    this.controls=new OrbitControls(this.camera,worldCanvas);
    this.controls.target.copy(LOOK_AT);
    this.controls.enableDamping=!this.reduced.matches;
    this.controls.dampingFactor=.13;
    this.controls.minZoom=.5;
    this.controls.maxZoom=6;
    this.controls.maxPolarAngle=Math.PI/2.08;
    this.controls.minPolarAngle=.05;
    this.controls.screenSpacePanning=false;
    this.controls.addEventListener('change',()=>this.invalidate());
    this.scene.add(new THREE.HemisphereLight(0xbfc9ed,0x696878,1.05));
    const key=new THREE.DirectionalLight(0xfff4e7,3.1);
    key.position.set(-12,38,23);
    key.castShadow=true;
    key.shadow.mapSize.set(2048,2048);
    Object.assign(key.shadow.camera,{left:-34,right:34,top:28,bottom:-28,near:.5,far:110});
    key.shadow.normalBias=.035;key.shadow.bias=-.00012;
    this.scene.add(key);
    const fill=new THREE.DirectionalLight(0xc1cdf4,.85);
    fill.position.set(20,15,-18);this.scene.add(fill);
    const room=new RoomEnvironment();
    const pmrem=new THREE.PMREMGenerator(this.renderer);
    this.environment=pmrem.fromScene(room,.04);
    const fpvPmrem=new THREE.PMREMGenerator(this.fpvRenderer);
    this.fpvEnvironment=fpvPmrem.fromScene(room,.04);
    this.scene.environment=this.environment.texture;
    this.scene.environmentIntensity=.65;
    room.dispose();pmrem.dispose();fpvPmrem.dispose();
    this.factory=buildFactoryMeshes(layout,this.scene);
    this.factory.root.updateMatrixWorld(true);
    this.factoryBounds=new THREE.Box3();
    for(const mesh of this.factory.root.children)if(mesh.visible)this.factoryBounds.expandByObject(mesh);
    this.makeRobots();this.drawRoute();
    this.composer=new EffectComposer(this.renderer);
    this.composer.addPass(new RenderPass(this.scene,this.camera));
    this.ao=new GTAOPass(this.scene,this.camera,640,360);
    this.ao.updateGtaoMaterial({radius:.75,distanceExponent:1,thickness:1.2,distanceFallOff:1,scale:1});
    this.ao.blendIntensity=.7;
    this.composer.addPass(this.ao);
    this.composer.addPass(new OutputPass());
    this.resizeObserver=new ResizeObserver(()=>this.resize());
    this.resizeObserver.observe(worldCanvas);this.resizeObserver.observe(fpvCanvas);
    this.resize();
    this.visibilityHandler=()=>{
      this.visible=!document.hidden;this.last=performance.now();
      if(!this.visible)this.setPlaying(false);else this.invalidate();
    };
    document.addEventListener('visibilitychange',this.visibilityHandler);
    for(const canvas of [worldCanvas,fpvCanvas]){
      canvas.addEventListener('webglcontextlost',e=>{
        e.preventDefault();this.lost=true;cancelAnimationFrame(this.frame);this.frame=0;
        onError('3D 그래픽 연결이 중단되었습니다. 다시 열기를 눌러 주세요.');
      });
      canvas.addEventListener('webglcontextrestored',()=>location.reload());
    }
    let start;
    worldCanvas.addEventListener('pointerdown',e=>{start=[e.clientX,e.clientY]});
    worldCanvas.addEventListener('pointerup',e=>{
      if(!start||Math.hypot(e.clientX-start[0],e.clientY-start[1])>5)return;
      const rect=worldCanvas.getBoundingClientRect(),ray=new THREE.Raycaster();
      ray.setFromCamera(new THREE.Vector2((e.clientX-rect.left)/rect.width*2-1,-(e.clientY-rect.top)/rect.height*2+1),this.camera);
      const hit=ray.intersectObjects(this.robots.map(r=>r.model),true)[0];
      if(hit){let parent=hit.object;while(parent&&!parent.userData.robotId)parent=parent.parent;if(parent)this.onRobot(parent.userData.robotId)}
    });
    this.invalidate();
  }

  createRenderer(canvas,shadows){
    const renderer=new THREE.WebGLRenderer({canvas,antialias:true,powerPreference:'high-performance'});
    renderer.setPixelRatio(Math.min(devicePixelRatio,1.5));
    renderer.outputColorSpace=THREE.SRGBColorSpace;
    renderer.toneMapping=THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure=1.12;
    renderer.shadowMap.enabled=shadows;
    renderer.shadowMap.type=THREE.PCFShadowMap;
    return renderer;
  }

  makeRobots(){
    const data=[['MD-01',-17,3.5,0],['MD-02',-1,-8,Math.PI/2],['MD-03',20,11,Math.PI/2]];
    this.robots=data.map(([id,x,z,angle])=>{
      const model=makeRobot();model.scale.setScalar(1.42);
      model.position.set(x,.04,z);model.rotation.y=angle;model.userData.robotId=id;
      this.scene.add(model);
      const ring=new THREE.Mesh(new THREE.RingGeometry(.68,.73,48),new THREE.MeshBasicMaterial({color:0xf3c75c,side:THREE.DoubleSide}));
      ring.rotation.x=-Math.PI/2;ring.position.set(x,.04,z);this.scene.add(ring);
      return{id,model,ring,base:[x,z,angle]};
    });
    this.attentionPosition=new THREE.Vector3(10,.2,3.8);
    const ring=new THREE.Mesh(new THREE.RingGeometry(.48,.54,40),new THREE.MeshBasicMaterial({color:0xffc94e,side:THREE.DoubleSide}));
    ring.rotation.x=-Math.PI/2;ring.position.copy(this.attentionPosition);ring.position.y=.055;this.scene.add(ring);
  }

  drawRoute(){
    const coords=[[-17,3.5],[-1,3.5],[-1,-12],[1,-12],[1,12],[20,12],[20,11]];
    this.routePoints=coords.map(([x,z])=>new THREE.Vector3(x,.05,z));
    const line=new THREE.Line(new THREE.BufferGeometry().setFromPoints(this.routePoints),new THREE.LineDashedMaterial({color:0xf7ce65,dashSize:.38,gapSize:.24}));
    line.computeLineDistances();this.scene.add(line);
    this.routeLength=this.routePoints.slice(1).reduce((sum,p,i)=>sum+p.distanceTo(this.routePoints[i]),0);
  }

  resize(){
    if(this.disposed)return;
    const a=this.renderer.domElement.getBoundingClientRect(),b=this.fpvRenderer.domElement.getBoundingClientRect();
    if(a.width>0&&a.height>0){
      const aspect=a.width/a.height;
      Object.assign(this.camera,getOverviewFrustum(this.factoryBounds,aspect));
      this.camera.updateProjectionMatrix();
      this.renderer.setSize(a.width,a.height,false);
      this.composer?.setSize(a.width,a.height);
      // Reduced-resolution AO is a spatial quality setting, not simulation frame skipping.
      this.ao?.setSize(Math.ceil(a.width*.8),Math.ceil(a.height*.8));
    }
    if(b.width>0&&b.height>0){
      this.fpvRenderer.setSize(b.width,b.height,false);
      this.fpvCamera.aspect=b.width/b.height;this.fpvCamera.updateProjectionMatrix();
    }
    this.invalidate();
  }

  selectRobot(id){if(this.robots.some(r=>r.id===id)){this.selected=id;this.invalidate()}}
  setPatrolRobot(id){if(this.robots.some(r=>r.id===id))this.patrolRobot=id}
  setPlaying(value){this.playing=!!value;this.last=performance.now();this.invalidate()}
  setCameraVisible(value){this.cameraVisible=value;this.invalidate()}
  setWorldVisible(value){this.worldVisible=!!value;this.controls.enabled=this.active&&this.worldVisible;this.invalidate()}
  setActive(value){
    if(this.active===!!value)return;
    this.active=!!value;this.controls.enabled=this.active&&this.worldVisible;this.last=performance.now();
    if(this.active)this.resize();else{cancelAnimationFrame(this.frame);this.frame=0}
  }
  setView(mode){
    this.viewMode=mode;this.camera.zoom=1;
    const robot=this.robots.find(r=>r.id===this.selected);
    if(mode==='top'){this.camera.position.set(.01,75,.01);this.controls.target.set(0,0,0)}
    else if(mode==='robot'){const pos=robot.model.position;this.camera.position.set(pos.x+12,10,pos.z+13);this.controls.target.copy(pos);this.camera.zoom=2.5}
    else{this.camera.position.copy(OVERVIEW);this.controls.target.copy(LOOK_AT)}
    this.camera.updateProjectionMatrix();this.controls.update();this.invalidate();
  }
  zoom(factor){this.camera.zoom=THREE.MathUtils.clamp(this.camera.zoom/factor,.5,6);this.camera.updateProjectionMatrix();this.invalidate()}
  focusZone(zone){
    this.viewMode='zone';this.controls.target.set(zone.center[0],0,-zone.center[1]);
    this.camera.position.copy(this.controls.target).add(new THREE.Vector3(24,35,30));
    this.camera.zoom=1.7;this.camera.updateProjectionMatrix();this.controls.update();this.invalidate();
  }
  orbit(angle){const offset=this.camera.position.clone().sub(this.controls.target).applyAxisAngle(new THREE.Vector3(0,1,0),angle);this.camera.position.copy(this.controls.target).add(offset);this.controls.update();this.invalidate()}

  updateRobot(dt){
    if(!this.playing)return;
    this.elapsed+=dt;
    const robot=this.robots.find(r=>r.id===this.patrolRobot)||this.robots[0],points=this.routePoints;
    // A reversible illustrative route avoids teleporting at an open route's end.
    const traveled=(this.elapsed*.7)%(this.routeLength*2);
    let distance=traveled<this.routeLength?traveled:this.routeLength*2-traveled;
    const reverse=traveled>=this.routeLength;
    let start=points[0],end=points[1];
    for(let i=0;i<points.length-1;i++){const length=points[i].distanceTo(points[i+1]);if(distance<=length){start=points[i];end=points[i+1];break}distance-=length}
    const direction=end.clone().sub(start);
    robot.model.position.copy(start).lerp(end,Math.min(1,distance/direction.length()));
    robot.model.position.y=.04;robot.model.rotation.y=-Math.atan2(direction.z,direction.x)+(reverse?Math.PI:0);
    for(const [i,leg]of robot.model.userData.legs.entries())leg.rotation.z=Math.sin(this.elapsed*6.5+(i%2?Math.PI:0))*.14;
    robot.ring.position.x=robot.model.position.x;robot.ring.position.z=robot.model.position.z;
  }

  invalidate(){if(!this.disposed&&!this.frame&&!this.lost&&this.visible&&this.active)this.frame=requestAnimationFrame(t=>this.render(t))}
  render(now){
    this.frame=0;if(this.disposed||this.lost||!this.visible||!this.active)return;
    const dt=Math.min((now-this.last)/1000,.1);this.last=now;
    this.updateRobot(dt);if(this.worldVisible)this.controls.update();
    for(const mesh of this.factory.interior)mesh.visible=false;
    if(this.worldVisible)this.composer.render();
    const selected=this.robots.find(r=>r.id===this.selected);
    this.onObservation?.({id:selected.id,x:selected.model.position.x,z:selected.model.position.z,heading:selected.model.rotation.y});
    const offset=new THREE.Vector3(.82,.61,0).applyAxisAngle(new THREE.Vector3(0,1,0),selected.model.rotation.y);
    this.fpvCamera.position.copy(selected.model.position).add(offset);
    const forward=new THREE.Vector3(15,.68,0).applyAxisAngle(new THREE.Vector3(0,1,0),selected.model.rotation.y);
    this.fpvCamera.lookAt(selected.model.position.clone().add(forward));
    if(this.cameraVisible){
      selected.model.visible=false;
      for(const mesh of this.factory.interior)mesh.visible=true;
      this.scene.environment=this.fpvEnvironment.texture;
      this.fpvRenderer.render(this.scene,this.fpvCamera);
      this.scene.environment=this.environment.texture;
      for(const mesh of this.factory.interior)mesh.visible=false;
      selected.model.visible=true;
    }
    const size=this.renderer.getSize(new THREE.Vector2());
    const project=(id,position)=>{const p=position.clone().project(this.camera);return{id,x:(p.x+1)*size.x/2,y:(1-p.y)*size.y/2,visible:p.z>-1&&p.z<1}};
    if(this.worldVisible)this.onProject([...this.robots.map(r=>project(r.id,r.model.position.clone().add(new THREE.Vector3(0,1.25,0)))),project('attention',this.attentionPosition.clone().add(new THREE.Vector3(0,1.8,0)))]);
    if(this.playing)this.invalidate();
  }
  dispose(){
    this.disposed=true;cancelAnimationFrame(this.frame);this.resizeObserver.disconnect();
    document.removeEventListener('visibilitychange',this.visibilityHandler);
    this.controls.dispose();this.composer.passes.forEach(p=>p.dispose?.());this.composer.dispose();
    this.renderer.dispose();this.fpvRenderer.dispose();this.environment.dispose();this.fpvEnvironment.dispose();disposeFactoryResources();
  }
}

export function renderRobotPreviews(canvases){
  const buffer=document.createElement('canvas');
  const renderer=new THREE.WebGLRenderer({canvas:buffer,alpha:true,antialias:true,preserveDrawingBuffer:true});
  renderer.setSize(200,132);renderer.outputColorSpace=THREE.SRGBColorSpace;
  renderer.toneMapping=THREE.ACESFilmicToneMapping;renderer.toneMappingExposure=1.15;
  const scene=new THREE.Scene();scene.add(new THREE.HemisphereLight(0xffffff,0x414150,2));
  const light=new THREE.DirectionalLight(0xffedbd,4);light.position.set(1,3,3);scene.add(light);
  const robot=makeRobot();scene.add(robot);
  const camera=new THREE.PerspectiveCamera(26,200/132,.01,20);camera.position.set(1.5,1.0,-2);camera.lookAt(0,.43,0);
  for(const canvas of canvases){canvas.width=200;canvas.height=132;renderer.render(scene,camera);canvas.getContext('2d').drawImage(buffer,0,0)}
  renderer.dispose();
}
