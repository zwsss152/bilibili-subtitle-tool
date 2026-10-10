"""Resumable text service driven by the scheduler's exclusive GPU dispatcher."""
import hashlib
import os
from pathlib import Path
import queue
import threading
import uuid
from ..paths import PROJECT_ROOT, OUTPUT_ROOT, relative
from ..config import atomic_json
from ..core import reading
from ..core.documents import record_path
from ..core.library import originals, reading_path
from ..core.editor_model import resources_ready
from ..core.editor_model import MODEL_NAME, MODEL_REVISION
from ..core.cloud import configured
from ..core.net import JobCancelled, check_cancel
from ..core.textout import read_document


class ReadingService:
    def __init__(self, scheduler):
        self.scheduler = scheduler
        self.pending = queue.PriorityQueue()
        self.keys = set()
        self.lock = threading.RLock()
        self.serial = 0

    def enqueue(self, key, historical=False):
        with self.lock:
            if key in self.keys:
                return
            self.keys.add(key)
            self.serial += 1
            self.pending.put((1 if historical else 0, self.serial, key))

    def scan(self):
        owned = set()
        for job in self.scheduler.jobs:
            for page in job.pages:
                if page.txt and (PROJECT_ROOT / page.txt).is_file():
                    owned.add((PROJECT_ROOT / page.txt).resolve())
                    document = read_document(PROJECT_ROOT / page.txt)
                    state = (document.get('raw') or {}).get('reading') or {}
                    status = state.get('status', page.reading_status)
                    unfinished = status in ('待整理','写入中') or status.startswith('整理中')
                    requested = page.reading_requested or (document.get('raw') or {}).get('reading_requested', False)
                    if requested and unfinished:
                        self.enqueue((job.jid, page.number), historical=True)
        for path in originals():
            if path.resolve() not in owned:
                document = read_document(path)
                raw = document.get('raw') or {}
                status = (raw.get('reading') or {}).get('status','')
                if raw.get('reading_requested') and (status in ('待整理','写入中') or status.startswith('整理中')):
                    self.enqueue(str(path.resolve()), historical=True)

    def request(self, path):
        """Persist one explicit request; never enqueue other documents."""
        path = Path(path).resolve()
        from ..core.library import ORIGINAL
        if not path.is_relative_to((OUTPUT_ROOT / ORIGINAL).resolve()) or not path.is_file():
            raise ValueError('请先选择存在的时间戳原稿')
        with self.scheduler.persist_lock, self.scheduler.lock:
            document = read_document(path)
            raw = document['raw'] or dict(version=1, title=document['title'], url=document['url'])
            progress = raw.get('reading') or {}
            if (reading_path(path).is_file() and progress.get('source_hash') == reading.fingerprint(document)
                    and progress.get('status') in ('完成','原文排版')):
                return False
            owner = next(((job, page) for job in self.scheduler.jobs if not job.cancelled
                          for page in job.pages if page.txt and (PROJECT_ROOT / page.txt).resolve() == path), None)
            key = (owner[0].jid, owner[1].number) if owner else str(path)
            with self.lock:
                if key in self.keys:
                    return False
            raw['reading_requested'] = True
            if progress.get('status') not in ('写入中', '整理中'):
                progress['status'] = '待整理'
            raw['reading'] = progress
            atomic_json(record_path(path), raw)
            if owner:
                job, page = owner
                page.reading_requested = True
                page.reading_status, page.reading_error = '待整理', ''
                job.detail = '原稿已保存；阅读稿等待整理'
            self.scheduler._persist()
            self.enqueue(key)
            return True

    def _resolve(self, key):
        if isinstance(key, str):
            path = Path(key).resolve()
            return (path, None, None) if path.is_relative_to(OUTPUT_ROOT.resolve()) and path.is_file() else (None,None,None)
        job = next((j for j in self.scheduler.jobs if j.jid == key[0] and not j.cancelled), None)
        page = next((p for p in job.pages if p.number == key[1]), None) if job else None
        path = PROJECT_ROOT / page.txt if page and page.txt else None
        return (path, job, page) if path and path.is_file() else (None,job,page)

    def cancel(self, key):
        with self.scheduler.lock:
            return self.scheduler.stopping.is_set() or self._resolve(key)[0] is None

    def has_work(self, allow_history=True):
        with self.lock:
            return bool(self.pending.queue and (allow_history or self.pending.queue[0][0] == 0))

    def step(self, editor, allow_history=True):
        """Process at most one chunk, then give new audio work a chance to run."""
        with self.lock:
            if not self.has_work(allow_history):
                return False
            priority, serial, key = self.pending.get_nowait()
        finished = False
        cached = False
        cancel = lambda: self.cancel(key)
        try:
            with self.scheduler.lock:
                path, job, page = self._resolve(key)
                check_cancel(cancel)
                document = read_document(path)
                engine = job.engine if job else ((document['raw'] or {}).get('recognition') or {}).get('engine','local')
                if engine == 'cloud':
                    if not configured():
                        raise RuntimeError('在线整理尚未配置，请在设置中填写百炼 API Key')
                    editor = self.scheduler.cloud_editor
                    strategy = 'cloud'
                else:
                    strategy = 'local' if reading.enabled() and resources_ready() else 'layout'
                record = reading.initial_record(document, strategy)
                progress = record['reading']
                destination = reading_path(path)
                if progress.get('status') in ('完成', '原文排版') and destination.is_file():
                    if page:
                        page.reading, page.reading_status = relative(destination), progress['status']
                        page.reading_error = ''
                    finished = True
                    cached = True
                    return True
                units = reading.source_units(document)
                blocks = progress['blocks']
                if [u['id'] for u in blocks] != list(range(len(blocks))) or len(blocks) > len(units):
                    blocks = []
                source_hash = progress['source_hash']
                if page:
                    page.reading_status = f'整理中 {len(blocks)}/{len(units)}'
            if strategy in ('local', 'cloud'):
                batch = reading.chunks(units[len(blocks):])[0] if len(blocks) < len(units) else []
                try:
                    if batch:
                        editor.start(cancel)
                        progress.update(model=getattr(editor,'model_name',MODEL_NAME),
                                        model_revision=getattr(editor,'model_revision',MODEL_REVISION),
                                        runtime='百炼 HTTP' if strategy == 'cloud' else 'llama.cpp b11146',
                                        device=getattr(editor,'device','unknown'))
                    context = units[len(blocks)-1]['text'][-300:] if blocks else ''
                    additions = reading.organize_chunk(batch, editor, context, cancel) if batch else []
                except JobCancelled:
                    raise
                except Exception as exc:
                    check_cancel(cancel)
                    editor.close()
                    if strategy == 'cloud':
                        raise
                    progress['model_error'] = str(exc)
                    additions = [dict(**u, heading='', mode='原文排版', reason=f'本地模型不可用：{exc}')
                                 for u in units[len(blocks):]]
                    self.scheduler.log(f'本地整理不可用，保留原文排版：{type(exc).__name__}: {exc}')
            else:
                additions = [dict(**u, heading='', mode='原文排版', reason='本地整理尚未通过验收或模型不可用')
                             for u in units[len(blocks):]]
            check_cancel(cancel)
            with self.scheduler.persist_lock, self.scheduler.lock:
                current_path, job, page = self._resolve(key)
                check_cancel(cancel)
                current = read_document(current_path)
                if reading.fingerprint(current) != source_hash:
                    self.scheduler.log('原稿已被修改，整理进度已重新计算')
                    return True
                destination = reading_path(current_path)
                # A concurrent paired rename updates the owned draft's byte hash.
                # Use its current ownership marker, not the pre-rename snapshot.
                owned_progress = (current['raw'] or {}).get('reading') or {}
                if owned_progress.get('file_hash'):
                    progress['file_hash'] = owned_progress['file_hash']
                if owned_progress.get('pending_file_hash'):
                    progress['pending_file_hash'] = owned_progress['pending_file_hash']
                blocks = blocks + additions
                progress.update(blocks=blocks, path=relative(destination), status='整理中')
                if len(blocks) == len(units):
                    old_hash = progress.get('file_hash')
                    owned_hashes = {h for h in (old_hash, progress.get('pending_file_hash')) if h}
                    if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() not in owned_hashes:
                        raise ValueError('阅读稿已存在或被外部修改，已保留，未覆盖')
                    fallback = sum(b['mode'] == '原文排版' for b in blocks)
                    prefix = '在线' if strategy == 'cloud' else '本地'
                    label = prefix + '完整书面稿' if not fallback else '保留原文排版' if fallback == len(blocks) else prefix + '完整书面稿（部分段落保留原文）'
                    content = reading.render_reading(current, blocks, label).encode('utf-8')
                    progress.update(status='写入中', pending_file_hash=hashlib.sha256(content).hexdigest())
                    staged_record = current['raw'] or dict(version=1, title=current['title'], url=current['url'])
                    staged_record['reading'] = progress
                    atomic_json(record_path(current_path), staged_record)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    temporary = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.tmp')
                    try:
                        with temporary.open('wb') as output:
                            output.write(content)
                            output.flush()
                            os.fsync(output.fileno())
                        os.replace(temporary, destination)
                    finally:
                        temporary.unlink(missing_ok=True)
                    progress.update(status='原文排版' if fallback == len(blocks) else '完成', fallback_count=fallback,
                                    file_hash=hashlib.sha256(content).hexdigest())
                    progress.pop('pending_file_hash', None)
                    finished = True
                latest_record = current['raw'] or dict(version=1, title=current['title'], url=current['url'])
                latest_record['reading'] = progress
                atomic_json(record_path(current_path), latest_record)
                if page:
                    page.raw = relative(record_path(current_path))
                    page.reading, page.reading_status, page.reading_error = relative(destination), progress['status'], ''
                    if job.status == '完成':
                        job.detail = f'原稿已保存；阅读稿{progress["status"]}'
                self.scheduler._persist()
            if finished:
                self.scheduler.log(f'已整理：{current["title"]}（{label}）')
        except JobCancelled:
            finished = True
        except Exception as exc:
            with self.scheduler.persist_lock, self.scheduler.lock:
                path, job, page = self._resolve(key)
                if page:
                    page.reading_status, page.reading_error = '整理失败', str(exc)
                if path:
                    try:
                        failed_record = read_document(path)['raw'] or {}
                        failed_record.setdefault('reading', {}).update(status='整理失败', error=str(exc))
                        atomic_json(record_path(path), failed_record)
                    except OSError:
                        self.scheduler.log('整理失败状态未能保存，请检查磁盘')
                self.scheduler._persist()
            self.scheduler.log(f'阅读稿整理失败：{type(exc).__name__}: {exc}')
            finished = True
        finally:
            with self.lock:
                if finished:
                    self.keys.discard(key)
                else:
                    self.serial += 1
                    self.pending.put((priority, self.serial, key))
            if cached:
                self.scheduler._persist()
        return True
