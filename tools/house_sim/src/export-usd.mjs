import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {ROOT,write} from './unify.mjs';
const tuple=v=>'('+v.map(x=>Number(x).toPrecision(9)).join(', ')+')';
const lin=x=>x<=.04045?x/12.92:((x+.055)/1.055)**2.4;
export function exportUSD(scene){
 const objects=scene.objects,lines=['#usda 1.0','(','    defaultPrim = "World"','    metersPerUnit = 1','    upAxis = "Z"','    customLayerData = {',`        string sceneRevision = "${scene.revision}"`,'        bool navigationReady = false','        string sourceFrame = "floorplan_xy_m_z_up"','    }',')','def Xform "World"','{','def Scope "Materials"','{'];
 const textures=[];
 for(const [id,m]of Object.entries(scene.materials)){
 const base='/World/Materials/'+id;
 lines.push(`def Material "${id}" { token outputs:surface.connect = <${base}/Surface.outputs:surface>`,`def Shader "Surface" { uniform token info:id = "UsdPreviewSurface"`,`color3f inputs:diffuseColor = ${tuple((m.color||[.8,.8,.8]).map(lin))}`,`float inputs:roughness = ${m.roughness??.65}`,`float inputs:metallic = ${m.metalness??0}`,`float inputs:opacity = ${m.opacity??1}`);
 if(m.emissive)lines.push(`color3f inputs:emissiveColor = ${tuple(m.emissive)}`);
 if(m.texture){lines.push(`color3f inputs:diffuseColor.connect = <${base}/Texture.outputs:rgb>`);textures.push(m.texture);const dest=path.join(ROOT,'exports',m.texture);fs.mkdirSync(path.dirname(dest),{recursive:true});fs.copyFileSync(path.join(ROOT,'data',m.texture),dest);}
 lines.push('token outputs:surface','}');
 if(m.texture)lines.push('def Shader "ST" { uniform token info:id = "UsdPrimvarReader_float2"','token inputs:varname = "st"','float2 outputs:result','}', 'def Shader "Texture" { uniform token info:id = "UsdUVTexture"',`asset inputs:file = @${m.texture}@`,'token inputs:sourceColorSpace = "sRGB"','token inputs:wrapS = "repeat"','token inputs:wrapT = "repeat"',`float2 inputs:st.connect = <${base}/ST.outputs:result>`,'float3 outputs:rgb','}');lines.push('}');
 }
 lines.push('}','def Scope "Objects"','{');let triangles=0,vertices=0;
 for(const o of objects){lines.push(`def Xform "${o.id}" {`,`double3 xformOp:translate = ${tuple(o.position)}`,`float xformOp:rotateZ = ${o.yaw||0}`,'uniform token[] xformOpOrder = ["xformOp:translate", "xformOp:rotateZ"]');if(o.visible===false)lines.push('token visibility = "invisible"');
 for(const [i,p]of o.parts.entries()){triangles+=p.triangles.length;vertices+=p.vertices.length;lines.push(`def Mesh "part_${String(i).padStart(3,'0')}" (prepend apiSchemas = ["MaterialBindingAPI"]) {`,`point3f[] points = [${p.vertices.map(tuple).join(',')}]`,`int[] faceVertexCounts = [${p.triangles.map(()=>3).join(',')}]`,`int[] faceVertexIndices = [${p.triangles.flat().join(',')}]`,'uniform token subdivisionScheme = "none"','uniform bool doubleSided = true',`rel material:binding = </World/Materials/${p.material}>`);
 if(p.normals)lines.push(`normal3f[] normals = [${p.normals.map(tuple).join(',')}] (interpolation = "vertex")`);
 if(p.uvs){const r=scene.materials[p.material].repeat||[1,1];lines.push(`texCoord2f[] primvars:st = [${p.uvs.map(v=>tuple(v.map((x,k)=>x*r[k]))).join(',')}] (interpolation = "vertex")`);}lines.push('}');}
 lines.push('}');}
 lines.push('}','def DomeLight "Ambient" { float inputs:intensity = 600 }','}');
 const text=lines.join('\n');fs.mkdirSync(path.join(ROOT,'exports'),{recursive:true});fs.writeFileSync(path.join(ROOT,'exports/house_sim.usda'),text);write('exports/scene.json',scene);
 const report={revision:scene.revision,objects:objects.length,vertices,triangles,textures:[...new Set(textures)],sha256:crypto.createHash('sha256').update(text).digest('hex'),navigation_ready:false,commands_enabled:false,stage_open_verified:false,exporter:'ASCII USD; PXR reopen pending; visual-only, no physics'};write('exports/manifest.json',report);return report;
}
