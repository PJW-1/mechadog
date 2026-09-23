"""TF 카드 문장 표 (WBS 4.7.21) — 문장 → 트랙 번호, 누락 검사, 카드 음원 제작.

로봇 MP3 모듈은 `SOUND {track}` 을 받으면 TF 카드 `/MP3/NNNN<이름>.mp3` 를 튼다(번호는
파일 이름 앞 네 자리 · 4.7.20 실측). 한국어 문장이 정본이고 호스트 ↔ 음성 프로세스 계약도
문장 그대로다. 재생 직전에 `track_for(문장)` 으로 번호를 얻는다.

표는 `tf_tracks.tsv`(한 줄 = `번호<TAB>문장`)다. **번호는 바꾸거나 다시 쓰지 않는다** —
이미 구운 카드와 어긋난다. 새 문장은 가장 큰 번호 다음 번호를 받는다.

    python tf_tracks.py --check      # 말할 문장이 전부 표에 있나 (CI)
    python tf_tracks.py --add        # 빠진 문장을 표 끝에 붙인다
    python tf_tracks.py --build OUT  # OUT/MP3/NNNN<이름>.mp3 를 만든다 (Piper + ffmpeg)

`--build` 는 출력 폴더에만 쓴다. 카드에 복사하는 일은 사람이 한다(README).
"""

from __future__ import annotations

import argparse
import ast
import functools
import io
import subprocess
import sys
import unicodedata
import wave
from pathlib import Path

import phrases
import robotlink
import scenarios

HERE = Path(__file__).parent
TABLE_PATH = HERE / "tf_tracks.tsv"
CONFIG_PATH = HERE.parents[1] / "config" / "config.yaml"
MAX_TRACK = 3000  # 펌웨어 SOUND 범위 (PROTOCOL.md)

# 발화 경로 — 이 파일들의 say 호출에서 고정 문장을 모은다.
SPEAKERS = ("scenarios.py", "voice_pipeline.py", "robotlink.py")
SAY_TEXT_ARG = {"say": 0, "_say": 2}  # ctx.say(text) · _say(device, piper, text, speed)
SPOKEN_RETURNS = {"auth_prompt", "run_action"}  # 돌려준 문장을 호출자가 그대로 말한다

# 44.1 kHz 모노 128k — 모듈이 그대로 튼다(2026-09-24 실기).
# 음량 15 에서 원음(말소리 구간 -16.8 dB)이 작았다. 저음을 자르고 +9 dB 올려 리미터로 피크를 막으면
# 공장 곡(-11 dB 안팎)과 비슷해진다. 여전히 작으면 여기 이득부터 조정한다.
LOUDNESS_FILTER = "highpass=f=150,volume=9dB,alimiter=limit=0.9:attack=1:release=30:level=false"
FFMPEG_ARGS = ("-af", LOUDNESS_FILTER, "-ar", "44100", "-ac", "1", "-b:a", "128k")


def norm(text: str) -> str:
    """조회 키 — NFC 로 맞추고 공백·줄바꿈을 한 칸으로 줄인다. 문장부호는 그대로 본다."""
    return " ".join(unicodedata.normalize("NFC", text).split())


def load_table(path: Path = TABLE_PATH) -> dict[int, str]:
    """표 파일 → {번호: 문장}. 번호 범위·중복 번호·중복 문장은 ValueError."""
    table: dict[int, str] = {}
    seen: set[str] = set()
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        num, sep, text = line.partition("\t")
        n, text = int(num), norm(text)
        if not sep or not text or not 1 <= n <= MAX_TRACK:
            raise ValueError(f"{path.name}:{i}: `번호<TAB>문장` (1~{MAX_TRACK}) 이 아니다")
        if n in table or text in seen:
            raise ValueError(f"{path.name}:{i}: 번호나 문장이 겹친다 — {n}")
        table[n] = text
        seen.add(text)
    return table


@functools.cache
def _default_index() -> dict[str, int]:
    return {text: n for n, text in load_table().items()}


def track_for(text: str, table: dict[int, str] | None = None) -> int | None:
    """문장 → 트랙 번호. 표에 없으면 None."""
    index = _default_index() if table is None else {norm(t): n for n, t in table.items()}
    return index.get(norm(text))


# ── 말할 문장 모으기 ────────────────────────────────────────────────────────


def warning_lines(path: Path = CONFIG_PATH) -> dict[str, str]:
    """config `escalation.sound` 의 `*_warning` 문장 (키 규칙 `{원인 소문자}_warning`)."""
    import yaml  # 음성 PC 조회 경로는 yaml 없이 돈다 — 모을 때만 쓴다

    sound = yaml.safe_load(path.read_text(encoding="utf-8"))["escalation"]["sound"]
    return {k: v for k, v in sound.items() if k.endswith("_warning") and v}


def _texts(node):
    """발화 식에서 고정 문장을 꺼낸다. f-문자열은 None(동적)으로 알린다.

    `pick(...)` 은 phrases 가, 변수는 그 변수를 채운 식(`spoken = ...`)이 덮는다.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
    elif isinstance(node, ast.JoinedStr):
        yield None
    elif isinstance(node, ast.IfExp):
        yield from _texts(node.body)
        yield from _texts(node.orelse)
    elif isinstance(node, ast.BoolOp | ast.Tuple):
        for child in node.values if isinstance(node, ast.BoolOp) else node.elts:
            yield from _texts(child)


def scan_say_sites(path: Path):
    """소스에서 말하는 자리를 찾는다 → (고정 문장 목록, 동적 문장이 있는 `파일:함수` 집합)."""
    texts, dynamic = [], set()
    for fn in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            expr = None
            if isinstance(node, ast.Call):
                i = SAY_TEXT_ARG.get(getattr(node.func, "attr", getattr(node.func, "id", None)))
                if i is not None and len(node.args) > i:
                    expr = node.args[i]
            elif isinstance(node, ast.Assign):
                if any(getattr(t, "id", None) == "spoken" for t in node.targets):
                    expr = node.value
            elif isinstance(node, ast.Return) and fn.name in SPOKEN_RETURNS:
                expr = node.value
            for text in _texts(expr):
                if text is None:
                    dynamic.add(f"{path.name}:{fn.name}")
                else:
                    texts.append(text)
    return texts, dynamic


class _Recorder:
    """시나리오에 넘기는 ctx — 말한 문장만 모은다. 듣기는 늘 `answer` 를 돌려준다."""

    def __init__(self, retrieve, docs):
        self._retrieve, self.docs, self.answer, self.lines = retrieve, docs, "", []

    def say(self, text):
        self.lines.append(text)

    def listen(self, _timeout_s=12.0):
        return self.answer

    def retrieve(self, query):
        return self._retrieve(self.docs, query)

    def command(self, _action):
        return True

    def event(self, _role, _text):
        pass


def knowledge_lines() -> list[str]:
    """지식 문서 본문을 붙이는 시나리오가 실제로 말할 문장 — 시나리오를 돌려서 모은다.

    공지·안내는 검색어가 고정이라 본문이 정해진다. 길안내(`sc_visitor_guide`)는 질문에
    따라 첫 문서가 바뀌므로 문서마다 «그 문서가 뽑힌 경우» 를 만든다. 문서를 고치면
    문장이 바뀌어 `--check` 가 실패한다 — 카드를 다시 만들라는 뜻이다.
    """
    import voice_pipeline  # 지연 import — voice_pipeline 이 이 모듈을 부른다

    docs = voice_pipeline.load_knowledge()
    ctx = _Recorder(voice_pipeline.retrieve, docs)
    for _desc, func in scenarios.SCENARIOS.values():
        func(ctx)  # 무응답 갈래
    for doc in docs:
        ctx.docs, ctx.answer = [doc], doc[0]
        scenarios.sc_visitor_guide(ctx)
    return ctx.lines


def collect_lines() -> dict[str, str]:
    """재생할 수 있어야 하는 문장 전부 → {정규화 문장: 출처}. 경고 문장이 앞에 온다."""
    found: dict[str, str] = {}

    def add(text, source):
        if norm(text):
            found.setdefault(norm(text), source)

    for key, text in warning_lines().items():
        add(text, f"config escalation.sound.{key}")
    for cat, text in phrases.all_lines():
        add(text, f"phrases.{cat}")
    for _action, ack in robotlink.ACTIONS.values():
        add(ack, "robotlink.ACTIONS")
    for name in SPEAKERS:
        for text in scan_say_sites(HERE / name)[0]:
            add(text, name)
    for text in knowledge_lines():
        add(text, "knowledge")
    return found


def dynamic_sites() -> set[str]:
    """f-문자열로 말하는 자리. 표로 덮이는지는 `knowledge_lines` 가 맡는다."""
    return set().union(*(scan_say_sites(HERE / name)[1] for name in SPEAKERS))


def missing(table: dict[int, str]) -> dict[str, str]:
    have = set(table.values())
    return {t: src for t, src in collect_lines().items() if t not in have}


def add_missing(path: Path = TABLE_PATH) -> list[tuple[int, str]]:
    """빠진 문장을 표 끝에 붙인다. 기존 번호는 건드리지 않고 가장 큰 번호 다음부터 준다."""
    table = load_table(path)
    new = list(missing(table))
    start = max(table, default=0) + 1
    if start + len(new) - 1 > MAX_TRACK:
        raise ValueError(f"트랙 번호가 {MAX_TRACK} 을 넘는다")
    rows = [(start + i, text) for i, text in enumerate(new)]
    body = path.read_text(encoding="utf-8")
    body += "" if body.endswith("\n") or not body else "\n"
    body += "".join(f"{n:04d}\t{text}\n" for n, text in rows)
    path.write_text(body, encoding="utf-8", newline="\n")
    return rows


# ── 카드 음원 만들기 ────────────────────────────────────────────────────────


def file_name(n: int, text: str) -> str:
    """`NNNN<짧은 이름>.mp3`. 이름에는 글자만 남긴다 — 숫자가 붙으면 모듈이 번호로 읽을 수 있다."""
    return f"{n:04d}{''.join(ch for ch in text if ch.isalpha())[:12]}.mp3"


def piper_synth(model: Path, length_scale: float = 1.2):
    """Piper 모델을 한 번 올리고 «문장 → WAV 바이트» 함수를 돌려준다 (gen_voice_cache 와 같은 호출)."""
    from piper import PiperVoice, SynthesisConfig

    voice = PiperVoice.load(str(model))
    syn = SynthesisConfig(length_scale=length_scale) if length_scale != 1.0 else None

    def synth(text: str) -> bytes:
        pcm = b"".join(c.audio_int16_bytes for c in voice.synthesize(text, syn_config=syn))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(voice.config.sample_rate)
            w.writeframes(pcm)
        return buf.getvalue()

    return synth


def ffmpeg_mp3(wav: bytes, out: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "wav", "-i", "pipe:0"]
        + [*FFMPEG_ARGS, str(out)],
        input=wav,
        check=True,
    )


def build(out: Path, synth, table: dict[int, str], encode=ffmpeg_mp3) -> list[Path]:
    """표의 문장마다 `out/MP3/NNNN<이름>.mp3` 를 쓴다. `out/MP3` 가 비어 있지 않으면 쓰지 않는다.

    남아 있던 파일과 번호가 겹치면 카드에서 어느 것이 나올지 모르기 때문이다.
    """
    out = out.resolve()
    if out == Path(out.anchor):
        raise ValueError(
            f"드라이브 루트({out})에는 쓰지 않는다 — 출력 폴더를 만든 뒤 사람이 복사한다"
        )
    mp3_dir = out / "MP3"
    if mp3_dir.exists() and any(mp3_dir.iterdir()):
        raise ValueError(f"{mp3_dir} 가 비어 있지 않다")
    mp3_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for n, text in sorted(table.items()):
        path = mp3_dir / file_name(n, text)
        encode(synth(text), path)
        written.append(path)
    return written


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="표에 없는 문장이 있으면 실패")
    mode.add_argument("--add", action="store_true", help="빠진 문장에 다음 번호를 준다")
    mode.add_argument("--build", type=Path, metavar="OUT", help="OUT/MP3/ 에 음원을 만든다")
    ap.add_argument(
        "--piper-model", type=Path, default=Path(r"C:\dev\voice\piper\ko_KR-kss-medium.onnx")
    )
    ap.add_argument("--speed", type=float, default=1.2, help="piper length_scale (기본 1.2)")
    args = ap.parse_args(argv)

    if args.add:
        rows = add_missing()
        for n, text in rows:
            print(f"+ {n:04d} {text}")
        print(f"[tf] {len(rows)}개 추가 -> {TABLE_PATH.name}")
        return 0

    table = load_table()
    gaps = missing(table)
    for text, src in gaps.items():
        print(f"[tf] 표에 없음 ({src}): {text}")
    if gaps:
        print(f"[tf] {len(gaps)}개 누락 — `python tf_tracks.py --add` 뒤 카드를 다시 만든다")
        return 1
    if args.check:
        print(f"[tf] 트랙 {len(table)}개 · 누락 0")
        print(
            f"[tf] f-문자열 발화 자리 (지식 본문 외에는 표로 못 덮는다): {sorted(dynamic_sites())}"
        )
        return 0
    paths = build(args.build, piper_synth(args.piper_model, args.speed), table)
    print(f"[tf] {len(paths)}개 -> {args.build / 'MP3'} — 카드 /MP3/ 로 복사한다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
