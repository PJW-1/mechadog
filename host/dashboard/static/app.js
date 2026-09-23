import {FactoryView,renderRobotPreviews} from './scene.js';
import {icon,renderIcons} from './icons.js';
import {Operations,ROBOTS} from './operations.js';
import {OperationalPanels} from './panels.js';
import {registerPageTools} from './webmcp.js';
import {RobotDetailView} from './robot-view.js';
import {RobotLink} from './robot-link.js';
import {VoiceLink,resolveVoiceBase} from './voice-link.js';
import {VisionFeed} from './vision-feed.js';
import {EventFeed} from './event-feed.js';
import {TelemetryFeed,describeTelemetry} from './telemetry-feed.js';

renderIcons();
const $=id=>document.getElementById(id);
let view=null,robotView=null,toastTimer,currentPage='dashboard',lastOpener=null,observationFailed=false;
let visionFeed=null,visionStatus={state:'connecting'};
// 로봇 시점 창의 문구는 **실제로 받고 있는 상태**를 말한다. 연결만 됐다고
// "실시간" 이라 하지 않는다 — 멈춘 장면이 실시간처럼 보이면 안 된다.
const VISION_TEXT={off:['영상 없음 · 비전 꺼짐','비전 채널 없음'],connecting:['영상 연결 중','연결 중'],waiting:['영상 대기 · 추론 결과 없음','연결됨 · 영상 대기'],live:['실시간 · 검출 박스','실시간 수신 중'],stale:['영상 멈춤 · 마지막 장면','새 영상 없음'],closed:['영상 끊김 · 다시 연결 중','연결 끊김 · 다시 연결 중']};
function syncVisionStatus(){
 $('app').classList.toggle('vision-has-frame',!!visionStatus.lastFrameAt);
 if(!operations.live)return;
 const [badge,status]=VISION_TEXT[visionStatus.state]||VISION_TEXT.connecting;
 $('frame-source').textContent=badge;
 $('camera-status').textContent=visionStatus.state==='live'?status+' · 검출 '+visionStatus.detections+'건 · 사람 '+visionStatus.persons+'명':status;
 $('camera-resolution').hidden=visionStatus.state!=='live';
 if(visionStatus.state==='live'){
  $('camera-resolution').textContent=visionStatus.width+' × '+visionStatus.height;
  // 칸이 영상 비율을 따라가야 옆여백이 안 생긴다 — 프레임 크기가 곧 정답이다.
  if(visionStatus.width>0&&visionStatus.height>0)document.documentElement.style.setProperty('--vision-aspect',visionStatus.width+' / '+visionStatus.height);
 }
 const at=visionStatus.lastFrameAt?new Date(visionStatus.lastFrameAt).toLocaleTimeString('ko-KR',{hour12:false}):'—';
 $('frame-time').innerHTML='마지막 영상 수신　'+at+' <span class="muted">· 관측 전용</span>';
}
const cameraDock=$('camera-dock'),cameraHome=cameraDock.parentElement,cameraNext=cameraDock.nextElementSibling;
let storage=null;try{storage=localStorage}catch{/* Restricted browsers can still use session-only drafts. */}
// 실제 대시보드 연결은 **대시보드 서버가 이 페이지를 직접 내보냈을 때만** 붙는다
// (WBS 4.6.3). 그 밖에서 열면 링크가 없고 웹은 예시 모드 그대로다 — 공개된 화면이
// 저 혼자 로봇을 움직이게 두지 않는다.
// ⚠️ **다른 출처의 API 를 가리키는 길은 두지 않는다.** 예전의 `?api=`·`<meta>` 는
// 다른 포트를 받았지만, 서버의 출처 검사가 그 페이지의 명령(비상정지 포함)과 WS 를
// 전부 거절해 **연결된 척하고 아무것도 못 보내는 화면**이 됐다.
async function resolveApiBase(){
 try{
  // 대시보드 서버가 이 페이지를 직접 서빙하면 같은 출처가 곧 API 다.
  // 단, hostname 이 localhost 라는 것만으로는 붙지 않는다 — file:// 이나
  // 다른 로컬 개발 서버 위에서 열렸을 수 있으므로 /health 로 확인한다.
  if(!['127.0.0.1','localhost','[::1]'].includes(location.hostname))return null;
  const probe=await fetch('/health',{signal:globalThis.AbortSignal?.timeout?.(1500)}).catch(()=>null);
  if(!probe?.ok)return null;
  const info=await probe.json().catch(()=>null);
  return info?.service==='telemetry'?location.origin:null;
 }catch{return null}
}
const apiBase=await resolveApiBase();
// 여러 대(`host.fleet`)면 서버가 로봇 목록과 각자의 API 경로(`/robots/<id>`)를 알려 준다. 없으면 한 대짜리 서버다.
// ⚠️ 경로는 **같은 출처 안에서만** 받는다 — 서버 출처 검사가 다른 출처의 명령을 거절한다 (위 설명).
async function resolveFleet(base){
 const body=await fetch(base+'/api/fleet').then(response=>response.ok?response.json():null).catch(()=>null);
 const robots=Array.isArray(body?.robots)?body.robots.filter(r=>typeof r?.id==='string'&&typeof r?.base==='string'&&/^\/robots\/[A-Za-z0-9_-]{1,80}$/.test(r.base)):[];
 return robots.length?robots.slice(0,ROBOTS.length):null;
}
const fleetInfo=apiBase?await resolveFleet(apiBase):null;
// 자리(MD-01~03)마다 API 주소. 한 대짜리면 MD-01 하나가 이 출처 자체다.
const bases=Object.fromEntries(fleetInfo?fleetInfo.map((robot,index)=>[ROBOTS[index],apiBase+robot.base]):apiBase?[[ROBOTS[0],apiBase]]:[]);
const operations=fleetInfo
 ?new Operations({storage,fleet:fleetInfo.map((robot,index)=>({device:robot.id,registered:robot.registered!==false,link:new RobotLink({baseUrl:bases[ROBOTS[index]]})}))})
 :new Operations({storage,link:apiBase?new RobotLink({baseUrl:apiBase}):null});
// 음성 중계는 대시보드 명령 링크와 별개다 — voice_pipeline --web 이 떠 있으면
// 로컬 기본 주소(127.0.0.1:8090)로 자동으로 붙고, 없으면 패널이 준비만 표시한다.
const voiceBase=await resolveVoiceBase();
const voiceLink=voiceBase?new VoiceLink({baseUrl:voiceBase}):null;
const visionAvailable={},feeds=[];
let visionRobot=null;
// 로봇 시점 창은 캔버스가 하나다 — **고른 로봇의** /ws/vision 만 붙인다. 로봇을 바꾸면 이전 연결을 닫는다
// (XIAO 스트림은 워커가 점유하고 여기서는 재송출만 받으므로, 세 채널을 늘 열어 둘 이유가 없다).
function syncVisionFeed(){
 const robot=operations.live?operations.selected:null;
 if(robot===visionRobot)return;
 visionFeed?.stop();visionFeed=null;visionRobot=robot;
 visionStatus={state:robot&&visionAvailable[robot]?'connecting':'off'};
 if(robot&&visionAvailable[robot]){
  visionFeed=new VisionFeed({url:bases[robot].replace(/^http/,'ws')+'/ws/vision',canvas:$('vision-frame'),onStatus:status=>{if(visionRobot===robot){visionStatus=status;syncVisionStatus()}}});
  visionFeed.start();
 }
 syncVisionStatus();
}
if(operations.connected){
 operations.setDemo(false);
 // 로봇 시점 창은 /ws/vision 을 그린다 (WBS 4.6.1) — 검출 박스와 그 박스를
 // 계산한 JPEG 가 한 메시지로 온다. 카메라 스트림에 박스를 얹지 않는다.
 // 비전을 끄고 띄우면(--no-vision) /health 의 vision_clients 가 null 이고
 // 채널이 없으므로 연결을 시도하지 않는다.
 const frame=$('vision-frame'),fpv=$('fpv');
 frame.hidden=false;
 if(fpv)fpv.hidden=true;
 await Promise.all(Object.entries(bases).map(async([robot,base])=>{
  const health=await fetch(base+'/health').then(response=>response.json()).catch(()=>null);
  visionAvailable[robot]=health?.vision_clients!==null;
  // 설정 화면의 단계 표 값 (B7). 못 받으면 화면이 «미수신» 이라고 적는다.
  fetch(base+'/api/policy').then(response=>response.ok?response.json():null).then(policy=>{if(policy)operations.setPolicy(policy,robot)}).catch(()=>{});
  // 실시간 사건도 같은 서버에서 온다 (WBS 4.6.4) — /ws/events 는 비전 채널과
  // 무관하게 항상 있다. 백로그를 먼저 넘겨주므로 늦게 열어도 최근 사건을 본다.
  const ws=base.replace(/^http/,'ws');
  feeds.push(new EventFeed({url:ws+'/ws/events',
   onEvent:event=>operations.ingestLiveEvent(event,base,robot),
   onGap:dropped=>operations.noteEventGap(dropped),
   onStatus:status=>operations.setEventFeed(status,robot)}));
  // 로봇 상태 게이지 (WBS 4.6.2) — /ws/telemetry 는 표시용 10Hz 다. 수신률은 새 seq 로만 센다.
  feeds.push(new TelemetryFeed({url:ws+'/ws/telemetry',onUpdate:view=>operations.setTelemetry(view,robot)}));
 }));
 for(const feed of feeds)feed.start();
 syncVisionFeed();
}
function toast(message){$('toast').textContent=message;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,5000)}
function attempt(action){try{return action()}catch(error){toast(error.message)}}
const panels=new OperationalPanels({store:operations,container:$('panel-content'),title:$('panel-title'),onNavigate:navigate,onToast:toast,voiceLink,
 onManualObservation:mount=>{
  if(mount)mount.append(cameraDock);
  else if(cameraDock.parentElement!==cameraHome)cameraHome.insertBefore(cameraDock,cameraNext);
  syncObservationView();view?.resize();
 },
 onFocusZone:zone=>{navigate('dashboard');view?.focusZone(zone);toast(zone.label+' · 예시 구역 위치')},
 onFocusRobot:id=>{navigate('dashboard');operations.selectRobot(id);view?.setView('robot')},
 onRobotPreview:canvas=>{
  if(!robotView){try{robotView=new RobotDetailView({canvas,onError:message=>panels.setRobotPreviewError(message)})}catch{panels.setRobotPreviewError('3D 모델을 표시하지 못했습니다. 상태 정보는 계속 확인할 수 있습니다.');return}}
  robotView.setActive(currentPage==='devices');robotView.resize();
 },
 onRobotViewAction:action=>{if(action==='reset')robotView?.reset();else if(action==='left')robotView?.orbit(-Math.PI/8);else if(action==='right')robotView?.orbit(Math.PI/8);else robotView?.zoom(action==='in'?.8:1.25)}
});

function syncObservationView(){
 const manual=currentPage==='missions',dashboard=currentPage==='dashboard';
 $('app').classList.toggle('observation-waiting',!operations.demo||operations.stale);
 view?.setWorldVisible(dashboard);
 // ⚠️ **실제 데이터 모드에서는 예시 공장 3D 를 그리지 않는다.** 지도가 없는데(LiDAR 미연결) 가상 공장과
 // 예시 로봇 3대·"확인 필요" 표시가 실제 화면에 섞이면 배치와 위치를 아는 것처럼 보인다. 숨긴 캔버스를
 // 계속 그릴 이유도 없다. LiDAR 지도가 생기면 이 자리는 2D 지도로 채운다.
 view?.setActive(!observationFailed&&operations.demo&&(dashboard||(manual&&!operations.stale)));
 view?.setCameraVisible(!observationFailed&&(dashboard||manual)&&operations.demo&&!operations.stale&&(manual||!cameraDock.classList.contains('collapsed')));
}
function reportObservationError(message){
 observationFailed=true;panels.setObservationError(message);operations.suspend('3D 렌더링 중단');syncObservationView();$('scene-failure').hidden=false;toast(message);
}
function syncMain(){
 const selected=operations.selected,playing=operations.demo&&!operations.stale&&!operations.estop&&operations.mission.status==='running';
 view?.selectRobot(selected);view?.setPatrolRobot(operations.mission.robot);view?.setPlaying(playing);
 syncObservationView();
 robotView?.setActive(currentPage==='devices');
 document.querySelectorAll('[data-robot]').forEach(element=>{const active=element.dataset.robot===selected;element.classList.toggle('selected',active);element.setAttribute('aria-pressed',String(active))});
 // 실제 연결에서는 서버에 붙은 로봇만 보인다 — 한 대짜리면 MD-01 하나, 플릿이면 서버가 알려 준 만큼 (operations.robots).
 for(const element of document.querySelectorAll('.robot-tab,.camera-robot-switch [data-robot]'))element.hidden=!operations.robots.includes(element.dataset.robot);
 syncVisionFeed();
 $('app').classList.toggle('data-waiting',!operations.demo);
 $('source-status').textContent=operations.demo?(operations.stale?'웹 예시 · 수신 만료 시험':'웹 예시'):operations.live?'실제 연결':'실제 데이터 대기';
 // ⚠️ **머리글의 제어 문구도 사실대로 말한다.** 연결돼 있는데 "정지 명령 전달 불가" 라고 적혀 있으면
 // 운용자가 비상정지를 누르지 않거나, 반대로 화면 문구를 믿고 물리 정지 수단을 찾는다.
 {const note=document.querySelector('.safety-note>span:last-child');note.firstChild.nodeValue=operations.live?'실시간 제어 연결 ':'실시간 제어 잠김 ';note.querySelector('small').textContent=operations.live?'비상정지 즉시 전송':'미연결 · 정지 명령 전달 불가';}
 // 웹 시연 임무 칸은 실제 연결에서 숨긴다 — 실제 순찰과 헷갈린다. 실제 순찰은 순찰·제어 화면에 있다.
 document.querySelector('.mission-summary').hidden=operations.live;
 syncTelemetry();
 $('scene-subtitle').textContent=operations.demo?'예시 공간 · 실제 위치 미수신':'지도 없음 · 예시 공장 숨김';
 $('map-placeholder').hidden=operations.demo;
 if(operations.live)syncVisionStatus();
 else{
  $('app').classList.remove('vision-has-frame');
  $('frame-source').textContent=operations.demo?(operations.stale?'예시 수신 만료 · 갱신 대기':'예시 화면 · 실시간 아님'):'영상 미수신 · 연결 대기';
  $('camera-status').textContent='실제 영상 미연결';
  $('camera-resolution').hidden=true;
  $('frame-time').innerHTML='마지막 영상 수신　— <span class="muted">· 관측 전용</span>';
 }
 $('estop').classList.toggle('preview-latched',operations.estop);
 $('estop').setAttribute('aria-label',operations.live?'긴급 정지 · 로봇에 즉시 전송':operations.estop?'긴급 정지 안내 · 웹 예시 정지 잠금 중':'긴급 정지 안내 · 실제 장비 미연결');
 // 버튼에 적힌 말과 실제로 하는 일이 다르면 안 된다.
 {const note=$('estop').querySelector('.mobile-stop-note');if(note)note.textContent=operations.live?'연결됨 · 즉시 전송':'장비 미연결 · 정지 전송 불가';}
 $('demo-toggle').innerHTML=icon(playing?'pause':'play');
 $('demo-toggle').setAttribute('aria-label',playing?'예시 순찰 일시정지':operations.mission.status==='paused'?'예시 순찰 재개':'예시 순찰 준비');
 $('demo-toggle').disabled=operations.blocked&&!playing;
 const missionStatus={idle:'대기',running:'점검',paused:'일시정지',ended:'종료'}[operations.mission.status];
 $('demo-status').textContent=missionStatus;
 document.querySelector('.mission-heading h2').textContent='예시 임무 · '+(operations.mission.status==='idle'?'시작 전':operations.mission.zone+' · '+missionStatus);
 document.querySelectorAll('.mission-progress .step').forEach((element,index)=>{element.classList.remove('done');element.classList.toggle('current',index===(operations.mission.status==='idle'?0:['running','paused'].includes(operations.mission.status)?1:2));element.textContent=index===0?'대기':index===1?(operations.mission.status==='paused'?'일시정지':'점검'):'종료'});
 const events=operations.queryEvents(),pending=events.filter(event=>event.review==='pending').length;
 const liveCount=events.filter(event=>event.source==='LIVE_FEED').length;
 document.querySelector('.event-summary strong').textContent=(operations.demo?'예시·저장 사건':'저장 사건')+' · 검토 대기 '+pending+'건';
 document.querySelector('.event-summary p').textContent=liveCount?'실시간 '+liveCount+'건 포함 · 검토 메모와 판단 근거를 확인하세요.':events.length?'검토 메모와 판단 근거를 확인하세요.':'실시간 사건은 수신되지 않았습니다.';
 $('attention-marker').setAttribute('aria-label','예시 사건 검토 열기');
}
// 하단 상태 칸 — 연결 여부와 로봇 상태를 **사실대로** 말한다 (4.6.2). 연결이 없으면 예시 문구다.
// 연결됐어도 로봇에서 받은 값이 없거나 끊겼으면 그렇게 말한다 — 아는 척하지 않는다.
function syncTelemetry(){
 syncRobotLabels();
 syncAlarm();
 const card=document.querySelector('.actual-status');
 if(!operations.live){card.dataset.tone='off';card.querySelector('strong').textContent='현장 상태 확인 불가 · 장비 미연결';card.querySelector('p').textContent='웹 예시 화면으로, 실제 장비가 연결되어 있지 않습니다.';$('state-time').textContent='—';return}
 const text=describeTelemetry(operations.telemetry);
 card.dataset.tone=text.tone;card.querySelector('strong').textContent=text.headline;card.querySelector('p').textContent=text.summary;
 $('state-time').textContent=text.age;
}
// 로봇 이름과 연결 칸. 실제 연결이면 서버가 알려 준 개체 이름과 **로봇 수신 상태**를 쓴다 —
// 예전에는 연결돼 있어도 탭이 "MD-01 · 미연결" 이었다. 이름은 첫 상태 전문에 실려 오므로 10Hz 경로에서 맞춘다.
function syncRobotLabels(){
 const name=operations.robotName(operations.selected),text=operations.live?describeTelemetry(operations.telemetry):null;
 $('camera-title').textContent=name+' · 로봇 시점';$('camera-axis').textContent=name+' / FRONT';
 // 탭마다 **그 로봇의** 수신 상태다. 실물이 아직 없는 자리(텔레메트리 ID 미설정)는 받은 것이 없으면 «미등록».
 for(const tab of document.querySelectorAll('.robot-tab')){
  const id=tab.dataset.robot,strong=tab.querySelector('strong');
  strong.firstChild.nodeValue=operations.robotName(id)+(strong.children.length?' ':'');
  const telemetry=operations.telemetryOf(id),own=operations.live&&!tab.hidden?describeTelemetry(telemetry):null;
  // ⚠️ «미등록» 은 텔레메트리가 와도 뗀다 — 검증할 MAC 이 없어 이름만으로 받은 것이다.
  tab.querySelector('.robot-health').lastChild.nodeValue=!own?'미연결':(!operations.isRegistered(id)?'미등록 · ':'')+own.badge[0];
 }
 for(const button of document.querySelectorAll('.camera-robot-switch [data-robot]'))button.textContent=operations.robotName(button.dataset.robot);
 document.querySelector('.source-status .muted').textContent=text?'로봇 '+text.badge[0]:'실제 장비 미연결';
}
// 경보 띠 (B1 · TC-F-013 «음성 미장착 시 시각 경보»). L2·L3·F 에서만 뜬다. 단계는 상태 전문, 사유·경고 문장은
// 같은 단계의 마지막 단계 사건에서 온다. ⚠️ L3 확인과 F 해제는 다른 버튼이다 — 띠는 처리 화면으로 안내만 한다.
const ALARM_TEXT={L2:'L2 · 인증 대응 — 미인증 대상에게 신원 확인을 요구하는 중',L3:'L3 · 경보 — 관리자 확인이 필요합니다 («경보 확인»)',F:'F · 안전 잠금 — 원인 확인 뒤 «안전 해제 (RESET_SAFE)»'};
// 여러 대면 **가장 심한 로봇**을 띠에 싣고, 나머지는 이름·단계만 덧붙인다. 보고 있는 로봇이 아니어도 뜬다.
let alarmRobot=null;
function syncAlarm(){
 const [alarm,...others]=operations.alarms,banner=$('alarm-banner'),many=operations.robots.length>1;
 banner.hidden=!alarm;alarmRobot=alarm?.robot??null;if(!alarm)return;
 banner.dataset.level=alarm.level;banner.classList.toggle('stale',alarm.stale);
 const [head,...rest]=ALARM_TEXT[alarm.level].split(' — ');
 $('alarm-level').textContent=(many?alarm.name+' · ':'')+head;
 $('alarm-text').textContent=[rest.join(' — '),alarm.reason&&'사유 '+alarm.reason,alarm.warning&&'경고 「'+alarm.warning+'」',alarm.stale&&'수신 끊김 · 마지막 값',others.length&&'다른 로봇 '+others.map(other=>other.name+' '+other.level).join(', ')].filter(Boolean).join(' · ');
 $('alarm-action').hidden=alarm.level==='L2'||(currentPage==='missions'&&operations.selected===alarm.robot);
}
$('alarm-action').addEventListener('click',()=>{if(alarmRobot&&alarmRobot!==operations.selected)attempt(()=>operations.selectRobot(alarmRobot));navigate('missions')});
// 텔레메트리는 초당 10번 온다 — 화면 전체(syncMain)를 다시 맞추지 않고 상태 칸과 게이지만 고친다.
operations.subscribe(reason=>{if(reason==='telemetry'){syncTelemetry();panels.refresh(reason);return}syncMain();panels.refresh(reason)});

function navigate(page){
 const target=['dashboard','missions','events','records','zones','devices','voice','settings'].includes(page)?page:'dashboard';
 if(location.hash!=='#'+target)location.hash=target;
 openPage(target);
}
function openRobotDetail(id){
 if(currentPage==='dashboard')lastOpener=document.activeElement;
 operations.selectRobot(id);navigate('devices');
}
function openPage(page){
 if(currentPage===page&&((page==='dashboard'&&$('detail-panel').hidden)||!$('detail-panel').hidden))return;
 operations.release('화면 전환');
 if(currentPage==='dashboard'&&page!=='dashboard')lastOpener=document.activeElement;
 currentPage=page;const isDashboard=page==='dashboard';
 $('app').classList.toggle('workspace-open',!isDashboard);
 for(const element of document.querySelectorAll('.fleet-band,#stage,.operation-rail'))element.hidden=!isDashboard;
 $('detail-panel').hidden=isDashboard;
 document.querySelector('.skip-link').href=isDashboard?'#scene-title':'#panel-title';
 document.querySelectorAll('[data-view]').forEach(button=>{const active=button.dataset.view===page;button.classList.toggle('active',active);if(active)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current')});
 document.querySelectorAll('[data-camera]').forEach(button=>{const active=button.dataset.camera===(page==='zones'?'top':page==='devices'?'robot':page==='dashboard'?'overview':'none');button.classList.toggle('selected',active);button.setAttribute('aria-pressed',String(active))});
 panels.render(page);
 if(page==='dashboard'){const fallback=document.querySelector('.robot-tab[data-robot="'+operations.selected+'"]');(lastOpener?.isConnected&&lastOpener.matches('button,a')&&!lastOpener.closest('[hidden]')?lastOpener:fallback||$('scene-title')).focus();lastOpener=null}
 else{$('panel-title').focus();$('panel-content').scrollTop=0}
 syncMain();
 operations.log('화면 열기',page);
}
window.addEventListener('hashchange',()=>{const page=location.hash.slice(1);navigate(page==='history'?'records':page)});
document.querySelector('.skip-link').addEventListener('click',event=>{event.preventDefault();(currentPage==='dashboard'?$('scene-title'):$('panel-title')).focus()});
for(const id of ROBOTS){const marker=document.createElement('button');marker.className='map-marker';marker.dataset.robot=id;marker.append(document.createTextNode(id+' · '));const small=document.createElement('small');small.textContent='예시';marker.append(small);$('markers').append(marker)}
document.querySelectorAll('[data-robot]').forEach(element=>element.addEventListener('click',()=>element.closest('.camera-robot-switch')?operations.selectRobot(element.dataset.robot):openRobotDetail(element.dataset.robot)));
document.querySelectorAll('[data-view]').forEach(element=>element.addEventListener('click',()=>navigate(element.dataset.view)));
document.querySelectorAll('[data-camera]').forEach(element=>element.addEventListener('click',()=>{const mode=element.dataset.camera;if(mode==='overview'){navigate('dashboard');view?.setView('overview')}else navigate(mode==='top'?'zones':'devices')}));
$('close-panel').addEventListener('click',()=>navigate('dashboard'));
$('open-events').addEventListener('click',()=>navigate('events'));
$('attention-marker').addEventListener('click',()=>navigate('events'));
$('zoom-in').addEventListener('click',()=>view?.zoom(.8));$('zoom-out').addEventListener('click',()=>view?.zoom(1.25));
$('orbit-left').addEventListener('click',()=>view?.orbit(Math.PI/8));
$('reset-view').addEventListener('click',()=>{view?.setView('overview');toast('기본 시야로 돌아왔어요.')});
$('fullscreen').addEventListener('click',async()=>{try{if(document.fullscreenElement)await document.exitFullscreen();else await document.documentElement.requestFullscreen()}catch{toast('이 브라우저에서는 전체 화면을 사용할 수 없어요.')}});
function setCameraDockState({expanded=false,collapsed=false}){
 const dock=$('camera-dock'),expand=$('expand-camera'),collapse=$('collapse-camera');
 if(expanded){dock.saveDockGeom?.();dock.style.left='';dock.style.top='';dock.style.bottom='';dock.style.width=''}
 else if(!expanded&&dock.classList.contains('expanded'))dock.restoreDockGeom?.();
 dock.classList.toggle('expanded',expanded);dock.classList.toggle('collapsed',collapsed);
 const expandLabel=expanded?'로봇 시점 원래 크기로':'로봇 시점 크게 보기';
 expand.setAttribute('aria-label',expandLabel);expand.title=expandLabel;
 expand.querySelector('span:last-child').textContent=expanded?'축소':'확대';
 collapse.setAttribute('aria-expanded',String(!collapsed));
 collapse.setAttribute('aria-label',collapsed?'로봇 시점 펼치기':'로봇 시점 접기');
 collapse.innerHTML=icon(collapsed?'plus':'minus')+'<span>'+(collapsed?'펼치기':'접기')+'</span>';
 syncObservationView();
 // ResizeObserver also follows later viewport changes.
 view?.resize();
}
$('expand-camera').addEventListener('click',()=>setCameraDockState({expanded:!$('camera-dock').classList.contains('expanded')}));
$('collapse-camera').addEventListener('click',()=>setCameraDockState({collapsed:!$('camera-dock').classList.contains('collapsed')}));
// 카메라 창 자유 배치 — 헤더를 끌어 옮기고, 변·모서리를 끌어 크기를 바꾼다.
// 통합 관제에서만 동작한다 — 순찰·제어에서는 칸이 문서 흐름(position:static)이라
// 끌면 레이아웃이 깨진다. 크기는 너비만 바꾸고 높이는 --vision-aspect 가 맞춘다.
{
 const stage=$('stage'),dock=cameraDock,header=dock.querySelector('.camera-header');
 const clampTo=(v,min,max)=>Math.min(max,Math.max(min,v));
 const dockInStage=()=>dock.parentElement===stage&&currentPage==='dashboard';
 // 확대·접기 전의 자유 위치를 기억해 돌아올 때 복원한다.
 let dockGeom=null;
 const saveGeom=()=>{dockGeom={left:dock.style.left,top:dock.style.top,bottom:dock.style.bottom,width:dock.style.width}};
 const restoreGeom=()=>{if(dockGeom)Object.assign(dock.style,dockGeom)};
 dock.saveDockGeom=saveGeom;dock.restoreDockGeom=restoreGeom;
 header.addEventListener('pointerdown',event=>{
  if(!dockInStage()||event.target.closest('button'))return;
  event.preventDefault();
  const s=stage.getBoundingClientRect(),d=dock.getBoundingClientRect();
  // ⚠️ 순서가 중요하다 — expanded 를 지우면 기본 위치로 돌아가므로, 보이는
  // 위치(rect)를 먼저 재고 나서 지운다. 그래야 끌기 시작해도 창이 안 뛴다.
  dock.classList.remove('expanded');
  dock.style.left=(d.left-s.left)+'px';dock.style.top=(d.top-s.top)+'px';dock.style.bottom='auto';
  const offX=event.clientX-d.left,offY=event.clientY-d.top;
  const move=move=>{
   dock.style.left=clampTo(move.clientX-s.left-offX,0,Math.max(0,s.width-dock.offsetWidth))+'px';
   dock.style.top=clampTo(move.clientY-s.top-offY,0,Math.max(0,s.height-dock.offsetHeight))+'px';
  };
  const up=()=>{header.removeEventListener('pointermove',move);header.removeEventListener('pointerup',up);header.removeEventListener('pointercancel',up);saveGeom();view?.resize()};
  header.setPointerCapture?.(event.pointerId);
  header.addEventListener('pointermove',move);
  header.addEventListener('pointerup',up);
  header.addEventListener('pointercancel',up);
 });
 const aspect=()=>{const w=visionStatus.width,h=visionStatus.height;return w>0&&h>0?w/h:4/3};
 for(const grip of dock.querySelectorAll('.camera-resize'))grip.addEventListener('pointerdown',event=>{
  if(!dockInStage())return;
  event.preventDefault();
  const edge=grip.dataset.edge,s=stage.getBoundingClientRect(),startW=dock.getBoundingClientRect().width,startX=event.clientX,startY=event.clientY;
  const move=move=>{
   const dx=move.clientX-startX,dy=move.clientY-startY;
   const grown=edge==='e'?dx:edge==='s'?dy*aspect():Math.max(dx,dy*aspect());
   dock.style.width=clampTo(startW+grown,280,Math.min(s.width-20,1100))+'px';
   // 너비가 커지면 오른쪽 경계를 넘을 수 있다 — 왼쪽을 당겨 안에 둔다.
   const over=dock.getBoundingClientRect().right-s.right;
   if(over>0)dock.style.left=clampTo((dock.offsetLeft||0)-over,0,s.width)+'px';
  };
  const up=()=>{grip.removeEventListener('pointermove',move);grip.removeEventListener('pointerup',up);grip.removeEventListener('pointercancel',up);saveGeom();view?.resize()};
  grip.setPointerCapture?.(event.pointerId);
  grip.addEventListener('pointermove',move);
  grip.addEventListener('pointerup',up);
  grip.addEventListener('pointercancel',up);
 });
}
$('demo-toggle').addEventListener('click',()=>attempt(()=>{if(operations.mission.status==='running')operations.pauseMission();else if(operations.mission.status==='paused')operations.resumeMission();else navigate('missions')}));
function openStopDialog(){operations.suspend('긴급 정지 안내 열기');if(!$('stop-dialog').open)$('stop-dialog').showModal()}
// 연결돼 있으면 비상정지는 한 번 눌러 바로 나간다. 모달을 한 단계 끼우면 급할 때
// 그만큼 늦고, 그 모달은 "장비가 연결되지 않았어요" 라고 거짓을 말한다. 연결이
// 없을 때만 안내를 띄운다 — 그때는 실제로 보낼 곳이 없다.
function onEstopPressed(){
 if(operations.live){operations.requestEstop();toast(operations.linkError||'비상정지를 보냈습니다.');return}
 openStopDialog();
}
$('estop').addEventListener('click',onEstopPressed);
$('stop-preview').addEventListener('click',()=>operations.requestEstop());
document.addEventListener('keydown',event=>{
 if(event.key==='Escape'){operations.stop('Escape');if(!$('stop-dialog').open)navigate('dashboard')}
 // 단축키도 버튼과 **같은 경로**를 탄다 (FR-4.4). 예전에는 여기서 모달만 열어
 // 연결돼 있어도 한 번 더 눌러야 나갔다 — 버튼 쪽 주석이 "모달을 한 단계 끼우면
 // 급할 때 그만큼 늦다" 고 적어 둔 바로 그 문제를 단축키만 안고 있었다.
 if(event.shiftKey&&event.key.toLowerCase()==='e'&&!/INPUT|TEXTAREA|SELECT/.test(event.target.tagName)){onEstopPressed();event.preventDefault()}
});
document.addEventListener('visibilitychange',()=>{if(document.hidden)operations.suspend('페이지 숨김 · 자동 재개 안 함')});
window.addEventListener('blur',()=>operations.suspend('창 초점 이탈 · 자동 재개 안 함'));
window.addEventListener('pagehide',event=>{operations.suspend('페이지 종료');if(!event.persisted){visionFeed?.stop();for(const feed of feeds)feed.stop();robotView?.dispose();panels.dispose();view?.dispose()}});
const unregisterTools=registerPageTools({document,store:operations,navigate,onError:()=>operations.log('페이지 도구 등록 실패','일반 화면 조작은 계속 사용 가능')});
window.addEventListener('pagehide',event=>{if(!event.persisted)unregisterTools()});
$('retry-render').addEventListener('click',()=>location.reload());
syncMain();navigate(location.hash.slice(1)||'dashboard');

try{
 const response=await fetch('./factory-layout.json');if(!response.ok)throw new Error('공장 레이아웃을 불러오지 못했습니다.');
 const layout=await response.json();panels.setZones(layout.zones);
 view=new FactoryView({worldCanvas:$('world'),fpvCanvas:$('fpv'),layout,onRobot:openRobotDetail,onObservation:observation=>panels.updateRobotObservation(observation),onError:reportObservationError,onProject:positions=>{for(const position of positions){const marker=position.id==='attention'?$('attention-marker'):$('markers').querySelector('[data-robot="'+position.id+'"]');marker.style.left=position.x+'px';marker.style.top=position.y+'px';marker.hidden=!position.visible}}});
 renderRobotPreviews(document.querySelectorAll('.robot-preview'));syncMain();operations.log('3D 현장 열기','신규 공장 · 웹 시각 예시');
}catch(error){reportObservationError(error.message)}
