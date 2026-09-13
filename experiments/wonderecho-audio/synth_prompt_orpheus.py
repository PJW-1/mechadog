"""Render the prompt with the Korean Orpheus fine-tune (canopylabs/3b-ko-ft-research_release).

Orpheus is a Llama-3.2-3B speech LLM: it emits SNAC audio codes as ordinary
tokens, which the SNAC 24 kHz decoder turns into waveform. The token layout below
follows the upstream reference implementation - seven codes per frame, spread
across three codebooks.

Runs on the host PC only; the module receives the baked PCM, not the model.
"""

import argparse
import json
import sys
from pathlib import Path

DEFAULT_TEXT = "신원을 말씀해 주세요"
MODEL = "canopylabs/3b-ko-ft-research_release"
SNAC_MODEL = "hubertsiuzdak/snac_24khz"
VOICES = ["유나", "준서"]

START = 128259
END = [128009, 128260, 128261, 128257]
AUDIO_START = 128257  # last occurrence marks where audio codes begin
PAD = 128258  # dropped before decoding
CODE_BASE = 128266  # first SNAC code id
SNAC_RATE = 24000


def _redistribute(codes):
    """Seven tokens per frame: 0 -> book1, {1,4} -> book2, {2,3,5,6} -> book3."""
    import torch

    l1, l2, l3 = [], [], []
    for i in range(len(codes) // 7):
        f = codes[7 * i : 7 * i + 7]
        l1.append(f[0])
        l2.append(f[1] - 4096)
        l3.append(f[2] - 2 * 4096)
        l3.append(f[3] - 3 * 4096)
        l2.append(f[4] - 4 * 4096)
        l3.append(f[5] - 5 * 4096)
        l3.append(f[6] - 6 * 4096)
    return [torch.tensor(x).unsqueeze(0) for x in (l1, l2, l3)]


_LOADED = {}


def _load(device):
    """Load the model once; generating several phrases in a row reuses it."""
    if "model" not in _LOADED:
        import torch
        from snac import SNAC
        from transformers import AutoModelForCausalLM, AutoTokenizer

        tok = AutoTokenizer.from_pretrained(MODEL)
        model = AutoModelForCausalLM.from_pretrained(
            MODEL, dtype=torch.bfloat16 if device.startswith("cuda") else torch.float32
        )
        model.to(device).eval()
        _LOADED.update(
            tok=tok, model=model, snac=SNAC.from_pretrained(SNAC_MODEL).eval().to(device)
        )
    return _LOADED


def synth_one(target, text, device="cuda:0", temperature=0.4, seed=7, voice="준서"):
    """Write one raw 24 kHz clip. Raises if the model emits malformed codes."""
    import soundfile as sf
    import torch

    ctx = _load(device)
    if seed is not None:
        torch.manual_seed(seed)
    ids = ctx["tok"](f"{voice}: {text}", return_tensors="pt").input_ids
    ids = torch.cat([torch.tensor([[START]]), ids, torch.tensor([END])], dim=1).to(device)
    with torch.no_grad():
        gen = ctx["model"].generate(
            ids,
            attention_mask=torch.ones_like(ids),
            max_new_tokens=1200,
            do_sample=True,
            temperature=temperature,
            top_p=0.8,
            repetition_penalty=1.3,
            eos_token_id=PAD,
        )
    row = gen[0].tolist()
    if AUDIO_START not in row:
        raise ValueError("No audio-start token in output")
    row = row[len(row) - 1 - row[::-1].index(AUDIO_START) + 1 :]
    row = [t for t in row if t != PAD]
    row = [t - CODE_BASE for t in row[: len(row) // 7 * 7]]
    if not row:
        raise ValueError("No audio codes produced")
    books = _redistribute(row)
    if any(int(b.min()) < 0 or int(b.max()) > 4095 for b in books):
        raise ValueError("SNAC code out of range; generation was malformed")
    with torch.no_grad():
        audio = ctx["snac"].decode([b.to(device) for b in books])
    sf.write(str(target), audio.squeeze().float().cpu().numpy(), SNAC_RATE)


def run(out_dir, text, peak, device, temperature, top_p, repetition_penalty, seed):
    sys.path.insert(0, str(Path(__file__).parent))
    import soundfile as sf
    import torch
    from build_prompt_audio import convert
    from snac import SNAC
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(MODEL)
    model = AutoModelForCausalLM.from_pretrained(
        MODEL, dtype=torch.bfloat16 if device.startswith("cuda") else torch.float32
    )
    model.to(device).eval()
    snac = SNAC.from_pretrained(SNAC_MODEL).eval().to(device)

    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for voice in VOICES:
        raw = out_dir / ("_raw_" + voice + ".wav")
        final = out_dir / ("orpheus_" + voice + ".wav")
        try:
            if seed is not None:
                torch.manual_seed(seed)
            ids = tok(f"{voice}: {text}", return_tensors="pt").input_ids
            ids = torch.cat([torch.tensor([[START]]), ids, torch.tensor([END])], dim=1).to(device)
            with torch.no_grad():
                gen = model.generate(
                    ids,
                    attention_mask=torch.ones_like(ids),
                    max_new_tokens=1200,
                    do_sample=True,
                    temperature=temperature,
                    top_p=top_p,
                    repetition_penalty=repetition_penalty,
                    eos_token_id=PAD,
                )

            row = gen[0].tolist()
            if AUDIO_START not in row:
                raise ValueError("No audio-start token in output")
            row = row[len(row) - 1 - row[::-1].index(AUDIO_START) + 1 :]
            row = [t for t in row if t != PAD]
            row = [t - CODE_BASE for t in row[: len(row) // 7 * 7]]
            if not row:
                raise ValueError("No audio codes produced")
            books = _redistribute(row)
            if any(int(b.min()) < 0 or int(b.max()) > 4095 for b in books):
                raise ValueError("SNAC code out of range; generation was malformed")
            with torch.no_grad():
                audio = snac.decode([b.to(device) for b in books])
            sf.write(str(raw), audio.squeeze().float().cpu().numpy(), SNAC_RATE)

            info = convert(raw, final, peak)
            raw.unlink()
            results.append(
                {
                    "voice": voice,
                    "seconds": info["seconds"],
                    "bytes": info["bytes"],
                    "clean": info["clean"],
                    "internal_gap_seconds": info["internal_gap_seconds"],
                    "edge_breath_blocks": info["edge_breath_blocks"],
                }
            )
        except Exception as exc:
            results.append({"voice": voice, "error": f"{type(exc).__name__}: {exc}"})
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    return results


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("out_dir", type=Path)
    p.add_argument("--text", default=DEFAULT_TEXT)
    p.add_argument("--peak", type=int, default=29500)
    p.add_argument("--device", default="cuda:0")
    p.add_argument("--temperature", type=float, default=0.6)
    p.add_argument("--top-p", type=float, default=0.8)
    p.add_argument("--repetition-penalty", type=float, default=1.3)
    p.add_argument("--seed", type=int)
    a = p.parse_args()
    run(a.out_dir, a.text, a.peak, a.device, a.temperature, a.top_p, a.repetition_penalty, a.seed)
