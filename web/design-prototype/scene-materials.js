import * as THREE from 'three';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';

const materials = new Map();
const geometries = new Map();

export function getMaterial(color, metalness = .2, roughness = .55, options = {}) {
  const key = JSON.stringify([color, metalness, roughness, options]);
  if (!materials.has(key)) {
    const value = Array.isArray(color) ? new THREE.Color(...color).convertSRGBToLinear() : new THREE.Color(color);
    materials.set(key, new THREE.MeshStandardMaterial({ color: value, metalness, roughness, ...options }));
  }
  return materials.get(key);
}

function geometry(shape = 'box', bevel = false) {
  const key = shape + bevel;
  if (!geometries.has(key)) {
    let value;
    if (shape === 'cylinder') {
      value = new THREE.CylinderGeometry(.5, .5, 1, 16);
      value.rotateX(Math.PI / 2);
    } else if (shape === 'sphere') value = new THREE.SphereGeometry(.5, 12, 8);
    else value = bevel ? new RoundedBoxGeometry(1, 1, 1, 2, .024) : new THREE.BoxGeometry(1, 1, 1);
    geometries.set(key, value);
  }
  return geometries.get(key);
}

export function buildFactoryMeshes(layout, scene) {
  const root = new THREE.Group();
  root.rotation.x = -Math.PI / 2;
  scene.add(root);
  const batches = new Map();
  const interior = [];
  const bevelKinds = new Set(['cnc-cabinet','cnc-lid','cnc-door','storage-crate','service-tank','control-housing','pump-skid','truck-cab','truck-box','high-voltage-cabinet','service-cabinet']);
  for (const object of layout.objects) {
    const bevel = (object.shape || 'box') === 'box' && bevelKinds.has(object.kind);
    const key = JSON.stringify([object.shape || 'box', bevel, object.color, object.metalness ?? .15, object.roughness ?? .6, object.view || 'all',object.kind==='floor',['led-diffuser','wall-lamp'].includes(object.kind)]);
    if (!batches.has(key)) batches.set(key, []);
    batches.get(key).push(object);
  }
  const transform = new THREE.Object3D();
  for (const objects of batches.values()) {
    const first = objects[0];
    const shape = first.shape || 'box';
    const bevel = shape === 'box' && bevelKinds.has(first.kind);
    let material = getMaterial(first.color, first.metalness ?? .15, first.roughness ?? .6);
    if (['led-diffuser','wall-lamp'].includes(first.kind)) {
      material = getMaterial(first.color, .05, .2, {emissive:0xd4deff,emissiveIntensity:2.5});
    }
    if (first.kind === 'floor') {
      const texture = concreteTexture();
      material = new THREE.MeshPhysicalMaterial({
        color:0x777986,metalness:.16,roughness:.42,clearcoat:.48,clearcoatRoughness:.36,
        bumpMap:texture,bumpScale:.014,envMapIntensity:.8
      });
      materials.set('factory-floor',material);
    }
    const mesh = new THREE.InstancedMesh(geometry(shape, bevel), material, objects.length);
    mesh.castShadow = first.kind !== 'floor' && first.kind !== 'marking' && first.kind !== 'light';
    mesh.receiveShadow = true;
    mesh.name = first.kind;
    objects.forEach((object,index) => {
      transform.position.set(...object.position);
      transform.rotation.set(...(object.rotation || [0,0,0]),'XYZ');
      transform.scale.set(...object.size);
      transform.updateMatrix();
      mesh.setMatrixAt(index,transform.matrix);
    });
    mesh.instanceMatrix.needsUpdate = true;
    mesh.computeBoundingSphere();
    root.add(mesh);
    if (first.view === 'interior') {
      mesh.visible = false;
      interior.push(mesh);
    }
  }
  return {root,interior,batchCount:batches.size};
}

function concreteTexture() {
  const n=128, data=new Uint8Array(n*n*4);
  for(let y=0;y<n;y++)for(let x=0;x<n;x++){
    const grain=(Math.sin(x*127.1+y*311.7)*43758.5453)%1;
    const v=Math.round(128+grain*14+Math.sin(x*.13)*2);
    const i=(y*n+x)*4;data[i]=data[i+1]=data[i+2]=v;data[i+3]=255;
  }
  const texture=new THREE.DataTexture(data,n,n,THREE.RGBAFormat);
  texture.wrapS=texture.wrapT=THREE.RepeatWrapping;
  texture.repeat.set(12,8);texture.needsUpdate=true;
  return texture;
}

const ROBOT_COLORS={yellow:0xe1ac36,lightYellow:0xefbd4d,edgeYellow:0xb88423,dark:0x272c31,graphite:0x424951,silver:0xb9bec2,rubber:0x202328,board:0x192b24};
function robotGeometry(key,create){
  const id='mechdog:'+key;
  if(!geometries.has(id))geometries.set(id,create());
  return geometries.get(id);
}
function robotMesh(parent,shape,position,color,rotation=[0,0,0]){
  const silver=color===ROBOT_COLORS.silver,rubber=color===ROBOT_COLORS.rubber;
  const mesh=new THREE.Mesh(shape,getMaterial(color,silver?.75:rubber?.02:.23,silver?.3:rubber?.86:.48));
  mesh.position.set(...position);mesh.rotation.set(...rotation);
  mesh.castShadow=true;mesh.receiveShadow=true;parent.add(mesh);return mesh;
}
function box(parent,size,position,color,rotation=[0,0,0]){
  const mesh=robotMesh(parent,geometry('box',true),position,color,rotation);mesh.scale.set(...size);return mesh;
}
function disc(parent,diameter,depth,position,color,rotation=[0,0,0]){
  const mesh=robotMesh(parent,geometry('cylinder'),position,color,rotation);mesh.scale.set(diameter,diameter,depth);return mesh;
}
function ring(parent,radius,tube,position,color,rotation=[0,0,0]){
  return robotMesh(parent,robotGeometry('ring:'+radius+':'+tube,()=>new THREE.TorusGeometry(radius,tube,6,24)),position,color,rotation);
}
function polygonPath(points,Path=THREE.Path){
  const path=new Path();path.moveTo(...points[0]);for(const point of points.slice(1))path.lineTo(...point);path.closePath();return path;
}
function plate(parent,outline,holes,depth,position,color,rotation=[0,0,0]){
  const shape=robotGeometry('plate:'+JSON.stringify([outline,holes,depth]),()=>{
    const profile=polygonPath(outline,THREE.Shape);
    for(const hole of holes){
      let path;
      if(hole.points)path=polygonPath(hole.points);
      else if(hole.radius){path=new THREE.Path();path.absarc(...hole.center,hole.radius,0,Math.PI*2,true)}
      else{
        const [x,y]=hole.center,w=hole.width/2,h=hole.height/2,r=Math.min(w,h)*.8;
        path=new THREE.Path();path.moveTo(x-w+r,y-h);path.lineTo(x+w-r,y-h);path.quadraticCurveTo(x+w,y-h,x+w,y-h+r);
        path.lineTo(x+w,y+h-r);path.quadraticCurveTo(x+w,y+h,x+w-r,y+h);path.lineTo(x-w+r,y+h);path.quadraticCurveTo(x-w,y+h,x-w,y+h-r);
        path.lineTo(x-w,y-h+r);path.quadraticCurveTo(x-w,y-h,x-w+r,y-h);path.closePath();
      }
      profile.holes.push(path);
    }
    const value=new THREE.ExtrudeGeometry(profile,{depth,bevelEnabled:true,bevelThickness:.0015,bevelSize:.0015,bevelSegments:1,steps:1,curveSegments:8});
    value.translate(0,0,-depth/2);return value;
  });
  return robotMesh(parent,shape,position,color,rotation);
}
function rod(parent,start,end,diameter,color){
  const from=new THREE.Vector3(...start),to=new THREE.Vector3(...end),direction=to.clone().sub(from);
  const mesh=disc(parent,diameter,direction.length(),from.clone().add(to).multiplyScalar(.5).toArray(),color);
  mesh.quaternion.setFromUnitVectors(new THREE.Vector3(0,0,1),direction.normalize());return mesh;
}
// Shared endpoints preserve the existing illustrative group swing. This is
// photo-based display geometry, not a measured mechanism or an IK solver.
function robotLink(parent,start,end,width,depth,color,name){
  const from=new THREE.Vector3(...start),to=new THREE.Vector3(...end),direction=to.clone().sub(from);
  const mesh=box(parent,[width,direction.length(),depth],from.clone().add(to).multiplyScalar(.5).toArray(),color);
  mesh.quaternion.setFromUnitVectors(new THREE.Vector3(0,1,0),direction.normalize());mesh.name=name;return mesh;
}
function buildMechDogLeg(leg,side){
  const c=ROBOT_COLORS,hip=[0,0,0],knee=[-.145,-.215,0],ankle=[.045,-.515,0],outside=side*.054;
  // Servo cases sit inside folded sheet brackets, rather than exposed wheels.
  box(leg,[.089,.111,.065],[.012,.018,-side*.022],c.dark);
  box(leg,[.086,.118,.064],[-.068,-.070,-side*.020],c.dark,[0,0,-.42]);
  box(leg,[.098,.019,.080],[.008,.079,.008*side],c.lightYellow,[0,0,-.12]);
  box(leg,[.012,.087,.078],[.063,.032,0],c.yellow,[0,0,.18]);
  disc(leg,.065,.065,hip,c.dark).name='hip';
  robotLink(leg,hip,knee,.042,.042,c.edgeYellow,'upper-leg');
  robotLink(leg,knee,ankle,.052,.046,c.graphite,'lower-leg');
  const outline=[[.062,.080],[-.024,.089],[-.069,.059],[-.087,-.032],[-.193,-.184],[-.192,-.224],[-.163,-.247],[-.127,-.234],[.042,-.048]];
  plate(leg,outline,[{center:[0,0],radius:.027},{center:[.015,.056],radius:.010},{center:[-.102,-.133],radius:.009},{center:knee.slice(0,2),radius:.010}],.014,[0,0,outside],c.yellow);
  // The inner cheek and the folded top lip make the plate thickness readable.
  plate(leg,[[.048,.075],[-.042,.068],[-.067,.012],[-.034,-.044],[.033,-.038]],[],.009,[0,0,-side*.046],c.yellow);
  disc(leg,.040,.008,[0,0,outside+side*.009],c.dark);
  ring(leg,.023,.004,[0,0,outside+side*.012],c.silver);
  for(const [x,y]of [[.015,.056],[-.102,-.133]]){
    disc(leg,.019,.008,[x,y,outside+side*.010],c.dark);
    box(leg,[.008,.002,.003],[x,y,outside+side*.015],c.graphite);
  }
  disc(leg,.034,.062,knee,c.dark).name='knee';
  ring(leg,.012,.003,[knee[0],knee[1],outside+side*.011],c.silver);
  disc(leg,.012,.009,[knee[0],knee[1],outside+side*.013],c.silver);
  // Separate offset rod ends and silver tie rod, visible outside the upper plate.
  const a=[-.101,-.005,side*.088],b=[-.246,-.220,side*.088];
  robotLink(leg,[knee[0],knee[1],side*.078],b,.025,.018,c.dark);
  robotLink(leg,[-.045,.018,side*.069],a,.029,.019,c.dark);
  const along=t=>a.map((value,i)=>value+(b[i]-value)*t);
  rod(leg,a,b,.012,c.silver);
  rod(leg,a,along(.22),.026,c.dark);rod(leg,along(.78),b,.026,c.dark);
  for(const point of [a,b]){
    ring(leg,.012,.004,point,c.dark);
    disc(leg,.012,.011,[point[0],point[1],point[2]+side*.009],c.silver);
  }
  disc(leg,.073,.061,ankle,c.rubber).name='foot';
  const foot=box(leg,[.064,.073,.061],[.026,-.484,0],c.rubber);
  foot.quaternion.copy(leg.getObjectByName('lower-leg').quaternion);
}
// Merge static details by material once and reuse the resulting buffers in
// every robot. Keep named links/joints and the four movable groups intact.
function mergeRobotDetails(parent,assembly){
  const batches=new Map();
  for(const child of parent.children)if(child.isMesh&&!child.name){
    if(!batches.has(child.material))batches.set(child.material,[]);batches.get(child.material).push(child);
  }
  for(const [material,parts]of batches){
    const merged=robotGeometry(assembly+':'+material.uuid,()=>{
      const copies=parts.map(part=>{part.updateMatrix();const copy=part.geometry.index?part.geometry.toNonIndexed():part.geometry.clone();return copy.applyMatrix4(part.matrix)});
      const result=mergeGeometries(copies,false);copies.forEach(copy=>copy.dispose());return result;
    });
    parts.forEach(part=>parent.remove(part));
    const mesh=new THREE.Mesh(merged,material);mesh.castShadow=true;mesh.receiveShadow=true;parent.add(mesh);
  }
}

// Photo-derived display geometry; dimensions are illustrative, not a CAD model.
function buildMechDogBody(root, {box, plate, disc, ring, rod, colors}) {
  const {yellow, lightYellow, edgeYellow, dark, graphite, silver, board} = colors;
  const sideScrew = (x, y, z, diameter = .017) => {
    disc(root, diameter, .007, [x, y, z], dark);
    box(root, [diameter * .43, .002, .001], [x, y, z + Math.sign(z) * .004], graphite);
  };
  const frontScrew = (y, z) => {
    disc(root, .016, .008, [.459, y, z], dark, [0, Math.PI / 2, 0]);
  };

  // The chassis is an open folded frame. A solid yellow core would hide the
  // triangular openings, which are one of the real robot's defining features.
  for (const x of [-.29, .285]) {
    box(root, [.034, .026, .39], [x, .536, 0], edgeYellow);
  }
  box(root, [.31, .043, .22], [-.065, .554, 0], dark);
  box(root, [.29, .014, .265], [-.055, .691, 0], board);
  box(root, [.115, .032, .16], [-.305, .649, 0], graphite);
  const sideOutline = [
    [-.414, .552], [-.414, .694], [-.333, .729], [.060, .729],
    [.186, .714], [.373, .693], [.419, .656], [.401, .548],
    [.234, .528], [-.254, .528]
  ];
  const sideHoles = [
    {points: [[-.197, .570], [-.146, .646], [-.100, .570]]},
    {points: [[-.126, .646], [-.079, .570], [-.028, .646]]},
    {points: [[-.058, .570], [-.007, .646], [.040, .570]]},
    {points: [[.012, .646], [.059, .570], [.110, .646]]},
    {points: [[.088, .570], [.134, .640], [.176, .570]]}
  ];
  for (const side of [-1, 1]) {
    const z = side * .207;
    plate(root, sideOutline, sideHoles, .009, [0, 0, z], yellow);
    box(root, [.765, .014, .018], [-.003, .541, side * .194], edgeYellow);
    // Connector strip above the truss; no invented screen or status lights.
    box(root, [.257, .050, .009], [-.035, .681, side * .213], dark);
    box(root, [.230, .028, .006], [-.035, .689, side * .219], silver);
    for (const x of [-.123, -.077, -.031, .015, .061]) {
      box(root, [.031, .014, .002], [x, .689, side * .222], graphite);
      for (const offset of [-.009, 0, .009]) {
        disc(root, .0034, .002, [x + offset, .691, side * .2235], silver);
      }
    }
    for (const [x, y] of [[-.377, .579], [-.354, .680], [-.217, .673],
      [-.218, .552], [.203, .557], [.210, .675], [.361, .620]]) {
      sideScrew(x, y, side * .216);
    }
  }

  // Low rear deck and the small raised, slotted controller cover.
  plate(root, [[-.074, -.183], [.074, -.183], [.074, .183], [-.074, .183]],
    [-.042, .042].flatMap(x => [-.125, .125].map(y => ({center: [x, y], radius: .011}))),
    .009, [-.348, .729, 0], yellow, [-Math.PI / 2, 0, 0]);
  const coverOutline = [[-.173, -.139], [-.147, -.159], [.140, -.159],
    [.173, -.130], [.173, .130], [.140, .159], [-.147, .159], [-.173, .139]];
  const coverSlots = [-.090, -.057, -.024, .009, .042]
    .map(x => ({center: [x, 0], width: .012, height: .182}));
  coverSlots.push({center: [.117, -.088], radius: .008}, {center: [.117, .088], radius: .008});
  plate(root, coverOutline, coverSlots, .009, [-.111, .781, 0], lightYellow, [-Math.PI / 2, 0, 0]);
  for (const side of [-1, 1]) {
    plate(root, [[-.284, .744], [-.258, .781], [.029, .781], [.062, .755], [.047, .732], [-.270, .732]],
      [], .008, [0, 0, side * .153], yellow);
  }

  // Front hood is a sloping perforated sheet with an open notch at the nose.
  const hoodOutline = [[-.195, -.198], [.196, -.198], [.204, -.135],
    [.123, -.121], [.123, .121], [.204, .135], [.196, .198], [-.195, .198]];
  const hoodHoles = [];
  for (const x of [-.116, -.035, .046]) {
    for (const y of [-.079, .079]) {
      hoodHoles.push({center: [x, y], width: .048, height: .014});
    }
    for (const y of [-.153, .153]) hoodHoles.push({center: [x + .011, y], radius: .009});
  }
  for (const x of [-.142, -.063, .015, .081]) {
    hoodHoles.push({center: [x, 0], radius: .009});
  }
  const hoodSlope = Math.atan(.33);
  plate(root, hoodOutline, hoodHoles, .010, [.243, .749, 0], lightYellow, [-Math.PI / 2, hoodSlope, 0]);
  for (const side of [-1, 1]) {
    plate(root, [[.053, .811], [.429, .687], [.414, .617], [.341, .629], [.296, .687], [.081, .727]],
      [{points: [[.090, .785], [.230, .739], [.138, .737]]}],
      .009, [0, 0, side * .192], yellow);
    sideScrew(.346, .646, side * .201, .014);
    sideScrew(.089, .741, side * .201, .014);
  }

  // A thin front bezel frames the two metal ultrasonic transducers.
  const faceOutline = [[-.191, -.061], [-.166, -.096], [.166, -.096], [.191, -.061],
    [.191, .077], [.167, .096], [-.167, .096], [-.191, .077]];
  const eyeHoles = [-.083, .083].map(z => ({center: [z, 0], radius: .049}));
  plate(root, faceOutline, eyeHoles, .012, [.445, .593, 0], yellow, [0, Math.PI / 2, 0]);
  plate(root, [[-.149, -.055], [-.130, -.066], [-.034, -.062], [0, -.045],
    [.034, -.062], [.130, -.066], [.149, -.055], [.149, .055], [.130, .066],
    [.034, .062], [0, .045], [-.034, .062], [-.130, .066], [-.149, .055]],
    eyeHoles, .008, [.456, .593, 0], dark, [0, Math.PI / 2, 0]);
  for (const z of [-.083, .083]) {
    disc(root, .103, .029, [.462, .593, z], graphite, [0, Math.PI / 2, 0]);
    disc(root, .086, .003, [.478, .593, z], dark, [0, Math.PI / 2, 0]);
    ring(root, .047, .004, [.480, .593, z], silver, [0, Math.PI / 2, 0]);
    ring(root, .042, .0015, [.483, .593, z], silver, [0, Math.PI / 2, 0]);
    // Crossed wires form a real grille instead of a flat black or glass eye.
    for (const offset of [-.030, -.020, -.010, 0, .010, .020, .030]) {
      const half = Math.sqrt(.039 ** 2 - offset ** 2);
      rod(root, [.483, .593 + offset, z - half], [.483, .593 + offset, z + half], .0013, silver);
      rod(root, [.484, .593 - half, z + offset], [.484, .593 + half, z + offset], .0011, silver);
    }
  }
  for (const y of [.520, .667]) for (const z of [-.165, .165]) frontScrew(y, z);
  box(root, [.009, .105, .258], [-.422, .615, 0], graphite);
}

export function makeRobot(){
  // Manufacturer photos inform appearance only. Existing scene coordinates and
  // example motion remain unrelated to measured MechDog dimensions/telemetry.
  const root=new THREE.Group();root.userData.legs=[];
  buildMechDogBody(root,{box,plate,disc,ring,rod,colors:ROBOT_COLORS});
  mergeRobotDetails(root,'body');
  for(const x of [-.29,.285])for(const z of [-.245,.245]){
    const leg=new THREE.Group();leg.position.set(x,.61,z);
    buildMechDogLeg(leg,Math.sign(z));mergeRobotDetails(leg,'leg:'+Math.sign(z));
    root.add(leg);root.userData.legs.push(leg);
  }
  return root;
}

export function disposeFactoryResources(){
  for(const value of geometries.values())value.dispose();
  for(const value of materials.values()){value.bumpMap?.dispose();value.dispose();}
  geometries.clear();materials.clear();
}
