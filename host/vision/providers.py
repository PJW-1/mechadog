"""추론 실행 프로바이더(EP) 선택 (WBS 3.3.1 ④ · DR-13).

**설정은 선호 순서일 뿐이다.** 실제로 쓸 수 있는 것과의 교집합을 취하므로, 팀원 PC 의
GPU 가 무엇이든 · GPU 가 아예 없든 **같은 코드가 그대로 돈다** (ADR-13).

⚠️ **onnxruntime 을 import 하지 않고도 시험된다.** 사용 가능 목록을 인자로 받기
때문이다 — 이 모듈을 시험하려고 GPU 나 특정 휠을 요구하면, 정작 폴백 경로를
검증할 수 없다(CI 러너에는 GPU 가 없다).

⚠️ **DirectML 은 미지원 연산을 조용히 CPU 로 내려보낸다.** 그래서 선택 결과를
기동 시 반드시 로그로 남긴다. 기록이 없으면 "왜 느린가"의 답을 찾을 수 없다
(ENGINEERING_GUIDE 「같은 코드, 같은 모델 파일, 다른 환경」).
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

    **여기서만 onnxruntime 을 만진다.** 선택 로직(`select_providers`)은 순수 함수로
    남겨 두어야 GPU 없는 곳에서도 시험된다.
    """
    import onnxruntime as ort

    return list(ort.get_available_providers())


def select_providers(
    preferred: Sequence[str],
    available: Iterable[str] | None = None,
) -> list[str]:
    """선호 순서를 유지하면서 **실제로 쓸 수 있는 것만** 남긴다.

    ⚠️ **없는 EP 를 요구하면 안 된다.** onnxruntime 은 미설치 EP 를 넘기면 경고만
    내고 무시하는 판이 있어, 우리가 걸러 두지 않으면 **DirectML 을 쓰는 줄 알고
    CPU 로 도는 상태**가 조용히 만들어진다.

    ⚠️ **CPU 를 항상 뒤에 붙인다.** 설정에서 빠뜨렸을 때 GPU 가 없는 팀원 PC 나 CI
    에서 기동이 실패하는데, 그건 설정 실수가 실행 실패로 번지는 것이다.
    """
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
    """무엇으로 도는지 기록한다. **GPU 를 기대했는데 CPU 면 경고로 올린다.**

    같은 코드가 세 환경(개발 PC · CI · 팀원 PC)에서 다르게 도는 것이 정상인 구조라,
    로그가 없으면 성능 수치의 출처를 알 수 없다.
    """
    # 설치가 잘못되어 CPU 하나만 보이는 경우가 가장 중요한 폴백이다. 사용 가능 EP 수로
    # 판단하면 바로 그 경우를 INFO 로 숨긴다. 설정이 GPU를 우선했는지와 실제 선택을 비교한다.
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
