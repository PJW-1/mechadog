"""추론 실행 프로바이더(EP) 선택 (WBS 3.3.1 ④ · ADR-13).

설정은 선호 순서일 뿐이고 실제로 쓸 수 있는 것과의 교집합을 취한다 — GPU 가 무엇이든,
없든 같은 코드가 돈다. 선택 로직은 사용 가능 목록을 인자로 받는 순수 함수라 onnxruntime
없이 시험된다. 선택 결과는 기동 때 로그로 남긴다 (ENGINEERING_GUIDE 「같은 코드, 같은 모델
파일, 다른 환경」).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from host.common.logging_setup import event_logger

LOG = event_logger("mechadog.vision")

#: 어떤 환경에서도 있는 마지막 수단. 이것까지 없으면 설치가 깨진 것이다.
CPU_PROVIDER = "CPUExecutionProvider"


class ProviderError(RuntimeError):
    """쓸 수 있는 EP 가 하나도 없음 — 설치가 깨졌다는 뜻이다."""


def available_providers() -> list[str]:
    """설치된 onnxruntime 이 제공하는 EP 목록.

    여기서만 onnxruntime 을 만진다.
    """
    import onnxruntime as ort

    return list(ort.get_available_providers())


def select_providers(
    preferred: Sequence[str],
    available: Iterable[str] | None = None,
) -> list[str]:
    """선호 순서를 유지하면서 실제로 쓸 수 있는 EP 만 남기고, CPU 를 항상 뒤에 붙인다."""
    if available is None:
        available = available_providers()
    usable = set(available)
    if not usable:
        raise ProviderError("사용 가능한 EP 가 없다 — onnxruntime 설치를 확인한다")

    chosen = [name for name in preferred if name in usable]
    if CPU_PROVIDER in usable and CPU_PROVIDER not in chosen:
        chosen.append(CPU_PROVIDER)
    if not chosen:
        raise ProviderError(
            f"선호 EP 중 쓸 수 있는 것이 없다. 선호={list(preferred)} 사용가능={sorted(usable)}"
        )
    return chosen


def log_selection(
    chosen: Sequence[str], available: Sequence[str], preferred: Sequence[str]
) -> None:
    """무엇으로 도는지 기록한다. 설정이 GPU 를 우선했는데 CPU 로 떨어졌으면 경고로 올린다."""
    # 사용 가능 EP 수가 아니라 설정의 우선순위와 실제 선택을 비교한다.
    gpu_expected = bool(preferred) and preferred[0] != CPU_PROVIDER
    fell_back = chosen[:1] == [CPU_PROVIDER] and gpu_expected
    event = "execution_provider_cpu_only" if fell_back else "execution_provider"
    emit = LOG.warning if fell_back else LOG.info
    emit(
        event,
        selected=chosen[0],
        order=list(chosen),
        preferred=list(preferred),
        available=list(available),
    )
