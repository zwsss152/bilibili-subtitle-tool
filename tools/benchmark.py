"""Local measurements; human references are required before selecting a new model."""
import argparse
import gc
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.core import asr
from src.config import atomic_json, read_json
from src.paths import PROJECT_ROOT


def canonical(text):
    from src.core.textout import to_simplified, content_key
    return content_key(to_simplified(text)).lower()


def distance(first, second):
    previous = list(range(len(second) + 1))
    for i, a in enumerate(first, 1):
        row = [i]
        for j, b in enumerate(second, 1):
            row.append(min(row[-1] + 1, previous[j] + 1, previous[j - 1] + (a != b)))
        previous = row
    return previous[-1]


class GPURecorder:
    """Sample total device VRAM; this includes any other apps using the GPU."""
    def __init__(self):
        self.values = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stop.is_set():
            try:
                output = subprocess.check_output(['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits'],
                                                 text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self.values.append(int(output.strip().splitlines()[0]))
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            self.stop.wait(0.1)

    def finish(self):
        self.stop.set()
        self.thread.join(timeout=3)
        return max(self.values) if self.values else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="*", default=["medium", "large-v3-turbo", "large-v3"])
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--compute", default="float16")
    parser.add_argument("--beam", type=int, default=3)
    parser.add_argument('--legacy', action='store_true', help='Measure the original medium decoding settings')
    args = parser.parse_args()
    directory = PROJECT_ROOT / "benchmarks"
    samples = [item for item in read_json(directory / "samples.json", []) if "path" in item]
    references = directory / "references"
    references.mkdir(exist_ok=True)
    for name in args.models:
        key = f"{name}-{args.compute}-batch{args.batch}-beam{args.beam}" + ('-legacy' if args.legacy else '')
        profile = dict(model=name, device="cuda", compute_type=args.compute, batch_size=args.batch, beam_size=args.beam)
        records = []
        memory = GPURecorder()
        memory.thread.start()
        print(f"Benchmark {key}", flush=True)
        loaded = time.perf_counter()
        asr.get_asr_model(name, device="cuda", compute_type=args.compute)
        load_seconds = time.perf_counter() - loaded
        for sample in samples:
            if args.legacy:
                started = time.perf_counter()
                model = asr.get_asr_model(name, device='cuda', compute_type=args.compute)
                segments, info = model.transcribe(str(PROJECT_ROOT / sample['path']), batch_size=args.batch,
                    beam_size=3, vad_filter=True, condition_on_previous_text=True, repetition_penalty=1.15,
                    no_repeat_ngram_size=4, hallucination_silence_threshold=2.0, language_detection_segments=3,
                    initial_prompt='以下是一段普通话对话的转写，话题可能涉及人工智能、游戏、科技等，请输出带正确标点符号的简体中文。',
                    hotwords=sample['title'][:60])
                items = [dict(start=s.start, end=s.end, text=s.text.strip()) for s in segments]
                result = dict(segments=items, recognition={**profile, 'legacy_settings':True,
                    'duration':info.duration, 'language':info.language, 'elapsed_seconds':time.perf_counter()-started})
            else:
                result = asr.transcribe(PROJECT_ROOT / sample["path"], profile, hotwords=sample["title"], log=lambda m: print(m, flush=True))
            text = "".join(segment["text"] for segment in result["segments"])
            reference_path = references / f"{sample['id']}.txt"
            reference = reference_path.read_text(encoding="utf-8") if reference_path.exists() else None
            cer = distance(canonical(reference), canonical(text)) / max(1, len(canonical(reference))) if reference else None
            output = directory / "results" / key
            output.mkdir(parents=True, exist_ok=True)
            atomic_json(output / f"{sample['id']}.json", result)
            (output / f"{sample['id']}.txt").write_text(text, encoding="utf-8")
            seconds = result["recognition"]["elapsed_seconds"]
            records.append(dict(sample=sample["id"], duration=sample["duration"], elapsed_seconds=seconds,
                                actual_profile=result['recognition'], characters=len(text), cer=cer, human_reference_available=bool(reference)))
            print(f"{sample['id']}: {seconds:.3f}s / {sample['duration']}s audio; CER={cer}", flush=True)
            atomic_json(directory / f"{key}.json", dict(profile=profile, model_load_seconds=load_seconds, samples=records,
                                                        quality_verified=all(r["human_reference_available"] for r in records)))
        print(f"Total {key}: {sum(r['elapsed_seconds'] for r in records):.3f}s", flush=True)
        atomic_json(directory / f"{key}.json", dict(profile=profile, model_load_seconds=load_seconds, samples=records,
                                                    peak_device_vram_mib=memory.finish(),
                                                    quality_verified=all(r["human_reference_available"] for r in records)))


if __name__ == "__main__":
    main()
