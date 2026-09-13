#pragma once

// 이 파일을 wifi_secrets.h 로 복사한 뒤 실제 값을 넣는다.
// wifi_secrets.h 는 .gitignore 대상이므로 비밀번호가 저장소에 올라가지 않는다.
#define MECHDOG_WIFI_SSID "YOUR_WIFI_SSID"
#define MECHDOG_WIFI_PASSWORD "YOUR_WIFI_PASSWORD"

// 스캔 데이터그램의 UDP 목적지 — Host PC 의 주소다.
// 중계 노드는 단방향 송신이라 명령에서 host 를 학습할 수 없어
// 여기서 정해야 한다. config.yaml 의 lidar.scan_port 와 맞출 것.
#define LIDAR_HOST_IP "192.168.0.29"
#define LIDAR_HOST_PORT 5201
