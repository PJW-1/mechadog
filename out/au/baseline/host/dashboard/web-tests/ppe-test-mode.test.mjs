import assert from 'node:assert/strict';
import test from 'node:test';
import {overlayBoxes, drawOverlay} from '../static/vision-feed.js';
import {describeEvidence, eventCategory, EVENT_TITLES} from '../static/operations.js';

test('PPE has separate original-coordinate boxes, colors, scores and clipping reasons', () => {
  const header = {width:640,height:480,tracks:[{track_id:1,box:[100,10,300,470]}],detections:[],
    ppe:[{track_id:1,regions:[
      {item:'helmet',label:'no_helmet',score:.9,box:[100,20,150,80]},
      {item:'vest',label:'vest',score:.8,box:[100,100,200,300]},
    ]},{track_id:2,regions:[{item:'helmet',label:'UNDETERMINED',box:[300,0,400,60],reason:'머리 클리핑'}]}]};
  const boxes = overlayBoxes(header);
  assert.equal(boxes[0].kind,'person');
  assert.deepEqual(boxes.slice(1).map(b=>b.color),['#ef4444','#22c55e','#9ca3af']);
  assert.match(boxes[1].text,/미착용 90%/);
  assert.match(boxes[3].text,/판정 불가 · 머리 클리핑/);
  const strokes=[];
  const context={canvas:{width:640,height:480},drawImage(){},measureText(){return {width:50}},
    strokeRect(...box){strokes.push([this.strokeStyle,...box])},fillRect(){},fillText(){}};
  drawOverlay(context,{},header);
  assert.deepEqual(strokes[1],['#ef4444',100,20,50,60]);
});

test('cross verification renders yes/no/unknown and delay beside incident evidence', () => {
  for (const vlm of [true,false,null]) {
    const kind=vlm===true?'person_fallen':'fall_review_required';
    const evidence=describeEvidence(kind,{judgement:{rule_yes:true,vlm,latency_ms:123,
      status:vlm===true?'확정':'확인 필요',reason:'test',test_mode:true,reference_reason:'candidate_missing'}});
    assert.ok(evidence.rows.some(([label,value])=>label==='VLM'&&value===(vlm==null?'판정 불가':vlm?'예':'아니요')));
    assert.ok(evidence.rows.some(([label,value])=>label==='VLM 지연'&&value==='123 ms'));
    assert.equal(eventCategory(kind),'SAFETY');
  }
  assert.equal(EVENT_TITLES.fall_review_required,'쓰러짐 확인 필요');
});
