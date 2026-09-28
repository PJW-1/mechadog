# PPE XIAO 검수 절차

이 문서는 `tools/ppe_live_check.py`로 실제 기체의 XIAO 영상을 관찰하는 절차다. 도구는
읽기 전용이며 로봇에 자세·이동·경고 명령을 보내지 않는다. 시험 구간의 정본은
`config/ppe_acceptance.json`이다.

## 준비

- 시험 개체의 `--device`와 **현재** XIAO IP를 확인한다. 과거 DHCP 주소나 아래 예시 주소를
  현재 주소로 간주하지 않는다.
- `models/coco.onnx`와 `models/ppe.onnx`를 준비한다.
- PPE 모델은 3,677,797바이트, SHA-256
  `a294c1b7d887fe9a80888adf5335602c741727c4963578beea183fc2876e8ed4`인지 확인한다. (ppe-v3)
- XIAO가 실제 기체의 15cm 장착 위치에 있고 다른 스트림 클라이언트가 붙어 있지 않은지
  확인한다.

## 실행

```powershell
$xiaoIp = "현재 XIAO IP로 교체"
$runDate = Get-Date -Format yyyyMMdd
python tools/ppe_live_check.py `
  --device mechdog-01 `
  --xiao-ip $xiaoIp `
  --seconds 900 `
  --segments `
  --scenario xiao `
  --web-port 8008 `
  --save-dir "TEST_MECHDOG/results/${runDate}_ppe-xiao/frames" `
  --report "TEST_MECHDOG/results/${runDate}_ppe-xiao/summary.md" `
  --session "TEST_MECHDOG/results/${runDate}_ppe-xiao/session.json"
```

실제 개체명·IP·날짜로 바꿔 실행한다. XIAO 검수에서는 `--window-ms`와 `--hits`를 주지
않는다. `config/config.yaml`의 운영값으로 알람 창을 확인해야 한다.

브라우저에서 구간을 선택한 뒤 네 방향을 차례로 관찰한다. 직립과 웅크림은 전신이 화면에
들어온 상태에서 각각 네 가지 착용 조건을 수행한다. `clipped-base`는 머리가 잘렸을 때
`확인불가`가 나오는지 확인한다. `pitch-up`·`sit`은 별도의 안전한 조작 경로로
해당 단계에 도달한 다음 선택하며, 필요한 단계에서 전신 판정이 회복되면 이후 단계는
불필요하게 실행하지 않는다. 출고 설정에서 제외한 후진은 PPE 검수 중 실행하지 않는다.
11개 구간 × 네 방향 × 방향당 15초 = **최소 660초**이므로, 900초 실행에 전환 여유가 있다.

시작 전 프레임 저장 공간을 확인한다. `--save-dir`은 **모든 프레임**을 저장하므로
900초 검수는 파일이 수만 장이 될 수 있다. 종료할 때 브라우저의 **시험 종료 및 결과
저장**을 누른다. 정상 종료되면 `summary.md`, 재계산 가능한 `session.json`,
판정을 그린 `frames/`가 함께 남는다.

## 결과 읽기

- **판정 가능률**: 확인불가가 아닌 판정 / 전체 판정
- **조건부 정확도**: 확인불가를 제외하고 기대 상태와 일치한 비율
- **실효 성공률**: 기대 상태와 일치한 판정 / 전체 판정

조건부 정확도만으로 통과를 주장하지 않는다. 카메라가 낮을 때 확인불가가 많으면 정확도가
높아도 실제 기능은 판정을 거의 내리지 못한다. 최종 결과에는 모델 해시, 개체 프로파일,
운영 알람 창, 구간별 원자료가 모두 있어야 한다.

웹 구간 버튼과 방향별 시간 구분은 사람이 누르는 시각에 따라 어긋날 수 있으므로
`summary.md`의 구간별 비율은 **예비 수치**다. 최종 합격 판단은 저장 프레임에서
착용 상태·자세·머리 포함 여부를 다시 확인해 라벨과 판정을 대조한 뒤 한다.

## 현재 상태와 다음 작업

현재 상태는 ~~**운용 판정기와 런타임 연결 구현 · 자동 회귀 통과, 현행 ppe-v3 XIAO 실기 미검증**이다.~~
*(2026-09-28 개정)* **운용 판정기와 런타임 연결 구현 · 자동 회귀 통과, PPE 후보 모델 v23b XIAO 실기 기각**이다.
2026-09-22 XIAO 부분 실측은 이전 모델(SHA-256 `ee46da018e8d35c60b41f89dfca33e47786d4e487bb942d78405557a7457db13`)로 수행했다. 11개 구간 중 4개만 관측했고 판정 가능률 14%·실효 성공률 7%였다. 이는 현행 ppe-v3의 합격 근거가 아니며, 당시 모델의 성능도 부족했다(`TEST_MECHDOG/results/20260922_ppe-xiao-run2/summary.md`).

**2026-09-28 XIAO 실측 — v23b 기각.** PPE 후보 모델 v23b(YOLOX-S, 5클래스
helmet/no_helmet/vest/no_vest/person_down, sha256 5b35eb4f…6089)를 mechdog-01 에서
직립·전신·전부 착용 구간(69.4초) 실측했다. 적합 668 · 위반 227(19%) · 확인불가 299였고,
위반 확정이 4회(4방향 각 1회) 나왔다 — 정상 착용자 오경고로, 승격 조건 ④(정상 착용자
오경고 ≤1)에 못 미친다. 오검출 유형은 둘이다 — 정면에서 주황 안전모를 `no_helmet`으로,
측면에서 조끼를 `no_vest`로 봤다. 결과 폴더는 `TEST_MECHDOG/results/20260928_ppe-xiao/`이나
커밋되지 않아 경로만 인용한다. 다음 모델은 `person_down`을 빼고 **4클래스**로 다시 학습한다
(데이터는 Roboflow Universe construction-safety CC BY 4.0 + XIAO 실측 어려운 사례, 아직 학습 전).

**2026-09-29 — 4클래스 후보 둘을 학습했다(XIAO 미실측).** 둘 다 YOLOX-S · COCO 사전학습에서
출발했고 사람 크롭 입력이다. 파일은 깃이 무시하는 `models/candidates/` 에 있다.

- `ppe4-cs-v1` — Roboflow construction-safety 만. 학습 no_helmet 박스가 80개뿐이었다.
- `ppe4-cs-md-v2` — 위 + Mendeley PPE2286(Huang·Cheng, DOI 10.17632/zkzghjvpn2.6, CC BY 4.0).
  사람 라벨이 없어 런타임 COCO 검출기로 사람 박스를 만들었고, Roboflow 와 같은 사진(좌우 반전
  포함)은 dHash 로 뺐다(`tools/ppe/mendeley_prepare.py`). 학습 no_helmet 박스 346개.
  ⚠️ 머리 박스 규약이 두 세트에서 다르다(Mendeley 얼굴 포함 세로형, Roboflow 가로형). 몸통은 같다.

| 같은 잣대 비교 | v2 | v1 | v23b |
| --- | --- | --- | --- |
| Roboflow test AP50 (AP) | 85.8 (41.6) | 87.4 (43.6) | 68.2 (27.9) |
| Mendeley test AP50 (AP) | 96.6 (64.3) | 77.6 (37.1) | 96.0 (68.3) — 주 |
| Roboflow no_helmet 재현율 @0.5 | 85.7% | 71.4% | 64.3% |
| 운용 경로 목업 — 정상 착용자 → 위반 (Roboflow 173명) | **2** | 4 | 7 |
| 운용 경로 목업 — 실효 정확도 (Roboflow / Mendeley) | 78.0% / 78.5% | 78.6% / 81.5% | 72.8% / 78.5% |

주: v23b 는 Mendeley 로 학습했으므로 이 test 와 겹쳤을 수 있다. 목업(`tools/ppe/pipeline_mock.py`)은
정답 사진에 `ppe_live_check.process` 를 그대로 돌린 정지 사진 기준이라 시간 창이 없고, 표본이 작아
두세 명 차이는 잡음 범위다. **XIAO 후보는 v2 로 정했다** — v23b 기각 사유(정상 착용자 오경고)가
가장 적고, no_helmet 재현율이 올랐고, 두 세트에서 고르게 동작한다. 채택은 XIAO 로만 판단한다.

아직 WBS 3.7.3과 PPE 기능 전체 완료는 아니며, 다음 작업을 이어서 수행한다.

1. **다음 모델(4클래스) 학습 후 XIAO 재실측**: v23b는 기각됐다(위 단락). 다음 모델이 나오면
   실제 기체에서 운영 설정 `1500ms / 3회`로 위 절차를 수행하고 `summary.md`와
   `session.json`을 수집한다. XIAO 독립 Test set 은 학습·검증과 분리하고 촬영 세션·장소·
   인물 단위로 나눈다(2026-09-28 세션은 학습·검증에만 쓰고 Test 에는 쓰지 않는다).
2. **운용 판정기 구현 완료(실기 미검증)**: `host/vision/ppe_detector.py`에서 사람 크롭,
   상단 클리핑 보류, 추적 ID별 `1500ms / 3회` 위반 확정을 구현했다.
3. **런타임 통합 완료(실기 미검증)**: 공장 모드 전용 추론, 자세 상승과 복귀,
   동일 ID 중복 방지, `PPE_VIOLATION`·`PPE_SETTLED` 전이와
   `PPE_UNDETERMINED` 블랙박스 사건을 연결했다.
   2026-09-20 자세 실측 판단에 따라 출고 단계는 `pitch_up → sit`으로 제한한다.
   후진은 시야 이득이 작고 앉은 자세를 풀어야 하므로 운용 설정에서 제외했다.
4. **실기 종단 검증 필요**: 공장 모드에서 사람 발견 → 정지 확인 → 클리핑 검사 → 자세 상승 →
   PPE 판정 → L3 또는 순찰 복귀까지 실제 XIAO·MechDog 흐름을 검증한다.
5. **합격 기준 확정**: XIAO 실측 결과를 바탕으로 판정 가능률의 합격 하한과 오알람률의
   합격 상한을 명시한다.

위 다섯 항목과 관련 회귀 시험을 모두 통과한 뒤에만 WBS 3.7.3과 PPE 기능 전체 완료를
선언한다.

## XIAO 수집 세션

*(2026-09-28 추가)* 위 검수와 같은 도구·같은 구간 버튼으로 **PPE 재학습용 XIAO 원본 프레임**을
모은다. 검수가 «합격인가»를 재는 일이라면 수집은 «모델이 틀리는 장면의 원본»을 남기는 일이다.
로봇은 움직이지 않고 카메라 스트림만 쓴다 — 수집 세션에서는 `pitch-up`·`sit` 구간을 누르지 않는다.

### 세션 구분

| 구분 | 쓰임 | 규칙 |
| --- | --- | --- |
| (a) 학습·검증용 수집 세션 | `tools/ppe/xiao_hardcases.py`로 의사 라벨을 만들어 train/val에 섞는다 | 세션 안에서 10초 블록 단위로 train/val을 나눈다. test로는 내보내지 않는다 |
| (b) **독립 XIAO Test 세션** | 재학습한 모델의 최종 보고 | (a)와 **다른 날 또는 다른 장소**, 가능하면 **다른 인물**. `xiao_hardcases.py`에 넣지 않는다 — **학습에 절대 섞지 않는다.** 폴더 이름 끝에 `-test`를 붙여 구분한다 |

모델·임계값은 (a)의 val로 고른다. (b)를 보면서 모델을 고르면 (b)가 검증 세트가 되어 최종 수치가
부풀려진다. 2026-09-28 세션(직립·전신·전부 착용 한 구간)은 (a)로만 쓴다.

### 조건표

| 축 | 값 | 어디에 남기나 |
| --- | --- | --- |
| 조명 | 밝은 실내 / 어두운 실내 / 역광 | 세션 폴더 이름 (`bright`·`dark`·`backlit`) |
| 방향 | 정면 / 측면 / 후면 | 구간 안에서 15초마다 정면→우측→후면→좌측으로 돈다 (`orientation_step_s`) |
| 자세 | 서 있음 / 웅크림 | 구간 버튼 `standing-*` / `crouching-*` |
| 거리 | 0.5 / 1 / 2 m | 세션 폴더 이름 (`0.5m`·`1m`·`2m`, 렌즈에서 발끝까지 바닥에 표시) |
| 착용 | 안전모+조끼 / 안전모만 / 조끼만 / 둘 다 없음 | 구간 버튼 `*-all` / `*-novest` / `*-nohelmet` / `*-none` |
| 가시성 | 전신 / 머리 잘림 / 몸통 가림 | 전신은 위 구간, 머리 잘림은 `clipped-base`, 몸통 가림은 전용 버튼이 없다 |

**전 조합을 다 찍지 않는다.** 2026-09-28 실측에서 실제로 틀린 조건을 먼저 확보한다.

1. **측면 조끼** — `*-all`·`*-nohelmet`의 우측·좌측 15초 (조끼를 `no_vest`로 봤다)
2. **정면 주황 안전모** — `*-all`·`*-novest`의 정면 15초 (주황 안전모를 `no_helmet`으로 봤다)
3. **맨머리** — `*-nohelmet`·`*-none` (공개 데이터를 합쳐 학습 `no_helmet` 박스는 346개가 됐지만 XIAO 시점 맨머리는 0장이다)

조건별 표본 수는 세션마다 남긴다. 방향별 프레임 수는 `summary.md`의 구간별 방향 줄에서,
구간별 채택 수는 세션 카드(`xiao_<세션>_card.json`)의 `segments.used.<구간>.selected`에서
읽어 이 절 끝 «수집 기록»에 옮긴다.

의사 라벨은 착용을 확실히 아는 구간에서만 만든다. 직립·웅크림 × 착용 네 가지(8구간)를 쓰고,
`clipped-base`(기대=확인불가, 머리 잘림)와 `pitch-up`·`sit`(로봇 자세 구간)은 쓰지 않는다 —
뺀 이유는 카드의 `segments.excluded`에 남는다. 몸통 가림은 착용 구간 버튼을 누른 채로 찍지 않는다.
가려진 몸통에 조끼 정답이 붙기 때문이다. 찍으려면 **구간 해제** 상태로 찍거나 (b)에서만 다룬다.

### 1회 세션 권장 순서와 분량

세션 하나는 **조명·거리 한 조합**이다. 그 안에서 착용 × 자세 8구간을 **구간당 60초(15초 × 4방향)**
찍는다. 8분에 옷 갈아입는 시간을 더해 `--seconds 900`이면 된다. 옷을 세 번만 갈아입도록 순서를 잡는다.

| 순서 | 구간 | 착용 | 앞 구간에서 바꿀 것 |
| --- | --- | --- | --- |
| 1 | `standing-all` | 안전모+조끼 | — |
| 2 | `crouching-all` | 안전모+조끼 | 자세만 |
| 3 | `standing-novest` | 안전모만 | 조끼 벗기 |
| 4 | `crouching-novest` | 안전모만 | 자세만 |
| 5 | `standing-none` | 둘 다 없음 | 안전모 벗기 |
| 6 | `crouching-none` | 둘 다 없음 | 자세만 |
| 7 | `standing-nohelmet` | 조끼만 | 조끼 입기 |
| 8 | `crouching-nohelmet` | 조끼만 | 자세만 |

세션 순서는 `bright-1m` → `bright-2m` → `dark-1m` → `backlit-1m` → `bright-0.5m`을 권한다.
0.5m는 15cm 카메라에서 머리가 잘리기 쉬워 확인불가가 많으므로, 이때 `clipped-base`도 60초
찍어 둔다(검수용이며 의사 라벨에는 쓰이지 않는다). (b) Test 세션은 다른 날·장소에서 같은 8구간
순서로 최소 `bright-1m`·`bright-2m`을 찍는다.

### 실행

시작 전에 런타임을 내리고, 카메라 주소를 연 브라우저 탭을 닫는다(스트림은 한 클라이언트만 받는다).
프레임 저장 공간을 확인한다 — `--save-dir`과 `--save-raw-dir`이 **모든 프레임**을 두 벌 저장한다.

```powershell
$xiaoIp = "현재 XIAO IP로 교체"
$dev = "mechdog-01"
$run = "TEST_MECHDOG/results/$(Get-Date -Format yyyyMMdd)_ppe-collect-bright-1m"

# 1) 방향 보정 — ppe_live_check 는 /orient 를 보내지 않는다
python -c "from host.common.config import load_config; from host.vision.stream_client import apply_profile, apply_orientation; c = load_config('$dev'); c['network']['xiao_ip'] = '$xiaoIp'; print(apply_profile(c)); print('rot', apply_orientation(c))"

# 2) 수집
python tools/ppe_live_check.py `
  --device $dev `
  --xiao-ip $xiaoIp `
  --ppe-model models/candidates/ppe4-cs-md-v2/ppe.onnx `
  --seconds 900 `
  --segments `
  --scenario xiao `
  --web-port 8008 `
  --save-dir "$run/frames" `
  --save-raw-dir "$run/raw" `
  --report "$run/summary.md" `
  --session "$run/session.json"
```

- **1)을 빼먹지 않는다.** `/orient`는 카메라 전원을 껐다 켜면 0으로 풀리는데, `ppe_live_check`는
  `apply_profile()`을 부르지 않는다. 2026-09-20에 이것을 빼먹어 거꾸로 선 사람이 찍혀 899프레임을
  날렸다. 1)의 출력이 `'ok': True`이고 마지막 줄이 `rot 180`인지 본다. `rot None`이면 방향 보정이
  실패한 것이다(경고 줄에 이유가 나온다). *(2026-09-29 정정)* 예전 문구는 로그의
  `orientation_applied rot=180`을 보라고 했으나, 이 한 줄 명령은 로깅을 켜지 않아 INFO 줄이 찍히지
  않는다. `apply_orientation`은 같은 `/orient` 요청을 한 번 더 보낼 뿐이라 두 번 불러도 된다.
  mechdog-01의 `vision.mount_rotation`은 180이다. 판정 화면에서 사람이 똑바로 서 있는지도 확인한다.
- 세션 중 카메라 전원이 흔들려 재부팅되면 방향이 다시 풀린다(콘솔에 재접속이 찍히면 의심한다).
  화면이 뒤집혔으면 종료하고 1)부터 새 폴더로 다시 시작한다.
- `--ppe-model` 은 후보를 이 실행에서만 쓴다. 런타임 `models/ppe.onnx` 는 바꾸지 않는다.
- 판정 화면 `http://127.0.0.1:8008/`에서 구간 버튼을 누른다. 폰에서 누르려면 `--web-host 0.0.0.0`을 더한다.

버튼 규칙:

- **옷을 갈아입는 동안에는 «구간 해제»를 눌러 둔다.** 해제 중 프레임은 라벨에 쓰이지 않는다.
- **착용을 마친 뒤 버튼을 먼저 누르고, 자리로 가서 자세를 잡는다.** 버튼을 누른 순간부터 그 구간의
  착용이 정답이 되고 15초 방향 시계도 그때 시작한다.
- **같은 버튼을 두 번 누르지 않는다.** 다시 누르면 방향 시계가 정면으로 돌아가 방향별 집계가 어긋난다.
  잘못 눌렀으면 «구간 해제» 뒤 맞는 버튼을 누른다.
- 끝나면 «시험 종료 및 결과 저장»을 누른다.

⚠️ **`frames/`·`raw/`에는 얼굴이 담긴다.** 커밋하지 않고(`.gitignore`의
`TEST_MECHDOG/results/**/frames/`·`**/raw/`), 클라우드 라벨링 도구를 포함해 외부로 올리지 않는다.
`xiao_hardcases.py`가 만드는 크롭·접촉 시트도 깃이 무시하는 `datasets/` 아래에만 쓴다.

### 촬영 뒤

1. **의사 라벨** — (a) 세션마다 돌린다. (b) Test 세션은 돌리지 않는다.
   `--build` 폴더에 `annotations/instances_train.json`이 있어야 한다 — 합본 빌드
   `ppe4_cs_md_v2`를 먼저 만든다(`tools/ppe/rf100_prepare.py` 다음 `tools/ppe/mendeley_prepare.py`).

   ```powershell
   python tools/ppe/xiao_hardcases.py --device mechdog-01 `
     --session $run `
     --ppe-model models/candidates/ppe4-cs-md-v2/ppe.onnx `
     --build datasets/ppe/build/ppe4_cs_md_v2 --merge
   ```

   `--ppe-model`은 박스 **위치**를 낼 모델이다. 박스 이름은 모델이 아니라 구간의 착용 정답으로
   정한다 — 머리 박스는 안전모 착용 여부, 몸통 박스는 조끼 착용 여부를 따른다.
2. **접촉 시트 사람 검토** — `datasets/ppe/build/ppe4_cs_md_v2/review/xiao_<세션>/sheet_NN.jpg`를
   본다. 칸 아래에 구간 이름이 적혀 있다. 구간과 보이는 착용이 맞는지, 머리·몸통 박스가 제자리인지
   확인하고, 특히 구간 첫머리 칸(버튼 직후)을 본다. 틀린 칸이 있으면 그 세션의
   `annotations/xiao_<세션>_{train,val}.json`을 지우고 합본을 다시 만든다. 칸 하나만 빼는
   옵션은 아직 없다.

   ```powershell
   python -c "from pathlib import Path; from tools.ppe.xiao_hardcases import merge_build; print(merge_build(Path('datasets/ppe/build/ppe4_cs_md_v2')))"
   ```
3. **재학습** — 합본 주석으로 학습한다 (학습 환경 `C:\dev\ppe-train`, 자세한 명령은
   `tools/ppe/yolox_exp_ppe_s.py` 머리말).

   ```powershell
   $env:PPE_DATA_DIR = "<저장소>\datasets\ppe\build\ppe4_cs_md_v2"
   $env:PPE_TRAIN_ANN = "instances_train_mix.json"
   $env:PPE_VAL_ANN = "instances_val_mix.json"
   ```

### 수집 기록

세션마다 한 줄씩 더한다. 채택 수는 카드의 `segments.used.<구간>.selected`(train/val) 합이다.

| 날짜 | 세션 폴더 | 구분 | 조명 | 거리 | 인물·장소 | 구간별 채택 수 | 비고 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| (첫 수집 세션 뒤 채운다) | | | | | | | |
