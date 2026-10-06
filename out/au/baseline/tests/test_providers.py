"""EP 선택 검증 (WBS 3.3.1 ④ · DR-13).

**GPU 없이 GPU 경로를 시험한다.** 사용 가능 목록을 인자로 받으므로 DirectML 이 없는
CI 러너에서도 "DirectML 이 있으면 그것을 고른다"를 검증할 수 있다.
"""

from __future__ import annotations

import pytest

from host.vision.providers import (
    CPU_PROVIDER,
    ProviderError,
    log_selection,
    select_providers,
)

DML = "DmlExecutionProvider"
CUDA = "CUDAExecutionProvider"
PREFERRED = (DML, CPU_PROVIDER)


def test_prefers_directml_when_available() -> None:
    assert select_providers(PREFERRED, [DML, CPU_PROVIDER]) == [DML, CPU_PROVIDER]


def test_falls_back_to_cpu_on_ci() -> None:
    """CI 러너에는 DirectML 이 없다. 그래도 기동해야 한다."""
    assert select_providers(PREFERRED, ["AzureExecutionProvider", CPU_PROVIDER]) == [CPU_PROVIDER]


def test_unavailable_preference_is_dropped_not_passed_through() -> None:
    """⚠️ **없는 EP 를 onnxruntime 에 넘기지 않는다.**

    넘기면 판에 따라 경고만 내고 무시해서, **DirectML 로 도는 줄 알고 CPU 로 도는
    상태**가 조용히 만들어진다. 그건 성능 이상의 원인을 못 찾게 만든다.
    """
    chosen = select_providers((CUDA, DML, CPU_PROVIDER), [CPU_PROVIDER])
    assert CUDA not in chosen and DML not in chosen


def test_cpu_is_appended_even_if_config_forgot_it() -> None:
    """설정 실수가 기동 실패로 번지지 않게 한다."""
    assert select_providers((DML,), [DML, CPU_PROVIDER]) == [DML, CPU_PROVIDER]


def test_order_follows_preference_not_availability() -> None:
    chosen = select_providers((DML, CPU_PROVIDER), [CPU_PROVIDER, DML])
    assert chosen[0] == DML


def test_empty_availability_is_refused() -> None:
    """설치가 깨진 상태다 — 조용히 넘기면 첫 추론에서 죽는다."""
    with pytest.raises(ProviderError):
        select_providers(PREFERRED, [])


def test_nothing_usable_is_refused() -> None:
    with pytest.raises(ProviderError, match="선호 EP"):
        select_providers((DML,), ["AzureExecutionProvider"])


def test_cpu_only_fallback_is_logged_as_warning(caplog: pytest.LogCaptureFixture) -> None:
    """⚠️ **어느 EP 로 도는지 기록이 없으면 성능 이상의 원인을 찾을 수 없다.**

    GPU 가 있는 환경에서 CPU 로 떨어진 것은 사고이므로 INFO 로 묻지 않는다.
    """
    with caplog.at_level("INFO", logger="mechadog.vision"):
        log_selection([CPU_PROVIDER], [CPU_PROVIDER], PREFERRED)
    assert [r.levelname for r in caplog.records] == ["WARNING"]
    assert caplog.records[0].event == "execution_provider_cpu_only"


def test_gpu_selection_is_logged_as_info(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO", logger="mechadog.vision"):
        log_selection([DML, CPU_PROVIDER], [DML, CPU_PROVIDER], PREFERRED)
    assert caplog.records[0].event == "execution_provider"
    assert caplog.records[0].detail["selected"] == DML


def test_intentional_cpu_selection_is_not_called_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CPU를 명시적으로 1순위로 둔 환경까지 장애로 부르면 로그가 거짓이 된다."""
    with caplog.at_level("INFO", logger="mechadog.vision"):
        log_selection([CPU_PROVIDER], [CPU_PROVIDER], [CPU_PROVIDER])
    assert [r.levelname for r in caplog.records] == ["INFO"]
    assert caplog.records[0].event == "execution_provider"
