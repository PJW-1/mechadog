// Both SVG editors use the same server pixel frame and normalized viewBox.
export function applyAffine(m, x, y) {
  return [m[0][0] * x + m[0][1] * y + m[0][2], m[1][0] * x + m[1][1] * y + m[1][2]];
}

export function mapPoint(meta, x, y) {
  const [u, v] = applyAffine(meta.patrol_to_px, x, y);
  return [u / meta.width * 1000, v / meta.height * 1000];
}

// Translation affects position, never heading. Include map rotation and axis flips.
export function mapHeading(meta, yawDeg) {
  const yaw = yawDeg * Math.PI / 180, m = meta.patrol_to_px;
  return Math.atan2(m[1][0] * Math.cos(yaw) + m[1][1] * Math.sin(yaw),
    m[0][0] * Math.cos(yaw) + m[0][1] * Math.sin(yaw)) * 180 / Math.PI;
}

export function mapRobotPoints(meta, pose, {normalized = true, length = .3} = {}) {
  const [x, y, yaw] = pose, angle = yaw * Math.PI / 180;
  const dx = Math.cos(angle), dy = Math.sin(angle);
  const project = normalized ? (x, y) => mapPoint(meta, x, y) : (x, y) => applyAffine(meta.patrol_to_px, x, y);
  // A single pointed nose, with a broad flat rear.
  return [[length, 0], [-length * .7, length * .6], [-length * .7, -length * .6]]
    .map(([forward, side]) => project(x + dx * forward - dy * side, y + dy * forward + dx * side));
}

export function worldPoint(meta, x, y) {
  return applyAffine(meta.px_to_patrol, x * meta.width, y * meta.height);
}

export function mapImage(svg, baseUrl, revision) {
  return svg('image', {href: (baseUrl || '') + '/api/map.png?rev=' + revision,
    width: 1000, height: 1000, preserveAspectRatio: 'none', class: 'map-raster'});
}
