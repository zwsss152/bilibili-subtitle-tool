"""Isolated local/cloud evaluation. Never updates the user's library or profiles."""
import argparse
import hashlib
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.config import atomic_json, read_json
from src.paths import PROJECT_ROOT
from src.core import reading


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--engine', choices=('local', 'cloud'), required=True)
    parser.add_argument('--run', required=True)
    parser.add_argument('--reported', action='store_true')
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--reuse-asr', help='Verified prior run in benchmarks/results; only reuse matching audio hashes')
    parser.add_argument('--cases', help='Subset of fixed sample names, comma separated')
    parser.add_argument('--audio', help='Additional test audio inside the project')
    parser.add_argument('--title', default='游戏、人工智能、军事或访谈；仅如实识别音频')
    args = parser.parse_args()
    root = PROJECT_ROOT / 'benchmarks' / 'results'
    directory = (root / args.run).resolve()
    if not directory.is_relative_to(root.resolve()):
        raise ValueError('测试输出必须在 benchmarks/results 内')
    directory.mkdir(parents=True, exist_ok=True)
    if args.reported:
        cases = [('age', PROJECT_ROOT/'benchmarks/reported-damage/age.wav'),
                 ('missing', PROJECT_ROOT/'benchmarks/reported-damage/missing.wav')]
    else:
        cases = [(name, PROJECT_ROOT/'benchmarks/clips'/f'{name}.wav') for name in
                 ('ai_1','ai_2','military_1','military_2','interview_1','interview_2')]
    if args.cases:
        requested = set(args.cases.split(','))
        if not requested.issubset({name for name,path in cases}):
            raise ValueError('未知样本名')
        cases = [(name,path) for name,path in cases if name in requested]
    if args.audio:
        audio = (PROJECT_ROOT/args.audio).resolve()
        if not audio.is_relative_to(PROJECT_ROOT.resolve()) or not audio.is_file():
            raise ValueError('测试音频必须位于本项目')
        cases = [('full',audio)]
    implementation = ['reading', 'textout', 'cloud'] if args.engine == 'cloud' else [
        'reading', 'textout', 'asr', 'chinese_review', 'editor_model']
    signature = hashlib.sha256(b''.join((PROJECT_ROOT/f'src/core/{name}.py').read_bytes()
        for name in implementation)).hexdigest()
    previous = read_json(directory/'summary.json', {})
    if previous and previous.get('code_hash') != signature:
        raise ValueError('实现已变化，请换用新的测试名称')
    results = []
    for name, path in cases:
        destination = directory/f'{name}.json'
        saved = read_json(destination, {})
        if not saved and args.reuse_asr:
            prior = (root/args.reuse_asr).resolve()
            if not prior.is_relative_to(root.resolve()):
                raise ValueError('复用记录必须在 benchmarks/results 内')
            reused = read_json(prior/f'{name}.json', {})
            if (reused.get('source_hash') == hashlib.sha256(path.read_bytes()).hexdigest()
                    and reused.get('asr',{}).get('recognition',{}).get('engine') == args.engine):
                saved = {k:reused[k] for k in ('source_hash','asr','asr_seconds')}
                atomic_json(destination,saved)
        if saved.get('source_hash') != hashlib.sha256(path.read_bytes()).hexdigest():
            started = time.monotonic()
            if args.engine == 'cloud':
                from src.core.cloud import transcribe
                output = transcribe(path, directory/f'{name}-asr.json',
                    context=args.title, progress=lambda p,s: print(name,s,flush=True))
            else:
                from src.core.asr import transcribe
                output = transcribe(path, progress=lambda p,s: print(name,s,flush=True))
            saved = dict(source_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
                         asr=output, asr_seconds=round(time.monotonic()-started,3))
            atomic_json(destination, saved)
        results.append((name, path, saved))
        print(name, 'ASR', saved['asr_seconds'], 'seconds', flush=True)
    # Release Whisper's GPU before loading the local editor.
    if args.engine == 'local':
        from src.core.asr import unload_model
        unload_model()
    editor = None
    try:
        if args.write:
            if args.engine == 'cloud':
                from src.core.cloud import CloudClient
                editor = CloudClient()
            else:
                from src.core.editor_model import LocalEditor
                editor = LocalEditor()
            editor.start()
            for name, path, saved in results:
                if 'reading' not in saved:
                    started = time.monotonic()
                    trace = []
                    class Traced:
                        engine = args.engine
                        def generate(self,system,content,schema,cancel=None):
                            value = editor.generate(system,content,schema,cancel)
                            trace.append(dict(input=content, result=value, audit='checks' in schema.get('properties',{})))
                            return value
                    from src.core.textout import build_paragraphs
                    units = [dict(id=i, start=str(start), text=text) for i,(start,text) in
                             enumerate(build_paragraphs(saved['asr']['segments']))]
                    blocks = []
                    for batch in reading.chunks(units):
                        blocks.extend(reading.organize_chunk(batch, Traced(),
                            units[batch[0]['id']-1]['text'][-300:] if batch[0]['id'] else ''))
                    saved.update(reading=blocks, trace=trace, reading_seconds=round(time.monotonic()-started,3),
                        fallback=sum(b['mode']=='原文排版' for b in blocks), units=len(units))
                    atomic_json(directory/f'{name}.json', saved)
                    document = dict(title=name, url='测试片段')
                    (directory/f'{name}.txt').write_text(reading.render_reading(document,blocks,'测试，不替换原稿'),encoding='utf-8')
                print(name,'READING',saved['reading_seconds'],'seconds; fallback',saved['fallback'], '/',saved['units'],flush=True)
    finally:
        if editor:
            editor.close()
        atomic_json(directory/'summary.json',dict(engine=args.engine, code_hash=signature,
            results=[dict(name=n,**{k:v for k,v in s.items() if k not in ('asr','reading','trace')}) for n,p,s in results],
            human_validated=False))


if __name__ == '__main__':
    main()
