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
from .tasks import Task, Page, migrate_legacy
from .recognizer import worker


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
            for page in job.pages:
                if page.txt and record_path(PROJECT_ROOT / page.txt).exists():
                    page.raw = relative(record_path(PROJECT_ROOT / page.txt))
        if autostart:
            self.start()

    def log(self, message):
        self.events.put(("log", str(message)))

    def start(self):
        cleanup_stale_cache(self.log)
        self._start_process()
        for target in (self._network_loop, self._recognition_loop):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)
        if self.jobs:
            self.log(f"恢复队列 {len(self.jobs)} 条；已完成的分 P 不会重复处理")

    def _start_process(self):
        self.inputs = self.context.Queue()
        self.outputs = self.context.Queue()
        self.cancel_signal.clear()
        self.process = self.context.Process(target=worker, args=(self.inputs, self.outputs, self.cancel_signal), daemon=True)
        self.process.start()

    def _terminate_process(self):
        if self.process:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(timeout=3)
            self.process.close()
            self.process = None
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

    def enqueue(self, text, topic, cookie):
        sources, rejected = extract_sources(text)
        added, duplicates = 0, 0
        with self.lock:
            self.cookie = cookie
            for source in sources:
                if any(job.key == (source.kind, source.ref, source.page) for job in self.jobs):
                    duplicates += 1
                    continue
                self.jobs.append(Task.create(source, topic))
                added += 1
        self._persist()
        return added, duplicates, rejected

    def remove(self, identities):
        removed = 0
        with self.lock:
            protected = [PROJECT_ROOT / path for job in self.jobs if job.jid not in identities
                         for page in job.pages for path in (page.txt, page.raw) if path]
            for job in self.jobs:
                if job.jid in identities:
                    job.cancelled = True
                    if self.current and self.current[0] == job.jid:
                        self.cancel_signal.set()
                    try:
                        delete_outputs(job.pages, protected)
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

    def retry(self, identities):
        with self.lock:
            for job in self.jobs:
                if job.jid in identities and job.kind in ("bilibili", "gcores", 'douyin'):
                    job.error = ""
                    for page in job.pages:
                        if page.status == "失败":
                            page.status, page.progress, page.error = "排队中", 0, ""
                    job.status = "排队中"
                    job.update_status()
        self._persist()

    def clear_finished(self):
        with self.lock:
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
        elif job.kind == 'douyin':
            info = douyin.fetch_douyin_video(source.ref, lambda: self._cancelled(job))
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
        if hasattr(job, "_info"):
            return job._info
        if job.kind == "bilibili":
            id_type, value = job.ref.split(":", 1)
            job._info = bilibili.fetch_video_info(id_type, value, self.cookie)
        elif job.kind == 'douyin':
            job._info = douyin.fetch_douyin_video(job.ref, lambda: self._cancelled(job))
        else:
            job._info = gcores.fetch_gcores_radio(job.ref)
        return job._info

    def _save(self, job, page, segments, method, metadata=None):
        with self.lock:
            check_cancel(lambda: self._cancelled(job))
            txt, raw = save_result(resolve_owned(job.output_dir), page.title, page.url,
                                  {'bilibili': 'B站', 'gcores': '机核', 'douyin': '抖音'}[job.kind], method, segments,
                                  f"{job.jid[:8]}_p{page.number}", metadata, self.log)
            page.txt, page.raw = relative(txt), relative(raw)
            page.status, page.progress = "完成", 100
            job.update_status()
        self.log(f"已生成：{page.title}（{job.topic}）")
        self._persist()

    def _prepare(self, job, page):
        cancel = lambda: self._cancelled(job)
        check_cancel(cancel)
        info = self._ensure_info(job)
        if job.kind == "bilibili":
            cid = next(p["cid"] for p in info["pages"] if p["page"] == page.number)
            try:
                subtitles = bilibili.fetch_subtitle_list(info["bvid"], cid, self.cookie)
                chosen = next((s for s in subtitles if "zh" in s.get("lan", "").lower()), None)
                if chosen:
                    items = bilibili.download_subtitle(chosen["subtitle_url"], self.cookie)
                    segments = [dict(start=a, end=b, text=t) for a, b, t in items]
                    check_cancel(cancel)
                    self._save(job, page, segments, f"CC字幕（{chosen.get('lan', '')}）")
                    return
            except JobCancelled:
                raise
            except Exception as exc:
                self.log(f"字幕提取失败，改为音频识别（{type(exc).__name__}）")
        acquired = False
        directory = ASR_CACHE_DIR / f"job_{job.jid}" / f"p{page.number}"
        try:
            while not self.slots.acquire(timeout=0.1):
                check_cancel(cancel)
            acquired = True
            check_cancel(cancel)
            self._change(job, page, "下载中", "下载音频", 0, True)
            progress = lambda msg, pct=None: self._change(job, page, detail=msg, pct=pct)
            last_error = None
            for attempt in range(2):
                check_cancel(cancel)
                try:
                    if job.kind == "gcores":
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
            self._change(job, page, "等待识别", "音频已下载，等待识别", 0, True)
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
                continue
            self.slots.release()
            if self._cancelled(job):
                self._cleanup(directory)
                continue
            identity = job.jid, page.number
            self.current = identity
            self.cancel_signal.clear()
            self._change(job, page, "识别中", "本地语音识别", 0, True)
            self.inputs.put(dict(identity=identity, audio=audio, title=job.title))
            cancelled_at = None
            try:
                while not self.stopping.is_set():
                    if job.cancelled:
                        self.cancel_signal.set()
                        cancelled_at = cancelled_at or time.monotonic()
                        if time.monotonic() - cancelled_at > 3:
                            self._terminate_process()
                            if not self.stopping.is_set():
                                self._start_process()
                            raise JobCancelled()
                    try:
                        event, result_identity, value = self.outputs.get(timeout=0.15)
                    except queue.Empty:
                        if not self.process.is_alive():
                            self._terminate_process()
                            self._start_process()
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
                        reviewed = (recognition.get('chinese_review') or {}).get('available')
                        method = f"语音识别（{recognition['model']}" + (' + 中文语音复核）' if reviewed else '）')
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

    def _drain_idle_logs(self):
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
