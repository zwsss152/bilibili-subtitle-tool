"""Run local-only editing samples and a full existing transcript; save comparisons."""
from pathlib import Path
import argparse
import json
import subprocess
import sys
import threading
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.core.editor_model import LocalEditor, MODEL_REVISION
from src.core import reading
from src.core.textout import normalize_text, read_document
from src.paths import PROJECT_ROOT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--long', action='store_true')
    parser.add_argument('--run', default='reading-v1')
    args = parser.parse_args()
    directory = PROJECT_ROOT / 'benchmarks' / 'results' / args.run
    directory.mkdir(parents=True, exist_ok=True)
    cases = []
    for name in ('ai_1','ai_2','military_1','military_2','interview_1','interview_2'):
        record = json.loads((PROJECT_ROOT / 'benchmarks' / 'results' / 'large-v3-turbo-float16-batch8-beam3' / (name+'.json')).read_text(encoding='utf-8'))
        units = [dict(id=i, start=str(s['start']), text=normalize_text(s['text'])) for i,s in enumerate(record['segments']) if s['text'].strip()]
        cases.append((name, units))
    if args.long:
        longest = max((PROJECT_ROOT/'文字稿').rglob('*.txt'), key=lambda p:p.stat().st_size)
        cases.append(('long', reading.source_units(read_document(longest))))
    editor, stopped, samples = LocalEditor(), threading.Event(), []
    def monitor():
        while not stopped.is_set():
            try:
                result = subprocess.check_output(['nvidia-smi','--query-gpu=memory.used','--format=csv,noheader,nounits'],
                                                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0), timeout=4)
                samples.append(int(result.decode().strip()))
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            stopped.wait(2)
    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    summary = dict(model_revision=MODEL_REVISION, results=[])
    try:
        started = time.monotonic()
        editor.start()
        summary['load_seconds'] = round(time.monotonic()-started,3)
        print('MODEL READY',flush=True)
        for name, units in cases:
            started = time.monotonic()
            blocks = []
            for number, batch in enumerate(reading.chunks(units)):
                checkpoint = directory / f'{name}-chunk-{number}.json'
                if checkpoint.exists():
                    output = json.loads(checkpoint.read_text(encoding='utf-8'))
                else:
                    output = reading.organize_chunk(batch, editor, units[batch[0]['id']-1]['text'][-300:] if batch[0]['id'] else '')
                    checkpoint.write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
                blocks.extend(output)
                print(name,'chunk',number+1,'units',len(blocks),'fallback',sum(b['mode']=='原文排版' for b in output),flush=True)
            result = dict(name=name, seconds=round(time.monotonic()-started,3), units=len(units),
                          characters=sum(len(u['text']) for u in units),fallback=sum(b['mode']=='原文排版' for b in blocks))
            (directory / (name+'.json')).write_text(json.dumps(dict(original=units,reading=blocks,stats=result),ensure_ascii=False,indent=2),encoding='utf-8')
            summary['results'].append(result)
            summary['gpu_total_peak_mib'] = max(samples,default=0)
            (directory/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(result),flush=True)
    finally:
        stopped.set()
        editor.close()
        thread.join(timeout=5)


if __name__=='__main__':main()
