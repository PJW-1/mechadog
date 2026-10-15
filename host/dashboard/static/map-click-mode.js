// All map actions use one mutually exclusive mode. I's point localization can
// attach its existing confirmation callback without taking over drawing clicks.
export const MAP_CLICK_MODES = Object.freeze([
  ['move', '이동'], ['locate', '위치 알려주기'], ['draw', '선 그리기'],
]);

export function mapClickMode(panel, selected, onChange, enabled = () => true) {
  return panel.el('div', {class: 'route-map-modes', role: 'group', 'aria-label': '지도 클릭 모드'},
    MAP_CLICK_MODES.map(([mode, label]) => panel.button(label, () => onChange(mode), {
      'data-map-mode': mode, 'aria-pressed': mode === selected, disabled: !enabled(mode),
      class: 'op-button' + (mode === selected ? ' selected' : ''),
    })));
}
