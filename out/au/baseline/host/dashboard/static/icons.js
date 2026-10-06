const paths={
cube:'<path d="m12 2 9 5v10l-9 5-9-5V7zM3 7l9 5 9-5M12 12v10M7.5 4.5l9 5"/>',
zones:'<path d="M3 3h7v7H3zM14 3h7v5h-7zM3 14h5v7H3zM12 12h9v9h-9z"/>',
device:'<rect x="5" y="3" width="14" height="18" rx="1"/><path d="M8 7h8M8 11h8M8 15h5"/>',
document:'<path d="M5 2h9l5 5v15H5zM14 2v6h5M8 12h8M8 16h8M8 19h5"/>',
offline:'<path d="m3 3 18 18M3 9a15 15 0 0 1 3-2m4-2a15 15 0 0 1 11 4M6 13a10 10 0 0 1 4-2m4 0a10 10 0 0 1 4 2M9 17a5 5 0 0 1 6 0m-3 4h.01"/>',
target:'<circle cx="12" cy="12" r="7"/><circle cx="12" cy="12" r="3"/><path d="M12 2v3m0 14v3M2 12h3m14 0h3"/>',
camera:'<rect x="3" y="5" width="13" height="14" rx="2"/><path d="m16 10 5-3v10l-5-3z"/>',
lock:'<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 5v2"/>',
stop:'<path d="m8 3-5 5v8l5 5h8l5-5V8l-5-5z"/><path d="M9 9h6v6H9z"/>',
expand:'<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/>',
chevron:'<path d="m6 9 6 6 6-6"/>',
rotate:'<path d="M3 10a9 9 0 1 1 2 8M3 4v6h6"/>',
plus:'<path d="M12 5v14M5 12h14"/>',minus:'<path d="M5 12h14"/>',
home:'<path d="m3 10 9-7 9 7v11h-7v-7h-4v7H3z"/>',
route:'<circle cx="5" cy="5" r="2"/><circle cx="19" cy="19" r="2"/><path d="M7 5h8a4 4 0 0 1 0 8H9a3 3 0 0 0 0 6h8"/>',
play:'<path d="m8 5 11 7-11 7z"/>',pause:'<path d="M8 5v14M16 5v14"/>',
arrow:'<path d="M4 12h16m-6-6 6 6-6 6"/>',close:'<path d="m6 6 12 12M6 18 18 6"/>'
};
export function icon(name){return '<svg viewBox="0 0 24 24" aria-hidden="true">'+(paths[name]||paths.camera)+'</svg>'}
export function renderIcons(root=document){root.querySelectorAll('[data-icon]').forEach(el=>{el.innerHTML=icon(el.dataset.icon)})}
