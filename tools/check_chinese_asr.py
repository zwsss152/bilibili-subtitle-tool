"""Evaluate an independent Chinese recognizer on the user's real audio."""
import json
from pathlib import Path
import sys
import tarfile
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.core.models import download_file
from src.paths import MODELS_DIR, PROJECT_ROOT
from src.config import atomic_json


def main():
    folder = MODELS_DIR / 'sense-voice'
    folder.mkdir(exist_ok=True)
    if not (folder / 'model.int8.onnx').exists():
        archive = folder / 'download.tar.bz2'
        download_file('https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/'
                      'sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2', archive)
        with tarfile.open(archive) as bundle:
            for filename in ['model.int8.onnx', 'tokens.txt']:
                member = next(m for m in bundle.getmembers() if m.isfile() and Path(m.name).name == filename)
                with bundle.extractfile(member) as source, (folder / filename).open('wb') as dest:
                    import shutil
                    shutil.copyfileobj(source, dest)
        archive.unlink()
    import sherpa_onnx
    from faster_whisper.audio import decode_audio
    engine = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(folder / 'model.int8.onnx'), tokens=str(folder / 'tokens.txt'),
        language='zh', num_threads=8, use_itn=True)
    results = []
    samples = json.loads((PROJECT_ROOT / 'benchmarks/samples.json').read_text(encoding='utf-8'))
    samples.insert(0, dict(id='reported-error', path='benchmarks/reported-error/opening.wav'))
    for sample in samples:
        audio = decode_audio(str(PROJECT_ROOT / sample['path']))
        started = time.perf_counter()
        texts = []
        for begin in range(0, len(audio), 20 * 16000):
            stream = engine.create_stream()
            stream.accept_waveform(16000, audio[begin:begin+20*16000])
            engine.decode_stream(stream)
            texts.append(stream.result.text)
        results.append(dict(id=sample['id'], text=''.join(texts), elapsed=time.perf_counter()-started))
        atomic_json(PROJECT_ROOT / 'benchmarks/reported-error/sense-voice.json', results)
        print(json.dumps(results[-1], ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
