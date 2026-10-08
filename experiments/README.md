# experiments/

운용 런타임(`host/`)에 들어가지 않는 시제품을 기록으로 남겨 둔 폴더다.

- [wonderecho-audio/](wonderecho-audio/README.md) — WonderEcho 모듈로 만든 음성 시제품(안내 재생·녹음·PC 한국어 인식).
  로봇 I²C 경유 음성 중계(WBS 4.7.9)는 2026-09-23 실측으로 불가 판정됐고, 음성 LLM 경로도 함께
  폐기됐다. 운용 음성은 XIAO 마이크(듣기)와 로봇 MP3 모듈(말하기)로 옮겼다([ADR-38](../docs/DECISIONS.md#adr-38)).

`host/` 는 이 폴더를 import 하지 않는다. CI 는 이 폴더의 단위 시험과 `tf_tracks.py --check` 만 돌린다.
