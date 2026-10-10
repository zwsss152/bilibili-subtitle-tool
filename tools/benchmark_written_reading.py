"""Compare complete written drafts to actual original TXT, entirely locally."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.config import atomic_json, read_json
from src.core import reading
from src.core.editor_model import LocalEditor
from src.core.library import originals
from src.core.textout import read_document
from src.paths import PROJECT_ROOT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', required=True)
    parser.add_argument('--preview', action='store_true', help='Only the first three units per manuscript')
    parser.add_argument('--files', type=int, default=6)
    parser.add_argument('--model',choices=('qwen3-8b',),default='qwen3-8b')
    args = parser.parse_args()
    directory = PROJECT_ROOT / 'benchmarks' / 'results' / args.run
    directory.mkdir(parents=True, exist_ok=True)
    import src.core.editor_model as editor_module
    code_hash = hashlib.sha256(Path(reading.__file__).read_bytes() + Path(editor_module.__file__).read_bytes()).hexdigest()
    summary_path = directory / 'summary.json'
    previous = read_json(summary_path, {})
    if previous and previous.get('code_hash') != code_hash:
        raise RuntimeError('规则已变化，请使用新的 --run 名称，避免复用旧结果')
    summary = dict(version=reading.VERSION, code_hash=code_hash, preview=args.preview, model_id=args.model, results=[])
    editor = LocalEditor(args.model)
    try:
        started = time.monotonic()
        editor.start()
        summary['load_seconds'] = round(time.monotonic() - started, 3)
        print('MODEL READY', flush=True)
        for number, path in enumerate(sorted(originals())[:args.files]):
            document = read_document(path)
            units = reading.source_units(document)
            if args.preview:
                units = units[:3]
            key = hashlib.sha256(str(path).encode()).hexdigest()[:12]
            source_hash = reading.fingerprint(document)
            blocks = []
            trace = []
            class TracedEditor:
                def generate(self, system, content, schema, cancel=None):
                    result = editor.generate(system, content, schema, cancel)
                    trace.append(dict(input=json.loads(content), result=result, audit='checks' in result))
                    return result
            started = time.monotonic()
            for chunk_number, batch in enumerate(reading.chunks(units)):
                checkpoint = directory / f'{key}-chunk-{chunk_number}.json'
                saved = read_json(checkpoint, {})
                if saved and saved.get('source_hash') == source_hash:
                    output = saved['blocks']
                else:
                    context = units[batch[0]['id']-1]['text'][-300:] if batch[0]['id'] else ''
                    output = reading.organize_chunk(batch, TracedEditor(), context)
                    atomic_json(checkpoint, dict(source_hash=source_hash, blocks=output, trace=trace))
                trace.clear()
                blocks.extend(output)
                print(f'{number+1}/6 {path.stem[:24]} chunk {chunk_number+1}: {len(blocks)}/{len(units)} units, '
                      f'fallback {sum(b["mode"]=="原文排版" for b in output)}', flush=True)
            result = dict(original=str(path.relative_to(PROJECT_ROOT)), source_hash=source_hash,
                seconds=round(time.monotonic()-started, 3), units=len(units),
                changed=sum(reading._letters(a['text']) != reading._letters(b['text']) for a,b in zip(units,blocks)),
                fallback=sum(b['mode']=='原文排版' for b in blocks))
            atomic_json(directory / f'{key}.json', dict(original=units, reading=blocks, stats=result))
            (directory / f'{key}.txt').write_text(reading.render_reading(document, blocks, '完整书面稿验收'), encoding='utf-8')
            summary['results'].append(result)
            atomic_json(summary_path, summary)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        editor.close()


if __name__ == '__main__':
    main()
