"""Local Korean recognition of completed WonderEcho captures; no network at inference."""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import wave
from pathlib import Path


def claimed_name(text):
    """Extract a spoken name, not an authenticated identity. Reject ambiguous answers."""
    pattern = re.compile(r"사원\s*([가-힣]{2,5})\s*입니다")
    matches = list(pattern.finditer(text))
    if not matches or pattern.sub("", text).strip(" \t\r\n.!?。"):
        return None
    names = {match.group(1) for match in matches}
    return next(iter(names)) if len(names) == 1 else None


def validate_capture(folder):
    report = json.loads((folder / "capture.json").read_text(encoding="utf-8-sig"))
    if (
        report.get("success") is not True
        or report.get("frames") != 250
        or any(report.get(key) != 0 for key in ("checksum_errors", "length_errors", "timeouts"))
        or not any(
            e.get("phase") == 3 and e.get("reason") == 2 and e.get("sent") == 250
            for e in report.get("events", [])
        )
    ):
        raise ValueError("Only a successfully completed five-second capture may be transcribed")
    audio = folder / "voice.wav"
    with wave.open(str(audio), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()) != (
            1,
            2,
            16000,
            80000,
        ):
            raise ValueError("Expected five seconds of 16 kHz mono PCM16")
    return audio


def gpu_dll_directories():
    """Use only this virtual environment's NVIDIA runtime, without global PATH edits."""
    handles = []
    if os.name == "nt":
        vendor = Path(sys.prefix) / "Lib/site-packages/nvidia"
        folders = sorted(vendor.glob("*/bin"))
        # CTranslate2 also uses LoadLibrary by basename, which consults PATH.
        # This changes only the current process, not user/system settings.
        os.environ["PATH"] = (
            os.pathsep.join(map(str, folders)) + os.pathsep + os.environ.get("PATH", "")
        )
        for folder in folders:
            handles.append(os.add_dll_directory(str(folder)))
    return handles


def transcribe(model, folder, model_path, device, load_seconds):
    audio = validate_capture(folder)
    output = folder / "transcript.json"
    if output.exists():
        raise FileExistsError(output)
    started = time.perf_counter()
    # No name hints: a prompted expected answer must not become a test result.
    segments, info = model.transcribe(
        str(audio),
        language="ko",
        task="transcribe",
        beam_size=5,
        condition_on_previous_text=False,
        vad_filter=False,
        temperature=0.0,
    )
    parts = [
        {
            "start": s.start,
            "end": s.end,
            "text": s.text,
            "avg_logprob": s.avg_logprob,
            "no_speech_prob": s.no_speech_prob,
        }
        for s in segments
    ]
    text = "".join(s["text"] for s in parts).strip()
    result = {
        "source": "physical WonderEcho WAV; local inference",
        "audio_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
        "model_directory": str(model_path),
        "device": device,
        "model_load_seconds": round(load_seconds, 3),
        "transcribe_seconds": round(time.perf_counter() - started, 3),
        "language": info.language,
        "text": text,
        "segments": parts,
        "claimed_name": claimed_name(text),
        "identity_verified": False,
    }
    with output.open("x", encoding="utf-8") as file:
        json.dump(result, file, ensure_ascii=False, indent=2)
        file.write("\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model", required=True, type=Path, help="Previously downloaded local model folder"
    )
    parser.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument("capture_dirs", nargs="+", type=Path)
    args = parser.parse_args()
    for folder in args.capture_dirs:
        validate_capture(folder)
        if (folder / "transcript.json").exists():
            raise FileExistsError(folder / "transcript.json")
    if not (args.model / "model.bin").is_file():
        raise FileNotFoundError("Download the approved model before running offline recognition")
    dll_handles = gpu_dll_directories()
    from faster_whisper import WhisperModel

    start = time.perf_counter()
    model = WhisperModel(
        str(args.model),
        device=args.device,
        compute_type="float16" if args.device == "cuda" else "int8",
        cpu_threads=4,
        num_workers=1,
        local_files_only=True,
    )
    load_seconds = time.perf_counter() - start
    for folder in args.capture_dirs:
        print(
            json.dumps(
                transcribe(model, folder, args.model, args.device, load_seconds),
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
    # Keep DLL search registrations alive until native inference has finished.
    del model
    for handle in dll_handles:
        handle.close()


if __name__ == "__main__":
    main()
