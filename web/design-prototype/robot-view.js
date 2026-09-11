import * as THREE from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';
import {RoomEnvironment} from 'three/addons/environments/RoomEnvironment.js';
import {makeRobot} from './scene-materials.js';

const DEFAULT_DIRECTION = new THREE.Vector3(1.5, 1.0, -2).normalize();
const UP = new THREE.Vector3(0, 1, 0);

// A static example model. This viewer does not represent live robot telemetry.
export class RobotDetailView {
  constructor({canvas, onError}) {
    Object.assign(this, {canvas, onError, active:false, disposed:false, lost:false, frame:0, fitDistance:0});
    try {
      this.scene = new THREE.Scene();
      this.scene.background = new THREE.Color(0x20222d);
      this.renderer = new THREE.WebGLRenderer({canvas, antialias:true});
      this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
      this.renderer.outputColorSpace = THREE.SRGBColorSpace;
      this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
      this.renderer.toneMappingExposure = 1.15;
      const room = new RoomEnvironment(), pmrem = new THREE.PMREMGenerator(this.renderer);
      try { this.environment = pmrem.fromScene(room, .04); }
      finally { room.dispose(); pmrem.dispose(); }
      this.scene.environment = this.environment.texture;
      this.scene.environmentIntensity = .55;
      this.scene.add(new THREE.HemisphereLight(0xffffff, 0x414150, 2));
      const key = new THREE.DirectionalLight(0xffedbd, 4);
      key.position.set(1, 3, 3);
      this.scene.add(key);
      const fill = new THREE.DirectionalLight(0xc1cdf4, .7);
      fill.position.set(-3, 2, -2);
      this.scene.add(fill);

      this.robot = makeRobot();
      this.scene.add(this.robot);
      const bounds = new THREE.Box3().setFromObject(this.robot);
      this.center = bounds.getCenter(new THREE.Vector3());
      this.radius = Math.max(.1, bounds.getSize(new THREE.Vector3()).length() / 2);
      this.camera = new THREE.PerspectiveCamera(34, 1, this.radius * .03, this.radius * 40);
      this.camera.position.copy(this.center).add(DEFAULT_DIRECTION);
      this.controls = new OrbitControls(this.camera, canvas);
      this.controls.target.copy(this.center);
      this.controls.enablePan = false;
      this.controls.enableDamping = false;
      this.controls.autoRotate = false;
      this.controls.minPolarAngle = .15;
      this.controls.maxPolarAngle = Math.PI / 2.05;
      this.controls.enabled = false;
      this.changeHandler = () => this.invalidate();
      this.controls.addEventListener('change', this.changeHandler);
      this.controls.update();

      this.visibilityHandler = () => {
        if (this.disposed) return;
        this.controls.enabled = this.canRender();
        if (document.hidden) this.cancelFrame();
        else this.resize();
      };
      this.contextLostHandler = event => {
        event.preventDefault();
        if (this.disposed) return;
        this.lost = true;
        this.controls.enabled = false;
        this.cancelFrame();
        this.onError?.('3D 모델 연결이 중단되었습니다. 그래픽 연결을 복구하고 있습니다.');
      };
      this.contextRestoredHandler = () => {
        if (this.disposed) return;
        this.lost = false;
        this.controls.enabled = this.canRender();
        this.resize();
        this.onError?.(null);
      };
      document.addEventListener('visibilitychange', this.visibilityHandler);
      canvas.addEventListener('webglcontextlost', this.contextLostHandler);
      canvas.addEventListener('webglcontextrestored', this.contextRestoredHandler);
      this.resizeObserver = new ResizeObserver(() => this.resize());
      this.resizeObserver.observe(canvas);
      this.resize();
    } catch (error) {
      this.dispose();
      throw error;
    }
  }

  canRender() { return !this.disposed && this.active && !this.lost && !document.hidden; }
  cancelFrame() { cancelAnimationFrame(this.frame); this.frame = 0; }

  setActive(value) {
    if (this.disposed || this.active === Boolean(value)) return;
    this.active = Boolean(value);
    this.controls.enabled = this.canRender();
    if (this.active) this.resize();
    else this.cancelFrame();
  }

  resize() {
    if (this.disposed || this.lost) return;
    const {width, height} = this.canvas.getBoundingClientRect();
    if (width <= 0 || height <= 0) { this.cancelFrame(); return; }
    const relativeDistance = this.fitDistance ? this.camera.position.distanceTo(this.center) / this.fitDistance : 1;
    const direction = this.camera.position.clone().sub(this.center).normalize();
    this.camera.aspect = width / height;
    const halfVertical = THREE.MathUtils.degToRad(this.camera.fov / 2);
    const halfHorizontal = Math.atan(Math.tan(halfVertical) * this.camera.aspect);
    // Bounding-sphere framing keeps the complete model in view at every angle.
    this.fitDistance = this.radius / Math.sin(Math.min(halfVertical, halfHorizontal)) * 1.08;
    this.controls.minDistance = this.fitDistance * .58;
    this.controls.maxDistance = this.fitDistance * 2.2;
    const distance = this.fitDistance * THREE.MathUtils.clamp(relativeDistance, .58, 2.2);
    this.camera.position.copy(this.center).addScaledVector(direction, distance);
    this.camera.far = Math.max(this.radius * 40, this.controls.maxDistance + this.radius * 4);
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(width, height, false);
    this.controls.update();
    this.invalidate();
  }

  orbit(angleRadians) {
    if (this.disposed || !Number.isFinite(angleRadians)) return;
    const offset = this.camera.position.clone().sub(this.center).applyAxisAngle(UP, angleRadians);
    this.camera.position.copy(this.center).add(offset);
    this.controls.update();
    this.invalidate();
  }

  zoom(factor) {
    if (this.disposed || !Number.isFinite(factor) || factor <= 0 || !this.fitDistance) return;
    const offset = this.camera.position.clone().sub(this.center);
    const distance = THREE.MathUtils.clamp(offset.length() * factor, this.controls.minDistance, this.controls.maxDistance);
    this.camera.position.copy(this.center).add(offset.setLength(distance));
    this.controls.update();
    this.invalidate();
  }

  reset() {
    if (this.disposed) return;
    this.controls.target.copy(this.center);
    this.camera.position.copy(this.center).addScaledVector(DEFAULT_DIRECTION, this.fitDistance || 1);
    this.controls.update();
    this.invalidate();
  }

  invalidate() {
    if (!this.canRender() || this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      if (!this.canRender()) return;
      const {width, height} = this.canvas.getBoundingClientRect();
      if (width <= 0 || height <= 0) return;
      try { this.renderer.render(this.scene, this.camera); }
      catch (error) { this.onError?.('3D 모델을 표시할 수 없습니다. 상세 보기를 다시 열어 주세요.'); }
    });
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.cancelFrame();
    this.resizeObserver?.disconnect();
    document.removeEventListener('visibilitychange', this.visibilityHandler);
    this.canvas?.removeEventListener('webglcontextlost', this.contextLostHandler);
    this.canvas?.removeEventListener('webglcontextrestored', this.contextRestoredHandler);
    this.controls?.removeEventListener('change', this.changeHandler);
    // Each cleanup is independent so a partially constructed renderer can be released.
    for (const resource of [this.controls, this.environment, this.renderer]) {
      try { resource?.dispose(); } catch { /* Best-effort cleanup after initialization failure. */ }
    }
    // makeRobot shares its geometry and materials with the factory; never dispose them here.
    this.scene?.clear();
  }
}
