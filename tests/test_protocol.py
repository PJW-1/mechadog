"""통신 규약 구현 검증 (WBS 3.1.1 · 6.2.1).

`test_protocol_fixtures.py` 는 **픽스처 자체의 일관성**을 본다. 이 파일은
그 픽스처로 **구현(host/common/protocol.py)** 을 검증한다. 둘은 역할이 다르다.

C++ 파서도 같은 픽스처를 통과해야 하므로(M1), 여기서 확인하는 동작이 곧
펌웨어의 기대 동작이다. 기대값을 픽스처의 `_expect` 에서 읽어오기 때문에
케이스를 추가할 때 테스트 코드를 고칠 필요가 없다.
"""

import re

import pytest
from conftest import FIXTURES, ROOT, FakeClock, load_jsonl

from host.common import protocol as p

SAMPLES = load_jsonl(FIXTURES / "protocol_samples.jsonl")
INVALID = load_jsonl(FIXTURES / "protocol_invalid.jsonl")
TELEMETRY_SAMPLES = load_jsonl(FIXTURES / "telemetry_samples.jsonl")
TELEMETRY_INVALID = load_jsonl(FIXTURES / "telemetry_invalid.jsonl")


def _case(msg: dict) -> str:
    return msg.get("_case", "(이름 없음)")


# ══════════════════════════════════════════════════════════════
#  제어 명령 — 골든 픽스처 대조
# ══════════════════════════════════════════════════════════════


def test_decoder_accepts_every_sample() -> None:
    """정본 픽스처 전 라인이 무손실로 통과해야 한다.

    한 디코더 인스턴스로 순서대로 먹인다. seq 게이트를 함께 지나므로
    "유효 스트림 전체"에 대한 검증이 된다.
    """
    decoder = p.CommandDecoder()
    for msg in SAMPLES:
        expected = p.strip_meta(msg)
        result = decoder.decode(p.serialize(expected))
        assert result.verdict is p.Verdict.ACCEPT, f"{_case(msg)}: {result.reason}"
        assert result.message == expected, f"{_case(msg)}: 필드가 변형됨"
        assert not result.clamped
        assert result.refreshes_link


def test_encoder_reproduces_every_sample() -> None:
    """인코더 출력이 정본 픽스처와 바이트 단위로 일치해야 한다.

    송신측이 픽스처와 다른 것을 내보내면 C++ 파서만 픽스처를 통과하고
    실물은 실패한다 — 가장 잡기 어려운 형태의 불일치다.
    """
    for msg in SAMPLES:
        expected = p.strip_meta(msg)
        encoder = p.CommandEncoder(clock=lambda ts=expected["ts"]: ts, start_seq=expected["seq"])
        fields = {k: v for k, v in expected.items() if k not in p.COMMON_REQUIRED}
        assert encoder.build(expected["type"], **fields) == expected, _case(msg)


def test_invalid_fixture_matches_expected_verdict() -> None:
    """폐기·클램핑 픽스처의 `_expect` 대로 판정되어야 한다 (규칙 ①~④)."""
    decoder = p.CommandDecoder()
    for msg in INVALID:
        result = decoder.decode(p.serialize(p.strip_meta(msg)))
        assert result.verdict.value == msg["_expect"], f"{_case(msg)}: {result.reason}"


def test_serialized_output_is_single_line() -> None:
    """JSONL·UDP 양쪽에서 한 줄이어야 한다."""
    encoder = p.CommandEncoder(clock=FakeClock())
    assert "\n" not in encoder.move(60, 0)


# ── 규칙 ① seq ────────────────────────────────────────────────


def test_seq_reversal_and_duplicate_are_discarded() -> None:
    decoder = p.CommandDecoder()
    assert decoder.decode('{"seq":10,"ts":1,"type":"STOP"}').accepted
    assert not decoder.decode('{"seq":9,"ts":2,"type":"STOP"}').accepted  # 역전
    assert not decoder.decode('{"seq":10,"ts":3,"type":"STOP"}').accepted  # 중복
    assert decoder.decode('{"seq":11,"ts":4,"type":"STOP"}').accepted
    assert decoder.last_seq == 11


def test_seq_advances_even_when_content_is_rejected() -> None:
    """내용 때문에 폐기된 패킷의 seq 도 "지나간 순번"이다.

    그렇지 않으면 폐기된 seq 를 재사용하는 옛 패킷이 뒤늦게 받아들여진다.
    """
    decoder = p.CommandDecoder()
    assert decoder.decode('{"seq":5,"ts":1,"type":"FUTURE_CMD"}').warns
    assert decoder.last_seq == 5
    assert not decoder.decode('{"seq":5,"ts":2,"type":"STOP"}').accepted


# ── 규칙 ② 클램핑 ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("field", "sent", "expected"),
    [
        ("step", 500, 100),
        ("step", -500, -100),
        ("angle", -90, -30),
        ("angle", 90, 30),
    ],
)
def test_out_of_range_is_clamped_not_discarded(field: str, sent: int, expected: int) -> None:
    """범위 초과는 폐기가 아니라 클램핑이다 — 명령이 조용히 사라지면 안 된다."""
    base = {"seq": 1, "ts": 1, "type": "MOVE", "step": 0, "angle": 0}
    result = p.CommandDecoder().decode(p.serialize({**base, field: sent}))
    assert result.verdict is p.Verdict.CLAMP
    assert result.message[field] == expected
    assert result.clamped == (field,)
    assert result.refreshes_link, "클램핑된 명령도 링크는 살아 있다"


def test_action_id_is_clamped() -> None:
    result = p.CommandDecoder().decode('{"seq":1,"ts":1,"type":"ACTION","id":99}')
    assert result.message["id"] == 15


def test_encoder_clamps_before_sending() -> None:
    """수신측이 잘라 주더라도 송신측이 먼저 자른다. 로그가 실제 전송값과 같아야 한다."""
    encoder = p.CommandEncoder(clock=FakeClock())
    assert encoder.build("MOVE", step=500, angle=-90) == {
        "seq": 1,
        "ts": 1_756_800_000_000,
        "type": "MOVE",
        "step": 100,
        "angle": -30,
    }


# ── 규칙 ③ 파싱 실패 ──────────────────────────────────────────


@pytest.mark.parametrize("raw", ["", "{", "not json", "[1,2,3]", '"문자열"', b"\xff\xfe"])
def test_broken_packet_is_discarded_without_refreshing_link(raw: str | bytes) -> None:
    """깨진 패킷을 "살아 있음"으로 세면 페일세이프가 걸리지 않는다."""
    result = p.CommandDecoder().decode(raw)
    assert result.verdict is p.Verdict.DISCARD
    assert not result.refreshes_link


# ── 규칙 ④ 미지 타입 ──────────────────────────────────────────


def test_unknown_type_is_discarded_with_warning() -> None:
    """이 규칙이 타입 추가를 하위 호환으로 만든다 (PROTOCOL.md 4절)."""
    result = p.CommandDecoder().decode('{"seq":1,"ts":1,"type":"FUTURE_CMD","foo":1}')
    assert result.verdict is p.Verdict.DISCARD_WARN
    assert result.warns
    assert not result.refreshes_link


@pytest.mark.parametrize("missing", ["seq", "ts", "type"])
def test_missing_common_field_is_discarded(missing: str) -> None:
    msg = {"seq": 1, "ts": 1, "type": "STOP"}
    del msg[missing]
    assert p.CommandDecoder().decode(p.serialize(msg)).verdict is p.Verdict.DISCARD


def test_missing_type_specific_field_is_discarded() -> None:
    result = p.CommandDecoder().decode('{"seq":1,"ts":1,"type":"MOVE","step":60}')
    assert result.verdict is p.Verdict.DISCARD
    assert "angle" in result.reason


@pytest.mark.parametrize("value", ["60", True, None, {"v": 1}])
def test_non_numeric_field_is_discarded(value: object) -> None:
    """`True` 가 `1` 로 통과하면 안 된다 — bool 은 수치가 아니다."""
    msg = {"seq": 1, "ts": 1, "type": "MOVE", "step": value, "angle": 0}
    assert p.CommandDecoder().decode(p.serialize(msg)).verdict is p.Verdict.DISCARD


def test_led_color_must_be_string() -> None:
    msg = {"seq": 1, "ts": 1, "type": "LED", "color": 7, "blink_hz": 2}
    assert p.CommandDecoder().decode(p.serialize(msg)).verdict is p.Verdict.DISCARD


# ── 인코더는 송신측 버그에 관대하지 않다 ──────────────────────


def test_encoder_rejects_unknown_type() -> None:
    with pytest.raises(ValueError, match="알 수 없는 명령 타입"):
        p.CommandEncoder(clock=FakeClock()).build("DANCE")


def test_encoder_rejects_missing_field() -> None:
    with pytest.raises(ValueError, match="필수 필드 누락"):
        p.CommandEncoder(clock=FakeClock()).build("MOVE", step=60)


def test_encoder_seq_is_monotonic_and_ts_comes_from_clock() -> None:
    clock = FakeClock()
    encoder = p.CommandEncoder(clock=clock)
    first = encoder.build("STOP")
    clock.advance(100)
    second = encoder.build("STOP")
    assert (first["seq"], second["seq"]) == (1, 2)
    assert second["ts"] - first["ts"] == 100
    assert encoder.next_seq == 3


def test_encoder_convenience_methods_cover_every_type() -> None:
    """편의 메서드가 7종 전부를 덮어야 한다. 빠지면 호출부가 문자열을 쓰게 된다."""
    encoder = p.CommandEncoder(clock=FakeClock())
    decoder = p.CommandDecoder()
    emitted = [
        encoder.move(60, 12),
        encoder.pose(15, 0, 0, 300),
        encoder.gait(120, 180, 25),
        encoder.stop(),
        encoder.action(7),
        encoder.led("red", 2),
        encoder.sound(181),
        encoder.state("ALERT"),
    ]
    types = set()
    for raw in emitted:
        result = decoder.decode(raw)
        assert result.accepted, result.reason
        types.add(result.message["type"])
    assert types == set(p.COMMAND_TYPES)


def test_known_types_match_golden_fixture() -> None:
    """구현의 타입 목록과 정본 픽스처가 어긋나면 안 된다."""
    assert set(p.COMMAND_TYPES) == {m["type"] for m in SAMPLES}


# ══════════════════════════════════════════════════════════════
#  텔레메트리 — 방향만 반대이고 규칙은 대칭이다
# ══════════════════════════════════════════════════════════════


def test_telemetry_decoder_accepts_every_sample() -> None:
    """3대분이 섞여 들어와도 전부 통과해야 한다 (개체별 seq 추적)."""
    decoder = p.TelemetryDecoder()
    for msg in TELEMETRY_SAMPLES:
        expected = p.strip_meta(msg)
        result = decoder.decode(p.serialize(expected))
        assert result.verdict is p.Verdict.ACCEPT, f"{_case(msg)}: {result.reason}"
        assert result.message == expected


def test_telemetry_invalid_fixture_matches_expected_verdict() -> None:
    decoder = p.TelemetryDecoder()
    for msg in TELEMETRY_INVALID:
        result = decoder.decode(p.serialize(p.strip_meta(msg)))
        assert result.verdict.value == msg["_expect"], f"{_case(msg)}: {result.reason}"


def test_telemetry_seq_is_tracked_per_device() -> None:
    """한 카운터로 묶으면 개체끼리 서로의 패킷을 폐기한다 (WBS 8절)."""
    decoder = p.TelemetryDecoder()
    encoders = {
        "mechdog-a": p.TelemetryEncoder("mechdog-a", clock=FakeClock(), start_seq=100),
        "mechdog-b": p.TelemetryEncoder("mechdog-b", clock=FakeClock(), start_seq=1),
    }
    for device, encoder in encoders.items():
        result = decoder.decode(encoder.encode(**_telemetry_kwargs()))
        assert result.accepted, f"{device}: {result.reason}"
    assert decoder.last_seq("mechdog-a") == 100
    assert decoder.last_seq("mechdog-b") == 1


def test_telemetry_unknown_state_is_discarded_with_warning() -> None:
    """상태 추가를 하위 호환으로 만든다 — 명령 타입의 규칙 ④와 같은 논리다."""
    record = {**_valid_telemetry(), "state": "DANCING"}
    result = p.TelemetryDecoder().decode(p.serialize(record))
    assert result.verdict is p.Verdict.DISCARD_WARN


@pytest.mark.parametrize("missing", p.TELEMETRY_REQUIRED)
def test_telemetry_missing_required_field_is_discarded(missing: str) -> None:
    record = _valid_telemetry()
    del record[missing]
    assert p.TelemetryDecoder().decode(p.serialize(record)).verdict is p.Verdict.DISCARD


@pytest.mark.parametrize("nested", p.IMU_FIELDS)
def test_telemetry_missing_imu_field_is_discarded(nested: str) -> None:
    record = _valid_telemetry()
    del record["imu"][nested]
    assert p.TelemetryDecoder().decode(p.serialize(record)).verdict is p.Verdict.DISCARD


@pytest.mark.parametrize("nested", p.FLAG_FIELDS)
def test_telemetry_missing_flag_is_discarded(nested: str) -> None:
    record = _valid_telemetry()
    del record["flags"][nested]
    assert p.TelemetryDecoder().decode(p.serialize(record)).verdict is p.Verdict.DISCARD


@pytest.mark.parametrize(
    ("batt_v", "accepted"),
    [(6.0, True), (8.4, True), (5.9, False), (8.5, False), (14.8, False)],
)
def test_telemetry_battery_physical_range(batt_v: float, accepted: bool) -> None:
    """물리 범위 밖의 값은 측정 오류다. 저전압 **판정** 임계와는 다른 것이다."""
    record = {**_valid_telemetry(), "batt_v": batt_v}
    assert p.TelemetryDecoder().decode(p.serialize(record)).accepted is accepted


def test_telemetry_negative_distance_is_discarded() -> None:
    record = {**_valid_telemetry(), "dist_cm": -5}
    assert not p.TelemetryDecoder().decode(p.serialize(record)).accepted


def test_telemetry_tipped_must_agree_with_state() -> None:
    """`tipped` 는 온보드 안전 로직이 발동했다는 뜻이다. 순찰 중일 수 없다."""
    tipped_patrol = {**_valid_telemetry(), "state": "PATROL"}
    tipped_patrol["flags"]["tipped"] = True
    assert p.TelemetryDecoder().decode(p.serialize(tipped_patrol)).verdict is p.Verdict.DISCARD_WARN

    tipped_failsafe = {**_valid_telemetry(), "state": "FAILSAFE"}
    tipped_failsafe["flags"]["tipped"] = True
    assert p.TelemetryDecoder().decode(p.serialize(tipped_failsafe)).accepted


def test_telemetry_lowbatt_during_patrol_is_not_a_contradiction() -> None:
    """저전압은 경고 수준이라 순찰과 공존한다 — 전도와 달리 모순이 아니다."""
    record = _valid_telemetry()
    record["flags"]["lowbatt"] = True
    assert p.TelemetryDecoder().decode(p.serialize(record)).accepted


def test_telemetry_encoder_round_trip() -> None:
    """가상 MechDog(WBS 6.1.1)이 내보낼 레코드가 그대로 수신 검증을 통과해야 한다."""
    encoder = p.TelemetryEncoder("mechdog-ref", clock=FakeClock())
    result = p.TelemetryDecoder().decode(encoder.encode(**_telemetry_kwargs()))
    assert result.accepted, result.reason
    assert result.message["device_id"] == "mechdog-ref"


def test_telemetry_encoder_requires_device_id() -> None:
    """송신자를 IP 로 구분하면 안 된다 — DHCP 로 바뀌고 컨테이너면 게이트웨이로 보인다."""
    with pytest.raises(ValueError, match="device_id"):
        p.TelemetryEncoder("", clock=FakeClock())


def test_telemetry_encoder_rejects_unknown_state() -> None:
    with pytest.raises(ValueError, match="알 수 없는 상태"):
        p.TelemetryEncoder("mechdog-ref", clock=FakeClock()).build(
            **{**_telemetry_kwargs(), "state": "DANCING"}
        )


def test_known_states_match_golden_fixture() -> None:
    """구현의 상태 목록과 정본 픽스처가 어긋나면 안 된다."""
    assert set(p.FSM_STATES) == {m["state"] for m in TELEMETRY_SAMPLES}


# ── 픽스처 생성기 ────────────────────────────────────────────


def _telemetry_kwargs() -> dict:
    return {
        "state": "PATROL",
        "dist_cm": 180,
        "imu": {"pitch": 1.2, "roll": -0.4, "yaw": 183.5},
        "batt_v": 8.10,
        "last_cmd_age_ms": 34,
        "flags": {"lowbatt": False, "tipped": False, "link_ok": True},
    }


def _valid_telemetry() -> dict:
    """매번 새 dict 를 만든다. 테스트가 서로의 중첩 dict 를 오염시키면 안 된다."""
    return {
        "seq": 1,
        "ts": 1_756_800_000_000,
        "device_id": "mechdog-a",
        **_telemetry_kwargs(),
    }


# ══════════════════════════════════════════════════════════════
#  망가진 입력 — UDP 수신부는 무엇이든 받을 수 있다
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("mutation", "reason_hint"),
    [
        ({"seq": "1"}, "seq"),
        ({"device_id": ""}, "device_id"),
        ({"device_id": 7}, "device_id"),
        ({"imu": [1, 2, 3]}, "객체"),
        ({"flags": "ok"}, "객체"),
        ({"imu": {"pitch": "1", "roll": 0, "yaw": 0}}, "imu.pitch"),
        ({"flags": {"lowbatt": 0, "tipped": False, "link_ok": True}}, "flags.lowbatt"),
    ],
)
def test_telemetry_malformed_field_is_discarded(mutation: dict, reason_hint: str) -> None:
    result = p.TelemetryDecoder().decode(p.serialize({**_valid_telemetry(), **mutation}))
    assert result.verdict is p.Verdict.DISCARD
    assert reason_hint in result.reason


def test_telemetry_broken_packet_is_discarded() -> None:
    assert p.TelemetryDecoder().decode("{").verdict is p.Verdict.DISCARD


def test_command_non_numeric_seq_is_discarded() -> None:
    result = p.CommandDecoder().decode('{"seq":"1","ts":1,"type":"STOP"}')
    assert result.verdict is p.Verdict.DISCARD
    assert "seq" in result.reason


@pytest.mark.parametrize("dropped", ["imu", "flags"])
def test_telemetry_encoder_rejects_incomplete_nested_field(dropped: str) -> None:
    """송신측 버그는 예외로 낸다 — 수신측에서 폐기되는 것을 나중에 발견하면 늦다."""
    kwargs = _telemetry_kwargs()
    kwargs[dropped] = {}
    with pytest.raises(ValueError, match=f"{dropped} 필드 누락"):
        p.TelemetryEncoder("mechdog-ref", clock=FakeClock()).build(**kwargs)


def test_telemetry_encoder_seq_is_monotonic() -> None:
    encoder = p.TelemetryEncoder("mechdog-ref", clock=FakeClock())
    assert encoder.next_seq == 1
    encoder.build(**_telemetry_kwargs())
    assert encoder.next_seq == 2


def test_system_clock_is_epoch_milliseconds() -> None:
    """초로 잘못 쓰면 `last_cmd_age_ms` 계산이 1000배 어긋난다."""
    now = p.system_clock_ms()
    assert isinstance(now, int)
    assert now > 1_700_000_000_000  # 2023-11 이후. 초 단위였다면 여기서 걸린다


# ══════════════════════════════════════════════════════════════
#  STATE — 호스트가 자기 FSM 상태를 로봇에게 알려준다
# ══════════════════════════════════════════════════════════════


def test_state_command_round_trip() -> None:
    encoder = p.CommandEncoder(clock=FakeClock())
    result = p.CommandDecoder().decode(encoder.state("ALERT"))
    assert result.verdict is p.Verdict.ACCEPT
    assert result.message["state"] == "ALERT"


@pytest.mark.parametrize("state", sorted(p.FSM_STATES))
def test_state_command_accepts_every_fsm_state(state: str) -> None:
    """8종 전부 전달 가능해야 한다. 하나라도 막히면 그 상태는 로봇에 도달하지 못한다."""
    encoder = p.CommandEncoder(clock=FakeClock())
    assert p.CommandDecoder().decode(encoder.state(state)).accepted


def test_unknown_state_value_is_discarded_with_warning() -> None:
    """텔레메트리 규칙 ③과 대칭 — 같은 이유로 상태 추가가 하위 호환이 된다."""
    result = p.CommandDecoder().decode('{"seq":1,"ts":1,"type":"STATE","state":"DANCING"}')
    assert result.verdict is p.Verdict.DISCARD_WARN
    assert not result.refreshes_link


def test_encoder_rejects_unknown_state() -> None:
    """보내는 쪽은 엄격하다 — 송신측 버그를 로봇이 폐기한 뒤에 알면 늦다."""
    with pytest.raises(ValueError, match="알 수 없는 상태"):
        p.CommandEncoder(clock=FakeClock()).state("DANCING")


def test_state_is_backward_compatible_with_older_firmware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**STATE 를 모르는 펌웨어가 무시해도 아무것도 깨지지 않아야 한다.**

    additive 변경의 근거가 이것이다 (PROTOCOL.md 4절). 규칙 ④가 미지 타입을
    폐기 + WARN 으로 처리하므로, 호스트가 먼저 보내기 시작해도 기존 동작에
    영향이 없고 펌웨어는 나중에 핸들러를 추가하면 된다.

    `COMMAND_TYPES` 에서 STATE 를 빼서 **STATE 도입 이전의 수신자를 재현한다.**
    """
    monkeypatch.setattr(p, "COMMAND_TYPES", p.COMMAND_TYPES - {"STATE"})
    decoder = p.CommandDecoder()

    assert decoder.decode('{"seq":1,"ts":1,"type":"MOVE","step":60,"angle":0}').accepted

    old = decoder.decode('{"seq":2,"ts":2,"type":"STATE","state":"ALERT"}')
    assert old.verdict is p.Verdict.DISCARD_WARN, "죽지 않고 경고만 남겨야 한다"
    assert not old.refreshes_link

    # 그리고 그 다음 명령이 정상 처리된다 — 수신자가 망가지지 않았다
    assert decoder.decode('{"seq":3,"ts":3,"type":"MOVE","step":60,"angle":0}').accepted


def test_onboard_states_are_a_subset_of_fsm_states() -> None:
    """온보드가 아는 상태는 FSM 상태의 부분집합이어야 한다.

    로봇만 아는 상태가 생기면 호스트 FSM 전이표에 없는 값이 텔레메트리로 올라온다.
    """
    assert p.ONBOARD_STATES < p.FSM_STATES


def test_fsm_states_match_the_transition_table() -> None:
    """**FSM 상태 목록의 정본은 아키텍처 문서의 전이표다.** 코드가 그것과 일치해야 한다.

    FR-4.2 가 대시보드에 "현재 FSM 상태"를 스트리밍하도록 요구하므로, 전이표에
    있고 `FSM_STATES` 에 없는 상태는 **화면에 표시할 수 없는 상태**가 된다.
    반대로 코드에만 있는 상태는 전이표에 근거가 없다.

    실제로 이런 일이 있었다 — 전이표는 13종인데 규약은 8종만 인정하여
    `IDLE`·`MANUAL`·`AUTH_WAIT` 를 호스트가 로봇에게 알려줄 수 없었다.
    `STATE` 명령이 없던 동안에는 아무도 그 필드를 채울 수 없어 드러나지 않았다.
    """
    doc = ROOT / "docs" / "ARCHITECTURE.md"
    body = doc.read_text(encoding="utf-8")
    table = body[body.index("## 3. 행동 상태 전이표") : body.index("### 3.1 대응 에스컬레이션")]

    # 전이표는 상태와 트리거를 같은 표기로 쓴다. 트리거만 걸러낸다.
    triggers = {"CMD_START_PATROL"}
    documented = set(re.findall(r"`([A-Z][A-Z_]{2,})`", table)) - triggers

    assert documented == set(p.FSM_STATES), (
        f"전이표에만 있음: {sorted(documented - set(p.FSM_STATES))} / "
        f"코드에만 있음: {sorted(set(p.FSM_STATES) - documented)}"
    )


def test_onboard_states_need_no_host_notification() -> None:
    """온보드 3종은 로봇이 센서만으로 판정한다 — `STATE` 없이도 보고할 수 있다.

    이 구분이 흐려지면 "링크가 끊겼는데 호스트가 알려주지 않아 상태를 모른다"는
    모순이 생긴다. `FAILSAFE` 는 링크 두절 시에도 보고되어야 한다.
    """
    assert set(p.ONBOARD_STATES) == {"PATROL", "AVOID", "FAILSAFE"}
    assert p.ONBOARD_STATES < p.FSM_STATES


# ══════════════════════════════════════════════════════════════
#  악성·기형 입력 — UDP 수신부는 죽어서는 안 된다
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize("bad", [[], {}, 7, None, True])
def test_non_string_type_is_discarded_without_crashing(bad: object) -> None:
    """`{"type": []}` 가 수신 루프를 죽이던 버그의 회귀 시험.

    `[] in frozenset(...)` 은 `TypeError: unhashable type` 을 던진다. 폐기보다
    나쁘다 — 관제가 멈춘다. 목록과 대조하기 **전에** 문자열인지 확인해야 한다.
    """
    msg = p.serialize({"seq": 1, "ts": 1, "type": bad})
    result = p.CommandDecoder().decode(msg)
    assert result.verdict is p.Verdict.DISCARD
    assert not result.refreshes_link


@pytest.mark.parametrize("bad", [[], {}, 7, None, True])
def test_non_string_telemetry_state_is_discarded_without_crashing(bad: object) -> None:
    record = {**_valid_telemetry(), "state": bad}
    result = p.TelemetryDecoder().decode(p.serialize(record))
    assert result.verdict is p.Verdict.DISCARD


@pytest.mark.parametrize("bad", [[], {}, 7, None])
def test_non_string_state_command_value_is_discarded(bad: object) -> None:
    msg = p.serialize({"seq": 1, "ts": 1, "type": "STATE", "state": bad})
    assert not p.CommandDecoder().decode(msg).accepted


# ── seq·ts 는 정수만 ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("field", "bad"), [("seq", 1.5), ("seq", "1"), ("ts", "어제"), ("ts", 1.5)]
)
def test_non_integer_seq_or_ts_is_discarded(field: str, bad: object) -> None:
    msg = {"seq": 1, "ts": 1, "type": "STOP", field: bad}
    result = p.CommandDecoder().decode(p.serialize(msg))
    assert result.verdict is p.Verdict.DISCARD
    assert field in result.reason


def test_fractional_seq_no_longer_swallows_the_next_command() -> None:
    """`seq: 1.5` 를 받아들이면 게이트는 `1` 로 기억하고 메시지엔 `1.5` 가 남는다.

    그 상태에서 정상적인 `seq: 1` 이 "중복"으로 폐기된다 — **실수 하나가 정상
    명령 하나를 삼킨다.** 정수만 받으면 이 경로가 생기지 않는다.
    """
    decoder = p.CommandDecoder()
    assert not decoder.decode('{"seq":1.5,"ts":1,"type":"STOP"}').accepted
    assert decoder.last_seq is None, "폐기된 패킷이 게이트를 오염시키면 안 된다"
    assert decoder.decode('{"seq":1,"ts":1,"type":"STOP"}').accepted


@pytest.mark.parametrize(("field", "bad"), [("seq", 1.5), ("ts", "어제")])
def test_non_integer_telemetry_seq_or_ts_is_discarded(field: str, bad: object) -> None:
    record = {**_valid_telemetry(), field: bad}
    assert not p.TelemetryDecoder().decode(p.serialize(record)).accepted


# ── 인코더가 관리하는 필드는 넘길 수 없다 ────────────────────


@pytest.mark.parametrize("reserved", ["seq", "ts", "type"])
def test_command_encoder_rejects_reserved_fields(reserved: str) -> None:
    """예약 필드를 덮어쓰면 **단조 증가 seq 보장이 깨진다.**

    그것이 인코더가 지키는 유일한 계약이므로 우회를 허용하지 않는다.
    """
    encoder = p.CommandEncoder(clock=FakeClock())
    with pytest.raises(ValueError, match="인코더가 관리하는 필드"):
        encoder.build("MOVE", step=60, angle=0, **{reserved: 999})


@pytest.mark.parametrize("reserved", sorted(p.TELEMETRY_MANAGED))
def test_telemetry_encoder_rejects_reserved_fields(reserved: str) -> None:
    """특히 `device_id` — 덮을 수 있으면 **송신자를 위조할 수 있다** (DR-17).

    본문의 나머지 필드는 `build()` 의 명명 인자여서 `extra` 에 닿지 않는다.
    실제로 위조 가능한 것은 `seq`·`ts`·`device_id` 셋뿐이다.
    """
    encoder = p.TelemetryEncoder("mechdog-ref", clock=FakeClock())
    with pytest.raises(ValueError, match="인코더가 관리하는 필드"):
        encoder.build(**{**_telemetry_kwargs(), reserved: "위조"})


def test_telemetry_encoder_still_accepts_additive_extras() -> None:
    """예약 필드가 아닌 추가 필드는 자유롭게 실을 수 있어야 한다 (PROTOCOL.md 4절)."""
    encoder = p.TelemetryEncoder("mechdog-ref", clock=FakeClock())
    msg = encoder.build(**_telemetry_kwargs(), events={"timeout_stops": 3})
    assert msg["events"] == {"timeout_stops": 3}
    assert p.TelemetryDecoder().decode(p.serialize(msg)).accepted
