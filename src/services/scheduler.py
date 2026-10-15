"""A network preparation thread and one persistent, replaceable ASR process."""
import json
import multiprocessing as mp
from pathlib import Path
import queue
import threading
import time
from ..config import atomic_json, read_json
from ..paths import QUEUE_STATE_PATH, ASR_CACHE_DIR, PROJECT_ROOT, resolve_owned, relative
from ..core import bilibili, gcores, douyin
from ..core.links import Source, extract_sources, resolve_source
from ..core.net import JobCancelled, check_cancel
from ..core.download import download_audio, download_direct_audio, cleanup_owned, cleanup_stale_cache
from ..core.textout import save_result
from ..core.documents import delete_outputs, record_path
from ..core import rename as document_rename
from .tasks import Task, Page, migrate_legacy
from .recognizer import worker
from .reading_service import ReadingService
from ..core.cloud import CloudClient, configured, checkpoint_path, discard_checkpoints, model_settings
from ..core.library import reading_path
from ..core.local_media import inspect_media


class Scheduler:
    def __init__(self, cookie="", topic="", state_path=QUEUE_STATE_PATH, autostart=True):
        self.state_path = Path(state_path)
        self.cookie = cookie
        self.lock = threading.RLock()
        self.persist_lock = threading.RLock()
        self.events = queue.Queue()
        self.ready = queue.Queue()
        self.slots = threading.BoundedSemaphore(2)
        self.stopping = threading.Event()
        self.paused = threading.Event()
        self.current = None
        self.process = None
        self.context = mp.get_context("spawn")
        self.cancel_signal = self.context.Event()
        self.jobs = []
        self._threads = []
        document_rename.recover(self.state_path, self.log)
        stored = read_json(self.state_path, {})
        if self.state_path.exists():
            try:
                json.loads(self.state_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                import shutil
                backup = self.state_path.with_name(f'{self.state_path.stem}.corrupt-{time.time_ns()}.json')
                shutil.copy2(self.state_path, backup)
                self.log(f'队列文件损坏，已备份至 {backup.name}，可人工恢复')
        if not isinstance(stored, dict):
            stored = {}
        for item in stored.get('jobs', []):
            try:
                self.jobs.append(Task.from_dict(item) if stored.get('version') == 2 else migrate_legacy(item, topic))
            except (TypeError, KeyError, ValueError):
                import shutil
                backup = self.state_path.with_suffix('.invalid-records.json')
                if self.state_path.exists():
                    shutil.copy2(self.state_path, backup)
                self.log(f'跳过异常任务记录，原记录已备份至 {backup.name}')
        for job in self.jobs:
            if job.engine == 'cloud' and not job.cloud_models:
                job.cloud_models = model_settings()
            if job.engine == 'local':
                # Preserve prior upload intent: require an explicit retry.
                message = '本地模型已移除；点击重试将未完成部分转为在线处理'
                for page in job.pages:
                    if page.status != '完成':
                        page.status, page.error = '失败', message
                    if page.reading_requested and page.reading_status != '完成':
                        page.reading_requested = False
                        page.reading_status = '未整理'
                job.update_status()
                if not job.pages:
                    job.status, job.error = '失败', message
                job.detail = message
            for page in job.pages:
                if page.txt and record_path(PROJECT_ROOT / page.txt).exists():
                    page.raw = relative(record_path(PROJECT_ROOT / page.txt))
                if page.txt:
                    try:
                        page.reading = relative(reading_path(PROJECT_ROOT / page.txt))
                    except ValueError:
                        # Diagnostic fixtures may live outside the export library.
                        # Actual file operations still enforce their owned roots.
                        pass
        self.reading_service = ReadingService(self)
        self.cloud_editor = CloudClient()
        self.process_engine = None
        if autostart:
            self.start()

    def log(self, message):
        self.events.put(("log", str(message)))

    def start(self):
        cleanup_stale_cache(self.log)
        # Cloud requests share the serial dispatcher for predictable usage.
        self.reading_service.scan()
        for target in (self._network_loop, self._recognition_loop):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)
        if self.jobs:
            self.log(f"恢复队列 {len(self.jobs)} 条；已完成的分 P 不会重复处理")

    def _start_process(self, engine='cloud'):
        self.inputs = self.context.Queue()
        self.outputs = self.context.Queue()
        self.cancel_signal.clear()
        self.process = self.context.Process(target=worker, args=(self.inputs, self.outputs, self.cancel_signal), daemon=True)
        self.process.start()
        self.process_engine = engine

    def _terminate_process(self):
        if self.process:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(timeout=3)
            self.process.close()
            self.process = None
            self.process_engine = None
        for channel in (getattr(self, "inputs", None), getattr(self, "outputs", None)):
            if channel:
                channel.cancel_join_thread()
                channel.close()

    def _persist(self):
        try:
            with self.persist_lock, self.lock:
                atomic_json(self.state_path, dict(version=2, jobs=[job.to_dict() for job in self.jobs]))
        except OSError as exc:
            self.log(f"保存队列失败：{type(exc).__name__}，请检查磁盘")

    def snapshot(self):
        with self.lock:
            return [{**job.to_dict(), "progress": job.progress} for job in self.jobs]

    def enqueue(self, text, topic, cookie, engine='cloud'):
        if engine != 'cloud':
            return 0, 0, ['本地模型已移除，请使用在线方式']
        if not configured():
            return 0, 0, ['请先在设置中填写阿里云百炼 API Key，再使用在线方式']
        sources, rejected = extract_sources(text)
        models = model_settings()
        added, duplicates = 0, 0
        with self.lock:
            self.cookie = cookie
            for source in sources:
                if any(job.key == (source.kind, source.ref, source.page) and job.engine == engine
                       and job.cloud_models == models for job in self.jobs):
                    duplicates += 1
                    continue
                task = Task.create(source, topic, engine)
                task.cloud_models = models.copy()
                self.jobs.append(task)
                added += 1
        self._persist()
        return added, duplicates, rejected

    def remove(self, identities):
        removed = 0
        with self.persist_lock, self.lock:
            document_rename.recover(self.state_path, self.log)
            protected = [PROJECT_ROOT / path for job in self.jobs if job.jid not in identities
                         for page in job.pages for path in (page.txt, page.raw, page.reading) if path]
            for job in self.jobs:
                if job.jid in identities:
                    job.cancelled = True
                    if self.current and self.current[0] == job.jid:
                        self.cancel_signal.set()
                    try:
                        delete_outputs(job.pages, protected)
                        discard_checkpoints(job.jid)
                        removed += 1
                    except (OSError, ValueError) as exc:
                        job.status, job.error = '失败', f'删除稿件失败：{exc}'
                        job.detail = job.error
                        self.log(job.error)
                        continue
                    job._removed = True
            self.jobs = [job for job in self.jobs if not getattr(job, '_removed', False)]
        self._persist()
        return removed

    def can_rename_transcript(self, jid):
        with self.lock:
            job = next((job for job in self.jobs if job.jid == jid), None)
            return bool(job and job.status == '完成' and job.pages and all(
                page.status == '完成' and page.txt and (PROJECT_ROOT / page.txt).is_file()
                for page in job.pages))

    def rename_transcript(self, jid, new_name):
        # Same lock order as persistence; no worker can save during this transaction.
        with self.persist_lock, self.lock:
            document_rename.recover(self.state_path, self.log)
            if not self.can_rename_transcript(jid):
                raise ValueError('只能重命名已完成且稿件文件存在的任务')
            job = next(job for job in self.jobs if job.jid == jid)
            owned = {str((PROJECT_ROOT / p.txt).resolve()).casefold() for p in job.pages}
            if any(str((PROJECT_ROOT / p.txt).resolve()).casefold() in owned
                   for other in self.jobs if other.jid != jid for p in other.pages if p.txt):
                raise ValueError('稿件还被其他任务引用，不能单独重命名')
            old_title = job.title
            old_pages = [(p, p.title, p.txt, p.raw, p.reading) for p in job.pages]
            journal, transaction = document_rename.prepare(job, new_name, self.state_path)
            try:
                document_rename.apply(transaction)
                job.title = transaction['title']
                for page, renamed in zip(job.pages, transaction['pages']):
                    page.title, page.txt, page.raw = renamed['title'], renamed['txt'], renamed['raw']
                    page.reading = renamed['reading']
                # A failed save must propagate, unlike routine best-effort persistence.
                atomic_json(self.state_path, dict(version=2, jobs=[j.to_dict() for j in self.jobs]))
            except Exception:
                job.title = old_title
                for page, title, txt, raw, reading in old_pages:
                    page.title, page.txt, page.raw, page.reading = title, txt, raw, reading
                document_rename.rollback(transaction)
                journal.unlink()
                raise
            try:
                transaction['committed'] = True
                atomic_json(journal, transaction)
                journal.unlink()
            except OSError:
                self.log('重命名已保存，内部事务记录将在下次启动清理')
            mapping = {txt: page.txt for page, _, txt, _, _ in old_pages}
            mapping.update({reading: page.reading for page, _, _, _, reading in old_pages if reading})
            return mapping

    def retry(self, identities):
        with self.lock:
            for job in self.jobs:
                if job.jid in identities and job.kind in ("bilibili", "gcores", 'douyin', 'local'):
                    if not configured():
                        job.detail = '请先在设置中填写百炼 API Key，再重试'
                        continue
                    job.engine = 'cloud'
                    job.error = ""
                    for page in job.pages:
                        if page.status == "失败":
                            page.status, page.progress, page.error = "排队中", 0, ""
                        if page.txt and page.reading_requested and page.reading_status == '整理失败':
                            page.reading_status, page.reading_error = '待整理', ''
                            self.reading_service.enqueue((job.jid,page.number))
                    job.status = "排队中"
                    job.update_status()
        self._persist()

    def clear_finished(self):
        with self.persist_lock, self.lock:
            document_rename.recover(self.state_path, self.log)
            self.jobs = [job for job in self.jobs if job.status not in ("完成", "失败", "部分失败")]
        self._persist()

    def _cancelled(self, job):
        return self.stopping.is_set() or job.cancelled

    def _change(self, job, page=None, status=None, detail=None, pct=None, persist=False):
        with self.lock:
            if page:
                if status:
                    page.status = status
                if pct is not None:
                    page.progress = min(100, max(0, int(pct)))
                job.update_status()
            elif status:
                job.status = status
            if detail is not None:
                job.detail = detail
        if persist:
            self._persist()

    def _network_loop(self):
        while not self.stopping.is_set():
            if self.paused.is_set():
                self.stopping.wait(0.1)
                continue
            with self.lock:
                job = next((job for job in self.jobs if not job.cancelled and
                            (job.status == "排队中" or any(p.status == "排队中" for p in job.pages))), None)
                if job:
                    page = next((p for p in job.pages if p.status == "排队中"), None)
                    if page:
                        page.status = "解析中"
                    else:
                        job.status = "解析中"
            if not job:
                self.stopping.wait(0.1)
                continue
            try:
                if page is None:
                    self._resolve(job)
                else:
                    self._prepare(job, page)
            except JobCancelled:
                pass
            except Exception as exc:
                message = f"处理失败：{type(exc).__name__}: {exc}"
                with self.lock:
                    if page:
                        page.status, page.error = "失败", message
                        job.update_status()
                    else:
                        job.status, job.error = "失败", message
                    job.detail = message
                self.log(f"{job.title[:40]}：{message}")
                self._persist()

    def _resolve(self, job):
        source = resolve_source(Source(job.kind, job.ref, job.source_url, job.selected_page), lambda: self._cancelled(job))
        check_cancel(lambda: self._cancelled(job))
        if job.kind == "bilibili":
            id_type, value = source.ref.split(":", 1)
            info = bilibili.fetch_video_info(id_type, value, self.cookie)
            pages = info["pages"]
            if source.page:
                pages = [p for p in pages if p["page"] == source.page]
                if not pages:
                    raise ValueError("指定的分 P 不存在")
            new_pages = [Page(p["page"], info["title"] + (f"_P{p['page']}" if len(info["pages"]) > 1 else ""),
                              f"https://www.bilibili.com/video/{info['bvid']}?p={p['page']}", p.get("duration", 0)) for p in pages]
            job._info = info
            title = info["title"]
            ref = "bvid:" + info["bvid"]
        elif job.kind == 'local':
            info = inspect_media(source.ref)
            title, ref = info['title'], source.ref
            new_pages = [Page(1, title, info['url'], info['duration'])]
            job._info = info
        elif job.kind == 'douyin':
            info = douyin.fetch_douyin_video(source.ref, lambda: self._cancelled(job),
                                           lambda msg: self._change(job, detail=msg))
            title, ref = info['title'], source.ref
            new_pages = [Page(1, title, source.url, info['duration'])]
            job._info = info
        else:
            info = gcores.fetch_gcores_radio(source.ref)
            title, ref = info["title"], source.ref
            new_pages = [Page(1, title, source.url, info["duration"])]
            job._info = info
        check_cancel(lambda: self._cancelled(job))
        with self.lock:
            old_done = {p.number: p for p in job.pages if p.status == "完成"}
            job.pages = [old_done.get(p.number, p) for p in new_pages]
            job.ref, job.title = ref, title
            job.source_url = new_pages[0].url
            job.update_status()
        self._persist()

    def _ensure_info(self, job):
        if job.kind == 'local':
            # Recheck the original on each preparation/retry; never use a stale path.
            job._info = inspect_media(job.ref)
            return job._info
        if hasattr(job, "_info"):
            return job._info
        if job.kind == "bilibili":
            id_type, value = job.ref.split(":", 1)
            job._info = bilibili.fetch_video_info(id_type, value, self.cookie)
        elif job.kind == 'douyin':
            job._info = douyin.fetch_douyin_video(job.ref, lambda: self._cancelled(job),
                                                lambda msg: self._change(job, detail=msg))
        else:
            job._info = gcores.fetch_gcores_radio(job.ref)
        return job._info

    def _save(self, job, page, segments, method, metadata=None):
        with self.lock:
            check_cancel(lambda: self._cancelled(job))
            txt, raw = save_result(resolve_owned(job.output_dir), page.title, page.url,
                                  {'bilibili': 'B站', 'gcores': '机核', 'douyin': '抖音', 'local': '本地文件'}[job.kind], method, segments,
                                  f"{job.jid[:8]}_p{page.number}", metadata, self.log)
            page.txt, page.raw = relative(txt), relative(raw)
            page.reading = relative(reading_path(txt))
            page.reading_status = '未整理'
            page.reading_requested = False
            page.status, page.progress = "完成", 100
            job.update_status()
        self.log(f"已生成：{page.title}（{job.topic}）")
        self._persist()

    def request_reading(self, path):
        return self.reading_service.request(path)

    def _prepare(self, job, page):
        cancel = lambda: self._cancelled(job)
        check_cancel(cancel)
        info = self._ensure_info(job)
        if job.engine != 'cloud':
            raise ValueError('本地模型已移除，请点击重试转为在线处理')
        directory = ASR_CACHE_DIR / f"job_{job.jid}" / f"p{page.number}"
        acquired = False
        try:
            while not self.slots.acquire(timeout=0.1):
                check_cancel(cancel)
            acquired = True
            check_cancel(cancel)
            self._change(job, page, "下载中", "读取本地媒体" if job.kind == 'local' else "下载音频", 0, True)
            progress = lambda msg, pct=None: self._change(job, page, detail=msg, pct=pct)
            last_error = None
            for attempt in range(2):
                check_cancel(cancel)
                try:
                    if job.kind == 'local':
                        audio = info['path']
                    elif job.kind == "gcores":
                        audio = download_direct_audio(info["audio_url"], directory / "audio.mp3", progress, cancel=cancel)
                    elif job.kind == 'douyin' and info.get('media_url'):
                        audio = download_direct_audio(info['media_url'], directory / 'audio.mp4', progress, cancel=cancel)
                    else:
                        audio = download_audio(page.url, "audio", directory, progress, cookie=self.cookie, cancel=cancel)
                    last_error = None
                    break
                except JobCancelled:
                    raise
                except Exception as exc:
                    last_error = exc
                    self.log(f"音频下载重试 {attempt + 1}/2（{type(exc).__name__}）")
            if last_error:
                raise last_error
            check_cancel(cancel)
            self._change(job, page, "等待识别", "本地媒体已就绪，等待识别" if job.kind == 'local' else "音频已下载，等待识别", 0, True)
            self.ready.put((job, page, audio, directory))
            acquired = False  # recognition thread releases the prefetch slot
        finally:
            if acquired:
                self.slots.release()
                self._cleanup(directory)

    def _cleanup(self, directory):
        try:
            cleanup_owned(directory)
            parent = Path(directory).parent
            if parent.exists() and parent.is_relative_to(ASR_CACHE_DIR.resolve()):
                try:
                    parent.rmdir()
                except OSError:
                    pass
        except (OSError, RuntimeError) as exc:
            self.log(str(exc))

    def _recognition_loop(self):
        while not self.stopping.is_set():
            if self.paused.is_set():
                self._drain_idle_logs()
                self.stopping.wait(0.1)
                continue
            try:
                job, page, audio, directory = self.ready.get(timeout=0.1)
            except queue.Empty:
                self._drain_idle_logs()
                with self.lock:
                    active = any(j.status not in ('完成', '失败', '部分失败') and not j.cancelled for j in self.jobs)
                if self.reading_service.has_work(allow_history=not active):
                    if self.process:
                        self._terminate_process()
                    self.reading_service.step(self.cloud_editor, allow_history=not active)
                else:
                    self.cloud_editor.close()
                continue
            self.slots.release()
            if self._cancelled(job):
                self._cleanup(directory)
                continue
            identity = job.jid, page.number
            self.cloud_editor.close()
            if self.process and self.process_engine != job.engine:
                self._terminate_process()
            if self.process is None:
                self._start_process(job.engine)
            self.current = identity
            self.cancel_signal.clear()
            self._change(job, page, "识别中", "在线语音识别", 0, True)
            checkpoint = checkpoint_path(job.jid, page.number)
            context = job.title
            if job.engine == 'cloud':
                context = (job.title + '\n' + getattr(job,'_info',{}).get('description',''))[:400]
            self.inputs.put(dict(identity=identity, audio=audio, title=context,
                                 engine=job.engine, checkpoint=str(checkpoint), cloud_models=job.cloud_models))
            cancelled_at = None
            try:
                while not self.stopping.is_set():
                    if job.cancelled:
                        self.cancel_signal.set()
                        cancelled_at = cancelled_at or time.monotonic()
                        if time.monotonic() - cancelled_at > 3:
                            self._terminate_process()
                            if not self.stopping.is_set():
                                self._start_process(job.engine)
                            raise JobCancelled()
                    try:
                        event, result_identity, value = self.outputs.get(timeout=0.15)
                    except queue.Empty:
                        if not self.process.is_alive():
                            self._terminate_process()
                            self._start_process(job.engine)
                            raise RuntimeError("识别进程意外结束，任务可重试")
                        continue
                    if event == "log":
                        self.log(value)
                        continue
                    if tuple(result_identity) != identity:
                        continue
                    if event == "progress":
                        self._change(job, page, pct=value[0], detail=value[1])
                    elif event == "result":
                        recognition = value['recognition']
                        method = f"在线语音识别（{recognition['model']}）"
                        self._save(job, page, value["segments"], method, recognition)
                        break
                    elif event == "cancelled":
                        raise JobCancelled()
                    elif event == "error":
                        raise RuntimeError(value)
            except JobCancelled:
                self.log("任务已取消，已生成稿件保留")
            except Exception as exc:
                page.status, page.error = "失败", str(exc)
                job.update_status()
                job.detail = str(exc)
                self.log(f"{page.title[:40]}：{exc}")
                self._persist()
            finally:
                self.current = None
                self._cleanup(directory)
                if job.cancelled:
                    discard_checkpoints(job.jid)

    def _drain_idle_logs(self):
        if self.process is None:
            return
        try:
            while True:
                event, identity, value = self.outputs.get_nowait()
                if event == "log":
                    self.log(value)
        except queue.Empty:
            pass

    def shutdown(self):
        self.stopping.set()
        self.cancel_signal.set()
        self.cloud_editor.close()
        self._persist()
        for thread in self._threads:
            thread.join(timeout=1)
        self._terminate_process()
        while True:
            try:
                job, page, audio, directory = self.ready.get_nowait()
                self._cleanup(directory)
            except queue.Empty:
                break
        cleanup_stale_cache(self.log)
