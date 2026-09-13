import {FactoryView,renderRobotPreviews} from './scene.js';
import {icon,renderIcons} from './icons.js';
import {Operations,ROBOTS} from './operations.js';
import {OperationalPanels} from './panels.js';
import {registerPageTools} from './webmcp.js';
import {RobotDetailView} from './robot-view.js';
import {RobotLink} from './robot-link.js';

renderIcons();
const $=id=>document.getElementById(id);
let view=null,robotView=null,toastTimer,currentPage='dashboard',lastOpener=null,observationFailed=false;
const cameraDock=$('camera-dock'),cameraHome=cameraDock.parentElement,cameraNext=cameraDock.nextElementSibling;
let storage=null;try{storage=localStorage}catch{/* Restricted browsers can still use session-only drafts. */}
// 실제 대시보드 연결은 **명시적으로 켤 때만** 붙는다 (WBS 4.6.3). 주소를 주지
// 않으면 링크가 없고 웹은 예시 모드 그대로다 — 공개된 화면이 저 혼자 로봇을
// 움직이게 두지 않는다. 켜는 법은 둘 중 하나다.
//   1) 주소창에  ?api=http://127.0.0.1:8000
//   2) index.html 에  <meta name="mechadog-api" content="http://127.0.0.1:8000">
// 로컬 주소만 받는다. 원격 주소를 적어도 붙지 않는다.
function resolveApiBase(){
 try{
  const fromQuery=new URLSearchParams(location.search).get('api');
  const fromMeta=document.querySelector('meta[name="mechadog-api"]')?.content;
  const raw=(fromQuery||fromMeta||'').trim();
  if(!raw){
    // 대시보드 서버가 이 페이지를 직접 서빙하면 같은 출처가 곧 API 다.
    // file:// 로 연 화면은 hostname 이 비어 있어 예시 모드로 남는다.
    return ['127.0.0.1','localhost','[::1]'].includes(location.hostname)?location.origin:null;
  }
  const url=new URL(raw,location.href);
  if(!['127.0.0.1','localhost','[::1]'].includes(url.hostname))return null;
  return url.origin;
 }catch{return null}
}
const apiBase=resolveApiBase();
const link=apiBase?new RobotLink({baseUrl:apiBase}):null;
const operations=new Operations({storage,link});
if(link){
 operations.setDemo(false);
 // 실제 카메라는 XIAO 가 단일 클라이언트만 받으므로 서버가 비전 워커의
 // 최신 프레임을 재송출하는 /camera/stream 을 연다 — 판정에 들어간 화면과
 // 같은 영상이다.
 const frame=$('isaac-frame'),fpv=$('fpv');
 frame.src=apiBase+'/camera/stream';
 frame.hidden=false;
 if(fpv)fpv.hidden=true;
 frame.onerror=()=>{$('camera-status').textContent='영상 수신 실패 · 서버 확인';};
}
function toast(message){$('toast').textContent=message;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,5000)}
function attempt(action){try{return action()}catch(error){toast(error.message)}}
const panels=new OperationalPanels({store:operations,container:$('panel-content'),title:$('panel-title'),onNavigate:navigate,onToast:toast,
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
 view?.setActive(!observationFailed&&(dashboard||(manual&&operations.demo&&!operations.stale)));
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
 $('camera-title').textContent=selected+' · 로봇 시점';$('camera-axis').textContent=selected+' / FRONT';
 $('app').classList.toggle('data-waiting',!operations.demo);
 $('source-status').textContent=operations.demo?(operations.stale?'웹 예시 · 수신 만료 시험':'웹 예시'):'실제 데이터 대기';
 $('scene-subtitle').textContent=operations.demo?'예시 공간 · 실제 위치 미수신':'실제 지도·위치 미수신 · 예시 숨김';
 $('frame-source').textContent=operations.live?'실시간 · 로봇 카메라':(operations.demo?(operations.stale?'예시 수신 만료 · 갱신 대기':'예시 화면 · 실시간 아님'):'영상 미수신 · 연결 대기');
 $('camera-status').textContent=operations.live?'실시간 수신 중':'실제 영상 미연결';
 $('frame-time').innerHTML=operations.live?'실시간 스트림 수신 중 <span class="muted">· 관측 전용</span>':'마지막 영상 수신　— <span class="muted">· 관측 전용</span>';
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
 document.querySelector('.event-summary strong').textContent=(operations.demo?'예시·저장 사건':'저장 사건')+' · 검토 대기 '+pending+'건';
 document.querySelector('.event-summary p').textContent=events.length?'검토 메모와 판단 근거를 확인하세요.':'실시간 사건은 수신되지 않았습니다.';
 $('attention-marker').setAttribute('aria-label','예시 사건 검토 열기');
}
operations.subscribe(reason=>{syncMain();panels.refresh(reason)});

function navigate(page){
 const target=['dashboard','missions','events','records','zones','devices','settings'].includes(page)?page:'dashboard';
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
 if(event.shiftKey&&event.key.toLowerCase()==='e'&&!/INPUT|TEXTAREA|SELECT/.test(event.target.tagName)){openStopDialog();event.preventDefault()}
});
document.addEventListener('visibilitychange',()=>{if(document.hidden)operations.suspend('페이지 숨김 · 자동 재개 안 함')});
window.addEventListener('blur',()=>operations.suspend('창 초점 이탈 · 자동 재개 안 함'));
window.addEventListener('pagehide',event=>{operations.suspend('페이지 종료');if(!event.persisted){robotView?.dispose();panels.dispose();view?.dispose()}});
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
