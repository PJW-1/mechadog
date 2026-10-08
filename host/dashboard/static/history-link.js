// 사건 이력 조회·검토 (WBS 4.6.7 · ADR-46). 서버 쪽은 `/api/history/*` (4.6.6, `server.py` 의 `_history_routes`).
// ⚠️ 플릿에서도 저장소는 하나다 — 어느 로봇의 경로로 물어도 전 기체 기록이 나오고, 기체별은 `robot` 으로 거른다.
// 사진은 새 경로 없이 사건을 낸 로봇 서버의 `/events/<기록>/snapshot.jpg` 에서 받는다(`snapshotBase`).
const MESSAGES={
 history_disabled:'이력 저장이 꺼져 있습니다 — 서버 설정 logging.history_db 가 비어 있어 사건 이력을 남기지 않습니다.',
 history_unavailable:'이력 저장소를 읽지 못했습니다. 잠시 뒤 다시 조회하세요.',
 incident_not_found:'이력에 없는 사건입니다. 아직 저장되지 않았거나 다른 저장소의 사건입니다.',
 network:'관제 서버에 연결하지 못했습니다.'
};
export class HistoryError extends Error{
 constructor(code,status){super(MESSAGES[code]??'이력 요청이 거절됐습니다 ('+code+').');this.code=code;this.status=status}
}
export class HistoryLink{
 constructor({baseUrl='',fetch:fetchImpl=globalThis.fetch,snapshotBase=null}={}){
  // 네이티브 fetch 는 this 가 창이 아니면 거부한다 (robot-link.js 와 같은 이유).
  this.baseUrl=baseUrl;this.fetch=fetchImpl.bind(globalThis);this.snapshotBase=snapshotBase??(()=>baseUrl);
 }
 async request(path,init){
  let response;
  try{response=await this.fetch(this.baseUrl+'/api/history'+path,init)}catch{throw new HistoryError('network',0)}
  const body=await response.json().catch(()=>null);
  if(!response.ok)throw new HistoryError(typeof body?.error==='string'?body.error:'http_'+response.status,response.status);
  return body;
 }
 query(path,params={}){
  const query=new URLSearchParams(Object.entries(params).filter(([,value])=>value!=null&&value!=='').map(([key,value])=>[key,String(value)])).toString();
  return this.request(path+(query?'?'+query:''));
 }
 incidents(filters={}){return this.query('/incidents',filters)}
 incident(id){return this.request('/incidents/'+encodeURIComponent(id))}
 review(id,{reviewed,resolution}){return this.request('/incidents/'+encodeURIComponent(id)+'/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reviewed,resolution})})}
 runs(params={}){return this.query('/runs',params)}
 robots(){return this.request('/robots')}
 zones(){return this.request('/zones')}
}
