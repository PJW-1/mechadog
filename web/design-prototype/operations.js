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

function cleanText(value,max=1000){return typeof value==='string'?value.trim().slice(0,max):''}
export function parseBlackbox(value) {
 if(!value||typeof value!=='object'||Array.isArray(value))throw new Error('meta.json 객체를 선택해 주세요.');
 if(!Number.isSafeInteger(value.ts_ms)||value.ts_ms<0||value.ts_ms>8640000000000000)throw new Error('유효한 ts_ms가 없습니다.');
 for(const key of ['event','state','escalation'])if(typeof value[key]!=='string'||!value[key].trim()||value[key].length>160)throw new Error(key+' 필드를 확인해 주세요.');
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
 constructor({storage=null,clock=nowDefault}={}) {
  this.storage=storage;this.clock=clock;this.listeners=new Set();
  this.demo=true;this.stale=false;this.estop=false;this.role='operator';this.selected='MD-01';
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
 get blocked(){return !this.demo||this.stale||this.estop||this.role!=='operator'}
 requireControlContext(){if(this.blocked)throw new Error(!this.demo?'실제 제어는 연결되지 않았습니다.':this.stale?'예시 수신 만료 상태입니다.':this.estop?'예시 정지 잠금을 먼저 해제하세요.':'시연 역할을 운영자로 선택하세요.')}
 stop(reason='조작 해제'){
  const moving=this.command!=='STOP';this.command='STOP';
  if(moving){this.log('STOP',reason);this.emit('command')}
 }
 release(reason='제어권 반납'){
  const active=this.control||this.command!=='STOP';this.command='STOP';this.control=null;
  if(active){this.log('제어권 해제',reason);this.emit('command')}
 }
 suspend(reason){
  this.release(reason);
  if(this.mission.status==='running'){this.setMissionStatus('paused');this.log('임무 일시정지',reason);this.emit('mission')}
 }
 setMissionStatus(status){this.mission.status=status;const session=this.sessions.find(s=>s.id===this.mission.id);if(session)session.status=status}
 setDemo(value){this.suspend('예시 모드 변경');this.demo=!!value;this.log('모드 변경',this.demo?'예시':'실제 데이터 대기');this.emit('mode')}
 setStale(value){this.stale=!!value;if(value)this.suspend('예시 수신 만료');this.log('예시 연결 상태',value?'만료':'복구 · 자동 재개 안 함');this.emit('mode')}
 setRole(role){if(!['operator','reviewer','technician'].includes(role))throw new Error('알 수 없는 시연 역할입니다.');this.suspend('시연 역할 변경');this.role=role;this.log('시연 역할',role+' · 실제 인증 아님');this.emit('mode')}
 selectRobot(id){if(!ROBOTS.includes(id))throw new Error('알 수 없는 로봇입니다.');if(this.selected!==id)this.release('관측 장치 전환');this.selected=id;this.log('로봇 시점 선택',id);this.emit('robot')}
 claim(){this.requireControlContext();this.suspend('수동 조작 전환');this.control=this.selected;this.log('예시 제어권 요청',this.selected+' · 실제 권한/ACK 아님');this.emit('command')}
 move(command){
  if(command==='STOP'){this.stop();return}
  this.requireControlContext();if(this.control!==this.selected)throw new Error('먼저 예시 제어권을 요청하세요.');
  if(!ALLOWED_COMMANDS.includes(command))throw new Error('허용되지 않은 방향입니다.');
  this.command=command;this.log('예시 명령',command+' · 전송 안 함');this.emit('command');
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
 requestEstop(){this.suspend('예시 긴급 정지');this.estop=true;this.log('예시 정지 잠금','실물 명령 전송 없음 / ACK 없음');this.emit('mode')}
 clearPreviewStop(){this.estop=false;this.log('예시 잠금 초기화','실물 안전 잠금 해제 아님 · 자동 재개 안 함');this.emit('mode')}
 queryEvents({type='all',status='all',robot='all',query=''}={}){
  const q=query.trim().toLocaleLowerCase();
  return this.events.filter(e=>(this.demo||e.source!=='DEMO')&&(type==='all'||e.category===type)&&(status==='all'||e.review===status)&&(robot==='all'||e.robot===robot)&&(!q||[e.id,e.title,e.robot,e.zone,e.event,e.note].join(' ').toLocaleLowerCase().includes(q)));
 }
 reviewEvent(id,status,note){
  if(!Object.hasOwn(REVIEW_STATES,status))throw new Error('검토 상태를 선택해 주세요.');
  const event=this.events.find(e=>e.id===id);if(!event)throw new Error('사건을 찾을 수 없습니다.');
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
  const event={id:'FILE-'+(++this.serial),source:'IMPORTED_BLACKBOX',importKey:key,title:meta.event,category:'SYSTEM',robot:cleanText(meta.telemetry.device_id,80)||'장치 미상',zone:'파일에 구역 정보 없음',event:meta.event,state:meta.state,escalation:meta.escalation,auth:'필드 미제공',ppe:'필드 미제공',detail:'Git 블랙박스 형식의 저장 기록입니다. 현재 실시간 상태가 아니며 인증/PPE를 추정하지 않습니다.',ts_ms:meta.ts_ms,review:'pending',note:'',snapshot,meta};
  this.events.unshift(event);this.log('블랙박스 파일 가져오기',event.id,'LOCAL_IMPORTED_REVIEW');this.emit('import');return event;
 }
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
