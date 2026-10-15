# 지도 산출물

LiDAR 측위·항법 지도(점유 격자 `slam_map*.npy`·`.pgm`/`.yaml`, 구역 `zones*.json`·`zone_labels.npy`,
출발점 `S0.json`, 저장 동선 `routes.json`)를 둔다 (`docs/DECISIONS.md` ADR-18).

지도는 **공간에 종속**된다. 어느 공간·언제 만든 것인지 폴더 이름에 기록한다.

| 폴더 | 공간 | 비고 |
|---|---|---|
| `home_20261006_remap2/` | 10-06 시연 장소(집) | 작업실·가벽 보정(작업 AL). `mechdog-02` 기체 설정 `lidar.maps_dir` 가 쓴다. 근거 파일(`*_evidence*.json`·`AL_REPORT.json`)도 함께 둔다 |

2026-10-06 까지는 용량·사생활 때문에 커밋하지 않았으나, 다른 기체·PC 에서 같은 시연을 재현하도록
팀장 결정으로 저장소에 둔다(공개 저장소). 개인 PC 경로는 `<local>` 로 바꿨다.
새 지도는 이 규칙대로 폴더를 하나 더 만들고 `.gitignore` 예외에 추가한다.
