// Finite, retargetable camera interpolation. The scene owns requestAnimationFrame.
// This never changes a robot pose, simulated route or incoming telemetry.
export function createCameraFlight(from,to,start,duration=380){
  const a={position:[...from.position],target:[...from.target],zoom:from.zoom};
  const b={position:[...to.position],target:[...to.target],zoom:to.zoom};
  return {
    destination:b,
    sample(now){
      const progress=duration<=0?1:Math.min(1,Math.max(0,(now-start)/duration));
      const weight=progress===1?1:1-Math.pow(2,-10*progress);
      const mix=(x,y)=>progress===1?y:x+(y-x)*weight;
      return {position:a.position.map((value,i)=>mix(value,b.position[i])),
        target:a.target.map((value,i)=>mix(value,b.target[i])),
        zoom:mix(a.zoom,b.zoom),done:progress===1};
    }
  };
}
