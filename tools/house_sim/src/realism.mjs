import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {ROOT,read,write,revision,mapData} from './unify.mjs';
import {routeCollisions,mapAgreement,footprints} from './realism-geometry.mjs';
import {exportUSD} from './export-usd.mjs';

const scene=read('data/scene.json'),baseline=read('data/baseline.json'),migration=read('out/migration.json');
if(scene.realism?.task==='T'){console.log('Task T already applied; preserving later edits.');process.exit(0);}
const original=structuredClone(scene),map=mapData(),before=routeCollisions(scene),changes=[],conflicts=[];
const protectedIds=new Set(migration.audit.filter(a=>a.protected_fields?.length).map(a=>a.id));
for(const o of scene.objects){const b=baseline.objects.find(p=>p.id===o.id);if(['position','yaw','visible','parts'].some(k=>JSON.stringify(o[k])!==JSON.stringify(b?.[k])))protectedIds.add(o.id);}
// Removal is an explicit user observation; retain canonical geometry so undo
// remains possible, and never change the navigation map to infer new free cells.
const removed=scene.objects.find(o=>o.id==='passage_storage_boxes');
if(protectedIds.has(removed.id))conflicts.push({id:removed.id,reason:'user edit protected; removal pending'});
else{removed.visible=false;removed.evidence.push('Task T: user reports A–B passage storage removed 2026-10-05; navigation map unchanged.');changes.push({id:removed.id,label:removed.label,type:'removed',translation_m:0,reason:'explicit user observation'});}

const drawers=scene.objects.find(o=>o.id==='living_drawers');
if(protectedIds.has(drawers.id))conflicts.push({id:drawers.id,reason:'user edit protected; route conflict retained'});
else{
 const beforeMap=mapAgreement(drawers,map),old=structuredClone(drawers);
 // Preserve the rear face and TV clearance. Its modeled front extends into
 // repeatedly observed free cells. Shorten that axis rather than moving the
 // cabinet into a wall or its neighboring TV cabinet. Width remains a fit,
 // not an independently measured furniture dimension.
 const min=Math.min(...drawers.parts.flatMap(p=>p.vertices.map(v=>v[0]))),scale=.8;
 for(const p of [...drawers.parts,...(drawers.lod_parts||[])])for(const v of p.vertices)v[0]=min+(v[0]-min)*scale;
 const afterMap=mapAgreement(drawers,map);
 if(afterMap.free>=beforeMap.free||routeCollisions({objects:[drawers]}).hits.length){Object.assign(drawers,old);conflicts.push({id:drawers.id,reason:'fit did not improve observed free-space agreement'});}
 else{drawers.evidence.push('Task T: lidar free-cell/front conflict; rear-anchored local X fit 0.8, unmeasured width estimate.');changes.push({id:drawers.id,label:drawers.label,type:'shortened',local_axis:'X',scale,old_width_m:.42,new_width_m:.336,translation_m:0,front_retraction_m:.084,map_before:beforeMap,map_after:afterMap,reason:'observed free cells and swept S0–B–A robot envelope; TV-side rear face retained'});}
}

const audit=[];
for(const o of scene.objects.filter(o=>o.category==='furniture'&&o.visible)){
 const agreement=mapAgreement(o,map),hasSlab=footprints(o).length>0;
 audit.push({id:o.id,label:o.label,protected:protectedIds.has(o.id),slice_height_m:.225,agreement,status:!hasSlab?'elevated_robot_clearance_exception':!footprints(o,.2249,.2251).length?'below_lidar_slice_exception':protectedIds.has(o.id)&&agreement.free?'user_edit_conflict_preserved':agreement.free?'free_cell_disagreement_semantic_fit_unresolved':'no_observed_free_overlap',note:'Per-part convex slabs; tabletops above robot excluded, legs remain separate. Unobserved cells are not evidence of a solid cabinet.'});
}
const floorIds=new Set(scene.objects.filter(o=>o.tags?.includes('floor')).flatMap(o=>o.parts.map(p=>p.material)));
for(const id of floorIds){const m=scene.materials[id];m.roughness=.86;m.metalness=0;if(m.texture)m.repeat=[6,6];}
scene.realism={task:'T',source_revision:original.revision,protected_ids:[...protectedIds],changes,conflicts,limits:['one-height lidar is not semantic furniture identification','unverified body-to-lidar XY extrinsic','furniture width fit is not measured','original 8789 instance untouched; isolated task-t copy']};
scene.updated_at=new Date().toISOString();scene.revision=revision(scene);
write('out/scene_before_T.json',original);write('data/scene.json',scene);
// Baseline must share geometry for undo; retain original user transforms.
for(const change of changes){const o=scene.objects.find(o=>o.id===change.id),b=baseline.objects.find(o=>o.id===change.id);b.parts=structuredClone(o.parts);b.visible=o.visible;b.evidence=o.evidence;}
baseline.materials=structuredClone(scene.materials);baseline.realism=scene.realism;baseline.revision=revision(baseline);write('data/baseline.json',baseline);
const after=routeCollisions(scene);
const mapHashes=fs.readdirSync(path.join(ROOT,'data/map')).map(name=>({name,sha256:crypto.createHash('sha256').update(fs.readFileSync(path.join(ROOT,'data/map',name))).digest('hex')}));
write('out/realism.json',{created_at:new Date().toISOString(),revision:scene.revision,before,after,changes,conflicts,audit,map_hashes:mapHashes,original_source_revision:original.revision,protection:[...protectedIds].map(id=>({id,preserved:JSON.stringify(original.objects.find(o=>o.id===id))===JSON.stringify(scene.objects.find(o=>o.id===id))}))});
exportUSD(scene);console.log(JSON.stringify({changes,conflicts,route_hits:after.hits,protected:protectedIds.size},null,2));
