"""Isolated 100% widget verification; never calls a model or user queue."""
from contextlib import ExitStack
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import ImageGrab
from src.paths import PROJECT_ROOT
from src.core import documents, library, reading, rename, textout, topics
from src.services import reading_service
from src.services.scheduler import Scheduler
import src.ui.batch_app as ui


def main():
    artifacts = PROJECT_ROOT / 'tests' / '.artifacts'
    artifacts.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='manual-ui-', dir=artifacts) as tmp, ExitStack() as stack:
        root = Path(tmp).resolve()
        assert root.is_relative_to(artifacts.resolve())
        output = root / 'output'
        for module in (ui, documents, library, rename, reading_service):
            stack.enter_context(patch.object(module, 'OUTPUT_ROOT', output))
        for module in (documents, rename):
            stack.enter_context(patch.object(module, 'RECORDS_DIR', root / 'records'))
        stack.enter_context(patch.object(ui, 'load_config', return_value=dict(topic='',geometry='1180x820+30+30')))
        stack.enter_context(patch.object(ui, 'save_config', lambda **kw: None))
        stack.enter_context(patch.object(ui, 'Scheduler', lambda **kw: Scheduler(state_path=root/'queue.json',autostart=False)))
        app = ui.BatchApp(autostart=False, demo_scale=1)
        try:
            assert app.organize_button._state == 'disabled'
            topics.create_topic('删除验收',output)
            app.refresh_topics(); app.topic_var.set('删除验收')
            (output/library.ORIGINAL/'删除验收').rmdir()
            app.refresh_topics()
            assert '删除验收' not in app.topic_box['values']
            assert app.topic_var.get() == ''
            source, _ = textout.save_result(output/library.ORIGINAL/'手动整理验收','当前稿件',
                'https://www.bilibili.com/video/BV17x411w7KC','B站','测试',
                [dict(start=0,end=5,text='预算100000元，不是10000元。Hello world。')],'manual')
            source=Path(source)
            before=source.read_bytes()
            app.refresh_library();app.open_document(source);app.mode_var.set(library.READING);app.render();app.update()
            assert app.organize_button._state=='normal'
            assert '点击上方“整理”' in app.reader.get('1.0','end')
            assert not library.reading_path(source).exists()
            app.organize_button._activate();app.update()
            assert app.scheduler.reading_service.has_work()
            assert app.organize_button._state=='disabled'
            with patch.object(reading,'enabled',return_value=False):
                while app.scheduler.reading_service.has_work():app.scheduler.reading_service.step(None)
            app.render();app.update()
            assert '100000' in app.reader.get('1.0','end')
            assert app.organize_button._state=='disabled'
            assert source.read_bytes()==before
            for size in ('1180x820','960x680'):
                app.geometry(size);app.update()
                assert app.organize_button.winfo_ismapped()
                assert app.organize_button.winfo_rootx()+app.organize_button.winfo_width() <= app.winfo_rootx()+app.winfo_width()
            captures=PROJECT_ROOT/'docs'/'qa';captures.mkdir(exist_ok=True)
            ImageGrab.grab(window=app.winfo_id()).save(captures/'manual-reading.jpg',quality=92)
            print('manual request, topic removal, disabled states, narrow window and original bytes verified')
        finally:
            app.scheduler.shutdown();app.destroy()


if __name__=='__main__':main()
