"""Rename transactions use isolated files and never change the user's transcripts."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from src.paths import PROJECT_ROOT
from src.core import documents, rename, textout
from src.core.links import parse_source
from src.services.scheduler import Scheduler
from src.services.tasks import Page, Task


class RenameTests(unittest.TestCase):
    def setUp(self):
        artifacts = PROJECT_ROOT / 'tests' / '.artifacts'
        artifacts.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='rename-', dir=artifacts)
        self.root = Path(self.temp.name).resolve()
        assert self.root.is_relative_to(artifacts.resolve())
        self.addCleanup(self.temp.cleanup)
        self.output, self.records = self.root / 'output', self.root / 'records'
        for module in (documents, rename):
            for key, value in (('OUTPUT_ROOT', self.output), ('RECORDS_DIR', self.records)):
                patcher = patch.object(module, key, value)
                patcher.start()
                self.addCleanup(patcher.stop)
        patcher = patch.object(rename, '_folder', return_value=self.root / 'transactions')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = self.root / 'queue.json'
        self.scheduler = Scheduler(state_path=self.state, autostart=False)

    def fixture(self, count=1):
        job = Task.create(parse_source('BV17x411w7KC'), '测试')
        job.title = '原名称'
        for number in range(1, count + 1):
            txt, raw = textout.save_result(self.output / '主题', '原名称', 'https://example.invalid',
                'B站', 'ASR', [dict(start=1,end=2,text='预算100000元 Hello world')], f'page{number}')
            job.pages.append(Page(number, '原名称', 'https://example.invalid', status='完成', progress=100,
                txt=str(Path(txt).relative_to(PROJECT_ROOT)), raw=str(Path(raw).relative_to(PROJECT_ROOT))))
        job.status = '完成'
        self.scheduler.jobs = [job]
        self.scheduler._persist()
        return job

    def test_single_rename_updates_title_body_record_and_restores_as_complete(self):
        job = self.fixture()
        old = PROJECT_ROOT / job.pages[0].txt
        body = old.read_bytes().split(b'\n', 1)[1]
        old_record = textout.read_document(old)['raw']
        mapping = self.scheduler.rename_transcript(job.jid, '新名称.txt')
        new = PROJECT_ROOT / job.pages[0].txt
        self.assertEqual(new.name, '新名称.txt')
        self.assertEqual(mapping[str(old.relative_to(PROJECT_ROOT))], job.pages[0].txt)
        self.assertFalse(old.exists())
        self.assertEqual(new.read_bytes().split(b'\n', 1)[1], body)
        doc = textout.read_document(new)
        self.assertEqual(doc['title'], '新名称')
        self.assertEqual(doc['raw']['title'], '新名称')
        self.assertEqual(doc['raw']['segments'], old_record['segments'])
        self.assertEqual(doc['raw']['generated_at'], old_record['generated_at'])
        self.assertEqual([p.suffix for p in new.parent.iterdir()], ['.txt'])
        restored = Scheduler(state_path=self.state, autostart=False)
        self.assertEqual(restored.jobs[0].title, '新名称')
        self.assertEqual(restored.jobs[0].status, '完成')
        self.assertEqual(restored.remove({job.jid}), 1)
        self.assertFalse(new.exists())
        self.assertFalse(documents.record_path(new).exists())

    def test_multipart_names_keep_page_numbers(self):
        job = self.fixture(2)
        self.scheduler.rename_transcript(job.jid, '访谈')
        self.assertEqual([Path(p.txt).name for p in job.pages], ['访谈_P1.txt', '访谈_P2.txt'])
        self.assertEqual([textout.read_document(PROJECT_ROOT / p.txt)['title'] for p in job.pages], ['访谈_P1','访谈_P2'])

    def test_invalid_names_and_conflicts_leave_files_unchanged(self):
        job = self.fixture()
        old = PROJECT_ROOT / job.pages[0].txt
        before = old.read_bytes()
        for value in ('', '  ', '../外部', 'CON', 'a:b', '末尾.', 'LPT1.txt', 'x' * 101):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.scheduler.rename_transcript(job.jid, value)
        collision = old.parent / '已存在.txt'
        collision.write_text('另一份稿件', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.scheduler.rename_transcript(job.jid, '已存在')
        self.assertEqual(old.read_bytes(), before)
        self.assertEqual(collision.read_text(encoding='utf-8'), '另一份稿件')

    def test_pending_and_missing_files_cannot_be_renamed(self):
        job = self.fixture()
        job.status = '识别中'
        self.assertFalse(self.scheduler.can_rename_transcript(job.jid))
        with self.assertRaises(ValueError):
            self.scheduler.rename_transcript(job.jid, '新名')
        job.status = '完成'
        (PROJECT_ROOT / job.pages[0].txt).unlink()
        self.assertFalse(self.scheduler.can_rename_transcript(job.jid))

    def test_queue_write_failure_rolls_back_every_file(self):
        job = self.fixture(2)
        before = job.to_dict()
        paths = [PROJECT_ROOT / p.txt for p in job.pages]
        contents = [p.read_bytes() for p in paths]
        state = self.state.read_bytes()
        with patch('src.services.scheduler.atomic_json', side_effect=PermissionError('无法保存队列')):
            with self.assertRaises(PermissionError):
                self.scheduler.rename_transcript(job.jid, '新名')
        self.assertEqual(job.to_dict(), before)
        self.assertEqual([p.read_bytes() for p in paths], contents)
        self.assertEqual(self.state.read_bytes(), state)
        self.assertFalse(list((self.root / 'transactions').glob('*.json')))

    def test_locked_file_failure_keeps_original_and_cleans_new_file(self):
        job = self.fixture()
        old = PROJECT_ROOT / job.pages[0].txt
        original = Path.unlink
        def unlink(path, *args, **kwargs):
            if path == old:
                raise PermissionError('文件被占用')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', unlink), self.assertRaises(PermissionError):
            self.scheduler.rename_transcript(job.jid, '新名')
        self.assertTrue(old.exists())
        self.assertFalse((old.parent / '新名.txt').exists())
        self.assertEqual(job.title, '原名称')

    def test_restart_rolls_back_uncommitted_files(self):
        job = self.fixture(2)
        journal, transaction = rename.prepare(job, '新名', self.state)
        rename.apply(transaction)
        self.assertFalse((PROJECT_ROOT / job.pages[0].txt).exists())
        restored = Scheduler(state_path=self.state, autostart=False)
        self.assertFalse(journal.exists())
        self.assertEqual(restored.jobs[0].status, '完成')
        self.assertTrue(all((PROJECT_ROOT / p.txt).exists() for p in restored.jobs[0].pages))
        self.assertFalse((self.output / '主题' / '新名_P1.txt').exists())

    def test_restart_finalizes_committed_files_after_cleanup_failure(self):
        job = self.fixture()
        original = Path.unlink
        def unlink(path, *args, **kwargs):
            if path.parent == self.root / 'transactions':
                raise PermissionError('暂时无法清理')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', unlink):
            self.scheduler.rename_transcript(job.jid, '新名')
        self.assertTrue(list((self.root / 'transactions').glob('*.json')))
        restored = Scheduler(state_path=self.state, autostart=False)
        self.assertEqual(restored.jobs[0].title, '新名')
        self.assertEqual(restored.jobs[0].status, '完成')
        self.assertFalse(list((self.root / 'transactions').glob('*.json')))

    def test_case_only_rename_and_recordless_txt(self):
        job = self.fixture()
        self.scheduler.rename_transcript(job.jid, 'Example')
        self.scheduler.rename_transcript(job.jid, 'example')
        txt = PROJECT_ROOT / job.pages[0].txt
        self.assertEqual([p.name for p in txt.parent.iterdir()], ['example.txt'])
        (PROJECT_ROOT / job.pages[0].raw).unlink()
        job.pages[0].raw = ''
        self.scheduler.rename_transcript(job.jid, '历史稿')
        self.assertEqual(job.pages[0].raw, '')
        self.assertEqual(textout.read_document(PROJECT_ROOT / job.pages[0].txt)['title'], '历史稿')

    def test_cleanup_failure_followed_by_rename_does_not_restore_an_old_name(self):
        job = self.fixture()
        original = Path.unlink
        def unlink(path, *args, **kwargs):
            if path.parent == self.root / 'transactions':
                raise PermissionError('暂时无法清理')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', unlink):
            self.scheduler.rename_transcript(job.jid, '第一次')
        self.scheduler.rename_transcript(job.jid, '第二次')
        restored = Scheduler(state_path=self.state, autostart=False)
        self.assertEqual(restored.jobs[0].title, '第二次')
        self.assertEqual([p.name for p in (self.output / '主题').iterdir()], ['第二次.txt'])

    def test_partial_file_installation_is_recovered_before_restart_loads_tasks(self):
        job = self.fixture(2)
        journal, transaction = rename.prepare(job, '新名', self.state)
        original = rename._write
        calls = 0
        def write(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise SystemExit('模拟进程在第二个文件前退出')
            return original(*args, **kwargs)
        with patch.object(rename, '_write', write), self.assertRaises(SystemExit):
            rename.apply(transaction)
        restored = Scheduler(state_path=self.state, autostart=False)
        self.assertEqual(restored.jobs[0].title, '原名称')
        self.assertTrue(all((PROJECT_ROOT / page.txt).exists() for page in restored.jobs[0].pages))
        self.assertFalse(journal.exists())


if __name__ == '__main__':
    unittest.main()
