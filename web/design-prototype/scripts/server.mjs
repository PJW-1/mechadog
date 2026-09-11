import http from 'node:http';
import {readFile} from 'node:fs/promises';
import {extname,resolve,sep} from 'node:path';
const root=process.cwd();const port=4175;const types={'.html':'text/html; charset=utf-8','.css':'text/css; charset=utf-8','.js':'text/javascript; charset=utf-8','.json':'application/json; charset=utf-8','.png':'image/png','.jpg':'image/jpeg','.woff2':'font/woff2','.txt':'text/plain'};
const files=new Set(['index.html','styles.css','panels.css','app.js','operations.js','panels.js','webmcp.js','scene.js','scene-materials.js','robot-view.js','icons.js','factory-layout.json']);
const server=http.createServer(async(req,res)=>{
try{if(!['127.0.0.1:'+port,'localhost:'+port].includes(req.headers.host)){res.writeHead(403).end();return}
const pathname=decodeURIComponent(new URL(req.url,'http://127.0.0.1').pathname);
if(pathname==='/__health'){res.writeHead(200,{'Content-Type':'application/json'}).end('{"ok":true}');return}
if(req.method!=='GET'&&req.method!=='HEAD'){res.writeHead(405).end();return}
let relative=pathname.slice(1)||'index.html';
if(relative==='vendor/three.module.js'||relative==='vendor/three.core.js')relative='node_modules/three/build/'+relative.split('/').pop();
else if(relative.startsWith('vendor/addons/')&&relative.endsWith('.js')&&!relative.includes('..'))relative='node_modules/three/examples/jsm/'+relative.slice('vendor/addons/'.length);
else if(!files.has(relative)){res.writeHead(404).end('Not found');return}
const file=resolve(root,relative);if(!file.startsWith(root+sep)){res.writeHead(403).end();return}
const data=await readFile(file);res.writeHead(200,{'Content-Type':types[extname(file)]||'application/octet-stream','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'});res.end(req.method==='HEAD'?undefined:data);
}catch{res.writeHead(404).end('Not found')}
});
server.listen(port,'127.0.0.1',()=>console.log('Local: http://127.0.0.1:'+port+'/#dashboard'));
process.on('SIGINT',()=>server.close());
