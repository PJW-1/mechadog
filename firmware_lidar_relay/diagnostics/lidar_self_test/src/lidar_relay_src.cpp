// 진단 스케치에서 상위 펌웨어 소스를 컴파일하기 위한 래퍼.
// 소스 파일을 복사하지 않는다 — 복사본이 원본과 어긋나는 것을 막기 위해
// 이 번역 단위 하나가 원본 .cpp 를 그대로 include 한다.
// 컴파일: --build-property compiler.cpp.extra_flags=-I<...>/firmware_lidar_relay/src
#include "ld19.cpp"
#include "scan_encoder.cpp"
