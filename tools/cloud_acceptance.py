"""Explicit paid acceptance using a DPAPI credential and a synthetic audio file.

Never run in CI automatically. Credential plaintext is neither an argument nor
part of the report. Keep this tool's working directory outside release inputs.
"""
import argparse
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.config import atomic_json
from src.core import cloud, reading


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--credential', required=True, type=Path)
    parser.add_argument('--audio', required=True, type=Path)
    parser.add_argument('--workspace', default='')
    parser.add_argument('--editor-model')
    parser.add_argument('--review-model')
    args = parser.parse_args()
    root = args.credential.resolve().parent
    models = cloud.model_settings()
    if args.editor_model:
        models['editor_model'] = args.editor_model
    if args.review_model:
        models['review_model'] = args.review_model
    models = cloud.normalize_models(models)
    cloud.DATA, cloud.SECRET, cloud.SETTINGS = root, args.credential.resolve(), root / 'settings.json'
    atomic_json(cloud.SETTINGS, dict(workspace=args.workspace, **models))
    report = dict(models=models, input='synthetic Windows speech', real_api=True, ok=False, stage='asr')
    client = cloud.CloudClient(models=models)
    started = time.monotonic()
    try:
        result = cloud.transcribe(args.audio, root / 'checkpoint.json', context='安装版短音频验收', client=client)
        report.update(asr=True, duration=result['recognition']['duration'],
                      transcript=''.join(s['text'] for s in result['segments']), stage='editor_and_review')
        units = [dict(id=0, start='00:00', text=report['transcript'])]
        blocks = reading.organize_chunk(units, client)
        report.update(editor_and_review=True, reading=blocks[0]['text'],
                      fallback_count=sum(b['mode'] == '原文排版' for b in blocks))
        if report['fallback_count']:
            report['error'] = '云端调用成功，但内容核对触发原文回退，不能视为完整整理通过'
        else:
            report.update(ok=True, stage='complete')
    except Exception as exc:
        report.update(error=f'{type(exc).__name__}: {exc}')
    finally:
        client.close()
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        atomic_json(root / 'acceptance-result.json', report)
    print('Real cloud acceptance:', 'passed' if report['ok'] else 'not passed', '| stage:', report['stage'])
    if report.get('error'):
        print(report['error'])
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
