# 호스트 단위시험 — CI 와 로컬이 같은 명령을 쓴다.
#   저장소 루트에서:  make -C firmware_lidar_relay/test test
ROOT  := $(abspath ../..)
SRC   := $(ROOT)/firmware_lidar_relay/src
TEST  := $(ROOT)/firmware_lidar_relay/test
BUILD := $(ROOT)/build
CXX   ?= g++

.PHONY: test
test:
	mkdir -p $(BUILD)
	$(CXX) -std=c++17 -Wall -Wextra -Werror -O1 -I $(SRC) $(SRC)/ld19.cpp $(SRC)/scan_encoder.cpp $(TEST)/test_lidar_relay.cpp -o $(BUILD)/test_lidar_relay
	$(BUILD)/test_lidar_relay
	$(BUILD)/test_lidar_relay --emit-fixtures > $(BUILD)/lidar-emitted.jsonl
