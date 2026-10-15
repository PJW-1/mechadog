"""Bounded read-only local pose client. Render loops never perform HTTP work."""
import json
import math
import threading
import time
from urllib.parse import urlsplit
from urllib.request import urlopen


def visible_pose(state, scene_revision, elapsed_ms=0):
    if not isinstance(state, dict) or state.get('scene_revision') != scene_revision:
        return None
    pose = state.get('pose', {})
    if not pose.get('visible') or pose.get('frame_id') != 'floorplan_xy_m_z_up':
        return None
    coordinates = [pose.get(k) for k in ('x_m','y_m','yaw_rad')]
    if not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)
               for v in coordinates):
        return None
    if elapsed_ms > 500 or elapsed_ms < 0:
        return None
    if state.get('mode') in ('live', 'offline_test'):
        if pose.get('status') not in ('valid', 'lost', 'stale') or pose.get('registration_status') != 'validated':
            return None
    elif state.get('mode') == 'recorded':
        if pose.get('status') != 'recorded_candidate':
            return None
    else:
        return None
    return dict(pose, mode=state['mode'])


class RobotPoseClient:
    def __init__(self, url='http://127.0.0.1:8789/api/robot/state'):
        parsed = urlsplit(url)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
                or parsed.path != '/api/robot/state' or parsed.query or parsed.username
                or parsed.password or parsed.fragment):
            raise ValueError('Only the local /api/robot/state endpoint is supported')
        self.url, self.value, self.received = url, None, 0.
        self.lock, self.stop = threading.Lock(), threading.Event()
        self.thread = threading.Thread(target=self._receive, name='astra-isaac-pose', daemon=True)

    def start(self):
        self.thread.start()

    def _receive(self):
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                with urlopen(self.url,timeout=.4) as response:
                    raw=response.read(1_000_001)
                if len(raw)>1_000_000:
                    raise ValueError('Pose state exceeds bounded response size')
                value=json.loads(raw)
            except (OSError,ValueError,TimeoutError):
                value=None
            with self.lock:
                self.value,self.received=value,started
            self.stop.wait(.1)

    def current(self, scene_revision):
        with self.lock:
            state, received = self.value, self.received
        return visible_pose(state,scene_revision,(time.monotonic()-received)*1000)

    def close(self):
        self.stop.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=1)
