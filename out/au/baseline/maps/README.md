# 지도 산출물

Phase 2의 LiDAR SLAM 산출물인 ROS2 occupancy grid(`*.pgm` + `*.yaml`)를 둔다
(`docs/DECISIONS.md` ADR-18).

지도는 **공간에 종속**된다. 시연 장소용 지도를 사전 세션(WBS 6.4.2b)에서 생성하고,
어느 공간·언제 만든 것인지 파일명에 기록한다.

용량과 재현성 문제로 Git에 커밋하지 않는다.
