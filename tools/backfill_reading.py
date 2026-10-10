"""Backfill history only after the current policy is explicitly validated."""
from pathlib import Path
import hashlib
import json
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.main import acquire_instance
import src.main as bootstrap
from src.paths import OUTPUT_ROOT, QUEUE_STATE_PATH, PROJECT_ROOT
from src.config import atomic_json
from src.core import reading, rename
from src.core.documents import migrate_records
from src.core.library_migration import migrate_library
from src.core.library import originals, original_path, reading_path
from src.services.scheduler import Scheduler


def main():
    if not acquire_instance():
        raise RuntimeError('应用正在运行，不能同时迁移或整理，请关闭应用后再执行')
    scheduler=None
    try:
        expected={str(original_path(p)):hashlib.sha256(p.read_bytes()).hexdigest() for p in originals()}
        rename.recover(QUEUE_STATE_PATH,print)
        migrate_records(print)
        migrate_library(QUEUE_STATE_PATH,print)
        if not reading.history_enabled():
            raise RuntimeError('当前整理规则尚未通过验收，禁止批量改写历史稿件；旧版验收不能授权新版规则')
        scheduler=Scheduler(autostart=False)
        scheduler.reading_service.scan()
        while scheduler.reading_service.has_work():
            scheduler.reading_service.step(scheduler.editor)
            while not scheduler.events.empty():
                _,message=scheduler.events.get_nowait()
                if message.startswith(('已整理','阅读稿整理失败','本地整理不可用')):print(message,flush=True)
        results=[]
        for path in originals():
            assert hashlib.sha256(path.read_bytes()).hexdigest()==expected[str(path)],'原稿字节发生变化'
            dest=reading_path(path)
            assert dest.is_file(),f'阅读稿缺失：{path.name}'
            assert dest.name==path.name
            document=__import__('src.core.textout',fromlist=['read_document']).read_document(path)
            progress=document['raw']['reading']
            assert progress['source_hash']==reading.fingerprint(document)
            results.append(dict(original=str(path.relative_to(PROJECT_ROOT)),reading=str(dest.relative_to(PROJECT_ROOT)),
                units=len(progress['blocks']),fallback=progress.get('fallback_count'),device=progress.get('device'),status=progress['status']))
        atomic_json(PROJECT_ROOT/'benchmarks'/'results'/'reading-final'/'backfill.json',
                    dict(originals_unchanged=True,results=results))
        print('BACKFILL READY',len(results),'originals unchanged',flush=True)
    finally:
        if scheduler:scheduler.shutdown()
        if bootstrap._INSTANCE:
            bootstrap._INSTANCE.close()
            bootstrap._INSTANCE=None


if __name__=='__main__':main()
