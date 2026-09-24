"""VLM 고정 질문을 우리 사진으로 잰다 — 적중률·오경보율·지연 (WBS 3.6.4 · ADR-35).

구역 변화 감지에서 `fallen_object`·`blocked_path` 가 «같은 방문 안 2회 연속 예» 이면
L3 경보를 내도록 바꾸는 중이다(2026-09-24). **경보를 켜기 전에** 이것으로 잰다.

    python tools/vlm_bench.py capture --camera-ip <ip> --root <폴더> \
        --question fallen_object --label yes --scene 소화기_눕힘
    ~/.venv-mechdog-vlm/Scripts/python.exe tools/vlm_bench.py measure <폴더> \
        --repeat 2 --out bench.json

⚠️ **사진을 저장소에 커밋하지 않는다** — 얼굴이 찍힐 수 있다. `<폴더>` 는 저장소 밖에 둔다.

캡처 (`capture`)

    우리 카메라 프레임을 아래 폴더 구조에 바로 저장한다. Enter 마다 지금 프레임 한 장을
    `<root>/<질문키>/<yes|no>/<장면>_NN.jpg` 로 쓰고(NN 은 이어지는 번호), `q` + Enter 로
    끝낸다. 로봇이나 물건을 조금씩 옮겨 가며 같은 장면을 여러 장 찍는 용도다 — 그것이
    아래 장면 묶음이 된다.

    ⚠️ **런타임을 내린 상태에서 쓴다.** 런타임이 스트림을 쥐고 있으면 함께 열리지 않을 수
    있다. ⚠️ **로봇에는 아무 명령도 보내지 않는다** — 카메라만 만진다.
    ⚠️ **방향 보정(`apply_profile`)을 스스로 내려보낸다.** `/orient` 는 카메라 전원을 껐다
    켜면 0 으로 풀려, 런타임 없이 도는 도구가 부르지 않으면 180도 뒤집힌 사진이 찍힌다
    (2026-09-20 에 이것으로 899 프레임을 날렸다). 다시 붙을 때마다 또 내려보낸다.

입력 폴더

    <root>/<질문키>/<yes|no>/*.jpg      질문키는 `vlm_reader.QUESTIONS` 의 key
    예) <root>/fallen_object/yes/쓰러진소화기_01.jpg

    질문키 폴더마다 따로 채점한다 — 한 사진을 두 질문에 쓰려면 두 폴더에 넣는다.
    구조가 틀리면 틀린 곳을 모두 적고 멈춘다(종료 코드 2).

장면 묶음 (`--repeat N`)

    파일 이름 끝의 `_<숫자>` 를 뗀 것이 장면이고 숫자가 프레임 순서다.
    `소화기_01.jpg`·`소화기_02.jpg` → 장면 `소화기`, 프레임 1·2. 끝에 `_<숫자>` 가 없으면
    그 파일 하나가 한 장면이다. 이름이 같아도 yes/no 폴더가 다르면 다른 장면이다.
    **번호 순으로 이웃한 N 프레임이 모두 «예» 일 때만** 장면을 «예» 로 친다 — 운용의
    «같은 방문 안 N회 연속 예» 다. 판독 불가 프레임은 «예» 가 아니므로 연속을 끊는다.
    프레임이 N 장보다 적은 장면은 «예» 가 될 수 없고, `프레임 부족` 으로 따로 센다.

    ⚠️ 같은 사진을 두 번 읽히는 것은 뜻이 없다 — 결정적 디코딩(`do_sample=False`)이라
    같은 답이 나올 뿐이다. 같은 장면을 **다른 프레임으로** 찍어 넣는다.

측정 (`measure`)

    운용과 같은 `VlmReader`·질문 문장·`parse_answer`·설정(`vision.vlm`)을 쓴다. 이미지는
    운용처럼 JPEG 바이트 그대로 넘긴다.

    ⚠️ **사진마다 그 폴더의 질문 하나만 묻는다.** 세션의 `ask` 는 부를 때마다 대화를
    새로 만들고 표본을 뽑지 않으므로, 셋을 다 물어도 그 질문의 답과 지연은 같고 시간만
    세 배가 든다. 운용 예산(`budget_ms`)에 드는지는 질문별 p95 를 더해 본다.

    판독 불가(기능 저하·파싱 실패)는 운용에서 경보가 울리지 않으므로 «예 아님» 으로
    채점하고(FN 또는 TN), 그중 몇 장이었는지 따로 센다. 첫 질문은 워밍업이라 지연
    통계에서 빼고 따로 적는다.

    ⚠️ 가중치를 **내려받지 않는다** (`HF_HUB_OFFLINE=1`). 캐시에 없으면 적재에서 멈춘다 —
    받는 법은 `models/README.md` ③.

출력

    표준 출력에 마크다운 표, `--out x.json` 에 파일별 답·원문·지연. 이미지는 복사하지 않는다.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.common.config import load_base_config, load_config  # noqa: E402
from host.common.console import survive_encoding_errors  # noqa: E402
from host.common.protocol import system_clock_ms  # noqa: E402
from host.vision.stream_client import (  # noqa: E402
    FrameQueue,
    StreamReader,
    apply_profile,
    stream_endpoints,
)
from host.vision.vlm_reader import QUESTIONS, VlmReader, VlmSession  # noqa: E402
from host.vision.vlm_session import build_session_factory, missing_packages  # noqa: E402
from tools.camera_link_check import percentile  # noqa: E402

KEYS = tuple(question.key for question in QUESTIONS)
LABELS = ("yes", "no")
IMAGE_SUFFIXES = (".jpg", ".jpeg")
_FRAME = re.compile(r"^(?P<scene>.+)_(?P<frame>\d+)$")
#: 장면 이름에 못 쓰는 글자. 경로를 벗어나거나 Windows 가 파일 이름으로 받지 않는다.
_BAD_SCENE_CHARS = frozenset('<>:"/\\|?*')


class LayoutError(ValueError):
    """폴더 구조가 틀렸다. `problems` 에 틀린 곳이 모두 들어 있다."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass(frozen=True, slots=True)
class Sample:
    key: str
    label: str
    path: Path
    #: `<root>` 에서의 상대 경로. 원자료에는 이것만 남긴다.
    file: str
    scene: str
    frame: int


def scene_of(stem: str) -> tuple[str, int]:
    """`소화기_02` → (`소화기`, 2). 번호가 없으면 파일 하나가 한 장면이다."""
    match = _FRAME.match(stem)
    if match is None:
        return stem, 0
    return match["scene"], int(match["frame"])


def collect(root: Path) -> list[Sample]:
    """폴더를 훑어 사진 목록을 만든다. 틀린 곳이 있으면 모두 모아 `LayoutError`."""
    if not root.is_dir():
        raise LayoutError([f"{root}: 폴더가 없다"])
    keys = sorted(KEYS)
    problems: list[str] = []
    samples: list[Sample] = []
    for key_dir in sorted(root.iterdir()):
        if not key_dir.is_dir() or key_dir.name not in keys:
            problems.append(f"{key_dir.name}: 질문키 폴더가 아니다 (허용: {', '.join(keys)})")
            continue
        before = len(samples)
        for label_dir in sorted(key_dir.iterdir()):
            where = f"{key_dir.name}/{label_dir.name}"
            if not label_dir.is_dir() or label_dir.name not in LABELS:
                problems.append(f"{where}: yes 나 no 폴더가 아니다")
                continue
            for path in sorted(label_dir.iterdir()):
                if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
                    problems.append(f"{where}/{path.name}: .jpg 사진이 아니다")
                    continue
                scene, frame = scene_of(path.stem)
                file = path.relative_to(root).as_posix()
                samples.append(Sample(key_dir.name, label_dir.name, path, file, scene, frame))
        if len(samples) == before:
            problems.append(f"{key_dir.name}: 사진이 없다")
    if not problems and not samples:
        problems.append(f"{root}: 질문키 폴더가 없다 (허용: {', '.join(keys)})")
    if problems:
        raise LayoutError(problems)
    return samples


def next_frame_path(root: Path, key: str, label: str, scene: str) -> Path:
    """`<root>/<key>/<label>/<scene>_NN.jpg` 의 다음 번호. **`collect` 가 같은 장면으로 읽는다.**"""
    if key not in KEYS:
        raise ValueError(f"질문키가 아니다: {key!r} (허용: {', '.join(KEYS)})")
    if label not in LABELS:
        raise ValueError(f"yes 나 no 가 아니다: {label!r}")
    blank = not scene or scene != scene.strip() or scene in {".", ".."}
    if blank or _BAD_SCENE_CHARS & set(scene):
        raise ValueError(f'장면 이름으로 못 쓴다: {scene!r} (빈칸·앞뒤 공백·<>:"/\\|?* 금지)')
    folder = root / key / label
    taken = [0]
    if folder.is_dir():
        for path in folder.iterdir():
            name, frame = scene_of(path.stem)
            if name == scene and path.suffix.lower() in IMAGE_SUFFIXES:
                taken.append(frame)
    return folder / f"{scene}_{max(taken) + 1:02d}.jpg"


def capture(args: argparse.Namespace) -> int:
    """Enter 마다 지금 프레임을 저장한다. **로봇에는 아무것도 보내지 않는다.**"""
    config = load_config(args.device)
    network = {**config["network"], "xiao_ip": str(ipaddress.IPv4Address(args.camera_ip))}
    endpoints = stream_endpoints({"network": network})

    def orient() -> None:
        # ⚠️ 카메라가 재부팅하면 `/orient` 가 0 으로 풀린다 — 붙을 때마다 다시 내린다.
        apply_profile(config, endpoints=endpoints)

    orient()  # 카메라가 없으면 여기서 멈춘다 — 백오프 재시도에 묻히지 않게
    print(f"카메라 {endpoints.stream} · rot={config['vision']['mount_rotation']}")
    reader = StreamReader(config, endpoints=endpoints, before_connect=orient)
    latest = FrameQueue(capacity=1)

    def pump() -> None:
        for frame in reader.frames():
            latest.put(frame)

    threading.Thread(target=pump, name="vlm-bench-capture", daemon=True).start()
    #: 이보다 오래된 프레임은 저장하지 않는다 — 스트림이 멈춘 동안 옮긴 장면이 옛
    #: 프레임으로 찍히면 라벨이 틀린다. 기준은 비전 단절의 정의와 같다 (NFR-2.6).
    stale_ms = int(config["vision"]["stall_timeout_ms"])
    folder = args.root / args.question / args.label
    print(f"Enter = 저장 → {folder}{os.sep}{args.scene}_NN.jpg · q + Enter = 끝", flush=True)
    saved = 0
    try:
        for line in sys.stdin:
            if line.strip().lower() == "q":
                break
            frame = latest.latest()
            if frame is None or system_clock_ms() - frame.received_ms > stale_ms:
                print("  새 프레임이 없다 — 저장하지 않았다", file=sys.stderr, flush=True)
                continue
            path = next_frame_path(args.root, args.question, args.label, args.scene)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as out:  # 있던 사진을 덮지 않는다
                out.write(frame.payload)
            saved += 1
            print(f"  {path.name} ({frame.size_bytes // 1024} KB)", flush=True)
    finally:
        reader.stop()
    print(f"{saved}장 저장")
    return 0


def measure(
    samples: Sequence[Sample], session: VlmSession, *, budget_ms: int
) -> list[dict[str, Any]]:
    """사진마다 그 폴더의 질문 하나를 운용의 `VlmReader` 로 묻는다. 맨 첫 질문이 워밍업이다."""
    readers = {
        question.key: VlmReader(lambda: session, questions=(question,), budget_ms=budget_ms)
        for question in QUESTIONS
    }
    for reader in readers.values():
        reader.load()
    records: list[dict[str, Any]] = []
    for index, sample in enumerate(samples):
        reading = readers[sample.key].read(sample.path.read_bytes(), now_ms=0)
        answer = reading.answers[0] if reading.answers else None
        record = {
            "key": sample.key,
            "label": sample.label,
            "file": sample.file,
            "scene": sample.scene,
            "frame": sample.frame,
            "value": None if answer is None else answer.value,
            "raw": None if answer is None else answer.raw,
            "latency_ms": None if answer is None else answer.latency_ms,
            "degraded": reading.degraded,
            "reason": reading.reason,
            "warmup": index == 0,
        }
        records.append(record)
        print(
            f"  {sample.file}  {record['latency_ms']}ms  {record['raw']!r}",
            file=sys.stderr,
            flush=True,
        )
    return records


def score(pairs: Iterable[tuple[bool, bool]]) -> dict[str, Any]:
    """(정답이 예인가, 판독이 예인가) 짝으로 혼동 행렬과 비율. 분모가 0 이면 `None`."""
    counts = Counter(pairs)
    tp, fn = counts[True, True], counts[True, False]
    fp, tn = counts[False, True], counts[False, False]
    return {
        "tp": tp,
        "fn": fn,
        "fp": fp,
        "tn": tn,
        "recall": tp / (tp + fn) if tp + fn else None,
        "false_alarm": fp / (fp + tn) if fp + tn else None,
    }


def hits(values: Sequence[bool | None], repeat: int) -> bool:
    """이웃한 `repeat` 개가 모두 «예» 인 곳이 있나. 판독 불가(`None`)는 연속을 끊는다."""
    run = 0
    for value in values:
        run = run + 1 if value is True else 0
        if run >= repeat:
            return True
    return False


def summarize(records: Sequence[dict[str, Any]], repeat: int) -> dict[str, Any]:
    """원자료를 질문별로 채점한다. `repeat` 가 2 이상이면 장면 채점을 더한다."""
    by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_key[record["key"]].append(record)
    questions: dict[str, Any] = {}
    for key, rows in by_key.items():
        timed = [r for r in rows if r["latency_ms"] is not None and not r["warmup"]]
        latencies = [r["latency_ms"] for r in timed]
        entry: dict[str, Any] = {
            "frames": score((r["label"] == "yes", r["value"] is True) for r in rows),
            "unreadable": {
                label: sum(r["label"] == label and r["value"] is None for r in rows)
                for label in LABELS
            },
            "latency_ms": {
                "n": len(latencies),
                "p50": percentile(latencies, 0.5),
                "p95": percentile(latencies, 0.95),
                "max": max(latencies, default=None),
            },
        }
        if repeat > 1:
            scenes: dict[tuple[str, str], list[bool | None]] = defaultdict(list)
            for r in sorted(rows, key=lambda r: r["frame"]):
                scenes[r["label"], r["scene"]].append(r["value"])
            entry["scenes"] = score(
                (label == "yes", hits(values, repeat)) for (label, _), values in scenes.items()
            )
            entry["scenes"]["short"] = sum(len(values) < repeat for values in scenes.values())
        questions[key] = entry
    return {
        "warmup_ms": records[0]["latency_ms"] if records else None,
        "questions": questions,
    }


def _rate(value: float | None) -> str:
    return "-" if value is None else f"{value:.1%}"


def _num(value: Any) -> str:
    return "-" if value is None else str(value)


def render(summary: dict[str, Any], repeat: int) -> str:
    """사람이 읽을 마크다운 표. 결과 기록(`summary.md`)에 그대로 붙인다."""
    lines = [
        "## 프레임 단위",
        "",
        "| 질문 | 예/아니오 | TP | FN | FP | TN | 판독 불가 (예/아니오) | 적중률 | 오경보율 "
        "| p50 ms | p95 ms | 최대 ms |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for key, entry in summary["questions"].items():
        f, u, t = entry["frames"], entry["unreadable"], entry["latency_ms"]
        lines.append(
            f"| `{key}` | {f['tp'] + f['fn']}/{f['fp'] + f['tn']} | {f['tp']} | {f['fn']} "
            f"| {f['fp']} | {f['tn']} | {u['yes']}/{u['no']} | {_rate(f['recall'])} "
            f"| {_rate(f['false_alarm'])} | {_num(t['p50'])} | {_num(t['p95'])} "
            f"| {_num(t['max'])} |"
        )
    lines += ["", f"워밍업 (맨 첫 질문 · 지연 통계에서 뺌): {_num(summary['warmup_ms'])} ms"]
    if repeat > 1:
        lines += [
            "",
            f"## 장면 단위 — 이웃한 {repeat} 프레임이 모두 «예» 일 때만 «예»",
            "",
            "| 질문 | 예/아니오 장면 | TP | FN | FP | TN | 적중률 | 오경보율 | 프레임 부족 |",
            "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for key, entry in summary["questions"].items():
            s = entry["scenes"]
            lines.append(
                f"| `{key}` | {s['tp'] + s['fn']}/{s['fp'] + s['tn']} | {s['tp']} | {s['fn']} "
                f"| {s['fp']} | {s['tn']} | {_rate(s['recall'])} | {_rate(s['false_alarm'])} "
                f"| {s['short']} |"
            )
    return "\n".join(lines)


def run_measure(args: argparse.Namespace) -> int:
    try:
        samples = collect(args.root)
    except LayoutError as exc:
        print("폴더 구조가 틀렸다 — <root>/<질문키>/<yes|no>/*.jpg", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    config = load_base_config()
    vlm = config["vision"]["vlm"]
    factory = build_session_factory(config)
    if factory is None:
        print(
            f"VLM 의존성이 없다 {list(missing_packages())} — "
            "~/.venv-mechdog-vlm 의 python 으로 돌린다 (models/README.md ③)",
            file=sys.stderr,
        )
        return 1
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    print(f"적재 중… {vlm['model_id']}", file=sys.stderr, flush=True)
    started = time.monotonic()
    session = factory()
    load_s = round(time.monotonic() - started, 1)
    try:
        records = measure(samples, session, budget_ms=int(vlm["budget_ms"]))
    finally:
        session.close()

    summary = summarize(records, args.repeat)
    print(
        f"# VLM 벤치 — `{vlm['model_id']}` · 적재 {load_s}초 · 사진 {len(records)}장 "
        f"· 운용 예산 {vlm['budget_ms']}ms\n"
    )
    print(render(summary, args.repeat))
    if args.out is not None:
        args.out.write_text(
            json.dumps(
                {
                    "model_id": vlm["model_id"],
                    "max_new_tokens": vlm.get("max_new_tokens"),
                    "budget_ms": vlm["budget_ms"],
                    "load_seconds": load_s,
                    "repeat": args.repeat,
                    "summary": summary,
                    "records": records,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n원자료 → {args.out}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    survive_encoding_errors()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)

    bench = commands.add_parser("measure", help="폴더의 사진으로 적중률·오경보율·지연을 잰다")
    bench.add_argument("root", type=Path, help="<root>/<질문키>/<yes|no>/*.jpg")
    bench.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="장면 안 이웃한 N 프레임이 모두 «예» 여야 «예» (기본 1 = 장면 채점 안 함)",
    )
    bench.add_argument("--out", type=Path, help="원자료 JSON 경로 (이미지는 담지 않는다)")

    shoot = commands.add_parser("capture", help="카메라 프레임을 폴더 구조에 바로 저장한다")
    shoot.add_argument("--camera-ip", required=True)
    shoot.add_argument("--device", default="mechdog-01", help="장착 방향을 읽을 개체 프로파일")
    shoot.add_argument("--root", type=Path, required=True, help="저장소 밖 폴더")
    shoot.add_argument("--question", required=True, help=f"질문키 ({', '.join(KEYS)})")
    shoot.add_argument("--label", required=True, help="yes 또는 no")
    shoot.add_argument("--scene", required=True, help="장면 이름. 파일은 <장면>_NN.jpg")

    args = parser.parse_args(argv)
    if args.command == "capture":
        try:  # 카메라를 만지기 전에 경로부터 본다
            next_frame_path(args.root, args.question, args.label, args.scene)
        except ValueError as exc:
            shoot.error(str(exc))
        return capture(args)
    if args.repeat < 1:
        bench.error("--repeat 는 1 이상이어야 한다")
    return run_measure(args)


if __name__ == "__main__":
    raise SystemExit(main())
