import test from 'node:test';
import assert from 'node:assert/strict';
import {applyAffine, mapHeading, mapRobotPoints} from '../static/map-frame.js';

const wrap = angle => ((angle + 180) % 360 + 360) % 360 - 180;
for (const [rotation, matrix, expected] of [
  [0, [[10,0,123],[0,-10,456]], [0,-90,180,90]],
  [90, [[0,-10,123],[-10,0,456]], [-90,180,90,0]],
  [180, [[-10,0,123],[0,10,456]], [180,90,0,-90]],
]) {
  test(`map rotation ${rotation}: headings and pointed nose follow the affine frame`, () => {
    const meta = {width:400,height:100,patrol_to_px:matrix};
    for (const [i,yaw] of [0,90,180,-90].entries()) {
      assert.ok(Math.abs(wrap(mapHeading(meta,yaw)-expected[i])) < 1e-9);
      const [cx,cy] = applyAffine(matrix,2,3);
      const [tip,left,right] = mapRobotPoints(meta,[2,3,yaw],{normalized:false});
      const direction = Math.atan2(tip[1]-cy,tip[0]-cx)*180/Math.PI;
      assert.ok(Math.abs(wrap(direction-expected[i])) < 1e-9);
      // The midpoint of the flat rear lies behind the robot center.
      assert.ok((tip[0]-cx)*((left[0]+right[0])/2-cx)+(tip[1]-cy)*((left[1]+right[1])/2-cy)<0);
      const normalized = mapRobotPoints(meta,[2,3,yaw]);
      assert.ok(Math.abs(normalized[0][0]*meta.width/1000-tip[0])<1e-9);
      assert.ok(Math.abs(normalized[0][1]*meta.height/1000-tip[1])<1e-9);
    }
  });
}
