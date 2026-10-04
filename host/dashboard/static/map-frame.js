// Both SVG editors use the same server pixel frame and normalized viewBox.
export function applyAffine(m, x, y) {
  return [m[0][0] * x + m[0][1] * y + m[0][2], m[1][0] * x + m[1][1] * y + m[1][2]];
}

export function mapPoint(meta, x, y) {
  const [u, v] = applyAffine(meta.patrol_to_px, x, y);
  return [u / meta.width * 1000, v / meta.height * 1000];
}

export function worldPoint(meta, x, y) {
  return applyAffine(meta.px_to_patrol, x * meta.width, y * meta.height);
}

export function mapImage(svg, baseUrl, revision) {
  return svg('image', {href: (baseUrl || '') + '/api/map.png?rev=' + revision,
    width: 1000, height: 1000, preserveAspectRatio: 'none', class: 'map-raster'});
}
