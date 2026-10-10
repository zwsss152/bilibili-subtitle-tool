"""Isolated desktop fixture: no network, ASR, real queue or user output edits."""
from pathlib import Path
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.paths import PROJECT_ROOT
from src.core import documents, rename, textout, library, topics
from src.services import reading_service
from src.core.links import parse_source
from src.services.scheduler import Scheduler
from src.services.tasks import Task, Page
import src.ui.batch_app as app_module


if __name__ == '__main__':
    artifacts = PROJECT_ROOT / 'tests' / '.artifacts'
    artifacts.mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='rename-ui-', dir=artifacts))
    app_module.OUTPUT_ROOT = documents.OUTPUT_ROOT = rename.OUTPUT_ROOT = library.OUTPUT_ROOT = reading_service.OUTPUT_ROOT = root / 'output'
    documents.RECORDS_DIR = rename.RECORDS_DIR = root / 'records'
    app_module.create_topic = lambda name: topics.create_topic(name, root / 'output')
    app_module.load_config = lambda: dict(topic='验收主题', geometry='1180x820+30+30')
    app_module.save_config = lambda **values: None
    app_module.Scheduler = lambda **kwargs: Scheduler(state_path=root / 'queue.json', autostart=False)
    app = app_module.BatchApp(autostart=False)
    app._save_settings = lambda: None
    app.title('文字稿 · 重命名验收')
    app.geometry('1180x820+30+30')
    txt, raw = textout.save_result(root / 'output' / library.ORIGINAL / '验收主题', '示例稿件',
        'https://www.bilibili.com/video/BV17x411w7KC', 'B站', '界面验收样例',
        [dict(start=1,end=4,text='预算100000元，Hello world。这是重命名验收稿件。')], 'fixture')
    for index, status in enumerate(('完成', '识别中', '完成')):
        job = Task.create(parse_source(f'https://www.gcores.com/radios/{196305 + index}'), '验收主题')
        job.title = ('示例稿件', '尚未完成的稿件', '文件不存在的任务')[index]
        job.status = status
        job.pages = [Page(1, job.title, job.source_url, status=status, progress=100 if status == '完成' else 30,
            txt=str(Path(txt).relative_to(PROJECT_ROOT)) if index == 0 else
                str((root / 'output' / 'missing.txt').relative_to(PROJECT_ROOT)) if index == 2 else '',
            raw=str(Path(raw).relative_to(PROJECT_ROOT)) if index == 0 else '')]
        app.scheduler.jobs.append(job)
    app.scheduler._persist()
    app.refresh_library()
    app.open_document(Path(txt))
    app.mainloop()
