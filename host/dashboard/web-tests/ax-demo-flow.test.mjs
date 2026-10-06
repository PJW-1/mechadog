import assert from 'node:assert/strict';
import test from 'node:test';
import {describeEvidence, eventCategory} from '../static/operations.js';

test('PPE recheck shows normal only after wearing is confirmed for the same track', () => {
  const payload = {judgement: {state: '적합', reason: '착용 확인', rechecked: true, track_id: 7}};
  const evidence = describeEvidence('PPE_SETTLED', payload);
  assert.equal(eventCategory('PPE_SETTLED'), 'PPE');
  assert.equal(evidence.ppe, '정상 · 착용 확인');
  assert.ok(evidence.rows.some(([label, value]) => label === '대상 추적 ID' && value === '#7'));
});

test('warning completion or unknown verdict does not claim wearing confirmation', () => {
  for (const state of ['위반', '판정불가']) {
    const evidence = describeEvidence('PPE_SETTLED', {judgement: {state, reason: '경고 종료'}});
    assert.ok(!evidence.ppe.includes('정상'));
  }
});
