# 시연 장소 3D SIM (house_sim)

10-06 시연 장소(집)의 벽·가구를 실측 줄자·LiDAR 지도에 맞춘 3D 장면과 웹 뷰어다. 관제 웹의 «3D 보기»
(`dashboard.scene3d_url: http://127.0.0.1:8789/`)가 이 서버를 띄워 쓴다.

```bash
python tools/house_sim/fetch_data.py   # Release house-sim-20261006 장면 데이터(약 14MB)를 받아 푼다
cd tools/house_sim && node server.mjs  # http://127.0.0.1:8789/ — Node.js 만 필요, 추가 패키지 없음
```

- 장면 데이터(`data/`·`audit/`·`exports/`)는 용량(장면 62MB)이 커서 저장소가 아니라 Release 첨부다. 해시를 확인하고 푼다.
- 실시간 로봇 위치는 런타임 `lidar.pose_out_port`(5398)로 받는다(`src/` 의 포즈 수신).
- 좌표는 `maps/home_20261006_remap2` 의 plan 좌표와 같다 (plan = (4.825 − x, 3.125 − y), x·y 는 순찰 좌표).
- 개인 PC 경로는 `<local>` 로 바꿔 두었다.
