"""Local imports use the existing queue and never own the original media."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from src.core.links import parse_source, extract_sources
from src.core.local_media import inspect_media, media_path
from src.core.net import JobCancelled
from src.paths import PROJECT_ROOT
from src.services.scheduler import Scheduler
from src.services.tasks import Task


class LocalMediaTests(unittest.TestCase):
    def setUp(self):
        root = PROJECT_ROOT / 'tests' / '.artifacts'
        root.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.media = self.root / '[0601]第01讲 四格表 #100000.wav'
        with wave.open(str(self.media), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(b'\0\0' * 16000)

    def service(self):
        return Scheduler(state_path=self.root / 'queue.json', autostart=False)

    def test_quoted_path_uri_spaces_and_mixed_links(self):
        a = parse_source('"' + str(self.media) + '"')
        self.assertEqual(a.kind, 'local')
        self.assertEqual(parse_source(self.media.as_uri()).ref, a.ref)
        sources, errors = extract_sources('"' + str(self.media) + '"\nhttps://www.gcores.com/radios/123')
        self.assertEqual([s.kind for s in sources], ['gcores', 'local'])
        self.assertFalse(errors)
        self.assertEqual(inspect_media(a.ref)['duration'], 1)

    def test_reject_missing_empty_unsupported_and_corrupt_media(self):
        for filename, data in [('missing.mp4', None), ('empty.mp4', b''), ('not.txt', b'x'), ('bad.mp4', b'broken')]:
            path = self.root / filename
            if data is not None:
                path.write_bytes(data)
            with self.assertRaises(ValueError):
                inspect_media(path)
        with self.assertRaises(ValueError):
            media_path('file://remote-host/video.mp4')

    def test_queue_freezes_mode_theme_and_restores_local_job(self):
        service = self.service()
        self.assertEqual(service.enqueue(str(self.media), '卫生统计', '', 'local')[:2], (1, 0))
        self.assertEqual(service.enqueue(str(self.media), '其他', '', 'local')[:2], (0, 1))
        with patch('src.services.scheduler.configured', return_value=True):
            self.assertEqual(service.enqueue(str(self.media), '其他', '', 'cloud')[0], 1)
        recovered = self.service()
        self.assertEqual([(j.engine, j.topic) for j in recovered.jobs], [('local', '卫生统计'), ('cloud', '其他')])
        recovered._resolve(recovered.jobs[0])
        self.assertEqual(recovered.jobs[0].title, self.media.stem)

    def test_no_audio_track_is_rejected(self):
        with patch('av.open') as opened:
            opened.return_value.__enter__.return_value.streams.audio = []
            with self.assertRaisesRegex(ValueError, '没有音轨'):
                inspect_media(self.media)

    def test_prepare_does_not_download_or_copy_and_delete_keeps_original(self):
        original = self.media.read_bytes()
        service = self.service()
        job = Task.create(parse_source(str(self.media)), '测试')
        service.jobs.append(job)
        service._resolve(job)
        with patch('src.services.scheduler.download_audio', side_effect=AssertionError('network')), \
             patch('src.services.scheduler.download_direct_audio', side_effect=AssertionError('network')):
            service._prepare(job, job.pages[0])
        queued = service.ready.get_nowait()
        self.assertEqual(Path(queued[2]), self.media)
        service.slots.release()
        service._cleanup(queued[3])
        self.assertEqual(service.remove([job.jid]), 1)
        self.assertEqual(self.media.read_bytes(), original)

    def test_missing_after_enqueue_retry_and_cancel(self):
        service = self.service()
        job = Task.create(parse_source(str(self.media)), '测试')
        service.jobs.append(job)
        service._resolve(job)
        saved = self.media.read_bytes()
        self.media.unlink()
        with self.assertRaises(ValueError):
            service._prepare(job, job.pages[0])
        self.media.write_bytes(saved)
        job.pages[0].status = '失败'
        job.status = '失败'
        service.retry([job.jid])
        self.assertEqual(job.pages[0].status, '排队中')
        job.cancelled = True
        with self.assertRaises(JobCancelled):
            service._prepare(job, job.pages[0])
        self.assertEqual(self.media.read_bytes(), saved)
