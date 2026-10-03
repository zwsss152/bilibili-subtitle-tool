"""Compare decoding on the actual homophone error reported by the user."""
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.paths import PROJECT_ROOT
from src.config import atomic_json, load_config
from src.core.download import download_audio, find_ffmpeg
from src.core import asr


def main():
    directory = PROJECT_ROOT / 'benchmarks' / 'reported-error'
    directory.mkdir(parents=True, exist_ok=True)
    clip = directory / 'opening.wav'
    if not clip.exists():
        audio = download_audio('https://www.bilibili.com/video/BV1fYtyz1E2d', 'source', directory,
                               lambda *args: None, cookie=load_config().get('cookie', ''))
        subprocess.run([find_ffmpeg(), '-y', '-i', audio, '-t', '60', '-ar', '16000',
                        '-ac', '1', str(clip)], check=True, capture_output=True)
        Path(audio).unlink()
    title = '游戏策划的面试常见问题分享，提前准备起来！'
    results = []
    for model, beam in [('medium', 3), ('large-v3-turbo', 5), ('large-v3', 5)]:
        profile = dict(model=model, device='cuda', compute_type='float16', batch_size=8, beam_size=beam)
        result = asr.transcribe(clip, profile=profile, hotwords=title, log=print)
        text = ''.join(s['text'] for s in result['segments'])
        results.append(dict(profile=profile, text=text, result=result))
        atomic_json(directory / 'comparison.json', results)
        print(json.dumps(dict(model=model, beam=beam, text=text, recognition=result['recognition']),
                         ensure_ascii=True), flush=True)
    for model in ['large-v3-turbo', 'large-v3']:
        loaded = asr.get_asr_model(model, device='cuda', compute_type='float16').model
        for beam in [5, 10]:
            started = time.perf_counter()
            segments, info = loaded.transcribe(str(clip), language='zh', beam_size=beam,
                vad_filter=True, condition_on_previous_text=True, initial_prompt=title)
            output = [dict(start=s.start, end=s.end, text=s.text) for s in segments]
            results.append(dict(profile=dict(model=model, beam_size=beam, sequential=True),
                text=''.join(s['text'] for s in output), result=dict(segments=output,
                recognition=dict(elapsed_seconds=time.perf_counter()-started))))
            atomic_json(directory / 'comparison.json', results)
            print(json.dumps(results[-1], ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
