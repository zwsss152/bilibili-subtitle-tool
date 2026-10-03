"""Behavior-level regressions for the defects found during takeover."""
import io
import json
from pathlib import Path
import threading
import unittest
import uuid
from unittest.mock import patch
from src.core import asr, download, textout, models
from src.core.links import parse_source, extract_sources, resolve_source
from src.core.net import JobCancelled
from src.services.tasks import Task, Page
from src.services.scheduler import Scheduler
from src.paths import PROJECT_ROOT, ASR_CACHE_DIR
from src.core.topics import create_topic
from src.core import chinese_review
from src.core import documents, douyin
from src.paths import OUTPUT_ROOT, RECORDS_DIR


class Response(io.BytesIO):
    def __init__(self, data, status=200, headers=None, url=""):
        super().__init__(data)
        self.status, self.headers, self.url = status, headers or {}, url
    def geturl(self):
        return self.url


class RegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = PROJECT_ROOT / "tests" / ".artifacts"
        cls.directory.mkdir(exist_ok=True)

    def test_create_theme_and_reuse_existing_folder(self):
        root = self.directory / f'topics-{uuid.uuid4().hex}'
        folder, existed = create_topic('  游戏研究  ', root)
        self.assertEqual(folder.name, '游戏研究')
        self.assertTrue(folder.is_dir())
        self.assertFalse(existed)
        marker = folder / '稿件.txt'
        marker.write_text('已有稿件', encoding='utf-8')
        self.assertTrue(create_topic('游戏研究', root)[1])
        self.assertEqual(marker.read_text(encoding='utf-8'), '已有稿件')
        with self.assertRaises(ValueError):
            create_topic('   ', root)
        self.assertTrue(create_topic('../CON', root)[0].resolve().is_relative_to(root.resolve()))

    def test_chinese_review_can_resolve_a_homophone_from_audio_reading(self):
        original = '被裁员之后就出去捋了个油，然后回来上班。'
        candidate = '被裁员之后就出去旅了个游，然后回来上班。'
        self.assertEqual(chinese_review.choose_reading(original, candidate)[0], candidate)

    def test_chinese_review_rejects_numeric_conflicts_and_missing_text(self):
        original = '这个项目预算100000元，之后开始上班。'
        for candidate in ('', '这个项目预算10000元，之后开始上班。', '开始上班。',
                          '天气很好我们准备去旅行看看远处的山和海。'):
            self.assertEqual(chinese_review.choose_reading(original, candidate)[0], original)

    def test_chinese_review_failure_keeps_initial_transcript(self):
        original = [dict(start=1, end=4, text='这是已有的识别结果。')]
        with patch.object(chinese_review, 'engine', side_effect=RuntimeError('model failed')):
            output, info = chinese_review.review('unused', original)
        self.assertEqual(output, original)
        self.assertFalse(info['available'])

    def test_chinese_review_does_not_import_fillers_or_rewrite_meaning(self):
        original = '被裁员之后就出去捋了个油，然后回来上班。'
        candidate = '嗯，被裁员之后就出去旅了个游，然后然后回来上班。'
        selected, _ = chinese_review.choose_reading(original, candidate)
        self.assertEqual(selected, '被裁员之后就出去旅了个游，然后回来上班。')
        self.assertEqual(chinese_review.choose_reading('我们准备出去研究游戏行业。', '我们准备出去改造游戏行业。')[0],
                         '我们准备出去研究游戏行业。')

    def test_chinese_review_cancel_does_not_report_success(self):
        with patch.object(chinese_review, 'engine', side_effect=JobCancelled()):
            with self.assertRaises(JobCancelled):
                chinese_review.review('unused', [])

    def test_numbers_and_intentional_repetitions_survive(self):
        for value in ("预算100000元", "编号AAAAA", "非常非常非常非常", "哈哈哈哈哈"):
            self.assertEqual(textout.normalize_text(value), value)

    def test_simplified_and_english_boundaries(self):
        self.assertEqual(textout.normalize_text("臺灣,中文!"), "台湾，中文！")
        self.assertEqual(textout.build_paragraphs([(0, 1, "Hello"), (1, 2, "world")], punctuate=False), [(0, "Hello world")])

    def test_punctuation_cannot_rewrite_words_or_digits(self):
        self.assertEqual(textout.add_punctuation("预算100000元", punctuator=lambda s: "预算10元。"), "预算100000元")
        self.assertEqual(textout.add_punctuation("这是测试", punctuator=lambda s: "这是测试。"), "这是测试。")

    def test_missing_punctuation_model_falls_back(self):
        with patch.object(textout, "PUNCTUATION_PATH", self.directory / "nonexistent.onnx"):
            self.assertEqual(textout.add_punctuation("这是一段没有标点的内容"), "这是一段没有标点的内容")

    @unittest.skipUnless(textout.PUNCTUATION_PATH.is_file(), 'Local punctuation model not installed')
    def test_real_punctuation_preserves_digits_and_words(self):
        value = '我们预算100000元今天讨论人工智能明天讨论游戏开发'
        result = textout.add_punctuation(value)
        self.assertEqual(textout.content_key(result), value)
        self.assertRegex(result, '[，。！？]')

    def test_corrupt_queue_is_backed_up_before_new_state(self):
        state = self.directory / f'corrupt-{uuid.uuid4().hex}.json'
        state.write_text('{truncated', encoding='utf-8')
        scheduler = Scheduler(state_path=state, autostart=False)
        scheduler.enqueue('BV17x411w7KC', '测试', '')
        backups = list(state.parent.glob(f'{state.stem}.corrupt-*.json'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding='utf-8'), '{truncated')

    def test_short_link_and_selected_page(self):
        source = parse_source("https://b23.tv/test")
        with patch("urllib.request.urlopen", return_value=Response(b"", url="https://www.bilibili.com/video/BV17x411w7KC?p=2")):
            result = resolve_source(source)
        self.assertEqual(result.page, 2)
        self.assertEqual(result.ref, "bvid:BV17x411w7KC")
        with self.assertRaises(ValueError):
            parse_source("https://www.bilibili.com/video/BV17x411w7KC?p=-1")

    def test_douyin_and_share_text_are_supported(self):
        sources, errors = extract_sources("节目 https://www.gcores.com/radios/196305 和 BV17x411w7KC https://v.douyin.com/abc")
        self.assertEqual(len(sources), 3)
        self.assertFalse(errors)
        selected = parse_source('https://www.douyin.com/user/self?modal_id=7691217665213336868&showTab=favorite_collection')
        self.assertEqual(selected.ref, '7691217665213336868')
        self.assertEqual(selected.url, 'https://www.douyin.com/video/7691217665213336868')
        with patch('urllib.request.urlopen', return_value=Response(b'', url='https://www.iesdouyin.com/share/video/7691217665213336868/')):
            self.assertEqual(resolve_source(parse_source('https://v.douyin.com/abc')).ref, selected.ref)

    def test_douyin_share_page_uses_video_not_background_music(self):
        detail = dict(aweme_id='123', desc='测试讲解', video=dict(duration=45000, play_addr=dict(url_list=['https://example.invalid/video.mp4'])),
                      music=dict(play_url=dict(url_list=['https://example.invalid/music.mp3'])))
        page = '<script>window._ROUTER_DATA = ' + json.dumps({'loaderData': {'video': {'item_list': [detail]}}}) + ';</script>'
        info = douyin.parse_share_page(page, '123')
        self.assertEqual(info['media_url'], 'https://example.invalid/video.mp4')
        self.assertEqual(info['duration'], 45)

    def test_douyin_resolves_to_a_transcribable_task(self):
        scheduler = Scheduler(state_path=self.directory/f'douyin-{uuid.uuid4().hex}.json', autostart=False)
        scheduler.enqueue('https://www.douyin.com/video/1234567890123456789', '测试', '')
        with patch.object(douyin, 'fetch_douyin_video', return_value=dict(title='抖音讲解', duration=20, media_url='url')):
            scheduler._resolve(scheduler.jobs[0])
        self.assertEqual(scheduler.jobs[0].kind, 'douyin')
        self.assertEqual(scheduler.jobs[0].pages[0].title, '抖音讲解')

    def test_only_txt_is_exported_and_internal_raw_record_is_readable(self):
        directory = self.directory / f'txt-only-{uuid.uuid4().hex}'
        txt, raw = textout.save_result(directory, '测试', 'url', 'B站', 'ASR', [dict(start=1, end=2, text='原话')], 'only')
        self.assertEqual([p.suffix for p in directory.iterdir()], ['.txt'])
        self.assertTrue(Path(raw).is_relative_to(RECORDS_DIR))
        self.assertEqual(textout.read_document(txt)['raw']['segments'][0]['text'], '原话')

    def test_legacy_record_migration_preserves_txt_and_is_repeatable(self):
        directory = self.directory / f'migrate-{uuid.uuid4().hex}'
        directory.mkdir()
        txt = directory / '旧稿.txt'
        txt.write_text('# 旧稿\n预算100000元\n', encoding='utf-8')
        before = txt.read_bytes()
        legacy = txt.with_suffix('.json')
        legacy.write_text(json.dumps(dict(title='旧稿', segments=[dict(start=0,end=1,text='预算100000元')])), encoding='utf-8')
        with patch.object(documents, 'OUTPUT_ROOT', directory):
            self.assertEqual(len(documents.migrate_records()), 1)
            self.assertEqual(documents.migrate_records(), {})
        self.assertEqual(txt.read_bytes(), before)
        self.assertFalse(legacy.exists())
        self.assertEqual(textout.read_document(txt)['raw']['title'], '旧稿')
        documents.record_path(txt).unlink()

    def test_remove_deletes_owned_transcripts_and_keeps_other_files(self):
        scheduler = Scheduler(state_path=self.directory/f'delete-{uuid.uuid4().hex}.json', autostart=False)
        scheduler.enqueue('BV17x411w7KC', '测试', '')
        job = scheduler.jobs[0]
        directory = OUTPUT_ROOT / f'.test-delete-{uuid.uuid4().hex}'
        txt, raw = textout.save_result(directory, '测试', 'url', 'B站', 'ASR', [dict(start=1, end=2, text='原话')], 'only')
        unrelated = directory / '另一个稿件.txt'
        unrelated.write_text('保留', encoding='utf-8')
        job.pages = [Page(1, '测试', 'url', txt=str(Path(txt).relative_to(PROJECT_ROOT)), raw=str(Path(raw).relative_to(PROJECT_ROOT)))]
        self.assertEqual(scheduler.remove({job.jid}), 1)
        self.assertFalse(Path(txt).exists())
        self.assertFalse(Path(raw).exists())
        self.assertTrue(unrelated.exists())
        self.assertFalse(scheduler.jobs)
        unrelated.unlink()
        directory.rmdir()

    def test_delete_rejects_external_targets_and_cancelled_save_cannot_recreate(self):
        with self.assertRaises(ValueError):
            documents.delete_outputs([Page(1, '测试', 'url', txt=str(self.directory/'external.txt'))])
        scheduler = Scheduler(state_path=self.directory/f'late-save-{uuid.uuid4().hex}.json', autostart=False)
        job = Task.create(parse_source('BV17x411w7KC'), '测试')
        job.cancelled = True
        with patch('src.services.scheduler.save_result') as save:
            with self.assertRaises(JobCancelled):
                scheduler._save(job, Page(1,'测试','url'), [dict(start=0,end=1,text='原话')], 'ASR')
        save.assert_not_called()

    def test_task_theme_is_captured_at_enqueue(self):
        scheduler = Scheduler(state_path=self.directory / f"theme-{uuid.uuid4().hex}.json", autostart=False)
        scheduler.enqueue("BV17x411w7KC", "主题一", "")
        scheduler.enqueue("BV1wfLUzwEUv", "主题二", "")
        self.assertEqual([j.topic for j in scheduler.jobs], ["主题一", "主题二"])
        self.assertEqual(scheduler.jobs[0].output_dir, str(Path("文字稿") / "主题一"))

    def test_chinese_subtitle_is_preferred_and_cookie_is_forwarded(self):
        from src.services import scheduler as module
        scheduler = Scheduler(cookie='test-cookie', state_path=self.directory/f'cc-{uuid.uuid4().hex}.json',autostart=False)
        job = Task.create(parse_source('BV17x411w7KC'),'测试')
        page = Page(1,'测试','https://www.bilibili.com/video/BV17x411w7KC?p=1')
        job.pages=[page]
        scheduler.jobs=[job]
        info=dict(bvid='BV17x411w7KC',pages=[dict(page=1,cid=123)])
        with patch.object(scheduler,'_ensure_info',return_value=info), patch.object(module.bilibili,'fetch_subtitle_list',return_value=[dict(lan='zh-CN',subtitle_url='url')]) as listing, patch.object(module.bilibili,'download_subtitle',return_value=[(0,1,'预算100000元')]) as subtitle, patch.object(module,'download_audio') as audio, patch.object(scheduler,'_save') as save:
            scheduler._prepare(job,page)
        listing.assert_called_once_with('BV17x411w7KC',123,'test-cookie')
        subtitle.assert_called_once_with('url','test-cookie')
        audio.assert_not_called()
        self.assertEqual(save.call_args.args[2][0]['text'],'预算100000元')

    def test_no_subtitle_passes_cookie_to_audio_download(self):
        from src.services import scheduler as module
        scheduler = Scheduler(cookie='test-cookie',state_path=self.directory/f'no-cc-{uuid.uuid4().hex}.json',autostart=False)
        job=Task.create(parse_source('BV17x411w7KC'),'测试')
        page=Page(1,'测试','https://www.bilibili.com/video/BV17x411w7KC?p=1')
        job.pages=[page]
        scheduler.jobs=[job]
        with patch.object(scheduler,'_ensure_info',return_value=dict(bvid='BV17x411w7KC',pages=[dict(page=1,cid=123)])), patch.object(module.bilibili,'fetch_subtitle_list',return_value=[]), patch.object(module,'download_audio',return_value='audio.m4a') as audio:
            scheduler._prepare(job,page)
        self.assertEqual(audio.call_args.kwargs['cookie'],'test-cookie')
        self.assertEqual(page.status,'等待识别')

    def test_partial_restore_and_retry_keep_completed_page(self):
        state = self.directory / f"resume-{uuid.uuid4().hex}.json"
        scheduler = Scheduler(state_path=state, autostart=False)
        scheduler.enqueue("BV17x411w7KC", "测试", "")
        job = scheduler.jobs[0]
        output = self.directory / "completed.txt"
        output.write_text("完整原文", encoding="utf-8")
        job.pages = [Page(1, "P1", "url", status="完成", progress=100, txt=str(output.relative_to(PROJECT_ROOT))),
                     Page(2, "P2", "url", status="识别中", progress=70),
                     Page(3, "P3", "url", status="失败", error="网络错误")]
        scheduler._persist()
        restored = Scheduler(state_path=state, autostart=False)
        self.assertEqual([p.status for p in restored.jobs[0].pages], ["完成", "排队中", "失败"])
        restored.retry({job.jid})
        self.assertEqual([p.status for p in restored.jobs[0].pages], ["完成", "排队中", "排队中"])

    def test_concurrent_persistence_remains_valid(self):
        state = self.directory / f"atomic-{uuid.uuid4().hex}.json"
        scheduler = Scheduler(state_path=state, autostart=False)
        threads = [threading.Thread(target=lambda: [scheduler._persist() for i in range(20)]) for i in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["version"], 2)

    def test_delete_cancels_current_and_preserves_unrelated_file(self):
        scheduler = Scheduler(state_path=self.directory / f"cancel-{uuid.uuid4().hex}.json", autostart=False)
        scheduler.enqueue("BV17x411w7KC", "测试", "")
        job = scheduler.jobs[0]
        output = self.directory / "preserved.txt"
        output.write_text("稿件", encoding="utf-8")
        scheduler.current = (job.jid, 1)
        scheduler.remove({job.jid})
        self.assertTrue(scheduler.cancel_signal.is_set())
        self.assertTrue(job.cancelled)
        self.assertTrue(output.exists())
        self.assertFalse(scheduler.jobs)

    def test_single_stream_short_read_is_rejected(self):
        output = self.directory / "short.mp3"
        with patch.object(download, "_probe_url", return_value=(20000, False)), \
             patch("urllib.request.urlopen", return_value=Response(b"x" * 100, headers={"Content-Length": "20000"})):
            with self.assertRaises(RuntimeError):
                download.download_direct_audio("https://example.invalid", output, lambda *a: None)
        self.assertFalse(output.exists())

    def test_range_response_is_validated(self):
        output = self.directory / "range.mp3"
        with patch.object(download, "_probe_url", return_value=(3 * 1024 * 1024, True)), \
             patch("urllib.request.urlopen", side_effect=lambda *a, **k: Response(b"x" * 100, status=200)):
            with self.assertRaises(RuntimeError):
                download.download_direct_audio("https://example.invalid", output, lambda *a: None)
        self.assertFalse(output.exists())

    def test_cleanup_cannot_remove_outside_cache(self):
        with self.assertRaises(ValueError):
            download.cleanup_owned(self.directory)

    def test_cancel_before_network_transfer(self):
        with self.assertRaises(JobCancelled):
            download.download_audio("https://example.invalid", "audio", self.directory, lambda *a: None, cancel=lambda: True)

    def test_model_download_resume_and_cancel_keep_partial(self):
        target = self.directory / f'model-{uuid.uuid4().hex}.bin'
        partial = target.with_name(target.name+'.part')
        partial.write_bytes(b'abc')
        requests = []
        def response(request, **kw):
            requests.append(request)
            return Response(b'def',206,{'Content-Length':'3','Content-Range':'bytes 3-5/6'})
        with patch('urllib.request.urlopen', side_effect=response):
            models.download_file('https://example.invalid/model',target,expected_size=6)
        self.assertEqual(target.read_bytes(),b'abcdef')
        self.assertEqual(requests[0].get_header('Range'),'bytes=3-')
        partial.write_bytes(b'abc')
        with self.assertRaises(JobCancelled):
            models.download_file('https://example.invalid/model',target,cancel=lambda:True)
        self.assertEqual(partial.read_bytes(),b'abc')

    def test_gpu_fallback_reports_actual_cpu(self):
        fake_info = type("Info", (), {"duration": 1, "language": "zh"})()
        fake_segment = type("Segment", (), dict(start=0, end=1, text="测试", avg_logprob=-0.1, no_speech_prob=0))()
        class FakeModel:
            def transcribe(self, *a, **k):
                return iter([fake_segment]), fake_info
        def load(*args, **kwargs):
            if kwargs["device"] == "cuda":
                raise RuntimeError("GPU unavailable")
            return FakeModel()
        with patch.object(asr, "model_is_ready", return_value=True), patch.object(asr, "get_asr_model", side_effect=load):
            result = asr.transcribe("unused", dict(model="medium", device="cuda", compute_type="float16", batch_size=8, beam_size=3))
        self.assertEqual(result["recognition"]["device"], "cpu")

    def test_oom_halves_batch_before_cpu_fallback(self):
        seen = []
        fake_info = type("Info", (), {"duration": 1, "language": "zh"})()
        fake_segment = type("Segment", (), dict(start=0, end=1, text="测试", avg_logprob=-0.1, no_speech_prob=0))()
        class FakeModel:
            def transcribe(self, *a, **k):
                seen.append(k["batch_size"])
                if k["batch_size"] > 2:
                    raise RuntimeError("CUDA out of memory")
                return iter([fake_segment]), fake_info
        with patch.object(asr, "model_is_ready", return_value=True), patch.object(asr, "get_asr_model", return_value=FakeModel()):
            result = asr.transcribe("unused", dict(model="medium", device="cuda", compute_type="float16", batch_size=8, beam_size=3))
        self.assertEqual(seen, [8, 4, 2])
        self.assertEqual(result["recognition"]["device"], "cuda")

    def test_raw_and_readable_outputs_are_both_preserved(self):
        original = [dict(start=1, end=2, text="預算100000元")]
        txt, raw = textout.save_result(self.directory, "测试", "url", "B站", "CC", original, "fixed")
        self.assertEqual(json.loads(Path(raw).read_text(encoding="utf-8"))["segments"], original)
        self.assertIn("预算100000元", Path(txt).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
