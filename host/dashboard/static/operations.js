export const SOURCE_REVISION = 'b287eae4ca2b5751381e5ef4dfaf2d20b69886de';
export const REVIEW_STATES = Object.freeze({pending:'검토 대기',confirmed:'위반 확인',false_positive:'오탐',unverifiable:'확인 불가',resolved:'조치 완료'});
export const ROBOTS = ['MD-01','MD-02','MD-03'];
const STORAGE_KEY='mechadog-next:local-drafts:v1';
const ALLOWED_COMMANDS=['FORWARD','BACKWARD','LEFT','RIGHT'];
const nowDefault=()=>Date.now();
const copy=value=>JSON.parse(JSON.stringify(value));

export function demoEvents() {
 return [
  {id:'DEMO-PPE-01',source:'DEMO',title:'안전모 판정 보류',category:'PPE',robot:'MD-01',zone:'생산 구역',event:'ppe_review_example',state:'OBSERVE',escalation:'L1',auth:'미확인',ppe:'판정 보류',detail:'머리 영역이 설비에 가려진 예시입니다. 확인 불가는 위반이나 오탐과 다릅니다.',review:'pending',note:'',snapshot:null},
  {id:'DEMO-AUTH-02',source:'DEMO',title:'미인증 대상 재확인',category:'AUTH',robot:'MD-02',zone:'중앙 통로',event:'auth_review_example',state:'OBSERVE',escalation:'L2',auth:'미인증',ppe:'별도 판정',detail:'신원 확인과 PPE 판단은 독립적입니다. 이 예시는 실제 인증 또는 경보 발생 기록이 아닙니다.',review:'pending',note:'',snapshot:null},
  {id:'DEMO-OBJECT-03',source:'DEMO',title:'기준 물품 비교 필요',category:'OBJECT',robot:'MD-03',zone:'후면 적재',event:'object_review_example',state:'OBSERVE',escalation:'L1',auth:'해당 없음',ppe:'해당 없음',detail:'적재물 가림과 실제 수량 변화는 구별해야 합니다. 기준 스냅샷·실제 검출 자료는 제공되지 않았습니다.',review:'unverifiable',note:'비교 근거 없는 예시',snapshot:null}
 ];
}

// 사건 하나의 스냅샷 주소. **기록 이름과 파일명이 둘 다 있어야 만든다** — 하나라도
// 없으면 그림 없는 사건이고, 주소를 만들면 화면이 깨진 이미지를 그린다.
export function liveSnapshotUrl(baseUrl,payload){
 if(!baseUrl||!payload?.entry||!payload?.snapshot)return null;
 return baseUrl+'/events/'+encodeURIComponent(payload.entry)+'/snapshot.jpg';
}

function cleanText(value,max=1000){return typeof value==='string'?value.trim().slice(0,max):''}

// ── 사건 분류·제목·판단 근거 (B2·B3·B4) ─────────────────────────
// 예전에는 person_found 만 AUTH 였고 나머지는 전부 SYSTEM 이라 PPE 필터가 아무것도 걸러내지 못했다.
// 이름은 런타임(`runtime.py` 의 `_record_scene`·`FEED_TRANSITIONS`·`_announce_escalation`)이 정한다.
export const EVENT_CATEGORIES=Object.freeze({PPE:'PPE',AUTH:'출입 인증',SAFETY:'안전 · 경보',OBJECT:'구역 · 물품',SYSTEM:'시스템'});
const CATEGORY_OF={PPE_VIOLATION:'PPE',PPE_UNDETERMINED:'PPE',PPE_SETTLED:'PPE',person_found:'AUTH',auth_required:'AUTH',auth_granted:'AUTH',auth_failed:'AUTH',voice_auth_granted:'AUTH',person_fallen:'SAFETY',escalation_changed:'SAFETY',failsafe_entered:'SAFETY',failsafe_cleared:'SAFETY',zone_reading:'OBJECT',zone_changed:'OBJECT'};
export function eventCategory(name){return CATEGORY_OF[name]??'SYSTEM'}
export const EVENT_TITLES=Object.freeze({person_found:'사람 확인',PPE_VIOLATION:'보호구 미착용 확정',PPE_UNDETERMINED:'보호구 판정 불가',PPE_SETTLED:'보호구 판정 종료',person_fallen:'쓰러짐 감지',zone_reading:'구역 장면 판독',zone_changed:'구역 물체 변화 확정',escalation_changed:'대응 단계 변경',auth_required:'인증 요구 (대기 시작)',auth_granted:'인증 통과',auth_failed:'인증 실패',voice_auth_granted:'암구호 확인 · 사원증 대기',failsafe_entered:'안전 잠금',failsafe_cleared:'안전 잠금 해제'});
// 단계가 바뀐 사유 (`escalation.py` 의 reason). 모르는 값은 원문 그대로 보인다.
export const REASON_NAMES=Object.freeze({person_present:'사람 확인',unauthenticated_hold:'미인증 상태 지속',unauthenticated_left:'인증 요구 뒤 대상 이탈',AUTH_FAILED:'인증 실패 (시간 초과·시도 소진)',PPE_VIOLATION:'보호구 미착용 확정',ZONE_CHANGED:'구역 물체 변화 확정',PERSON_DOWN:'쓰러짐 확정',ONBOARD_FAILSAFE:'로봇 자체 안전 잠금',LINK_LOST:'링크 끊김',ESTOP:'비상정지',alarm_confirmed:'관리자 경보 확인',failsafe_confirmed:'안전 잠금 해제',failsafe_confirmed_alarm_kept:'안전 잠금 해제 — 경보 유지',authenticated:'인증 통과',target_lost:'대상 이탈',standby:'대기 전환',ppe_settled:'PPE 판정 종료'});
export const reasonName=reason=>REASON_NAMES[reason]??(cleanText(reason,80)||'사유 미수신');
const AUTH_TEXT={person_found:'검출 시점 · 인증 전',auth_required:'인증 요구됨',auth_granted:'통과',auth_failed:'실패 → 경보',voice_auth_granted:'암구호 확인 · 사원증 대기'};
const shown=value=>typeof value==='number'?String(Math.round(value*100)/100):typeof value==='boolean'?(value?'예':'아니요'):cleanText(String(value??''),300)||'—';
// 구역 물체 변화 (FR-8.3). 종류 이름은 `change_detect.py` 의 `ChangeKind` 와 1:1 이다.
const CHANGE_KINDS={removed:'반출',added:'반입',person:'인원 출현'};
// 격자 칸 `(열, 행)` → 위치 이름. 왼쪽 위가 (0, 0) 이고 픽셀이 아니라 칸이다. 3×3 이면 왼쪽/가운데/오른쪽 × 위/가운데/아래.
// ⚠️ 칸이나 격자 형식이 틀리면 null — 위치를 지어내지 않는다 (격자가 없으면 3×3 이라고 가정하지도 않는다).
function cellName(cell,grid){
 if(!Array.isArray(cell)||!Array.isArray(grid)||cell.length!==2||grid.length!==2||![...cell,...grid].every(n=>Number.isSafeInteger(n)&&n>=0)||cell[0]>=grid[0]||cell[1]>=grid[1])return null;
 const [c,r]=cell,[cols,rows]=grid;
 if(cols===3&&rows===3)return c===1&&r===1?'가운데':['왼쪽','가운데','오른쪽'][c]+' '+['위','가운데','아래'][r];
 return '열 '+(c+1)+'/'+cols+' · 행 '+(r+1)+'/'+rows;
}
/** 사건 이름과 서버가 실어 준 근거 → 인증·PPE 칸과 근거 표. 근거가 없으면 지어내지 않는다. */
export function describeEvidence(name,payload){
 const j=payload?.judgement&&typeof payload.judgement==='object'&&!Array.isArray(payload.judgement)?payload.judgement:{};
 const rows=[];
 let ppe=name.startsWith('PPE_')?'판정 근거 미수신':'해당 없음';
 if(name.startsWith('PPE_')&&j.state){ppe=cleanText(j.state,40)+(j.reason?' · '+cleanText(j.reason,160):'');if(j.track_id!=null)rows.push(['대상 추적 ID','#'+shown(j.track_id)])}
 // VLM 이 판독한 쓰러짐(source:'vlm')에는 규칙 값이 없다 — 빈 행을 그리지 않는다.
 if(name==='person_fallen')for(const [label,key,unit] of [['세로/가로 비','aspect',''],['정지 시간','still_ms',' ms'],['확정 기준','confirm_ms',' ms']])if(j[key]!=null)rows.push([label,shown(j[key])+unit]);
 if(name==='zone_reading'){
  rows.push(['구역',shown(j.zone)],['판독 저하',j.degraded?('예 · '+shown(j.reason)):'아니요']);
  if(j.answers&&typeof j.answers==='object')for(const [key,value] of Object.entries(j.answers).slice(0,12))rows.push(['판독 · '+cleanText(key,60),shown(value)]);
 }
 if(name==='zone_changed'){
  const changes=Array.isArray(j.changes)?j.changes.filter(c=>c&&typeof c==='object').slice(0,12):[];
  rows.push(['구역',shown(j.zone)]);
  for(const c of changes){const where=cellName(c.cell,j.grid);rows.push([CHANGE_KINDS[c.kind]??(cleanText(c.kind,40)||'종류 미수신'),(cleanText(c.label,60)||'라벨 미수신')+(Number.isSafeInteger(c.count)&&c.count>0?' ×'+c.count:'')+(where?' · '+where:'')])}
  if(!changes.length)rows.push(['변화 내역','미수신']);
  if(Number.isSafeInteger(j.baseline_ms)&&j.baseline_ms>=0&&j.baseline_ms<=8640000000000000)rows.push(['기준 시각',new Date(j.baseline_ms).toLocaleString('ko-KR',{hour12:false})]);
 }
 if(name==='escalation_changed')rows.push(['사유',reasonName(payload?.reason)],['경고 문장',cleanText(payload?.warning,300)||'읽을 문장 없음 (이 단계는 음성 경고 없음)']);
 if(payload?.trigger)rows.push(['원인 사건',cleanText(payload.trigger,40)]);
 if(payload?.previous)rows.push(['이전 상태',cleanText(payload.previous,40)]);
 return {auth:AUTH_TEXT[name]??'해당 없음',ppe,rows};
}
export function parseBlackbox(value) {
 if(!value||typeof value!=='object'||Array.isArray(value))throw new Error('meta.json 객체를 선택해 주세요.');
 if(!Number.isSafeInteger(value.ts_ms)||value.ts_ms<0||value.ts_ms>8640000000000000)throw new Error('유효한 ts_ms가 없습니다.');
 for(const key of ['event','state','escalation'])if(typeof value[key]!=='string'||!value[key].trim()||value[key].length>160)throw new Error(key+' 필드를 확인해 주세요.');
 if(value.mode!=null&&(typeof value.mode!=='string'||value.mode.length>40))throw new Error('mode 필드를 확인해 주세요.');
 for(const key of ['tracks','detections'])if(!Array.isArray(value[key])||value[key].length>128)throw new Error(key+' 배열 형식이 올바르지 않습니다.');
 const validBox=box=>Array.isArray(box)&&box.length===4&&box.every(n=>Number.isFinite(n)&&Math.abs(n)<1000000)&&box[2]>=box[0]&&box[3]>=box[1];
 for(const item of [...value.tracks,...value.detections]){
  if(!item||!validBox(item.box)||!Number.isFinite(item.score)||item.score<0||item.score>1)throw new Error('검출 좌표 또는 score를 확인해 주세요.');
 }
 for(const track of value.tracks)if(!Number.isSafeInteger(track.track_id)||track.track_id<0)throw new Error('track_id 정수 필드를 확인해 주세요.');
 for(const detection of value.detections)if(typeof detection.label!=='string'||!detection.label.trim()||detection.label.length>160)throw new Error('검출 label 필드를 확인해 주세요.');
 if(!value.telemetry||typeof value.telemetry!=='object'||Array.isArray(value.telemetry))throw new Error('telemetry 객체가 필요합니다.');
 if(value.telemetry.device_id!=null&&(typeof value.telemetry.device_id!=='string'||value.telemetry.device_id.length>160))throw new Error('telemetry.device_id 형식을 확인해 주세요.');
 return copy(value);
}

export function csvCell(value) {
 let text=String(value??'');
 if(/^[\s]*[=+@-]/.test(text)||/^[\t\r\n]/.test(text))text="'"+text;
 return '"'+text.replaceAll('"','""')+'"';
}
export function toCsv(rows,headers){return '\ufeff'+[headers,...rows].map(row=>row.map(csvCell).join(',')).join('\r\n')}

export class Operations {
 // `link` 를 주면 수동 명령이 실제로 대시보드로 나간다 (WBS 4.6.3). 주지 않으면
 // 지금까지와 똑같이 웹 상태와 이력만 바뀐다 — **예시 모드가 기본이다.**
 // `fleet` 은 여러 대(`host.fleet`) — `[{device,link,registered}]` 를 서버가 알려 준 순서대로 MD-01~03 자리에 붙인다.
 constructor({storage=null,clock=nowDefault,link=null,fleet=null}={}) {
  // 로봇 자리마다 따로 갖는 것 — 링크·상태 전문·마지막 단계 사건·설정값·사건 피드·정지 잠금.
  // ⚠️ **한 로봇의 값을 다른 로봇 화면에 보이면 안 된다.** MD-02 의 L3 를 MD-01 사유로 설명하는 식이 된다.
  this.slots=Object.fromEntries(ROBOTS.map(id=>[id,{link:null,device:null,registered:true,telemetry:{state:'off'},lastEscalation:null,policy:null,liveFeed:{state:'off',received:0,dropped:0},estop:false}]));
  if(fleet)fleet.slice(0,ROBOTS.length).forEach((unit,index)=>Object.assign(this.slots[ROBOTS[index]],{link:unit.link??null,device:cleanText(unit.device,80)||null,registered:unit.registered!==false}));
  else this.link=link;
  this.linkError=null;this.demoEstop=false;
  this.storage=storage;this.clock=clock;this.listeners=new Set();
  this.demo=true;this.stale=false;this.role='operator';this.selected='MD-01';
  this.control=null;this.command='STOP';this.mission={status:'idle',robot:'MD-01',zone:'생산 구역',id:null};
  this.records=[];this.sessions=[];this.events=demoEvents();this.policies={};this.storageAvailable=!!storage;
  this.serial=0;this.load();
 }
 subscribe(fn){this.listeners.add(fn);return()=>this.listeners.delete(fn)}
 emit(reason){for(const fn of this.listeners)fn(reason)}
 log(action,detail='',source='LOCAL_UI_PREVIEW_ONLY'){
  this.records.unshift({id:++this.serial,ts_ms:this.clock(),action,detail:cleanText(detail,1500),source});
  if(this.records.length>500)this.records.length=500;
 }
 load(){
  if(!this.storage)return;
  try{
   const raw=this.storage.getItem(STORAGE_KEY);if(!raw)return;
   if(raw.length>150000)throw new Error('저장 내용이 너무 큽니다.');
   const saved=JSON.parse(raw);
   if(saved?.version!==1)return;
   for(const event of this.events){
    const review=saved.reviews?.[event.id];
    if(review&&Object.hasOwn(REVIEW_STATES,review.status)&&typeof review.note==='string'&&!(review.status==='false_positive'&&!review.note.trim())){
     event.review=review.status;event.note=review.note.slice(0,2000);
    }
   }
   if(saved.policies&&typeof saved.policies==='object'){
    for(const [id,policy]of Object.entries(saved.policies).slice(0,50)){
     if(/^[a-z0-9-]{1,80}$/.test(id)&&typeof policy?.helmet==='boolean'&&typeof policy?.vest==='boolean')
      this.policies[id]={helmet:policy.helmet,vest:policy.vest,note:cleanText(policy.note,500)};
    }
   }
  }catch{this.storageAvailable=false}
 }
 save(){
  if(!this.storage)return;
  try{
   const reviews=Object.fromEntries(this.events.filter(e=>e.source==='DEMO').map(e=>[e.id,{status:e.review,note:e.note}]));
   this.storage.setItem(STORAGE_KEY,JSON.stringify({version:1,reviews,policies:this.policies}));this.storageAvailable=true;
  }catch{this.storageAvailable=false}
 }
 // 실제 연결(link)이 있으면 예시 모드가 아니어도 조작이 열린다. 링크가 없을
 // 때의 차단 사유는 그대로다 — **붙일 서버가 없는데 열어 두지 않는다.**
 get live(){return !this.demo&&this.connected}
 get connected(){return ROBOTS.some(id=>this.slots[id].link)}
 slot(id=this.selected){return this.slots[id]??null}
 // 명령은 **지금 고른 로봇**의 링크로만 나간다 — 비상정지도 마찬가지다 (선택 로봇 정지).
 get link(){return this.slot()?.link??null}
 set link(value){this.slots[ROBOTS[0]].link=value??null}
 // ⚠️ **실제 연결에서는 링크가 붙은 자리만 고를 수 있다.** 관제 서버 한 대짜리면 MD-01 하나다 —
 // 예시 로봇 MD-02·03 을 고를 수 있게 두면 "MD-02 제어권" 으로 잡은 조작이 실제로는 그 한 대를 움직인다.
 get robots(){return this.live?ROBOTS.filter(id=>this.slots[id].link):ROBOTS}
 // 화면에 쓰는 이름. 실제 연결이면 서버가 알려 준 개체 프로파일 이름을 쓴다 — 받기 전에는 내부 id.
 robotName(id){const slot=this.slots[id];return this.live&&slot?.link?(slot.device||cleanText(slot.telemetry?.snapshot?.deviceId,80)||id):id}
 // 텔레메트리 개체 ID(MAC)가 설정에 없는 자리 — 실물이 아직 없다 (config/devices/<id>.yaml).
 isRegistered(id){return this.slots[id]?.registered!==false}
 get estop(){return this.live?!!this.slot()?.estop:this.demoEstop}
 set estop(value){if(!this.live){this.demoEstop=!!value;return}const slot=this.slot();if(slot)slot.estop=!!value}
 get telemetry(){return this.slot()?.telemetry??{state:'off'}}
 telemetryOf(id){return this.slots[id]?.telemetry??{state:'off'}}
 get lastEscalation(){return this.slot()?.lastEscalation??null}
 get liveFeed(){return (this.slot()??this.slots[ROBOTS[0]]).liveFeed}
 get policy(){return this.slot()?.policy??this.slots[ROBOTS[0]].policy}
 get blocked(){return (!this.demo&&!this.link)||this.stale||this.estop||this.role!=='operator'}
 requireControlContext(){if(this.blocked)throw new Error(!this.demo&&!this.link?'실제 제어는 연결되지 않았습니다.':this.stale?'예시 수신 만료 상태입니다.':this.estop?'예시 정지 잠금을 먼저 해제하세요.':'시연 역할을 운영자로 선택하세요.')}
 // 전송 실패를 삼키지 않는다. 화면이 "보냈다" 고 말하면 안 되는 경우다.
 noteLinkError(action,error){this.linkError=action+': '+(error?.message||String(error));this.log('전송 실패',this.linkError,'LIVE_LINK');this.emit('mode')}
 stop(reason='조작 해제'){
  const moving=this.command!=='STOP';this.command='STOP';
  // 멈춤은 **움직이고 있었는지와 무관하게** 내보낸다. 웹이 STOP 이라고 믿는
  // 것과 로봇이 실제로 선 것은 다른 일이고, 어긋났을 때 손해가 큰 쪽이다.
  if(this.live)this.link.drive('STOP').catch(error=>this.noteLinkError('정지',error));
  if(moving){this.log('STOP',reason);this.emit('command')}
 }
 release(reason='제어권 반납'){
  const active=this.control||this.command!=='STOP';this.command='STOP';this.control=null;
  // 제어권을 놓으면 서버 쪽 MANUAL 도 함께 푼다. 웹만 놓고 로봇이 수동에
  // 남아 있으면 자율 주행이 돌아오지 않는다.
  if(active&&this.live)this.link.manual(false).catch(error=>this.noteLinkError('수동 해제',error));
  if(active){this.log('제어권 해제',reason);this.emit('command')}
 }
 suspend(reason){
  this.release(reason);
  if(this.mission.status==='running'){this.setMissionStatus('paused');this.log('임무 일시정지',reason);this.emit('mission')}
 }
 setMissionStatus(status){this.mission.status=status;const session=this.sessions.find(s=>s.id===this.mission.id);if(session)session.status=status}
 setDemo(value){this.suspend('예시 모드 변경');this.demo=!!value;if(!this.robots.includes(this.selected))this.selected=this.robots[0];this.log('모드 변경',this.demo?'예시':'실제 데이터 대기');this.emit('mode')}
 setStale(value){this.stale=!!value;if(value)this.suspend('예시 수신 만료');this.log('예시 연결 상태',value?'만료':'복구 · 자동 재개 안 함');this.emit('mode')}
 setRole(role){if(!['operator','reviewer','technician'].includes(role))throw new Error('알 수 없는 시연 역할입니다.');this.suspend('시연 역할 변경');this.role=role;this.log('시연 역할',role+' · 실제 인증 아님');this.emit('mode')}
 selectRobot(id){if(!this.robots.includes(id))throw new Error('알 수 없는 로봇입니다.');if(this.selected!==id)this.release('관측 장치 전환');this.selected=id;this.log('로봇 시점 선택',id);this.emit('robot')}
 claim(){
  this.requireControlContext();this.suspend('수동 조작 전환');this.control=this.selected;
  if(this.live){
   // 서버 쪽 MANUAL 진입. 실패하면 제어권을 잡은 척하지 않는다.
   this.link.manual(true).catch(error=>{this.control=null;this.noteLinkError('수동 진입',error);this.emit('command')});
   this.log('수동 제어권 요청',this.selected+' · 서버 MANUAL 요청','LIVE_LINK');
  }else{
   this.log('예시 제어권 요청',this.selected+' · 실제 권한/ACK 아님');
  }
  this.emit('command');
 }
 move(command){
  if(command==='STOP'){this.stop();return}
  this.requireControlContext();if(this.control!==this.selected)throw new Error(this.live?'먼저 수동 제어권을 요청하세요.':'먼저 예시 제어권을 요청하세요.');
  if(!ALLOWED_COMMANDS.includes(command))throw new Error('허용되지 않은 방향입니다.');
  this.command=command;
  if(this.live){
   this.link.drive(command).catch(error=>this.noteLinkError('수동 명령',error));
   this.log('수동 명령',command+' · 서버 전송','LIVE_LINK');
  }else{
   this.log('예시 명령',command+' · 전송 안 함');
  }
  this.emit('command');
 }
 startMission({robot=this.selected,zone='생산 구역',acknowledged=false}={}){
  this.requireControlContext();if(!acknowledged)throw new Error('예시 임무라는 안내를 먼저 확인해 주세요.');
  if(!ROBOTS.includes(robot)||!cleanText(zone,100))throw new Error('장치와 구역을 선택해 주세요.');
  if(['running','paused'].includes(this.mission.status))throw new Error('현재 임무를 종료한 뒤 새 임무를 시작하세요.');
  this.release('임무 시작');this.selected=robot;
  const id='DEMO-SESSION-'+this.clock()+'-'+(++this.serial);
  this.mission={status:'running',robot,zone:cleanText(zone,100),id};
  this.sessions.unshift({id,source:'LOCAL_UI_PREVIEW_ONLY',robot,zone:cleanText(zone,100),startedAt:this.clock(),endedAt:null,status:'running'});
  this.log('예시 임무 시작',robot+' · '+zone);this.emit('mission');
 }
 pauseMission(){if(this.mission.status!=='running')return;this.setMissionStatus('paused');this.log('예시 임무 일시정지');this.emit('mission')}
 resumeMission(){this.requireControlContext();if(this.mission.status!=='paused')throw new Error('일시정지된 임무가 없습니다.');this.release('순찰 재개');this.setMissionStatus('running');this.log('예시 임무 재개');this.emit('mission')}
 endMission(){if(this.role!=='operator')throw new Error('운영자 시연 역할에서 종료하세요.');if(!['running','paused'].includes(this.mission.status))return;this.release('임무 종료');this.setMissionStatus('ended');const session=this.sessions.find(s=>s.id===this.mission.id);if(session)session.endedAt=this.clock();this.log('예시 임무 종료');this.emit('mission')}
 // ⚠️ **비상정지는 조건을 검사하지 않는다** (FR-4.4). 제어권이 없어도, 역할이
 // 무엇이어도, 이미 잠겨 있어도 누르면 나간다. 서버 쪽 `estop()` 도 같은 규칙이다.
 requestEstop(){
  const live=this.live;
  if(live)this.link.estop().catch(error=>this.noteLinkError('비상정지',error));
  this.suspend('긴급 정지');this.estop=true;
  this.log(live?'긴급 정지':'예시 정지 잠금',live?'서버 ESTOP 전송 · '+this.robotName(this.selected):'실물 명령 전송 없음 / ACK 없음',live?'LIVE_LINK':undefined);
  this.emit('mode');
 }
 clearPreviewStop(){this.estop=false;this.log('예시 잠금 초기화','실물 안전 잠금 해제 아님 · 자동 재개 안 함');this.emit('mode')}
 // ── 실제 장비 명령 (폐기된 /live 최소 화면의 기능을 관제로 옮긴 것) ──────
 // 장비 상태는 /ws/telemetry 피드(setTelemetry · 4.6.2)에서 읽는다 — 따로 폴링하지 않는다.
 // 로봇에서 받은 값이 없으면 null 이다. 끊겼으면 마지막 값이며, 화면이 끊김을 함께 말한다.
 get deviceTelemetry(){return this.telemetry?.snapshot?.telemetry??null}
 get serviceMode(){return this.deviceTelemetry?.flags.service??null}
 get safetyLatched(){return this.deviceTelemetry?this.deviceTelemetry.safetyLatched:null}
 get fsmState(){return this.telemetry?.snapshot?.state??''}
 // 운용 모드 (FR-4.7). 로봇에서 받은 값이며 **모르면 null 이다** — 화면이 «경비» 로
 // 추측하면 공장 순찰을 경비로 착각한다.
 get missionMode(){return this.telemetry?.snapshot?.mode??null}
 // 실제 명령의 공통 경로 — 링크가 없으면 절대 나가지 않고, 거절도 숨기지 않는다.
 requestDevice(label,send){
  if(!this.live)throw new Error('실제 제어는 연결되지 않았습니다.');
  this.log('실제 '+label+' 요청',this.selected,'LIVE_LINK');
  return send().then(result=>{
   const rejected=result&&result.accepted===false;
   this.log('실제 '+label+' 응답',(rejected?'거절 · ':'')+(result?.detail||JSON.stringify(result)),'LIVE_LINK');
   // 상태 변화는 텔레메트리 피드가 곧 가져온다. 여기서는 기록이 바뀐 것만 알린다.
   this.emit('device');
   if(rejected)throw new Error('거절됨 — '+(result.detail||'로봇이 거절했습니다.'));
   return result;
  },error=>{this.noteLinkError(label,error);throw error});
 }
 requestService(on){return this.requestDevice('서비스 모드 '+(on?'진입':'해제'),()=>this.link.service(on?'enter':'exit'))}
 requestResetSafe(){return this.requestDevice('안전 해제',()=>this.link.resetSafe())}
 // 경보(L3) 확인. ⚠️ **안전 해제와 합치지 않는다** — 확인하는 대상이 다르다 (ADR-26).
 requestAlarmConfirm(){return this.requestDevice('경보 확인',()=>this.link.confirmAlarm())}
 requestPatrol(start){return this.requestDevice(start?'순찰 시작':'순찰 정지',()=>this.link.patrol(start?'start':'stop'))}
 // 본체 자세 (B6). 서버가 MANUAL 에서만 받는다 — 여기서는 제어권과 정지 상태를 먼저 본다.
 requestPose(preset){
  if(!['up','level','down'].includes(preset))throw new Error('허용되지 않은 자세입니다.');
  if(!this.control)throw new Error('먼저 수동 제어권을 요청해 주세요.');
  if(this.command!=='STOP')throw new Error('이동을 멈춘 뒤 자세를 바꿔 주세요.');
  return this.requestDevice('자세 '+{up:'앞쪽 들기',level:'기준 자세',down:'앞쪽 낮추기'}[preset],()=>this.link.pose(preset));
 }
 // 경보 띠 (B1). 단계는 10Hz 상태 전문에서, 사유·경고 문장은 같은 단계의 마지막 단계 사건에서 읽는다.
 // ⚠️ 단계가 다르면 사유를 붙이지 않는다 — 지난 L2 의 사유를 지금 L3 에 붙이면 거짓이다.
 alarmOf(id){
  const slot=this.slots[id];
  if(!this.live||!slot?.link)return null;
  const snapshot=slot.telemetry?.snapshot,level=snapshot?.escalation;
  if(!['L2','L3','F'].includes(level))return null;
  const last=slot.lastEscalation?.escalation===level?slot.lastEscalation:null;
  return {robot:id,name:this.robotName(id),level,state:snapshot.state??'',stale:!!(snapshot.stale||snapshot.runtimeStale),reason:last?reasonName(last.reason):null,warning:last?.warning||null};
 }
 get alarm(){return this.alarmOf(this.selected)}
 // 경보 띠는 **모든 로봇**을 본다 — MD-01 을 보고 있는 동안 MD-02 가 L3 가 돼도 띠가 떠야 한다. 심한 것부터.
 get alarms(){const rank={F:3,L3:2,L2:1};return this.robots.map(id=>this.alarmOf(id)).filter(Boolean).sort((a,b)=>rank[b.level]-rank[a.level])}
 // 형식이 다르면 받지 않는다 — 엉뚱한 응답을 «서버 설정값» 이라고 적으면 안 된다.
 setPolicy(policy,robot=ROBOTS[0]){this.slots[robot].policy=['l1_to_l2_hold_s','auth_timeout_s','auth_max_attempts','target_lost_timeout_s'].every(key=>Number.isInteger(policy?.[key]))?policy:null;this.emit('policy')}
 // 전환은 IDLE·MANUAL 에서만 받는다 (FR-11.3). 거절 사유는 requestDevice 가 그대로 남긴다.
 requestMode(name){return this.requestDevice('운용 모드 '+name,()=>this.link.mode(name))}
 queryEvents({type='all',status='all',robot='all',query=''}={}){
  const q=query.trim().toLocaleLowerCase();
  return this.events.filter(e=>(this.demo||e.source!=='DEMO')&&(type==='all'||e.category===type)&&(status==='all'||e.review===status)&&(robot==='all'||e.robot===robot)&&(!q||[e.id,e.title,e.robot,e.zone,e.event,e.note].join(' ').toLocaleLowerCase().includes(q)));
 }
 reviewEvent(id,status,note){
  if(!Object.hasOwn(REVIEW_STATES,status))throw new Error('검토 상태를 선택해 주세요.');
  const event=this.events.find(e=>e.id===id);if(!event)throw new Error('사건을 찾을 수 없습니다.');
  if(event.source==='LIVE_FEED')throw new Error('실시간 사건의 검토 결과는 서버 저장 기능이 없어 기록할 수 없습니다.');
  if(this.role==='technician')throw new Error('운영자 또는 검토자 시연 역할에서 검토하세요.');
  note=cleanText(note,2000);if(status==='false_positive'&&!note)throw new Error('오탐으로 판단한 근거를 입력해 주세요.');
  event.review=status;event.note=note;event.reviewedAt=this.clock();
  this.save();this.log('사건 검토 저장',id+' · '+REVIEW_STATES[status],event.source==='DEMO'?'LOCAL_UI_PREVIEW_ONLY':'LOCAL_IMPORTED_REVIEW');this.emit('review');
 }
 importBlackbox(raw,snapshot=null){
  const meta=parseBlackbox(raw),key=JSON.stringify(meta);
  const existing=this.events.find(e=>e.importKey===key);
  if(existing){if(snapshot&&!existing.snapshot)existing.snapshot=snapshot;this.emit('import');return existing}
  if(this.events.filter(e=>e.source==='IMPORTED_BLACKBOX').length>=50)throw new Error('이번 세션에는 최대 50건까지 가져올 수 있습니다.');
  const evidence=describeEvidence(meta.event,meta);
  const event={id:'FILE-'+(++this.serial),source:'IMPORTED_BLACKBOX',importKey:key,title:meta.event,category:eventCategory(meta.event),robot:cleanText(meta.telemetry.device_id,80)||'장치 미상',zone:'파일에 구역 정보 없음',event:meta.event,state:meta.state,escalation:meta.escalation,mode:cleanText(meta.mode,40)||null,auth:meta.judgement?evidence.auth:'필드 미제공',ppe:meta.judgement?evidence.ppe:'필드 미제공',evidence:evidence.rows,detail:'Git 블랙박스 형식의 저장 기록입니다. 현재 실시간 상태가 아니며 인증/PPE를 추정하지 않습니다.',ts_ms:meta.ts_ms,review:'pending',note:'',snapshot,meta};
  this.events.unshift(event);this.log('블랙박스 파일 가져오기',event.id,'LOCAL_IMPORTED_REVIEW');this.emit('import');return event;
 }
 // ── 실시간 사건 피드 (WBS 4.6.4) ────────────────────────────────
 // `/ws/events` 의 사건은 기록이다 — 합치지 않고 한 건씩 목록에 넣는다.
 // ⚠️ **사건 전문에는 파일명만 온다** — JPEG 를 실으면 사건 하나가 텔레메트리를
 // 밀어낸다(4.4.3). 그림은 서버의 `/events/<기록>/snapshot.jpg` 에서 받는다.
 // `snapshotBase` 가 없으면(서버 없이 연 화면) 예전처럼 그림 없이 목록만 남는다.
 // `robot` 은 사건을 보낸 서버의 자리다. 여러 대면 순번이 서버마다 따로라 id 에 자리를 넣는다.
 ingestLiveEvent(payload,snapshotBase=null,robot=ROBOTS[0]){
  const slot=this.slots[robot]??this.slots[ROBOTS[0]];
  const seq=payload.seq,id='LIVE-'+(slot.device?robot+'-':'')+seq;
  if(this.events.some(e=>e.id===id))return this.events.find(e=>e.id===id);
  // 실시간 사건은 세션 메모리에만 둔다 — 원본은 블랙박스가 디스크에 갖고 있다.
  if(this.events.filter(e=>e.source==='LIVE_FEED').length>=100)this.events.splice(this.events.findLastIndex(e=>e.source==='LIVE_FEED'),1);
  // 단계·인증 사건은 사진이 없어 텔레메트리를 싣지 않는다 — 이 서버가 알려 준 개체 이름을 쓴다.
  const name=cleanText(payload.event,160),device=slot.device||cleanText(payload.telemetry?.device_id,80)||cleanText(slot.telemetry?.snapshot?.deviceId,80)||'장치 미상';
  const person=(payload.tracks||[]).length,evidence=describeEvidence(name,payload);
  const label=name==='escalation_changed'?'대응 단계 → '+cleanText(payload.escalation,8):EVENT_TITLES[name];
  const photo=payload.entry!=null||payload.snapshot!=null;
  const event={id,seq,source:'LIVE_FEED',title:label?label+' · '+name:name,category:eventCategory(name),robot:device,zone:cleanText(payload.judgement?.zone,40)||'구역 미수신',event:name,state:cleanText(payload.state,40),escalation:cleanText(payload.escalation,40),mode:cleanText(payload.mode,40)||null,auth:evidence.auth,ppe:evidence.ppe,evidence:evidence.rows,detail:'실시간 수신된 사건입니다.'+(person?' 추적 '+person+'명이 함께 기록됐습니다. ':' ')+(payload.snapshot?'그때 저장된 스냅샷을 함께 보여 줍니다. 원본은 기록 디렉터리 '+(payload.entry||'')+' 안에 있습니다.':photo?'스냅샷 파일이 없는 사건입니다.':'상태 전이 사건이라 사진을 남기지 않습니다.'),ts_ms:payload.ts_ms,review:'pending',note:'',snapshot:liveSnapshotUrl(snapshotBase,payload),
   meta:{tracks:payload.tracks||[],detections:payload.detections||[],telemetry:payload.telemetry||{}}};
  // 최근 단계 사건 — 경보 띠가 «왜 이 단계인가» 와 경고 문장을 보인다 (B1). 백로그는 순번이 낮은 것부터 온다.
  if(name==='escalation_changed'&&(!slot.lastEscalation||seq>slot.lastEscalation.seq))slot.lastEscalation={seq,escalation:event.escalation,reason:cleanText(payload.reason,80),warning:cleanText(payload.warning,300),ts_ms:payload.ts_ms};
  this.events.unshift(event);this.log('실시간 사건 수신',id+' · '+event.title,'LIVE_EVENT_FEED');this.emit('import');return event;
 }
 noteEventGap(dropped){
  this.log('사건 수신 공백',dropped+'건을 버퍼에서 놓쳤습니다 · 서버가 조용히 넘기지 않고 알려준 것','LIVE_EVENT_FEED');this.emit('import');
 }
 setEventFeed(status,robot=ROBOTS[0]){const slot=this.slots[robot],prev=slot.liveFeed;slot.liveFeed=status;if(prev.state!==status.state)this.emit('mode')}
 // 로봇 상태 게이지 (WBS 4.6.2). 초당 10번 오므로 전체 화면을 다시 그리는 'mode' 가 아니라 'telemetry' 로 알린다.
 setTelemetry(view,robot=ROBOTS[0]){this.slots[robot].telemetry=view;this.emit('telemetry')}
 savePolicy(id,{helmet,vest,note}){
  if(!/^[a-z0-9-]{1,80}$/.test(id)||typeof helmet!=='boolean'||typeof vest!=='boolean')throw new Error('정책 입력을 확인해 주세요.');
  this.policies[id]={helmet,vest,note:cleanText(note,500)};this.save();
  this.log('PPE 정책 초안 저장',id+' · 실제 판정에 적용되지 않음');this.emit('policy');
 }
 exportRecords(){this.log('조작 이력 내보내기','CSV · 로컬 기록');return toCsv(this.records.map(r=>[new Date(r.ts_ms).toISOString(),r.source,r.action,r.detail]),['time','source','action','detail'])}
 exportReview(){
  return JSON.stringify({format:'MECHADOG_LOCAL_REVIEW_V1',exportedAt:new Date(this.clock()).toISOString(),sourceRevision:SOURCE_REVISION,live:false,events:this.events.filter(e=>this.demo||e.source!=='DEMO').map(({snapshot,importKey,...e})=>({...e,snapshotIncluded:false}))},null,2);
 }
}
