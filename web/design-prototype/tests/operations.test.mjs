import test from 'node:test';
import assert from 'node:assert/strict';
import {Operations,parseBlackbox,csvCell,REVIEW_STATES} from '../operations.js';

const raw=()=>({ts_ms:1700000000000,event:'person_found',state:'OBSERVE',escalation:'L1',tracks:[{track_id:1,box:[10,20,30,40],score:.85}],detections:[{label:'person',score:.9,box:[10,20,30,40]}],telemetry:{device_id:'mechdog-01'}});
const memory=()=>{const data=new Map();return {getItem:key=>data.get(key)??null,setItem:(key,value)=>data.set(key,value)}};

test('initial state is preview only, stopped, unowned, no real telemetry',()=>{
 const op=new Operations();assert.equal(op.demo,true);assert.equal(op.command,'STOP');assert.equal(op.control,null);assert.equal(op.mission.status,'idle');assert.equal(op.records.length,0);
});
test('mission requires preview confirmation; pause/resume/end update same session',()=>{
 const op=new Operations();assert.throws(()=>op.startMission(),/확인/);op.startMission({acknowledged:true});
 op.pauseMission();assert.equal(op.sessions[0].status,'paused');op.resumeMission();assert.equal(op.sessions[0].status,'running');op.endMission();assert.equal(op.sessions[0].status,'ended');assert.ok(op.sessions[0].endedAt);
});
test('same clock produces different session ids',()=>{
 const op=new Operations({clock:()=>100});op.startMission({acknowledged:true});op.endMission();op.startMission({acknowledged:true});assert.notEqual(op.sessions[0].id,op.sessions[1].id);
});
test('observation selection does not stop autonomous preview mission',()=>{
 const op=new Operations();op.startMission({acknowledged:true});op.selectRobot('MD-02');assert.equal(op.mission.status,'running');assert.equal(op.mission.robot,'MD-01');assert.equal(op.selected,'MD-02');
});
test('manual commands require ownership and all context gates',()=>{
 const op=new Operations();assert.throws(()=>op.move('FORWARD'),/제어권/);op.claim();op.move('FORWARD');assert.equal(op.command,'FORWARD');op.stop();assert.equal(op.command,'STOP');assert.throws(()=>op.move('FLY'),/방향/);
 op.selectRobot('MD-02');assert.equal(op.control,null);assert.throws(()=>op.move('FORWARD'),/제어권/);
});
test('claiming manual control pauses the mission',()=>{
 const op=new Operations();op.startMission({acknowledged:true});op.claim();assert.equal(op.mission.status,'paused');assert.equal(op.sessions[0].status,'paused');assert.equal(op.control,'MD-01');
});
for(const [name,interrupt,recover]of [
 ['stale',op=>op.setStale(true),op=>op.setStale(false)],
 ['mode',op=>op.setDemo(false),op=>op.setDemo(true)],
 ['role',op=>op.setRole('reviewer'),op=>op.setRole('operator')],
 ['estop',op=>op.requestEstop(),op=>op.clearPreviewStop()]
])test(name+' stops inputs, pauses sessions and never auto resumes',()=>{
 const op=new Operations();op.startMission({acknowledged:true});interrupt(op);assert.equal(op.mission.status,'paused');assert.equal(op.sessions[0].status,'paused');assert.throws(()=>op.resumeMission());recover(op);assert.equal(op.mission.status,'paused');
 op.claim();op.move('FORWARD');interrupt(op);assert.equal(op.command,'STOP');assert.equal(op.control,null);assert.throws(()=>op.claim());recover(op);assert.equal(op.command,'STOP');assert.equal(op.control,null);
});
test('reviewer cannot finish a mission; STOP is always allowed',()=>{
 const op=new Operations();op.startMission({acknowledged:true});op.setRole('reviewer');assert.throws(()=>op.endMission(),/운영자/);op.move('STOP');assert.equal(op.command,'STOP');
});
test('false positive requires grounds, unverifiable remains separate',()=>{
 const op=new Operations();assert.throws(()=>op.reviewEvent('DEMO-PPE-01','false_positive','  '),/근거/);
 op.reviewEvent('DEMO-PPE-01','unverifiable','가림');assert.equal(op.events[0].review,'unverifiable');assert.equal(Object.keys(REVIEW_STATES).length,5);
 op.reviewEvent('DEMO-PPE-01','false_positive','다른 물체');assert.equal(op.events[0].review,'false_positive');op.setRole('technician');assert.throws(()=>op.reviewEvent('DEMO-PPE-01','resolved','완료'),/검토자/);
});
test('review and policy drafts restore without resuming control or importing evidence',()=>{
 const storage=memory(),op=new Operations({storage});op.reviewEvent('DEMO-PPE-01','confirmed','확인');op.savePolicy('central-corridor',{helmet:true,vest:false,note:'검토 초안'});op.importBlackbox(raw());op.claim();op.move('FORWARD');
 const fresh=new Operations({storage});assert.equal(fresh.events[0].review,'confirmed');assert.equal(fresh.policies['central-corridor'].helmet,true);assert.equal(fresh.events.length,3);assert.equal(fresh.command,'STOP');assert.equal(fresh.control,null);
});
test('unavailable or corrupt storage is reported without blocking local work',()=>{
 const storage={getItem:()=>'{bad',setItem:()=>{throw new Error('quota')}};const op=new Operations({storage});assert.equal(op.storageAvailable,false);op.reviewEvent('DEMO-PPE-01','unverifiable','가림');assert.equal(op.storageAvailable,false);assert.equal(op.events[0].note,'가림');
});
test('query combines filters and demo-off does not hide imported files',()=>{
 const op=new Operations();assert.equal(op.queryEvents({type:'PPE',status:'pending',robot:'MD-01',query:'안전모'}).length,1);
 op.importBlackbox(raw());op.setDemo(false);assert.equal(op.queryEvents().length,1);assert.equal(op.queryEvents()[0].source,'IMPORTED_BLACKBOX');assert.equal(op.queryEvents({query:'absent'}).length,0);
});
test('blackbox importer matches current raw fields and copies input',()=>{
 const input=raw(),parsed=parseBlackbox(input);assert.notEqual(parsed,input);input.tracks[0].score=0;assert.equal(parsed.tracks[0].score,.85);
 const op=new Operations(),event=op.importBlackbox(parsed);assert.equal(event.escalation,'L1');assert.equal(event.auth,'필드 미제공');assert.equal(event.ppe,'필드 미제공');assert.equal(event.snapshot,null);
});
test('bad blackbox fields are rejected',()=>{
 for(const modify of [v=>{v.ts_ms=-1},v=>{v.ts_ms=Infinity},v=>{v.event=''},v=>{v.tracks=null},v=>{v.tracks[0].box=[3,2,1,0]},v=>{v.tracks[0].score=2},v=>{v.tracks[0].track_id={}},v=>{v.detections[0].label={}},v=>{v.telemetry=[]},v=>{v.telemetry.device_id={}}]){const input=raw();modify(input);assert.throws(()=>parseBlackbox(input))}
});
test('only matching full metadata deduplicates; devices and evidence stay distinct',()=>{
 const op=new Operations(),a=op.importBlackbox(raw(),'blob:a');assert.equal(op.importBlackbox(raw(),'blob:duplicate'),a);assert.equal(a.snapshot,'blob:a');
 const otherDevice=raw();otherDevice.telemetry.device_id='mechdog-02';const b=op.importBlackbox(otherDevice,'blob:b');assert.notEqual(a,b);assert.equal(a.snapshot,'blob:a');assert.equal(b.snapshot,'blob:b');
 const otherDetection=raw();otherDetection.detections[0].label='helmet';assert.notEqual(op.importBlackbox(otherDetection),a);
});
test('import limit bounds session memory',()=>{
 const op=new Operations();for(let index=0;index<50;index++){const item=raw();item.ts_ms+=index;op.importBlackbox(item)}const item=raw();item.ts_ms+=100;assert.throws(()=>op.importBlackbox(item),/50/);
});
test('CSV cells escape formula prefixes, quote and multiline data',()=>{
 for(const text of ['=1+1',' +cmd','@SUM(A1)','-1','\tfoo','\nfoo'])assert.ok(csvCell(text).startsWith('"\''));assert.equal(csvCell('a"b'),'"a""b"');
 const op=new Operations();op.log('=cmd','a,b\nsecond');const csv=op.exportRecords();assert.ok(csv.startsWith('\ufeff'));assert.ok(csv.includes('"\'=cmd"'));assert.ok(csv.includes('LOCAL_UI_PREVIEW_ONLY'));
});
test('review export discloses source and excludes blob URL and embedded images',()=>{
 const op=new Operations();op.importBlackbox(raw(),'blob:private-image');const text=op.exportReview(),json=JSON.parse(text);assert.equal(json.live,false);assert.ok(!text.includes('blob:'));assert.ok(json.events.every(event=>event.snapshotIncluded===false));
});
