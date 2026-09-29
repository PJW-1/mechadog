# 호스트 단위시험 — CI(firmware-quality)와 로컬이 같은 명령을 쓴다.
#   저장소 루트에서:  make -C firmware_mechdog_motion/test test
# 산출물은 저장소 루트의 build/ 에 둔다(.gitignore 대상).
ROOT  := $(abspath ../..)
SRC   := $(ROOT)/firmware_mechdog_motion/src
TEST  := $(ROOT)/firmware_mechdog_motion/test
BUILD := $(ROOT)/build
CXX   ?= g++
CXX11 := $(CXX) -std=c++11 -Wall -Wextra -Werror -O1 -I $(SRC)
CXX17 := $(CXX) -std=c++17 -Wall -Wextra -Werror -O1 -I $(SRC)

TESTS := command_parser ota_health reboot_deadline sensor_timing_stats \
         task_watchdog motion_safety_state mp3_player safety_monitor \
         telemetry_encoder

.PHONY: test $(TESTS)
test: $(TESTS)

$(BUILD):
	mkdir -p $(BUILD)

# C++ 파서 골든 픽스처 대조 (Python test_protocol_fixtures.py 와 같은 파일)
command_parser: | $(BUILD)
	$(CXX17) $(SRC)/command_parser.cpp $(TEST)/test_command_parser.cpp -o $(BUILD)/test_command_parser
	$(BUILD)/test_command_parser $(ROOT)/tests/fixtures

ota_health: | $(BUILD)
	$(CXX11) $(TEST)/test_ota_health.cpp -o $(BUILD)/test_ota_health
	$(BUILD)/test_ota_health

reboot_deadline: | $(BUILD)
	$(CXX11) $(TEST)/test_reboot_deadline.cpp -o $(BUILD)/test_reboot_deadline
	$(BUILD)/test_reboot_deadline

sensor_timing_stats: | $(BUILD)
	$(CXX11) $(TEST)/test_sensor_timing_stats.cpp -o $(BUILD)/test_sensor_timing_stats
	$(BUILD)/test_sensor_timing_stats

task_watchdog: | $(BUILD)
	$(CXX11) -I $(TEST)/watchdog_stubs $(TEST)/test_task_watchdog.cpp -o $(BUILD)/test_task_watchdog
	$(BUILD)/test_task_watchdog

motion_safety_state: | $(BUILD)
	$(CXX11) $(TEST)/test_motion_safety_state.cpp -o $(BUILD)/test_motion_safety_state
	$(BUILD)/test_motion_safety_state

mp3_player: | $(BUILD)
	$(CXX11) $(TEST)/test_mp3_player.cpp -o $(BUILD)/test_mp3_player
	$(BUILD)/test_mp3_player

safety_monitor: | $(BUILD)
	$(CXX11) $(SRC)/safety_monitor.cpp $(TEST)/test_safety_monitor.cpp -o $(BUILD)/test_safety_monitor
	$(BUILD)/test_safety_monitor

# 인코더 시험 + Python 디코더 교차 검증용 픽스처 출력
telemetry_encoder: | $(BUILD)
	$(CXX11) $(SRC)/command_parser.cpp $(SRC)/telemetry_encoder.cpp $(TEST)/test_telemetry_encoder.cpp -o $(BUILD)/test_telemetry_encoder
	$(BUILD)/test_telemetry_encoder
	$(BUILD)/test_telemetry_encoder --emit-fixtures > $(BUILD)/telemetry-emitted.jsonl
	$(BUILD)/test_telemetry_encoder --emit-optional-fixtures > $(BUILD)/telemetry-optional.jsonl
