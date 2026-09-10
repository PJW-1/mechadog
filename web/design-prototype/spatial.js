/* Presentation only: the facility and animated robot are a visual demo, not physical simulation.
 * Static geometry is batched once. Camera motion runs only on visible, active surfaces.
 * No robot commands, network requests, telemetry or sensor inference in this module.
 */
(() => {
  'use strict';
  const canvas = document.querySelector('#facility-canvas');
  if (!canvas) return;
  const fallback = document.querySelector('#facility-fallback');
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
  const demoButton = document.createElement('button');
  demoButton.id = 'facility-demo';
  const demoPanel = document.createElement('div');
  demoPanel.className = 'facility-demo-panel';
  const demoLabel = document.createElement('span');
  demoLabel.textContent = '가상 MechDog · 실제 로봇 미연결';
  demoPanel.append(demoLabel, demoButton);
  canvas.parentElement.append(demoPanel);
  let demoRunning = !reduced.matches;
  function updateDemoButton() {
    demoButton.textContent = demoRunning ? '시뮬레이션 일시정지' : '시뮬레이션 재생';
    demoButton.setAttribute('aria-pressed', String(demoRunning));
  }
  updateDemoButton();
  demoButton.addEventListener('click', () => { demoRunning = !demoRunning; updateDemoButton(); renderRequest(); });
  document.querySelector('#estop-open')?.addEventListener('click', () => {
    demoRunning=false; updateDemoButton(); renderRequest();
  });
  const zones = {
    all: { title:'전체 시설', description:'입구, 조립, 자재 보관. 구역을 선택해 확인 항목을 살펴보세요.', tags:['PPE 확인','출입 인증','물품 변화'], center:[0,0,0], extent:20 },
    A: { title:'A / 입구 통로', description:'출입 인원과 보호구 착용을 확인하는 구역입니다. 실제 인증·감지 결과는 연결 후 표시됩니다.', tags:['안전모','안전조끼','출입 인증'], center:[-10,0,1], extent:12 },
    B: { title:'B / 조립 구역', description:'작업자 보호구와 작업대 주변 물품 변화를 확인합니다. 설비 배치는 관제 화면을 위한 개념입니다.', tags:['PPE 확인','물품 변화'], center:[0,0,-3], extent:13 },
    C: { title:'C / 자재 보관', description:'보관 구역의 출입과 물품 변화를 검토합니다. 실측 지도·로봇 위치 데이터는 아직 연결되지 않았습니다.', tags:['출입 인증','물품 변화'], center:[10,0,-2], extent:12 },
  };
  let selected = 'all';
  let renderRequest = () => {};
  const target = { yaw:.65, elevation:.68, extent:20, center:[0,0,0] };
  const camera = { ...target, center:[...target.center] };
  let rotating = !reduced.matches;
  let topView = false;
  const orbitButton = document.querySelector('#facility-orbit');
  const topButton = document.querySelector('#facility-top');
  function updateButtons() {
    orbitButton.setAttribute('aria-pressed', String(rotating));
    orbitButton.textContent = rotating ? '회전 정지' : '자동 회전';
    topButton.setAttribute('aria-pressed', String(topView));
  }
  function setRotation(value) { rotating = value; updateButtons(); renderRequest(); }
  function selectZone(key) {
    selected = key;
    const zone = zones[key];
    document.querySelectorAll('[data-facility-zone]').forEach(button => button.setAttribute('aria-pressed',String(button.dataset.facilityZone === key)));
    document.querySelector('#facility-zone-title').textContent = zone.title;
    document.querySelector('#facility-zone-description').textContent = zone.description;
    document.querySelector('#facility-zone-tags').replaceChildren(...zone.tags.map(text => { const item=document.createElement('span'); item.textContent=text; return item; }));
    target.center = [...zone.center]; target.extent=zone.extent;
    if (key !== 'all') setRotation(false);
    renderRequest();
  }
  document.querySelectorAll('[data-facility-zone]').forEach(button => button.addEventListener('click',() => selectZone(button.dataset.facilityZone)));
  orbitButton.addEventListener('click',() => setRotation(!rotating));
  topButton.addEventListener('click',() => {
    topView = !topView; target.elevation=topView ? 1.48 : .68;
    setRotation(false);
  });
  document.querySelector('#facility-reset').addEventListener('click',() => {
    Object.assign(target,{yaw:.65,elevation:.68,extent:20,center:[0,0,0]});
    topView=false; setRotation(false); selectZone('all');
  });
  function zoom(delta) { target.extent=Math.max(8,Math.min(30,target.extent+delta)); renderRequest(); }
  document.querySelector('#facility-zoom-in').addEventListener('click',() => zoom(-2));
  document.querySelector('#facility-zoom-out').addEventListener('click',() => zoom(2));
  updateButtons();

  let gl;
  try { gl=canvas.getContext('webgl',{alpha:true,antialias:true,powerPreference:'low-power'}); } catch { gl=null; }
  function showFallback(message) {
    demoButton.disabled=true;
    demoLabel.textContent='가상 MechDog · 3D 표시 불가';
    fallback.hidden=false;
    if (message) fallback.querySelector('strong').textContent=message;
    for (const button of document.querySelectorAll('.spatial-view-controls button')) button.disabled=true;
    canvas.tabIndex=-1;
  }
  if (!gl) { showFallback(); return; }
  try { initialize(); } catch (error) {
    showFallback('3D 장면을 초기화하지 못했습니다');
    console.warn('Facility presentation unavailable:',error.message);
  }

  function initialize() {
    const vertexSource = `
      attribute vec3 aPosition;
      attribute vec3 aNormal;
      attribute vec3 aColor;
      uniform mat4 uMatrix;
      uniform float uDim;
      uniform vec3 uRobot;
      uniform float uIsRobot;
      uniform float uGait;
      varying vec3 vColor;
      void main() {
        vec3 light=normalize(vec3(-.5,1.0,.65));
        float diffuse=max(dot(aNormal,light),0.0);
        float rim=max(dot(aNormal,normalize(vec3(.8,.5,-.6))),0.0);
        vColor=aColor*(.44+diffuse*.57+rim*.16)*uDim;
        vec3 p=aPosition;
        if(uIsRobot>0.5){
          if(p.y<0.65) p.x+=sin(uGait+sign(p.z)*sign(p.x)*1.5708)*0.12;
          float c=cos(uRobot.z),s=sin(uRobot.z);
          p=vec3(c*p.x-s*p.z+uRobot.x,p.y,s*p.x+c*p.z+uRobot.y);
        }
        gl_Position=uMatrix*vec4(p,1.0);
      }`;
    const fragmentSource = `precision mediump float; varying vec3 vColor; void main(){gl_FragColor=vec4(vColor,1.0);}`;
    function shader(type,source) {
      const item=gl.createShader(type); gl.shaderSource(item,source); gl.compileShader(item);
      if (!gl.getShaderParameter(item,gl.COMPILE_STATUS)) throw new Error('Shader compilation failed');
      return item;
    }
    const program=gl.createProgram();
    const vertex=shader(gl.VERTEX_SHADER,vertexSource), fragment=shader(gl.FRAGMENT_SHADER,fragmentSource);
    gl.attachShader(program,vertex); gl.attachShader(program,fragment); gl.linkProgram(program);
    if (!gl.getProgramParameter(program,gl.LINK_STATUS)) throw new Error('Shader link failed');
    gl.deleteShader(vertex); gl.deleteShader(fragment);
    const batches = new Map();
    let group='base';
    const color = hex => [parseInt(hex.slice(0,2),16)/255,parseInt(hex.slice(2,4),16)/255,parseInt(hex.slice(4,6),16)/255];
    const palette={ floor:color('425868'), edge:color('243947'), silver:color('b2c5cd'), dark:color('334b5a'), blue:color('76bddc'), amber:color('c89956'), green:color('76ad9d'), lane:color('b4c4cc'), box:color('9d9178') };
    function triangle(a,b,c,normal,tint) {
      if (!batches.has(group)) batches.set(group,[]);
      const output=batches.get(group);
      for (const p of [a,b,c]) output.push(...p,...normal,...tint);
    }
    function quad(a,b,c,d,n,tint) { triangle(a,b,c,n,tint); triangle(a,c,d,n,tint); }
    function box(x,y,z,w,h,d,tint) {
      const x0=x-w/2,x1=x+w/2,y0=y-h/2,y1=y+h/2,z0=z-d/2,z1=z+d/2;
      quad([x0,y1,z0],[x0,y1,z1],[x1,y1,z1],[x1,y1,z0],[0,1,0],tint);
      quad([x0,y0,z1],[x0,y0,z0],[x1,y0,z0],[x1,y0,z1],[0,-1,0],tint);
      quad([x0,y0,z1],[x1,y0,z1],[x1,y1,z1],[x0,y1,z1],[0,0,1],tint);
      quad([x1,y0,z0],[x0,y0,z0],[x0,y1,z0],[x1,y1,z0],[0,0,-1],tint);
      quad([x1,y0,z1],[x1,y0,z0],[x1,y1,z0],[x1,y1,z1],[1,0,0],tint);
      quad([x0,y0,z0],[x0,y0,z1],[x0,y1,z1],[x0,y1,z0],[-1,0,0],tint);
    }
    function cylinder(x,y,z,r,h,tint,segments=16) {
      for(let i=0;i<segments;i++) {
        const a=i*2*Math.PI/segments,b=(i+1)*2*Math.PI/segments,m=(a+b)/2;
        const a0=[x+Math.cos(a)*r,y-h/2,z+Math.sin(a)*r],a1=[a0[0],y+h/2,a0[2]];
        const b0=[x+Math.cos(b)*r,y-h/2,z+Math.sin(b)*r],b1=[b0[0],y+h/2,b0[2]];
        quad(a0,b0,b1,a1,[Math.cos(m),0,Math.sin(m)],tint);
        triangle([x,y+h/2,z],a1,b1,[0,1,0],tint);
      }
    }
    // Stage and low perimeter: intentionally no ceiling, roof beams or simulated point cloud.
    box(0,-.55,0,33,1,24,palette.edge);
    box(0,0,0,32,.12,23,palette.floor);
    for(let x=-16;x<=16;x+=2) box(x,.07,0,.025,.012,23,color('506778'));
    for(let z=-11;z<=11;z+=2) box(0,.07,z,32,.012,.025,color('506778'));
    box(0,.55,-11.4,32,1.1,.22,palette.silver);
    box(15.9,.55,0,.22,1.1,23,palette.silver);
    box(-15.9,.3,0,.22,.6,23,palette.dark);
    box(0,.10,5.5,31,.03,3.2,color('243f50'));
    for(let x=-15;x<16;x+=2) box(x,.13,5.5,1,.02,.08,palette.lane);
    for(const z of [3.85,7.15]) box(0,.13,z,31,.025,.06,palette.blue);
    for(let x=-15;x<=15;x+=6) { box(x,1.9,-11,.22,3.8,.22,palette.dark); box(x,3.75,-11,.5,.1,.5,palette.silver); }
    // Zone A: entrance gates, safety bollards and inspection desk.
    group='A';
    box(-11,.10,-2,8,.06,10,color('385e75'));
    for(const z of [-6.5,2.5]) box(-11,.15,z,7,.03,.05,palette.blue);
    for(const x of [-14.5,-7.5]) box(x,.15,-2,.05,.03,9,palette.blue);
    for(const x of [-13,-10,-7]) {
      box(x,1.1,1.9,.25,2.2,.4,palette.silver); box(x,2.15,1.9,.32,.1,.5,palette.blue);
      box(x,.5,2.0,1.8,.08,.15,palette.silver);
    }
    box(-11.8,.8,-3.5,3,1.6,1.4,palette.silver); box(-11.8,1.68,-3.5,3.15,.1,1.55,palette.dark);
    box(-11.4,2.1,-3.7,.8,.7,.09,palette.dark); box(-11.4,2.1,-3.64,.64,.5,.02,palette.blue);
    for(const x of [-14,-8]) { cylinder(x,.45,4.1,.11,.9,palette.amber,8); cylinder(x,.75,4.1,.12,.12,palette.dark,8); }
    // Zone B: six workstations, conveyor rollers and service cabinets.
    group='B';
    box(0,.1,-2,11,.06,14,color('596367'));
    for(const z of [-8.5,4.5]) box(0,.15,z,10,.03,.05,palette.amber);
    for(const x of [-5,5]) box(x,.15,-2,.05,.03,13,palette.amber);
    for(const x of [-2.8,2.8]) for(const z of [-6,-1.8,2]) {
      box(x,.55,z,2.9,1.1,1.9,palette.dark);
      box(x,1.2,z,3.05,.2,2,palette.silver);
      box(x,2,z-.45,2.1,1.4,.85,color('8ba3b1'));
      box(x,2.13,z+.01,1.4,.8,.08,palette.dark);
      box(x+.95,1.9,z+.06,.27,.32,.06,palette.blue);
      box(x,2.8,z-.45,2.15,.13,.88,palette.silver);
      for(const dx of [-1.25,1.25]) box(x+dx,.45,z,.12,.9,1.7,palette.silver);
      cylinder(x+.98,2.82,z-.45,.08,.18,palette.amber,8);
    }
    box(0,.5,-2,.75,1,12,palette.dark);
    for(let z=-7.7;z<4;z+=.42) box(0,1.04,z,.92,.13,.18,palette.silver);
    // Zone C: industrial shelving with individually separated beams and crates.
    group='C';
    box(10,.1,-2,8,.06,14,color('375b59'));
    for(const z of [-8.5,4.5]) box(10,.15,z,7,.03,.05,palette.green);
    for(const x of [6.5,13.5]) box(x,.15,-2,.05,.03,13,palette.green);
    for(const x of [8,12]) for(const z of [-6.5,-1.5]) {
      for(const dx of [-1.15,1.15]) for(const dz of [-1.55,1.55]) box(x+dx,1.65,z+dz,.11,3.3,.11,palette.silver);
      for(const y of [.35,1.75,3.1]) {
        box(x,y,z,2.55,.12,3.3,palette.amber);
        for(const dz of [-.85,.85]) { box(x-.45,y+.43,z+dz,.75,.75,1.05,palette.box); box(x+.47,y+.36,z+dz,.72,.6,1.05,color('889ba1')); }
      }
    }
    for(const x of [8.1,9.6,11.1]) { cylinder(x,.75,2.4,.51,1.5,palette.silver); cylinder(x,1.35,2.4,.53,.09,palette.dark); }
    // Utilities and loading apron. No invented robots, workers or detection results.
    group='base';
    for(const x of [-10,-5,0,5,10]) { box(x,.10,9.5,3,.05,2.2,color('304959')); box(x,.14,10.6,3,.02,.045,palette.lane); }
    for(const x of [-12,-10,-8]) { box(x,1.25,-9,1.4,2.5,1,palette.silver); box(x,1.5,-8.48,.9,1.5,.04,palette.dark); box(x+.25,1.8,-8.44,.25,.25,.035,palette.blue); }
    // Stylized demo robot. Separate immutable mesh; animation uses shader uniforms only.
    group='demo';
    box(0,1,0,1.65,.45,.8,color('eebc48'));
    box(.95,1.03,0,.35,.38,.74,color('eebc48'));
    for(const z of [-.23,.23]) box(1.14,1.08,z,.05,.2,.2,palette.dark);
    for(const x of [-.6,.6]) for(const z of [-.5,.5]) {
      box(x,.65,z,.2,.55,.18,palette.dark);
      box(x-.13,.3,z,.18,.4,.16,palette.silver);
      box(x-.13,.13,z,.36,.13,.23,palette.dark);
    }
    const meshes=[...batches].map(([zone,data]) => {
      const buffer=gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER,buffer);
      gl.bufferData(gl.ARRAY_BUFFER,new Float32Array(data),gl.STATIC_DRAW);
      return {zone,buffer,count:data.length/9};
    });
    canvas.dataset.sceneVertices=String(meshes.reduce((sum,mesh)=>sum+mesh.count,0));
    canvas.dataset.sceneBatches=String(meshes.length);
    canvas.dataset.sceneKind='concept-only';
    const position=gl.getAttribLocation(program,'aPosition'),normal=gl.getAttribLocation(program,'aNormal'),tint=gl.getAttribLocation(program,'aColor');
    const matrix=gl.getUniformLocation(program,'uMatrix'),dim=gl.getUniformLocation(program,'uDim');
    const robotUniform=gl.getUniformLocation(program,'uRobot');
    const robotFlag=gl.getUniformLocation(program,'uIsRobot');
    const gaitUniform=gl.getUniformLocation(program,'uGait');
    let demoTime=0;
    gl.enable(gl.DEPTH_TEST);
    const multiply=(a,b) => {
      const out=new Float32Array(16);
      for(let col=0;col<4;col++) for(let row=0;row<4;row++) for(let k=0;k<4;k++) out[col*4+row]+=a[k*4+row]*b[col*4+k];
      return out;
    };
    const normalize=v => { const length=Math.hypot(...v); return v.map(n=>n/length); };
    const cross=(a,b) => [a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
    const dot=(a,b) => a.reduce((sum,n,i)=>sum+n*b[i],0);
    function viewMatrix() {
      const eye=[camera.center[0]+Math.sin(camera.yaw)*Math.cos(camera.elevation)*50,Math.sin(camera.elevation)*50,camera.center[2]+Math.cos(camera.yaw)*Math.cos(camera.elevation)*50];
      const z=normalize(eye.map((n,i)=>n-camera.center[i])),x=normalize(cross([0,1,0],z)),y=cross(z,x);
      return new Float32Array([x[0],y[0],z[0],0,x[1],y[1],z[1],0,x[2],y[2],z[2],0,-dot(x,eye),-dot(y,eye),-dot(z,eye),1]);
    }
    let frame=null,lastTime=null,visible=true,lost=false,drag=null,disposed=false;
    let currentView=document.querySelector('.view.is-visible')?.id === 'view-dashboard';
    let preview=!document.body.classList.contains('no-data');
    function allowed() { return !disposed && !lost && !document.hidden && visible && currentView && preview; }
    function cancel() { if(frame !== null) cancelAnimationFrame(frame); frame=null; lastTime=null; }
    renderRequest=() => { if(!allowed()) { cancel(); return; } if(frame===null) frame=requestAnimationFrame(render); };
    function render(time) {
      frame=null;
      if(!allowed()) { lastTime=null; return; }
      const dt=lastTime===null ? 16 : Math.min(time-lastTime,50); lastTime=time;
      if(demoRunning) demoTime+=dt/1000;
      // Fixed illustration route entirely on the open loading apron. No planner or telemetry.
      const phase=demoTime*.16;
      const robotX=10*Math.sin(phase),robotZ=8.7+.9*Math.cos(phase);
      const heading=Math.atan2(-.9*Math.sin(phase),10*Math.cos(phase));
      canvas.dataset.demoTime=demoTime.toFixed(3);
      if(rotating && !drag) target.yaw+=dt*.000075;
      const blend=reduced.matches ? 1 : 1-Math.exp(-dt/110);
      let moving=false;
      for(const key of ['yaw','elevation','extent']) {
        camera[key]+=(target[key]-camera[key])*blend;
        if(Math.abs(target[key]-camera[key])>.0005) moving=true; else camera[key]=target[key];
      }
      camera.center=camera.center.map((n,i)=>{const value=n+(target.center[i]-n)*blend; if(Math.abs(value-target.center[i])>.0005) moving=true; return value;});
      const dpr=Math.min(window.devicePixelRatio||1,1.5);
      const width=Math.max(1,Math.round(canvas.clientWidth*dpr)),height=Math.max(1,Math.round(canvas.clientHeight*dpr));
      if(canvas.width!==width||canvas.height!==height) { canvas.width=width;canvas.height=height; }
      gl.viewport(0,0,width,height); gl.clearColor(0,0,0,0); gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);
      const aspect=width/height;
      // A stable orthographic drone view; fit the complete floor on narrow screens.
      const horizontal=camera.extent*Math.max(1,aspect*.72),vertical=horizontal/aspect;
      const projection=new Float32Array([1/horizontal,0,0,0,0,1/vertical,0,0,0,0,-2/120,0,0,-.06,-1,1]);
      gl.useProgram(program); gl.uniformMatrix4fv(matrix,false,multiply(projection,viewMatrix()));
      for(const mesh of meshes) {
        gl.uniform1f(robotFlag,mesh.zone==='demo'?1:0);
        gl.uniform3f(robotUniform,robotX,robotZ,heading);
        gl.uniform1f(gaitUniform,demoTime*9);
        gl.bindBuffer(gl.ARRAY_BUFFER,mesh.buffer);
        for(const [location,offset] of [[position,0],[normal,12],[tint,24]]) { gl.enableVertexAttribArray(location);gl.vertexAttribPointer(location,3,gl.FLOAT,false,36,offset); }
        gl.uniform1f(dim,selected==='all'||mesh.zone==='base'||mesh.zone==='demo'||mesh.zone===selected ? 1 : .43);
        gl.drawArrays(gl.TRIANGLES,0,mesh.count);
      }
      if(rotating||moving||demoRunning) renderRequest(); else lastTime=null;
    }
    canvas.addEventListener('pointerdown',event=>{
      if(event.button!==0 || drag) return;
      drag={id:event.pointerId,x:event.clientX,y:event.clientY};
      canvas.setPointerCapture(event.pointerId);setRotation(false);
    });
    canvas.addEventListener('pointermove',event=>{
      if(!drag||drag.id!==event.pointerId)return;
      target.yaw-=(event.clientX-drag.x)*.006;
      target.elevation=Math.max(.30,Math.min(1.48,target.elevation+(event.clientY-drag.y)*.004));
      topView=target.elevation>1.4;updateButtons();
      drag.x=event.clientX;drag.y=event.clientY;renderRequest();
    });
    const release=event=>{if(drag&&(!event||event.pointerId===undefined||event.pointerId===drag.id))drag=null;};
    for(const name of ['pointerup','pointercancel','lostpointercapture'])canvas.addEventListener(name,release);
    window.addEventListener('blur',()=>release());
    canvas.addEventListener('wheel',event=>{
      // Do not trap page scroll: zoom with the focused canvas or Ctrl modifier.
      if(document.activeElement!==canvas&&!event.ctrlKey)return;
      event.preventDefault();zoom(Math.sign(event.deltaY)*1.2);
    },{passive:false});
    canvas.addEventListener('keydown',event=>{
      if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','+','=','-'].includes(event.key)) {
        event.preventDefault();setRotation(false);
        if(event.key==='ArrowLeft')target.yaw-=.15;
        if(event.key==='ArrowRight')target.yaw+=.15;
        if(event.key==='ArrowUp')target.elevation=Math.min(1.48,target.elevation+.12);
        if(event.key==='ArrowDown')target.elevation=Math.max(.3,target.elevation-.12);
        if(event.key==='+'||event.key==='=')zoom(-2);
        if(event.key==='-')zoom(2);
        topView=target.elevation>1.4;updateButtons();renderRequest();
      }
    });
    window.addEventListener('preview:view',event=>{currentView=event.detail==='dashboard';release();renderRequest();});
    window.addEventListener('preview:state',()=>{preview=!document.body.classList.contains('no-data');renderRequest();});
    document.addEventListener('visibilitychange',()=>{release();renderRequest();});
    window.addEventListener('resize',renderRequest);
    reduced.addEventListener('change',()=>{if(reduced.matches){setRotation(false);demoRunning=false;updateDemoButton();}renderRequest();});
    const intersection='IntersectionObserver' in window ? new IntersectionObserver(entries=>{visible=entries[0].isIntersecting;renderRequest();}) : null;
    intersection?.observe(canvas);
    const resize='ResizeObserver' in window ? new ResizeObserver(renderRequest) : null;
    resize?.observe(canvas);
    canvas.addEventListener('webglcontextlost',event=>{event.preventDefault();lost=true;cancel();showFallback('3D 표시가 중단되었습니다. 새로고침해 주세요.');});
    window.addEventListener('pagehide',event=>{
      cancel();
      if(event.persisted)return;
      disposed=true;intersection?.disconnect();resize?.disconnect();
      meshes.forEach(mesh=>gl.deleteBuffer(mesh.buffer));gl.deleteProgram(program);
    });
    window.addEventListener('pageshow',renderRequest);
    renderRequest();
  }
})();
