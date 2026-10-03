"""Acquire the six fixed two-minute acceptance clips, retaining them only for QA."""
import json
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.core.gcores import fetch_gcores_radio
from src.core.download import download_direct_audio, download_audio, find_ffmpeg, cleanup_owned
from src.paths import PROJECT_ROOT, ASR_CACHE_DIR
from src.config import atomic_json, load_config

PROGRAMS = [("ai", "gcores", "196305"), ("military", "gcores", "220465"),
            ("interview", "bilibili", "BV1wfLUzwEUv")]


def main():
    root = PROJECT_ROOT / "benchmarks"
    (root / "clips").mkdir(parents=True, exist_ok=True)
    manifest = []
    for name, platform, ref in PROGRAMS:
        directory = ASR_CACHE_DIR / "job_benchmark" / name
        try:
            if platform == "gcores":
                info = fetch_gcores_radio(ref)
                audio = download_direct_audio(info["audio_url"], directory / "audio.mp3", lambda *a: None)
                title = info["title"]
                url = f"https://www.gcores.com/radios/{ref}"
            else:
                url = f"https://www.bilibili.com/video/{ref}"
                audio = download_audio(url, "audio", directory, lambda *a: None, cookie=load_config()["cookie"])
                title = "战斗策划的入行与发展"
            for index, start in enumerate((120, 600), 1):
                destination = root / "clips" / f"{name}_{index}.wav"
                subprocess.run([find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-ss", str(start),
                                "-i", str(audio), "-t", "120", "-ac", "1", "-ar", "16000", str(destination)], check=True)
                manifest.append(dict(id=f"{name}_{index}", title=title, url=url, start=start,
                                     duration=120, path=str(destination.relative_to(PROJECT_ROOT))))
            print(f"Prepared {name}", flush=True)
        except Exception as exc:
            print(f"Sample {name} unavailable: {type(exc).__name__}: {exc}", flush=True)
            manifest.append(dict(id=name, url=ref, error=str(exc)))
        finally:
            cleanup_owned(directory)
            atomic_json(root / "samples.json", manifest)


if __name__ == "__main__":
    main()
