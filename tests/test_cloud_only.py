"""Cloud migration must never silently upload legacy local work."""
import ast
import queue
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from src import config
from src.config import atomic_json
from src.paths import PROJECT_ROOT
from src.core.links import parse_source
from src.services.scheduler import Scheduler
from src.services.recognizer import worker
from src.services.tasks import Task, Page


class CloudOnlyTests(unittest.TestCase):
    def setUp(self):
        root = PROJECT_ROOT / 'tests' / '.artifacts'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'queue.json'

    def legacy(self, include_engine=True):
        job = Task.create(parse_source('BV17x411w7KC'), '主题')
        completed = self.root / 'completed.txt'
        completed.write_text('已有稿件', encoding='utf-8')
        job.pages = [Page(1, 'P1', 'url', status='完成', txt=str(completed)),
                     Page(2, 'P2', 'url', status='识别中', reading_requested=True)]
        data = job.to_dict()
        data['engine'] = 'local'
        if not include_engine:
            data.pop('engine')
        atomic_json(self.state, dict(version=2, jobs=[data]))
        return Scheduler(state_path=self.state, autostart=False)

    def test_legacy_pending_page_waits_for_explicit_retry(self):
        for explicit in (True, False):
            scheduler = self.legacy(explicit)
            job = scheduler.jobs[0]
            self.assertEqual(job.engine, 'local')
            self.assertEqual([p.status for p in job.pages], ['完成', '失败'])
            self.assertFalse(job.pages[1].reading_requested)
            self.assertIn('点击重试', job.detail)
            self.assertFalse(scheduler.reading_service.has_work())
            scheduler.shutdown()

    def test_retry_converts_only_unfinished_pages_and_keeps_original(self):
        scheduler = self.legacy()
        job = scheduler.jobs[0]
        before = Path(job.pages[0].txt).read_bytes()
        with patch('src.services.scheduler.configured', return_value=True):
            scheduler.retry({job.jid})
        self.assertEqual(job.engine, 'cloud')
        self.assertEqual([p.status for p in job.pages], ['完成', '排队中'])
        self.assertEqual(Path(job.pages[0].txt).read_bytes(), before)

    def test_retry_without_credentials_keeps_legacy_task_blocked(self):
        scheduler = self.legacy()
        job = scheduler.jobs[0]
        with patch('src.services.scheduler.configured', return_value=False):
            scheduler.retry({job.jid})
        self.assertEqual(job.engine, 'local')
        self.assertEqual(job.pages[1].status, '失败')

    def test_no_key_prevents_manual_reading_request_before_persistence(self):
        scheduler = Scheduler(state_path=self.state, autostart=False)
        with patch('src.services.reading_service.configured', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'API Key'):
                scheduler.request_reading(self.root / 'unused.txt')
        self.assertFalse(scheduler.reading_service.has_work())
        self.assertFalse(self.state.exists())

    def test_worker_rejects_local_requests_without_loading_models(self):
        inputs, outputs = queue.Queue(), queue.Queue()
        inputs.put(dict(identity=('job', 1), engine='local'))
        inputs.put(None)
        worker(inputs, outputs, threading.Event())
        event, identity, message = outputs.get_nowait()
        self.assertEqual(event, 'error')
        self.assertIn('本地模型已移除', message)

    def test_legacy_default_setting_becomes_cloud(self):
        path = self.root / 'settings.json'
        atomic_json(path, dict(engine='local', topic='旧主题'))
        with patch.object(config, 'CONFIG_PATH', path):
            self.assertEqual(config.load_config()['engine'], 'cloud')
            self.assertEqual(config.load_config()['topic'], '旧主题')

    def test_runtime_has_no_local_inference_imports(self):
        forbidden = {'faster_whisper', 'ctranslate2', 'sherpa_onnx', 'torch',
                     'src.core.asr', 'src.core.models', 'src.core.editor_model'}
        for path in (PROJECT_ROOT / 'src').rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ''] if isinstance(node, ast.ImportFrom) else []
                self.assertFalse(forbidden.intersection(names), str(path))
