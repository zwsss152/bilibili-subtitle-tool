"""Visible isolated UI fixture for desktop verification; never starts network/ASR."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.ui.batch_app import BatchApp
from src.services.tasks import Task, Page
from src.core.links import parse_source
from src.paths import PROJECT_ROOT
import tempfile
import shutil
import src.ui.batch_app as app_module
from src.core.topics import create_topic
from src.core.documents import record_path

if __name__ == "__main__":
    scale = float(sys.argv[1]) if len(sys.argv) > 1 else 1
    # Isolate folder creation and library refresh as well as queue/config writes.
    original_library = app_module.OUTPUT_ROOT
    artifact_root = PROJECT_ROOT / 'tests' / '.artifacts'
    artifact_root.mkdir(exist_ok=True)
    app_module.OUTPUT_ROOT = Path(tempfile.mkdtemp(prefix='ui-library-', dir=artifact_root))
    sample = next(original_library.rglob('*.txt'), None)
    if sample:
        target = app_module.OUTPUT_ROOT / '未分类' / sample.name
        target.parent.mkdir()
        shutil.copy2(sample, target)
        record = record_path(sample)
        if not record.exists():
            record = sample.with_suffix('.json')
        if record.exists():
            record_path(target).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(record, record_path(target))
    app_module.create_topic = lambda name: create_topic(name, app_module.OUTPUT_ROOT)
    app = BatchApp(autostart=False, demo_scale=scale)
    app._save_settings = lambda: None
    app.title(f"文字稿 · 界面验收 {scale:.0%}")
    app.geometry("1280x900+30+30")
    for index, status in enumerate(("完成", "识别中", "下载中", "等待识别", "失败")):
        job = Task.create(parse_source(f"https://www.gcores.com/radios/{196305 + index}"), "机核")
        job.title = ["AI在游戏里能做什么？你希望游戏里出现什么样的AI？", "红海危局背后：破碎的也门与强势的胡塞", "游戏访谈：技术、创意与实践", "长标题节目：在复杂的项目中如何开展协作", "一次可重试的网络请求"][index]
        job.status = status
        job.pages = [Page(1, job.title, job.source_url, status=status, progress=45)]
        job.detail = "界面验收样例，不执行网络任务"
        app.scheduler.jobs.append(job)
    if app.documents:
        app.open_document(next((p for p in app.documents if p.parent.name == "机核"), app.documents[0]))
    # QA windows must not persist fixture jobs or change the user configuration.
    def close():
        app.closing = True
        app.destroy()
    app.protocol("WM_DELETE_WINDOW", close)
    app.mainloop()
