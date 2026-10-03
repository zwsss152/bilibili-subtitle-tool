"""Regenerate the reported transcript while backing up the previous version."""
import json
from pathlib import Path
import shutil
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.core.asr import transcribe
from src.core.download import download_audio, cleanup_owned
from src.core.textout import save_result, read_document
from src.core.documents import record_path
from src.paths import PROJECT_ROOT, ASR_CACHE_DIR, LOGS_DIR
from src.config import atomic_json, load_config


def main():
    original = next((PROJECT_ROOT / '文字稿').rglob('*9edbe472_p1.txt'))
    document = read_document(original)['raw']
    if document is None:
        raise RuntimeError('稿件缺少内部识别记录，无法重新生成')
    directory = ASR_CACHE_DIR / 'quality-improvement' / 'reported'
    directory.mkdir(parents=True, exist_ok=True)
    try:
        audio = download_audio(document['url'], 'source', directory, lambda *args: None,
                               cookie=load_config().get('cookie', ''))
        result = transcribe(audio, hotwords=document['title'], log=print)
        atomic_json(PROJECT_ROOT / 'benchmarks/reported-error/full-production.json', result)
        if not any('旅了个游' in segment['text'] for segment in result['segments']):
            raise RuntimeError('原报告错误尚未改善，未覆盖原稿')
        backup = LOGS_DIR / 'transcript-backups' / 'before-chinese-review'
        backup.mkdir(parents=True, exist_ok=True)
        for path in (original, record_path(original)):
            target = backup / path.name
            if path.exists() and not target.exists():
                shutil.copy2(path, target)
        txt, raw = save_result(original.parent, document['title'], document['url'], document['platform'],
            '语音识别（large-v3-turbo + 中文语音复核）', result['segments'], '9edbe472_p1', result['recognition'], print)
        print(json.dumps(dict(txt=str(txt), raw=str(raw), recognition=result['recognition']), ensure_ascii=True))
    finally:
        cleanup_owned(directory)


if __name__ == '__main__':
    main()
