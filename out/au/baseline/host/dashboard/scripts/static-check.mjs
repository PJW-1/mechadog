// 관제 화면 정적 파일 검사 — 대시보드 서버가 그대로 내보내는 `static/` 을 지킨다.
//
// 화면은 빌드 없이 서버가 바로 내보낸다. 그래서 three.js 도 `static/vendor/` 에
// 직접 싣는다 — 설치를 빠뜨리면 페이지 코드가 통째로 실행되지 않던 문제
// (three.js 404 → app.js 미실행 → 클릭 전부 불가)를 구조로 없앤다.
//
//   npm run check    JS 문법 검사 + static/vendor 가 잠긴 three 버전과 같은지 확인
//   npm run vendor   three 버전을 올린 뒤 static/vendor 를 다시 채운다
//
// 싣는 파일은 화면 코드가 **실제로 불러오는 것만**이다(전이 포함). 목록이 모자라면
// `web-tests/factory.test.mjs` 의 import 해석 시험이 실패한다.
import {copyFile, mkdir, readFile, readdir} from 'node:fs/promises';
import {spawnSync} from 'node:child_process';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const staticDir = resolve(here, '..', 'static');
const vendorDir = join(staticDir, 'vendor');
const three = resolve(here, '..', 'node_modules', 'three');

// [node_modules/three 안의 경로, static/vendor 안의 경로]
const VENDORED = [
  ['build/three.module.js', 'three.module.js'],
  ['build/three.core.js', 'three.core.js'],
  ['LICENSE', 'THREE-LICENSE.txt'],
  ...[
    'controls/OrbitControls.js',
    'environments/RoomEnvironment.js',
    'geometries/RoundedBoxGeometry.js',
    'math/SimplexNoise.js',
    'postprocessing/EffectComposer.js',
    'postprocessing/GTAOPass.js',
    'postprocessing/MaskPass.js',
    'postprocessing/OutputPass.js',
    'postprocessing/Pass.js',
    'postprocessing/RenderPass.js',
    'postprocessing/ShaderPass.js',
    'shaders/CopyShader.js',
    'shaders/GTAOShader.js',
    'shaders/OutputShader.js',
    'shaders/PoissonDenoiseShader.js',
    'utils/BufferGeometryUtils.js',
  ].map((path) => [`examples/jsm/${path}`, `addons/${path}`]),
];

async function writeVendor() {
  for (const [from, to] of VENDORED) {
    const target = join(vendorDir, to);
    await mkdir(dirname(target), {recursive: true});
    await copyFile(join(three, from), target);
  }
  console.log(`static/vendor 에 three.js ${VENDORED.length}개 파일을 실었다`);
}

async function check() {
  const failures = [];
  for (const name of (await readdir(staticDir)).filter((file) => file.endsWith('.js'))) {
    const result = spawnSync(process.execPath, ['--check', join(staticDir, name)], {encoding: 'utf8'});
    if (result.status !== 0) failures.push(`문법 오류 ${name}\n${result.stderr}`);
  }
  for (const [from, to] of VENDORED) {
    const [expected, actual] = await Promise.all([
      readFile(join(three, from)),
      readFile(join(vendorDir, to)).catch(() => null),
    ]);
    // Windows 에서 git 이 줄바꿈을 CRLF 로 바꿔 받아도 같은 파일이다 — 내용만 비교한다.
    const text = (buffer) => buffer.toString('utf8').replaceAll('\r\n', '\n');
    if (actual === null) failures.push(`vendor 에 없음: ${to}`);
    else if (text(expected) !== text(actual)) failures.push(`잠긴 three 버전과 다름: ${to} — npm run vendor`);
  }
  if (failures.length) {
    console.error(failures.join('\n'));
    process.exit(1);
  }
  console.log(`통과 — 문법 검사 · three.js ${VENDORED.length}개 파일 일치`);
}

await (process.argv.includes('--write') ? writeVendor() : check());
