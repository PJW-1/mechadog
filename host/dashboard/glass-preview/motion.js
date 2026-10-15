// Presentation only: no command, telemetry, camera-frame or polling changes.
const reduced=matchMedia('(prefers-reduced-motion: reduce)');
const running=new Map();
const ease='cubic-bezier(.16,1,.3,1)';
function animate(element,keyframes,duration=240){
  running.get(element)?.cancel();
  if(reduced.matches||!element?.animate)return;
  const animation=element.animate(keyframes,{duration,easing:ease});
  running.set(element,animation);
  const clean=()=>{if(running.get(element)===animation)running.delete(element)};
  animation.addEventListener('finish',clean,{once:true});
  animation.addEventListener('cancel',clean,{once:true});
}
const panel=document.querySelector('#panel-content');
const observer=new MutationObserver(records=>{
  if(records.some(record=>record.oldValue!==panel.dataset.page)){
    animate(panel,[{opacity:.72,transform:'translateY(10px)'},{opacity:1,transform:'none'}]);
  }
});
if(panel)observer.observe(panel,{attributes:true,attributeFilter:['data-page'],attributeOldValue:true});

// FLIP: commit the new size once; animate only its visual transform afterward.
// Click handlers remain synchronous, so resize and control semantics are unchanged.
let pending=0;
function onDockClick(event){
  if(!event.target.closest('#expand-camera,#collapse-camera')||reduced.matches)return;
  const dock=document.querySelector('#camera-dock');
  if(dock?.parentElement?.id!=='stage')return;
  running.get(dock)?.cancel();
  const before=dock.getBoundingClientRect();
  cancelAnimationFrame(pending);
  pending=requestAnimationFrame(()=>{
    pending=0;
    if(!dock.isConnected||dock.parentElement?.id!=='stage')return;
    const after=dock.getBoundingClientRect();
    if(!after.width||!after.height)return;
    animate(dock,[
      {transform:`translate(${before.left-after.left}px,${before.top-after.top}px) scale(${before.width/after.width},${before.height/after.height})`},
      {transform:'none'}
    ],280);
  });
}
document.addEventListener('click',onDockClick,true);
function cancelMotion(){
  cancelAnimationFrame(pending);pending=0;
  for(const animation of running.values())animation.cancel();
  running.clear();
}
reduced.addEventListener('change',cancelMotion);
window.addEventListener('pagehide',()=>{
  observer.disconnect();cancelMotion();
  document.removeEventListener('click',onDockClick,true);
  reduced.removeEventListener('change',cancelMotion);
},{once:true});
