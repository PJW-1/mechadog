import {mkdir,copyFile,cp} from 'node:fs/promises';
import {resolve} from 'node:path';
import {spawnSync} from 'node:child_process';
const publicFiles=['index.html','styles.css','panels.css','app.js','operations.js','panels.js','webmcp.js','scene.js','scene-materials.js','robot-view.js','icons.js','factory-layout.json'];
await mkdir('build',{recursive:true});
for(const file of publicFiles){if(file.endsWith('.js')){const result=spawnSync(process.execPath,['--check',file],{encoding:'utf8'});if(result.status!==0)throw new Error(result.stderr)}await copyFile(file,resolve('build',file))}
await mkdir('build/vendor/addons/controls',{recursive:true});
for(const name of ['three.module.js','three.core.js'])await copyFile('node_modules/three/build/'+name,'build/vendor/'+name);
for(const directory of ['controls','environments','geometries','postprocessing','shaders','math','objects','utils'])await cp('node_modules/three/examples/jsm/'+directory,'build/vendor/addons/'+directory,{recursive:true});
await copyFile('node_modules/three/LICENSE','build/vendor/THREE-LICENSE.txt');
console.log('Static build ready. Development metadata is excluded.');
