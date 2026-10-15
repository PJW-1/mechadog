import test from 'node:test';
import assert from 'node:assert/strict';
import {createCameraFlight} from '../static/camera-flight.js';
import * as THREE from 'three';
import {FactoryView} from '../static/scene.js';
const from={position:[46,43,55],target:[0,.3,-.7],zoom:1};
const to={position:[4,10,16],target:[-8,0,3],zoom:2.5};

test('camera flight reaches the exact destination and terminates even after a delayed frame',()=>{
  const flight=createCameraFlight(from,to,100,380);
  assert.deepEqual(flight.sample(100),{...from,done:false});
  assert.deepEqual(flight.sample(480),{...to,done:true});
  assert.deepEqual(flight.sample(9000),{...to,done:true});
});
test('flight interpolates position, look target and zoom without overshoot',()=>{
  const flight=createCameraFlight(from,to,0,380);
  let previous=from.zoom;
  for(let now=0;now<=380;now+=10){
    const pose=flight.sample(now);
    assert.ok(pose.zoom>=previous&&pose.zoom<=to.zoom);previous=pose.zoom;
    for(const key of ['position','target'])pose[key].forEach((n,i)=>assert.ok(n>=Math.min(from[key][i],to[key][i])&&n<=Math.max(from[key][i],to[key][i])));
  }
});
test('rapid retargeting starts at the current visible pose, not the previous endpoint',()=>{
  const first=createCameraFlight(from,to,0,380),visible=first.sample(120);
  const second=createCameraFlight(visible,from,120,240);
  assert.deepEqual(second.sample(120),{...visible,done:false});
  assert.deepEqual(second.sample(360),{...from,done:true});
});
test('zero-duration reduced motion resolves immediately and takes immutable input snapshots',()=>{
  const start=structuredClone(from),end=structuredClone(to);
  const flight=createCameraFlight(start,end,100,0);
  start.position[0]=900;end.target[0]=900;
  assert.deepEqual(flight.sample(100),{...to,done:true});
});

function viewStub(reduced=false){
  const view=Object.create(FactoryView.prototype);
  view.camera=new THREE.OrthographicCamera();view.camera.position.fromArray(from.position);view.camera.zoom=1;
  view.controls={target:new THREE.Vector3().fromArray(from.target),enableDamping:true,update(){}};
  view.reduced={matches:reduced};view.invalidate=()=>{};
  return view;
}
test('scene zoom accumulates rapid clicks and enforces the existing limits',()=>{
  const view=viewStub();
  view.zoom(.8);view.zoom(.8);
  assert.equal(view.cameraFlight.destination.zoom,1.5625);
  view.updateCameraFlight(Infinity);assert.equal(view.camera.zoom,1.5625);assert.equal(view.cameraFlight,null);
  view.zoom(.001);view.updateCameraFlight(Infinity);assert.equal(view.camera.zoom,6);
  view.zoom(100);view.updateCameraFlight(Infinity);assert.equal(view.camera.zoom,.5);
});
test('user orbit interruption leaves the visible pose in place and restores damping',()=>{
  const view=viewStub();view.zoom(.8);
  const position=view.camera.position.clone(),zoom=view.camera.zoom;
  view.cancelCameraFlight();view.updateCameraFlight(Infinity);
  assert.equal(view.cameraFlight,null);assert.equal(view.controls.enableDamping,true);
  assert.deepEqual(view.camera.position,position);assert.equal(view.camera.zoom,zoom);
});
test('reduced-motion scene zoom applies immediately without leaving an active flight',()=>{
  const view=viewStub(true);view.zoom(.8);
  assert.equal(view.camera.zoom,1.25);assert.equal(view.cameraFlight,null);assert.equal(view.controls.enableDamping,false);
});
test('leaving the scene cancels its flight and animation frame',t=>{
  const view=viewStub();view.active=true;view.frame=42;view.zoom(.8);
  const calls=[],previous=globalThis.cancelAnimationFrame;
  globalThis.cancelAnimationFrame=id=>calls.push(id);
  t.after(()=>{if(previous)globalThis.cancelAnimationFrame=previous;else delete globalThis.cancelAnimationFrame});
  view.setActive(false);assert.equal(view.cameraFlight,null);assert.equal(view.frame,0);assert.deepEqual(calls,[42]);
});
test('video resizing uses layout size, not temporary FLIP-transformed bounds',()=>{
  const view=viewStub(),sizes=[];
  view.renderer={domElement:{clientWidth:0,clientHeight:0,getBoundingClientRect:()=>({width:0,height:0})}};
  view.fpvRenderer={domElement:{clientWidth:640,clientHeight:480,getBoundingClientRect:()=>({width:1300,height:30})},setSize:(...size)=>sizes.push(size)};
  view.fpvCamera=new THREE.PerspectiveCamera();view.resize();
  assert.deepEqual(sizes,[[640,480,false]]);assert.equal(view.fpvCamera.aspect,4/3);
});
