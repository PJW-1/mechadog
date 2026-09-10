/* Existing sample records: review state is session-local, never an operational verdict. */
const sampleEvents = [
  { id:'EVT-001', time:'19:04:21', type:'PPE', title:'안전모 미착용 감지', zone:'ZONE B', evidence:'신뢰도 89%', status:'pending', note:'', guidance:'같은 추적 대상의 머리 영역과 안전모 착용 여부를 원본 프레임에서 확인하세요. 가림이나 프레임 누락이 있으면 검토 대기를 유지합니다.' },
  { id:'EVT-002', time:'18:58:06', type:'OBJECT', title:'통로 내 신규 물체', zone:'ZONE A', evidence:'기준 이미지 비교', status:'pending', note:'', guidance:'같은 구역의 기준 프레임과 현재 프레임을 비교하고, 다음 순찰에서도 변화가 유지되는지 확인하세요. 검토 판정만으로 기준 물품 목록은 변경되지 않습니다.' },
  { id:'EVT-003', time:'18:44:32', type:'AUTH', title:'미등록 배지 접근', zone:'GATE 02', evidence:'인증 2회 시도', status:'pending', note:'', guidance:'배지 판독 결과와 접근 대상의 추적 ID가 일치하는지 확인하세요. 배지를 읽지 못한 것과 실제 미등록자를 구분해야 합니다.' },
  { id:'EVT-004', time:'17:21:09', type:'PPE', title:'안전조끼 확인 불가', zone:'ZONE D', evidence:'저조도 · 판독 불가', status:'dismissed', note:'예시: 조도가 낮아 착용 여부를 판독할 수 없음.', guidance:'몸통 영역의 조도와 가림을 확인하세요. 안전조끼를 판독할 수 없는 상태만으로 미착용을 확정하지 않습니다.' },
];
const statusLabels = { pending:'검토 대기', confirmed:'확인 완료', dismissed:'오탐 처리' };
const reviewedIds = new Set();
let eventType = 'all';
let selectedEventId = null;
let reviewReturnTarget = null;
const reviewDialog = document.querySelector('#review-dialog');
const decisionInput = document.querySelector('#review-decision');
const noteInput = document.querySelector('#review-note');

function makeElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function openReview(id, opener) {
  const record = sampleEvents.find(item => item.id === id);
  if (!record || !operation.preview) return;
  stopDrive();
  selectedEventId = id;
  reviewReturnTarget = opener;
  document.querySelector('#review-id').textContent = record.id + ' / MD-01 · 예시';
  document.querySelector('#review-title').textContent = record.title;
  const meta = document.querySelector('#review-meta');
  meta.replaceChildren();
  [['감지 시각',record.time],['구역',record.zone],['감지 근거',record.evidence]].forEach(([label,value]) => {
    const cell = makeElement('div');
    cell.append(makeElement('span','',label), makeElement('strong','',value));
    meta.append(cell);
  });
  document.querySelector('#review-guidance').textContent = record.guidance;
  decisionInput.value = record.status;
  noteInput.value = record.note;
  noteInput.required = record.status === 'dismissed';
  noteInput.setCustomValidity('');
  reviewDialog.showModal();
}

function createOpenButton(record, compact) {
  const button = makeElement('button','',compact ? '검토 ↗' : '상세 보기 ↗');
  button.dataset.eventId = record.id;
  button.setAttribute('aria-label', record.title + ' 상세 보기');
  button.addEventListener('click', () => openReview(record.id, button));
  return button;
}

function renderEvents() {
  const records = operation.preview ? sampleEvents : [];
  const search = document.querySelector('#event-search').value.trim().toLocaleLowerCase();
  const status = document.querySelector('#event-status').value;
  const filtered = records.filter(record =>
    (eventType === 'all' || record.type === eventType) &&
    (status === 'all' || record.status === status) &&
    [record.title,record.zone,record.id,record.type].join(' ').toLocaleLowerCase().includes(search));
  document.querySelectorAll('[data-filter-count]').forEach(element => {
    element.textContent = records.filter(record => element.dataset.filterCount === 'all' || record.type === element.dataset.filterCount).length;
  });
  document.querySelectorAll('[data-event-filter]').forEach(button => {
    const active = button.dataset.eventFilter === eventType;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-pressed', String(active));
  });
  const pending = records.filter(record => record.status === 'pending');
  document.querySelectorAll('[data-pending-count]').forEach(element => { element.textContent = pending.length; });
  document.querySelector('#event-result-count').textContent = filtered.length;
  const tbody = document.querySelector('#event-table-body');
  tbody.replaceChildren();
  filtered.forEach(record => {
    const row = makeElement('tr');
    const time = makeElement('td','event-time',record.time);
    const type = makeElement('td'); type.append(makeElement('span','table-tag '+record.type.toLowerCase(),record.type));
    const title = makeElement('td','event-description'); title.append(makeElement('strong','',record.title),makeElement('small','',record.evidence));
    const zone = makeElement('td','event-zone',record.zone);
    const state = makeElement('td'); state.append(makeElement('span','review-status '+record.status,statusLabels[record.status]));
    const action = makeElement('td'); action.append(createOpenButton(record,false));
    row.append(time,type,title,zone,state,action); tbody.append(row);
  });
  const empty = document.querySelector('#event-empty');
  empty.hidden = filtered.length > 0;
  empty.querySelector('strong').textContent = !operation.preview ? '수신된 이벤트가 없습니다.' : '조건에 맞는 이벤트가 없습니다.';
  empty.querySelector('p').textContent = !operation.preview ? '상단에서 예시 데이터를 켜면 검토 흐름을 확인할 수 있습니다.' : '검색어나 필터를 바꿔 다시 확인하세요.';
  document.querySelector('#clear-event-filters').hidden = !operation.preview;
  const list = document.querySelector('.event-list'); list.replaceChildren();
  pending.forEach(record => {
    const row = makeElement('article','event-row' + (record.type === 'PPE' ? ' is-critical' : ''));
    const text = makeElement('div');
    text.append(makeElement('span','',record.type),makeElement('strong','',record.title),makeElement('small','',record.zone+' · '+record.evidence));
    row.append(makeElement('time','',record.time),text,createOpenButton(record,true)); list.append(row);
  });
  if (!pending.length) list.append(makeElement('div','empty-state',operation.preview ? '대기 중인 검토를 모두 정리했습니다.' : '수신된 이벤트가 없습니다.'));
  const count = document.querySelector('#reviewed-count');
  count.replaceChildren(document.createTextNode(String(reviewedIds.size)), makeElement('small','','건'));
}

document.querySelectorAll('[data-event-filter]').forEach(button => button.addEventListener('click', () => {
  eventType = button.dataset.eventFilter; renderEvents();
}));
document.querySelector('#event-search').addEventListener('input', renderEvents);
document.querySelector('#event-status').addEventListener('change', renderEvents);
document.querySelector('#clear-event-filters').addEventListener('click', () => {
  eventType = 'all'; document.querySelector('#event-search').value = ''; document.querySelector('#event-status').value = 'all'; renderEvents();
});
decisionInput.addEventListener('change', () => { noteInput.required = decisionInput.value === 'dismissed'; noteInput.setCustomValidity(''); });
noteInput.addEventListener('input', () => noteInput.setCustomValidity(''));
document.querySelector('#review-form').addEventListener('submit', event => {
  event.preventDefault();
  const record = sampleEvents.find(item => item.id === selectedEventId);
  if (!record || !operation.preview || !Object.hasOwn(statusLabels,decisionInput.value)) return;
  if (decisionInput.value === 'dismissed' && !noteInput.value.trim()) {
    noteInput.setCustomValidity('오탐으로 판단한 근거를 입력하세요.'); noteInput.reportValidity(); return;
  }
  const previous = record.status;
  record.status = decisionInput.value; record.note = noteInput.value.trim();
  reviewedIds.add(record.id);
  recordActivity('이벤트 예시 검토', record.id + ' · ' + statusLabels[previous] + ' → ' + statusLabels[record.status] + (record.note ? ' · '+record.note : ''));
  reviewDialog.close(); renderEvents();
  showToast('예시 판정을 적용했습니다. 실제 운영 기록에는 저장되지 않습니다.');
});
document.querySelector('#review-close').addEventListener('click', () => reviewDialog.close());
document.querySelector('#review-cancel').addEventListener('click', () => reviewDialog.close());
reviewDialog.addEventListener('close', () => {
  // Rows are rerendered after edits, so focus the corresponding replacement if present.
  queueMicrotask(() => {
    const replacement = [...document.querySelectorAll('.view.is-visible [data-event-id]')].find(button => button.dataset.eventId === selectedEventId);
    const fallback = document.querySelector('.view.is-visible [data-jump="events"], .view.is-visible #event-search');
    (reviewReturnTarget?.isConnected ? reviewReturnTarget : replacement || fallback)?.focus();
  });
});
window.addEventListener('preview:state', () => { if (!operation.preview && reviewDialog.open) reviewDialog.close(); renderEvents(); });

function renderAudit() {
  const list = document.querySelector('#audit-list'); list.replaceChildren();
  // The display is bounded; the CSV retains every entry from this session.
  auditEntries.slice(-30).reverse().forEach(entry => {
    const item = makeElement('li');
    const detail = makeElement('div');
    detail.append(makeElement('strong','',entry.action),makeElement('p','',entry.detail));
    const time = new Intl.DateTimeFormat('ko-KR',{timeZone:'Asia/Seoul',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(entry.time));
    item.append(makeElement('time','',time),detail); list.append(item);
  });
  document.querySelector('#audit-empty').hidden = auditEntries.length > 0;
  document.querySelector('#export-audit').disabled = !auditEntries.length;
}
function csvCell(value) {
  const text = String(value);
  const safe = /^[\s]*[=+\-@]/.test(text) || /^[\t\r\n]/.test(text) ? "'" + text : text;
  return '"' + safe.replace(/"/g, '""') + '"';
}
document.querySelector('#export-audit').addEventListener('click', () => {
  if (!auditEntries.length) return;
  const rows = [['time_utc','action','detail','source'], ...auditEntries.map(entry => [entry.time,entry.action,entry.detail,'LOCAL_UI_PREVIEW_ONLY'])];
  const content = '\uFEFF' + rows.map(row => row.map(csvCell).join(',')).join('\r\n');
  const url = URL.createObjectURL(new Blob([content],{type:'text/csv;charset=utf-8;'}));
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = 'mechadog-preview-activity.csv';
  document.body.append(anchor); anchor.click(); anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url),1000);
});
window.addEventListener('preview:audit',renderAudit);
renderEvents();
renderAudit();
