// Explicit static build: never publishes, changes Git state or copies private documents.
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const root = __dirname;
const files = ['index.html','styles.css','console.css','spatial.css','operation-state.js','app.js','review.js','spatial.js','assets/hiwonder-mechdog.jpg'];
for (const file of files) {
  const source=path.join(root,file);
  if (!fs.existsSync(source)) throw new Error('Missing asset: '+file);
  if(file.endsWith('.js')) execFileSync(process.execPath,['--check',source]);
}
for(const file of files) {
  const destination=path.join(root,'build',file);
  fs.mkdirSync(path.dirname(destination),{recursive:true});
  fs.copyFileSync(path.join(root,file),destination);
  if(!fs.readFileSync(path.join(root,file)).equals(fs.readFileSync(destination))) throw new Error('Build mismatch: '+file);
}
console.log('Static build verified: '+files.length+' files. No deployment performed.');
