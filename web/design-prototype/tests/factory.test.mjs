import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile,access} from 'node:fs/promises';
import {resolve,dirname} from 'node:path';
import * as THREE from 'three';
import {buildFactoryMeshes,makeRobot,disposeFactoryResources} from '../scene-materials.js';
import {getOverviewFrustum} from '../scene.js';

const layout=JSON.parse(await readFile(new URL('../factory-layout.json',import.meta.url),'utf8'));
test('factory is newly authored schema 2, metre/Z-up, with no claimed robot dynamics',()=>{
 assert.equal(layout.schemaVersion,2);assert.equal(layout.units,'m');assert.equal(layout.upAxis,'Z');
 assert.deepEqual(layout.dimensions,{width:48,depth:32});assert.deepEqual(layout.robots,[]);
 assert.ok(layout.objects.length>2000);assert.ok(layout.objects.length<=6000);
});
test('factory identifiers and all dimensions are safe for instancing',()=>{
 const ids=new Set();
 for(const object of layout.objects){
  assert.ok(!ids.has(object.id));ids.add(object.id);
  assert.ok(object.position.every(Number.isFinite));assert.ok(object.size.every(n=>Number.isFinite(n)&&n>0));
  assert.ok(object.color.every(n=>n>=0&&n<=1));
  assert.ok(['box','cylinder','sphere'].includes(object.shape));
 }
});
test('all 5019 shapes map to instanced meshes, with separate removable ceiling',()=>{
 const factory=buildFactoryMeshes(layout,new THREE.Scene());
 assert.equal(factory.root.children.reduce((n,m)=>n+m.count,0),layout.objects.length);
 assert.ok(factory.batchCount<70);assert.ok(factory.interior.length>0);
 for(const mesh of factory.interior)assert.equal(mesh.visible,false);
 assert.equal(factory.root.rotation.x,-Math.PI/2);
});
test('visual robot has four legs and no physics or command endpoint',()=>{
 const robot=makeRobot();assert.equal(robot.userData.legs.length,4);
 assert.equal(robot.userData.physics,undefined);
 // The visible bars must meet the joint centers, including during the
 // existing group swing; this does not validate physical articulation.
 for(const leg of robot.userData.legs){
  for(const swing of [0,-.14,.14]){
   leg.rotation.z=swing;robot.updateMatrixWorld(true);
   const hip=leg.getObjectByName('hip').getWorldPosition(new THREE.Vector3());
   assert.ok(hip.distanceTo(leg.getWorldPosition(new THREE.Vector3()))<1e-9);
   for(const [linkName,startName,endName]of [['upper-leg','hip','knee'],['lower-leg','knee','foot']]){
    const link=leg.getObjectByName(linkName);
    const start=new THREE.Vector3(0,-.5,0).applyMatrix4(link.matrixWorld);
    const end=new THREE.Vector3(0,.5,0).applyMatrix4(link.matrixWorld);
    assert.ok(start.distanceTo(leg.getObjectByName(startName).getWorldPosition(new THREE.Vector3()))<1e-9);
    assert.ok(end.distanceTo(leg.getObjectByName(endName).getWorldPosition(new THREE.Vector3()))<1e-9);
   }
  }
 }
});

test('overview contains the factory at desktop, wide and phone aspect ratios',()=>{
 const factory=buildFactoryMeshes(layout,new THREE.Scene());factory.root.updateMatrixWorld(true);
 const bounds=new THREE.Box3();for(const mesh of factory.root.children)if(mesh.visible)bounds.expandByObject(mesh);
 for(const aspect of [1586/715,2560/938,390/490,320/490,1.399,1.401]){
  const camera=new THREE.OrthographicCamera();Object.assign(camera,getOverviewFrustum(bounds,aspect));
  camera.position.set(46,43,55);camera.lookAt(0,.3,-.7);camera.updateProjectionMatrix();camera.updateMatrixWorld();
  for(const x of [bounds.min.x,bounds.max.x])for(const y of [bounds.min.y,bounds.max.y])for(const z of [bounds.min.z,bounds.max.z]){
   const projected=new THREE.Vector3(x,y,z).project(camera);
   assert.ok(Math.abs(projected.x)<1&&Math.abs(projected.y)<1,`Factory clipped at aspect ${aspect}`);
  }
 }
 const before=getOverviewFrustum(bounds,1.399),after=getOverviewFrustum(bounds,1.401);
 assert.ok(Math.abs(after.right/before.right-1)<.005,'No framing jump at the former 1.4 breakpoint');
});
test('every browser module import resolves within the shipped static output',async()=>{
 const base=resolve('build');const queue=[resolve(base,'app.js')];const visited=new Set();
 while(queue.length){
  const file=queue.pop();if(visited.has(file))continue;visited.add(file);
  await access(file);const text=await readFile(file,'utf8');
  // Only module declarations; an event name such as emit('import') is not an import.
  for(const match of text.matchAll(/(?:\bfrom\s+['"]([^'"]+)['"]|^\s*import\s*['"]([^'"]+)['"])/gm)){
   const name=match[1]||match[2];let target;
   if(name==='three')target=resolve(base,'vendor/three.module.js');
   else if(name.startsWith('three/addons/'))target=resolve(base,'vendor/addons',name.slice(13));
   else if(name.startsWith('.'))target=resolve(dirname(file),name);
   else throw new Error('Unmapped browser import: '+name);
   assert.ok(target.startsWith(base));queue.push(target);
  }
 }
 assert.ok(visited.size>15);
});
test.after(()=>disposeFactoryResources());
